# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Test_Exec Agent（真实 LLM 调用版本）

职责：理解测试用例 -> 调用 BMC 接口执行 -> 收集证据 -> 生成 ExecutionRecord。
严格遵守 "只执行，不判断" 原则。

技术方案：
- AsyncOpenAI + stream=True，支持 OpenAI-compatible API
- 多轮 Tool Calling 循环（redfish / ipmi / ssh / rag）
- httpx 真实 Redfish 请求（SSL 验证关闭，适配自签证书）
- Jinja2 渲染 system prompt
- 执行完成后自动保存 ExecutionRecord + 生成报告
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from jinja2 import Template
from openai import AsyncOpenAI
from pydantic import ValidationError

from src.core.config import AppConfig, load_config, get_component_config
from src.core.client_factory import ClientFactory
from src.core.schemas import ExecutionRecord, StepRecord, StepStatus
from src.tools.ipmi_tool import IPMITool
from src.tools.ssh_tool import SSHTool
from src.tools.ssh_session_manager import SSHSessionManager
from src.tools.environment_recovery_tool import EnvironmentRecoveryTool
from src.utils.file_handler import save_execution_record
from src.rag.retriever import HybridRetriever

# ======================================================================
# 日志配置（实时写入文件 + 控制台）
# ======================================================================

logger = logging.getLogger("exec_agent")


def setup_logging(log_dir: str = "./logs") -> None:
    """初始化日志系统，同时输出到文件和控制台。"""
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return  # 已初始化，避免重复 handler

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"exec_agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # 文件 handler：DEBUG 级别，实时 flush
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))

    # 控制台 handler：INFO 级别
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"日志文件: {log_file}")


# ======================================================================
# JSON 提取（多策略，高鲁棒性）
# ======================================================================

def extract_json_from_response(text: str) -> Optional[str]:
    """
    从 LLM 输出中提取 JSON，尝试多种策略。

    策略优先级：
    1. ```json ... ``` 代码块
    2. ``` ... ``` 代码块（无语言标记）
    3. 括号配对提取最大 { } 块
    4. 逐行扫描找 { 开头的行块
    """
    if not text or not text.strip():
        return None

    # 策略 1: ```json ... ```
    match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 策略 2: ``` ... ```（无语言标记）
    match = re.search(r"```\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 策略 3: 括号配对（找最大顶层 { } 块）
    result = _extract_balanced_json(text)
    if result:
        return result

    # 策略 4: 找第一个 { 到最后一个 }，暴力截取
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]

    return None


def _extract_balanced_json(text: str) -> Optional[str]:
    """
    通过括号配对提取文本中的顶层 JSON 对象。

    从第一个 { 开始，追踪括号平衡，找到最外层闭合 }。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    i = start

    while i < len(text):
        ch = text[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if ch == "\\":
            if in_string:
                escape_next = True
            i += 1
            continue

        if ch == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

        i += 1

    return None


def _strip_thinking_blocks(text: str) -> str:
    """
    移除 LLM 输出中的思考块和无关内容，提取纯 JSON。

    处理：</think> 块、</thinking> 标签、代码块标记、说明文字等。
    """
    # 移除 </think> 块（可能跨行）
    text = re.sub(r"</think>.*?</think>", "", text, flags=re.DOTALL)
    # 移除 </thinking> 标签及后续内容
    text = re.sub(r"</thinking>.*", "", text, flags=re.DOTALL)
    # 移除 markdown 代码块标记
    text = re.sub(r"^```[a-zA-Z]*\s*$", "", text, flags=re.MULTILINE)
    # 移除行首的行号标记如 "1. " 等
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # 移除常见说明文字
    text = re.sub(r"^(以下是|下面是|执行结果|结果如下|输出|Output|Result)[:：]\s*", "", text, flags=re.MULTILINE)
    # 移除多余空白行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sanitize_json_string(json_str: str) -> str:
    """
    清理 JSON 字符串中的常见格式问题。

    处理：尾逗号、单引号、注释、控制字符。
    """
    s = json_str

    # 移除 JS 风格单行注释 (// ...)
    s = re.sub(r"//[^\n]*", "", s)

    # 移除 JS 风格多行注释 (/* ... */)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)

    # 移除尾部逗号（}, ] 前的逗号）
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # 移除控制字符（保留 \n \r \t）
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)

    return s.strip()


# ======================================================================
# OpenAI function calling 工具定义
# ======================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "redfish_request",
            "description": "发送 Redfish API 请求到 BMC。用于查询或修改 BMC 资源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "endpoint": {
                        "type": "string",
                        "description": "Redfish 端点路径，如 /redfish/v1/AccountService/Accounts",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PATCH", "DELETE"],
                        "description": "HTTP 方法",
                    },
                    "body": {
                        "type": "object",
                        "description": "请求体（POST/PATCH 时使用，可选）",
                    },
                    "headers": {
                        "type": "object",
                        "description": "自定义 HTTP Headers，如 {\"If-Match\": \"etag_value\"}，用于条件更新",
                    },
                },
                "required": ["endpoint", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ipmi_command",
            "description": (
                "执行 IPMI 命令（pyghmi 后端，非 ipmitool CLI）。\n"
                "支持的命令格式（不要加 'ipmi' 或 'ipmitool' 前缀）：\n"
                "- mc info: 获取设备信息（制造商、固件版本、IPMI 版本）\n"
                "- mc guid: 获取系统 GUID\n"
                "- mc reset cold / mc reset warm: BMC 冷/热复位\n"
                "- chassis status: 获取机箱状态（电源、故障指示灯）\n"
                "- chassis power status: 查询电源状态\n"
                "- chassis power on/off/cycle/reset: 电源控制\n"
                "- sel info: SEL 日志信息\n"
                "- sel list: 列出 SEL 条目\n"
                "- sdr info: SDR 仓库信息\n"
                "- sdr list: 列出传感器数据记录\n"
                "- sensor list: 列出传感器读数\n"
                "- fru list: 列出 FRU 信息\n"
                "- user list: 列出用户\n"
                "- user summary: 用户摘要\n"
                "- raw 0xNN 0xMM [data...]: 发送原始 IPMI 命令\n"
                "示例: command='mc info', command='chassis status', command='raw 0x06 0x01'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "IPMI 命令（不含 ipmi/ipmitool 前缀），如 'mc info', 'chassis status', 'raw 0x06 0x01'",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
                    },
                    "user": {
                        "type": "string",
                        "description": "可选，覆盖默认 BMC 用户名（禁用 Administrator 后可用其他用户执行）",
                    },
                    "password": {
                        "type": "string",
                        "description": "可选，覆盖默认 BMC 密码（需与 user 配对传入）",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_exec",
            "description": (
                "通过 SSH 在远程主机上执行命令。支持两种模式：\n"
                "1. 非交互式：提供 command 参数执行单条命令\n"
                "2. 交互式：提供 interactions 参数执行需要交互的命令（如 ipmcset adduser）\n"
                "BMC Shell 端口默认 10022。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "目标主机 IP"},
                    "port": {
                        "type": "integer",
                        "description": "SSH 端口（BMC Shell 默认 10022，主机控制台默认 2200）",
                        "default": 10022,
                    },
                    "user": {"type": "string", "description": "用户名"},
                    "password": {"type": "string", "description": "密码"},
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（非交互模式）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
                    },
                    "interactions": {
                        "type": "array",
                        "description": (
                            "交互式命令的步骤序列。每步包含："
                            "expect（等待的输出模式）、"
                            "send（匹配后发送的文本）、"
                            "is_password（是否为密码，日志中脱敏）"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "expect": {
                                    "type": "string",
                                    "description": "等待的输出文本或正则模式",
                                },
                                "send": {
                                    "type": "string",
                                    "description": "匹配后发送的文本",
                                },
                                "is_password": {
                                    "type": "boolean",
                                    "description": "是否为密码（日志中脱敏）",
                                    "default": False,
                                },
                            },
                            "required": ["expect", "send"],
                        },
                    },
                },
                "required": ["host", "user", "password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bmc_command_rag",
            "description": (
                "根据自然语言描述检索最匹配的 BMC 命令或 API 模板。\n"
                "支持 IPMI 和 Redfish 两种接口类型的检索。\n"
                "当步骤描述模糊、缺少具体命令或参数时，必须优先调用此工具。\n"
                "- interface_type='auto'(默认): 自动判断查询适合 IPMI 还是 Redfish\n"
                "- interface_type='ipmi': 仅检索 IPMI 命令\n"
                "- interface_type='redfish': 仅检索 Redfish API\n"
                "- interface_type='both': 同时检索两种接口并合并结果"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_description": {
                        "type": "string",
                        "description": "操作的自然语言描述，如 '查看BMC固件版本'、'使用IPMI获取用户列表'",
                    },
                    "interface_type": {
                        "type": "string",
                        "enum": ["auto", "ipmi", "redfish", "both"],
                        "default": "auto",
                        "description": "接口类型: auto(自动判断) / ipmi / redfish / both(同时检索)",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 3,
                        "description": "每种接口类型返回的结果数量",
                    },
                },
                "required": ["operation_description"],
            },
        },
    },
    # ------------------------------------------------------------------
    # SSH Session 统一工具（长连接 + 流式交互）
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "ssh_session",
            "description": (
                "SSH 长连接会话管理，通过 action 参数指定操作类型：\n"
                "- open:   打开 SSH 长连接（无需参数）\n"
                "- exec:   在会话中执行命令并等待结果（参数: command, timeout）\n"
                "- expect: 等待输出中出现指定模式（参数: patterns, timeout）\n"
                "- send:   发送文本（参数: text, is_password, press_enter）\n"
                "- read:   读取当前可用输出（参数: timeout）\n"
                "- close:  关闭会话获取 Evidence（无需参数）\n"
                "连接参数由框架自动填充。每次 open 后必须最终调用 close 释放连接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "exec", "expect", "send", "read", "close"],
                        "description": "操作类型",
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（action=exec 时必填）",
                    },
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "等待的输出模式列表，正则表达式（action=expect 时必填）",
                    },
                    "text": {
                        "type": "string",
                        "description": "要发送的文本（action=send 时必填）",
                    },
                    "is_password": {
                        "type": "boolean",
                        "description": "是否为密码（日志中脱敏），默认 false",
                        "default": False,
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "发送后是否附加回车，默认 true",
                        "default": True,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数（各 action 默认值不同）",
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# ======================================================================
# ExecAgent
# ======================================================================

class ExecAgent:
    """
    Test_Exec Agent - 测试执行引擎

    通过 OpenAI-compatible API 与 LLM 交互，使用 Tool Calling 执行 BMC 操作。
    支持：真实 Redfish HTTP 调用、SSH 执行、IPMI 命令。
    """

    SYSTEM_PROMPT_PATH = Path("src/prompts/exec_system.txt")
    MAX_TOOL_ROUNDS = 50          # 最大工具调用轮次（安全上限，防无限循环）

    def __init__(self, config: dict):
        """
        初始化 ExecAgent。

        Args:
            config: 支持 dict（旧格式）或 AppConfig（新格式）。
                    旧 dict 格式兼容保留，内部自动转换。
        """
        # 兼容: 传入 dict 时自动加载 AppConfig
        if isinstance(config, dict):
            self._app_config = load_config(
                getattr(self, "_config_path", "config/config.yaml")
            )
            self.config = config  # 保留原始 dict 引用（某些工具可能依赖）
        elif isinstance(config, AppConfig):
            self._app_config = config
            # 向后兼容: 部分方法仍读取 dict
            self.config = config.model_dump()
        else:
            raise TypeError(f"config 类型错误: {type(config)}，期望 dict 或 AppConfig")

        # 初始化日志系统
        log_cfg = self._app_config.logging
        log_file = log_cfg.get("file", "./logs/test_framework.log") if isinstance(log_cfg, dict) else "./logs/test_framework.log"
        setup_logging(str(Path(log_file).parent) if Path(log_file).suffix else log_file)

        # 客户端工厂
        self._client_factory = ClientFactory(self._app_config)

        # Exec 组件配置（通过 get_component_config 获取合并后的完整参数）
        exec_comp = get_component_config(self._app_config, "exec")
        self.base_url = exec_comp.base_url
        self.model = exec_comp.model
        self.temperature = exec_comp.temperature
        self.max_tokens = exec_comp.max_tokens

        # 创建 Exec LLM 客户端
        self.client = self._client_factory.create_for("exec")

        # 工具调用超时配置（从 config.yaml agent 段读取，默认 600s = 10 分钟）
        agent_cfg = self.config.get("agent", {}) if isinstance(self.config, dict) else {}
        self._tool_timeout_seconds = float(agent_cfg.get("tool_timeout_seconds", 600.0))
        logger.info(f"工具超时配置: tool_timeout={self._tool_timeout_seconds}s")

        self.shared_dir = (
            self._app_config.storage.get("shared_dir", "./shared")
            if isinstance(self._app_config.storage, dict)
            else "./shared"
        )

        # 预加载 system prompt 模板
        self._system_template = Template(
            self.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )

        # BMC 连接信息
        target = self._app_config.target
        self.bmc_host = target.get("bmc_host", "127.0.0.1")
        self.bmc_port = target.get("bmc_port", 443)
        self.bmc_user = target.get("bmc_user", "Administrator")
        self.bmc_password = target.get("bmc_password", "")
        self.ipmi_port = target.get("ipmi_port", 623)
        self.ipmi_host = target.get("ipmi_host", self.bmc_host)
        self.ssh_port = target.get("ssh_port", 10022)
        self.ssh_host = target.get("ssh_host", self.bmc_host)

        # httpx 客户端（根据配置决定是否验证 SSL）
        self._http_client: Optional[httpx.AsyncClient] = None
        # 默认 False: BMC 设备通常使用自签名证书，内网环境需跳过 SSL 验证
        self._verify_ssl = target.get("verify_ssl", False)

        # IPMI Tool 实例（支持 binary / pyghmi 双后端）
        ipmi_config = target.get("ipmi", {})
        self._ipmi_tool = IPMITool(
            host=self.ipmi_host,
            port=self.ipmi_port,
            user=self.bmc_user,
            password=self.bmc_password,
            cipher_suite=17,
            config=ipmi_config,
        )

        # SSH 会话管理器
        self._session_mgr = SSHSessionManager(
            host=self.ssh_host,
            port=self.ssh_port,
            user=self.bmc_user,
            password=self.bmc_password,
        )

        # 环境恢复工具
        self._recovery_tool = EnvironmentRecoveryTool(target)

        # RAG 混合检索器 (HybridRetriever + QueryRewriter)
        rag_cfg = self._app_config.rag
        self._rag_enabled = rag_cfg.enabled
        self._retriever: Optional[HybridRetriever] = None
        self._embed_client: Optional[AsyncOpenAI] = None
        self._embed_model = self._app_config.models.embedding.model
        embed_comp = get_component_config(self._app_config, "embedding")
        self._embed_dimension = embed_comp.dimension or 1024
        self._rag_top_k = rag_cfg.top_k
        self._rag_rewrite_enabled = rag_cfg.enable_rewrite
        self._rag_rewrite_weight = rag_cfg.rewrite_weight
        self._rag_default_interface = rag_cfg.default_interface
        self._rag_auto_detect = rag_cfg.auto_detect

        if self._rag_enabled:
            try:
                self._init_rag()
                logger.info(
                    f"RAG 模块已启用 | top_k={self._rag_top_k} | "
                    f"rewrite={'on' if self._rag_rewrite_enabled else 'off'} | "
                    f"rewrite_weight={self._rag_rewrite_weight} | "
                    f"default_interface={self._rag_default_interface} | "
                    f"auto_detect={self._rag_auto_detect}"
                )
            except Exception as e:
                logger.warning(f"RAG 初始化失败，已禁用: {e}")
                self._rag_enabled = False

        # Tool 分发表
        self._tool_handlers = {
            "redfish_request": self._tool_redfish_request,
            "ipmi_command": self._tool_ipmi_command,
            "ssh_exec": self._tool_ssh_exec,
            "bmc_command_rag": self._tool_bmc_command_rag,
            "ssh_session": self._tool_ssh_session,
        }

        logger.info(f"使用模型: {self.model} | base_url: {self.base_url}")
        logger.info(f"参数: temperature={self.temperature}, max_tokens={self.max_tokens}")
        logger.info(f"目标 BMC: {self.bmc_host}:{self.bmc_port} | IPMI: {self.ipmi_host}:{self.ipmi_port} | SSH: {self.ssh_host}:{self.ssh_port}")

    # ==================================================================
    # httpx 生命周期
    # ==================================================================

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 异步客户端（懒初始化）。"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self._verify_ssl,
                timeout=30.0,
                trust_env=False,  # 禁用系统代理，避免 Windows 代理干扰
            )
        return self._http_client

    async def close(self):
        """清理资源。"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._ipmi_tool.close()
        # 清理 SSH 会话
        self._session_mgr.close_all()
        # 清理 RAG 资源
        if self._retriever:
            await self._retriever.close()
        # 通过 ClientFactory 统一释放所有客户端
        await self._client_factory.close()

    # ==================================================================
    # RAG 初始化与 Embedding
    # ==================================================================

    def _init_rag(self) -> None:
        """初始化 RAG 混合检索器和 Embedding 客户端。"""
        rag_cfg = self._app_config.rag

        # Rewrite 组件模型配置
        rewrite_model_cfg = self._app_config.models.rewrite

        # HybridRetriever（内部集成 QueryRewriter）
        self._retriever = HybridRetriever(
            chroma_path=rag_cfg.chroma_path,
            collection_name=rag_cfg.collection_name,
            alpha=rag_cfg.alpha,
            enable_rewrite=self._rag_rewrite_enabled,
            rewrite_provider=self._app_config.providers.get(rewrite_model_cfg.provider),
            rewrite_model=rewrite_model_cfg.model,
            verify_ssl=rag_cfg.verify_ssl,
            trust_env=rag_cfg.trust_env,
            timeout=rag_cfg.timeout,
        )

        # Embedding 客户端（通过 ClientFactory 统一创建）
        self._embed_client = self._client_factory.create_for("embedding")

        logger.info(
            f"RAG 初始化完成: chroma={rag_cfg.chroma_path}, "
            f"collection={rag_cfg.collection_name}, "
            f"alpha={rag_cfg.alpha}, embed_model={self._embed_model}, "
            f"rewrite={'on' if self._rag_rewrite_enabled else 'off'} "
            f"(model={rewrite_model_cfg.model}, weight={self._rag_rewrite_weight})"
        )

    async def _get_query_embedding(self, query: str) -> Optional[List[float]]:
        """通过 Embedding API 生成查询向量。"""
        if not self._embed_client:
            return None
        try:
            resp = await self._embed_client.embeddings.create(
                model=self._embed_model,
                input=[query],
            )
            embedding = resp.data[0].embedding
            logger.debug(f"[RAG] Embedding 生成成功 (dim={len(embedding)})")
            return embedding
        except Exception as e:
            logger.error(f"[RAG] Query embedding 生成失败: {e}")
            return None

    # ==================================================================
    # 公开接口
    # ==================================================================

    async def execute(self, case: dict, config: dict) -> ExecutionRecord:
        """
        执行单个测试用例。

        流程：渲染 prompt -> LLM 对话（含 Tool Calling）-> 解析 -> 保存
        """
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        logger.info(f"开始执行: {case_name}")

        started_at = datetime.now()

        # 构建消息
        messages = [
            {"role": "system", "content": self._render_system_prompt(case)},
            {"role": "user", "content": self._build_user_message(case)},
        ]

        # LLM 对话
        try:
            final_content = await self._run_conversation(messages)
        except Exception as e:
            logger.error(f"执行异常: {e}", exc_info=True)
            record = self._build_failure_record(case, started_at, error_msg=str(e))
            self._save_record(record)
            return record
        finally:
            # 清理可能残留的 SSH 会话
            if self._session_mgr:
                logger.info("清理残留的 SSH 会话")
                await asyncio.to_thread(self._session_mgr.close_all)

        # 解析 ExecutionRecord
        record = self._parse_record(final_content, case, started_at)

        # 自动保存
        self._save_record(record)

        logger.info(f"执行完成: {case_name} -> {record.overall_status}")

        # 环境恢复（用例执行完毕后自动恢复 BMC 环境）
        try:
            recovery_result = await self._recovery_tool.recover()
            if not recovery_result.recovered:
                logger.warning(
                    f"[Recovery] 环境恢复未完全成功，下一条用例可能受影响: "
                    f"{recovery_result.warnings}"
                )
        except Exception as e:
            logger.warning(f"[Recovery] 环境恢复异常: {e}")

        return record

    async def execute_batch(self, cases: list, config: dict) -> list:
        """批量执行测试用例（串行）。"""
        logger.info(f"批量执行 {len(cases)} 个用例")

        records = []
        for i, case in enumerate(cases):
            logger.info(f"--- 用例 {i + 1}/{len(cases)} ---")
            try:
                records.append(await self.execute(case, config))
            except Exception as e:
                logger.error(f"用例执行失败: {e}")
                records.append(
                    self._build_failure_record(case, datetime.now(), error_msg=str(e))
                )
        return records

    # ==================================================================
    # Prompt
    # ==================================================================

    def _render_system_prompt(self, case: dict) -> str:
        """Jinja2 渲染 system prompt，注入环境 + 用例信息。"""
        target = self.config.get("target", {})
        return self._system_template.render(
            bmc_host=target.get("bmc_host", "unknown"),
            bmc_user=target.get("bmc_user", "unknown"),
            bmc_password=target.get("bmc_password", ""),
            ssh_port=target.get("ssh_port", 10022),
            os_host=target.get("os_host"),
            os_user=target.get("os_user"),
            case=case,
            case_id=case.get("case_id", case.get("用例_编号", "")),
            case_name=case.get("name", case.get("用例_名称", "")),
            test_steps=case.get("测试步骤", []),
            expected_result=case.get("预期结果", []),
            precondition=case.get("预置条件", []),
            batch_cases=None,
        )

    def _build_user_message(self, case: dict) -> str:
        """构建 user message，移除内部字段后格式化为 JSON。"""
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        case_copy = {k: v for k, v in case.items() if not k.startswith("_")}
        case_json = json.dumps(case_copy, ensure_ascii=False, indent=2, default=str)
        return (
            f"请执行以下测试用例：\n\n"
            f"用例名称: {case_name}\n\n"
            f"用例内容:\n{case_json}\n\n"
            f"执行完成后，请输出完整的 ExecutionRecord JSON。"
        )

    # ==================================================================
    # LLM 对话循环
    # ==================================================================

    async def _run_conversation(self, messages: list) -> str:
        """
        LLM 对话主循环。

        每轮：流式接收 -> 如有 tool_calls 则执行 -> 继续
        无 tool_calls 时返回最终文本。
        """
        text = ""
        for round_num in range(self.MAX_TOOL_ROUNDS):
            logger.info(f"--- 第 {round_num + 1} 轮 ---")

            text, tool_calls, finish_reason = await self._stream_response(messages)

            # 无 tool call -> 返回
            if finish_reason != "tool_calls" or not tool_calls:
                return text

            # 执行 tool calls 并注入结果（单次调用受 tool_timeout_seconds 约束）
            await self._process_tool_calls(messages, text, tool_calls)

        logger.warning(f"达到最大对话轮次限制 ({self.MAX_TOOL_ROUNDS})")
        return text

    async def _stream_response(self, messages: list) -> tuple:
        """
        流式接收一轮 LLM 响应。

        实时打印文本内容，累积 tool call 分片。

        Returns:
            (text_content, tool_calls_map, finish_reason)
        """
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
        )

        text = ""
        tool_calls: Dict[int, dict] = {}
        finish_reason = None

        async for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # 文本 -> 实时打印 + 写日志
            if delta.content:
                print(delta.content, end="", flush=True)
                text += delta.content

            # Tool call 分片 -> 累积
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls[idx]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        print()  # 流式输出换行

        # 实时写日志：LLM 完整文本输出
        if text.strip():
            logger.debug(f"LLM 文本输出 ({len(text)} chars): {text[:500]}")
        if tool_calls:
            tc_names = [tc["name"] for tc in tool_calls.values()]
            logger.info(f"LLM 请求 Tool Calls: {tc_names}")

        return text, tool_calls, finish_reason

    async def _process_tool_calls(self, messages: list, text: str, tool_calls_map: dict) -> None:
        """
        执行 tool calls 并将 assistant + tool 消息注入 messages。

        Args:
            messages: 对话消息列表（就地修改）
            text: 本轮 assistant 文本内容
            tool_calls_map: {index: {id, name, arguments}}
        """
        # 构建 assistant message
        assistant_calls = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            assistant_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            })

        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": assistant_calls,
        })

        # 逐个执行 tool
        for tc_data in assistant_calls:
            tool_name = tc_data["function"]["name"]
            tool_call_id = tc_data["id"]

            try:
                args = json.loads(tc_data["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            args_preview = json.dumps(args, ensure_ascii=False)[:120]
            logger.info(f"[Tool Call] {tool_name}({args_preview})")

            # 基于实际耗时的单次工具超时控制（默认 600s = 10 分钟，可配置）
            result, tool_elapsed = await self._call_tool_with_timeout(tool_name, args)

            result_preview = str(result)[:500]
            logger.info(
                f"[Tool Result] {tool_name} -> {result_preview[:200]} "
                f"({tool_elapsed:.1f}s)"
            )
            logger.debug(f"[Tool Result Full] {tool_name}: {result_preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    # ==================================================================
    # Tool 分发（含超时控制）
    # ==================================================================

    async def _call_tool_with_timeout(
        self, tool_name: str, args: dict
    ) -> tuple[str, float]:
        """
        执行单次工具调用，带基于实际耗时的超时控制。

        默认 600s (10 分钟)，BMC 测试中 IPMI/Redfish/SSH 命令
        可能耗时 30~300 秒，基于实际耗时的超时比固定次数更合理。
        超时值通过 config.yaml agent.tool_timeout_seconds 配置。

        Returns:
            (result_json: str, elapsed_seconds: float)
        """
        tool_start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._dispatch_tool(tool_name, args),
                timeout=self._tool_timeout_seconds,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - tool_start
            logger.error(
                f"[Tool Timeout] {tool_name} 超时: "
                f"elapsed={elapsed:.1f}s, timeout={self._tool_timeout_seconds}s"
            )
            result = json.dumps({
                "error": (
                    f"工具 {tool_name} 执行超时"
                    f"（{elapsed:.1f}s > {self._tool_timeout_seconds}s），"
                    f"请在 config.yaml agent.tool_timeout_seconds 中调整"
                )
            }, ensure_ascii=False)

        elapsed = time.monotonic() - tool_start
        return result, elapsed

    async def _dispatch_tool(self, tool_name: str, args: dict) -> str:
        """分发 tool call 到对应 handler。"""
        handler = self._tool_handlers.get(tool_name)
        if not handler:
            return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

        try:
            return await handler(args)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 真实 Tool 实现
    # ------------------------------------------------------------------

    async def _tool_redfish_request(self, args: dict) -> str:
        """
        真实 Redfish 请求。

        使用 httpx 发送 HTTPS 请求到 BMC（禁用 SSL 验证）。
        从 config 中读取 bmc_host、bmc_port、bmc_user、bmc_password。
        """
        endpoint = args.get("endpoint", "/redfish/v1")
        method = args.get("method", "GET").upper()
        body = args.get("body")
        custom_headers = args.get("headers")

        client = await self._get_http_client()

        # 构建 Basic Auth
        import base64
        credentials = f"{self.bmc_user}:{self.bmc_password}"
        auth_header = "Basic " + base64.b64encode(credentials.encode()).decode()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": auth_header,
        }
        # 透传自定义 Headers（如 If-Match, If-None-Match 等）
        if custom_headers and isinstance(custom_headers, dict):
            headers.update(custom_headers)
            logger.debug(f"[Redfish] custom headers merged: {list(custom_headers.keys())}")

        try:
            if method == "GET":
                resp = await client.get(endpoint, headers=headers)
            elif method == "POST":
                resp = await client.post(endpoint, headers=headers, json=body)
            elif method == "PATCH":
                resp = await client.patch(endpoint, headers=headers, json=body)
            elif method == "DELETE":
                resp = await client.delete(endpoint, headers=headers)
            else:
                return json.dumps({"error": f"不支持的 HTTP 方法: {method}"}, ensure_ascii=False)

            # 尝试解析 JSON 响应
            try:
                resp_body = resp.json()
            except Exception:
                resp_body = resp.text

            return json.dumps(
                {
                    "http_status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp_body,
                },
                ensure_ascii=False,
                indent=2,
            )

        except httpx.ConnectError as e:
            return json.dumps(
                {"error": f"连接失败 ({self.bmc_host}:{self.bmc_port}): {e}"},
                ensure_ascii=False,
            )
        except httpx.TimeoutException:
            return json.dumps(
                {"error": f"请求超时 ({self.bmc_host}:{self.bmc_port})"},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"error": f"Redfish 请求异常: {e}"},
                ensure_ascii=False,
            )

    async def _tool_ipmi_command(self, args: dict) -> str:
        """
        真实 IPMI 命令（支持动态用户凭据 + 认证失败自动恢复）。

        通过 IPMITool 发送 IPMI 命令。
        - 支持 user/password 参数覆盖默认凭据
        - 认证失败时自动尝试备用凭据重试
        - 认证失败时触发 recovery_tool.recover() 恢复环境
        """
        command = args.get("command", "")
        timeout = args.get("timeout", 30)
        ipmi_user = args.get("user")
        ipmi_password = args.get("password")

        if not command.strip():
            return json.dumps(
                {"error": "IPMI 命令为空"},
                ensure_ascii=False,
            )

        # ---- 第一次尝试：使用请求的凭据 ----
        try:
            result = await self._ipmi_tool.execute(
                command,
                timeout=timeout,
                user=ipmi_user,
                password=ipmi_password,
            )
        except Exception as e:
            return json.dumps(
                {"error": f"IPMI 执行异常: {e}"},
                ensure_ascii=False,
            )

        # ---- 认证失败自动恢复 + 备用凭据重试 ----
        if not result.success and self._is_ipmi_auth_failure(result):
            logger.warning(
                "[IPMI] auth failure detected, triggering recovery + retry"
            )
            # 1. 触发环境恢复（确保 user 2 恢复为 Administrator）
            try:
                await self._recovery_tool.recover()
            except Exception as recovery_err:
                logger.warning(f"[IPMI] recovery failed: {recovery_err}")

            # 2. 用默认凭据重试一次
            retry_user = self.bmc_user
            retry_password = self.bmc_password
            if ipmi_user != retry_user:
                logger.info(
                    f"[IPMI] retrying with default credentials: {retry_user}"
                )
                try:
                    result = await self._ipmi_tool.execute(
                        command,
                        timeout=timeout,
                        user=retry_user,
                        password=retry_password,
                    )
                except Exception as e:
                    return json.dumps(
                        {"error": f"IPMI 重试异常: {e}"},
                        ensure_ascii=False,
                    )

        if not result.success:
            return json.dumps(
                {
                    "error": result.error,
                    "command": result.command,
                    "exit_code": result.exit_code,
                },
                ensure_ascii=False,
                indent=2,
            )

        return IPMITool.to_json(result)

    @staticmethod
    def _is_ipmi_auth_failure(result) -> bool:
        """检查 IPMI 结果是否为认证/会话建立失败"""
        auth_keywords = [
            "Unable to establish IPMI v2 / RMCP+ session",
            "Connection refused",
            "Unauthorized",
            "Authentication failed",
            "invalid user name",
            "set session privilege",
            "timeout",  # pyghmi 超时通常也是认证问题
        ]
        error_text = (result.error or "").lower()
        return any(kw.lower() in error_text for kw in auth_keywords)

    async def _tool_ssh_exec(self, args: dict) -> str:
        """
        SSH 命令执行（paramiko 后端）。

        支持两种模式:
        - 非交互式: 提供 command 参数
        - 交互式: 提供 interactions 参数（expect/send 对列表）
        """
        # 所有连接参数强制使用 config 值
        # LLM 经常编造错误密码和错误地址，不可信
        host = self.ssh_host
        port = self.ssh_port
        user = self.bmc_user
        password = self.bmc_password
        command = args.get("command", "")
        timeout = args.get("timeout", 30)
        interactions = args.get("interactions")

        if not command.strip() and not interactions:
            return json.dumps(
                {"error": "必须提供 command 或 interactions 参数"},
                ensure_ascii=False,
            )

        logger.info(f"[SSH] host={host}:{port} | cmd: {command[:80]}")
        if interactions:
            logger.info(f"[SSH] 交互模式: {len(interactions)} 步")

        ssh = SSHTool(host=host, port=port, user=user, password=password)

        try:
            result = await ssh.execute(
                command, timeout=timeout, interactions=interactions
            )
        except Exception as e:
            return json.dumps(
                {"error": f"SSH 执行异常: {e}"},
                ensure_ascii=False,
            )

        if not result.success:
            return json.dumps(
                {
                    "error": result.error,
                    "command": result.command,
                    "host": result.host,
                    "port": result.port,
                    "mode": result.mode,
                    "exit_code": result.exit_code,
                },
                ensure_ascii=False,
                indent=2,
            )

        return SSHTool.to_json(result)

    # ------------------------------------------------------------------
    # SSH Session 工具（统一 action 分发）
    # ------------------------------------------------------------------

    async def _tool_ssh_session(self, args: dict) -> str:
        """SSH 长连接会话统一入口，通过 action 参数分发。"""
        action = args.get("action", "")
        handler = {
            "open": self._ssh_action_open,
            "exec": self._ssh_action_exec,
            "expect": self._ssh_action_expect,
            "send": self._ssh_action_send,
            "read": self._ssh_action_read,
            "close": self._ssh_action_close,
        }.get(action)

        if not handler:
            return json.dumps(
                {
                    "error": (
                        f"未知 action: '{action}'，"
                        f"支持: open / exec / expect / send / read / close"
                    )
                },
                ensure_ascii=False,
            )
        return await handler(args)

    def _require_session(self):
        """
        获取活跃的 SSH Session。

        Manager.get() 会自动清理死会话。
        """
        return self._session_mgr.get()

    async def _ssh_action_open(self, args: dict) -> str:
        """打开 SSH 长连接会话。"""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._session_mgr.open),
                timeout=30,
            )
            logger.info(
                f"[SSH Session] 已打开 {self.ssh_host}:{self.ssh_port}"
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "error": (
                        f"SSH 连接超时 (30s): {self.ssh_host}:{self.ssh_port}，"
                        f"请检查网络连通性和 BMC SSH 服务状态"
                    )
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": (
                        f"SSH 会话打开失败: {e}。"
                        f"请检查 {self.ssh_host}:{self.ssh_port} 是否可达"
                    )
                },
                ensure_ascii=False,
            )

    async def _ssh_action_exec(self, args: dict) -> str:
        """在 SSH 会话中执行命令。"""
        session = self._require_session()
        if not session:
            return json.dumps(
                {
                    "error": (
                        "SSH 会话未打开或已断开，"
                        "请先调用 ssh_session(action=\"open\")"
                    )
                },
                ensure_ascii=False,
            )

        command = args.get("command", "")
        timeout = args.get("timeout", 30)

        if not command.strip():
            return json.dumps(
                {"error": "command 不能为空"},
                ensure_ascii=False,
            )

        logger.info(f"[SSH Session] exec: {command[:80]}")

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(session.send_command, command, timeout),
                timeout=timeout + 10,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "status": "timeout",
                    "error": (
                        f"命令执行超时 ({timeout}s): {command}。"
                        f"命令可能进入了交互模式，请使用 send+expect 模式"
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            error_msg = str(e)
            if "通道已关闭" in error_msg or "已断开" in error_msg:
                return json.dumps(
                    {
                        "error": (
                            f"SSH 通道已断开: {error_msg}。"
                            f"请重新 ssh_session(action=\"open\")"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"error": error_msg},
                ensure_ascii=False,
            )

    async def _ssh_action_expect(self, args: dict) -> str:
        """等待 SSH 会话中出现指定输出模式。"""
        session = self._require_session()
        if not session:
            return json.dumps(
                {
                    "error": (
                        "SSH 会话未打开或已断开，"
                        "请先调用 ssh_session(action=\"open\")"
                    )
                },
                ensure_ascii=False,
            )

        patterns = args.get("patterns", [])
        timeout = args.get("timeout", 10)

        if not patterns:
            return json.dumps(
                {"error": "patterns 不能为空"},
                ensure_ascii=False,
            )

        logger.info(
            f"[SSH Session] expect: {patterns} (timeout={timeout}s)"
        )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(session.read_until, patterns, timeout),
                timeout=timeout + 5,
            )
            output_preview = result.get("output", "")[-200:]
            logger.info(
                f"[SSH Session] expect result: "
                f"matched={result.get('matched')}, "
                f"pattern={result.get('matched_pattern')}, "
                f"output={output_preview}"
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "status": "timeout",
                    "error": (
                        f"等待模式超时 ({timeout}s)，"
                        f"patterns: {patterns}。"
                        f"可以使用 read action 查看当前输出"
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            error_msg = str(e)
            if "通道已关闭" in error_msg or "已断开" in error_msg:
                return json.dumps(
                    {
                        "error": (
                            f"SSH 通道已断开: {error_msg}。"
                            f"请重新 ssh_session(action=\"open\")"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"error": error_msg},
                ensure_ascii=False,
            )

    async def _ssh_action_send(self, args: dict) -> str:
        """向 SSH 会话发送文本（用于交互式输入）。"""
        session = self._require_session()
        if not session:
            return json.dumps(
                {
                    "error": (
                        "SSH 会话未打开或已断开，"
                        "请先调用 ssh_session(action=\"open\")"
                    )
                },
                ensure_ascii=False,
            )

        text = args.get("text", "")
        is_password = args.get("is_password", False)
        press_enter = args.get("press_enter", True)

        if not text:
            return json.dumps(
                {"error": "text 不能为空"},
                ensure_ascii=False,
            )

        log_text = "****" if is_password else text[:30]
        logger.info(f"[SSH Session] send: {log_text}")

        try:
            result = await asyncio.to_thread(
                session.send_line, text, is_password, press_enter
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_msg = str(e)
            if "通道已关闭" in error_msg or "已断开" in error_msg:
                return json.dumps(
                    {
                        "error": (
                            f"发送失败，SSH 通道已断开: {error_msg}。"
                            f"请重新 ssh_session(action=\"open\")"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"error": error_msg},
                ensure_ascii=False,
            )

    async def _ssh_action_read(self, args: dict) -> str:
        """读取 SSH 会话当前可用输出。"""
        session = self._require_session()
        if not session:
            return json.dumps(
                {
                    "error": (
                        "SSH 会话未打开或已断开，"
                        "请先调用 ssh_session(action=\"open\")"
                    )
                },
                ensure_ascii=False,
            )

        timeout = args.get("timeout", 2)

        try:
            result = await asyncio.to_thread(
                session.read_available, timeout
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_msg = str(e)
            if "通道已关闭" in error_msg or "已断开" in error_msg:
                return json.dumps(
                    {
                        "error": (
                            f"SSH 通道已断开: {error_msg}。"
                            f"请重新 ssh_session(action=\"open\")"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"error": error_msg},
                ensure_ascii=False,
            )

    async def _ssh_action_close(self, args: dict) -> str:
        """关闭 SSH 会话并获取完整 Evidence。"""
        session = self._session_mgr.get()
        if not session:
            return json.dumps(
                {
                    "status": "no_session",
                    "message": "没有打开的 SSH 会话",
                },
                ensure_ascii=False,
            )

        logger.info(
            f"[SSH Session] 关闭会话 "
            f"(interactions={session.interaction_count})"
        )

        try:
            result = await asyncio.to_thread(self._session_mgr.close)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps(
                {
                    "error": (
                        f"关闭会话异常: {e}。"
                        f"尝试 ssh_session(action=\"close\") 或忽略此错误"
                    )
                },
                ensure_ascii=False,
            )

    async def _tool_bmc_command_rag(self, args: dict) -> str:
        """
        BMC 命令 RAG（IPMI + Redfish 双接口支持）。

        流程:
        1. 接收模糊操作描述 + interface_type 参数
        2. 自动检测或按指定接口类型决定 doc_type 过滤策略
        3. 生成查询向量（Embedding API）
        4. 调用 HybridRetriever 进行混合检索（支持查询改写）
        5. 格式化返回结构化结果供 LLM 使用

        interface_type 策略:
        - "auto":  关键词自动检测，决定搜索 IPMI / Redfish / both
        - "ipmi":  仅搜索 IPMI 文档 (doc_type="ipmi", chunk_type="command")
        - "redfish": 仅搜索 Redfish 文档 (doc_type="redfish", chunk_type="resource")
        - "both":  同时搜索两种文档，合并结果

        容错: RAG 不可用时优雅降级为关键词匹配，不阻塞执行流程。
        """
        operation = args.get("operation_description", "").strip()
        interface_type = args.get("interface_type", self._rag_default_interface)
        top_k = int(args.get("top_k", self._rag_top_k))

        # ---- 参数校验 ----
        if not operation:
            logger.warning("[RAG] 收到空查询，跳过")
            return json.dumps(
                {"error": "operation_description 不能为空", "results": []},
                ensure_ascii=False,
            )

        # ---- 确定检索接口类型 ----
        doc_types = self._resolve_doc_types(operation, interface_type)
        logger.info(f"[RAG] === 开始检索 ===")
        logger.info(f"[RAG] 原始查询: '{operation}'")
        logger.info(f"[RAG] 参数: interface_type={interface_type} -> doc_types={doc_types}, top_k={top_k}")

        # ---- RAG 未启用: 降级为关键词匹配 ----
        if not self._rag_enabled or not self._retriever:
            logger.warning("[RAG] RAG 未启用或初始化失败，降级为关键词匹配")
            return self._rag_fallback_keyword(operation, interface_type)

        # ---- Step 1: 生成查询向量 ----
        query_embedding = None
        try:
            query_embedding = await self._get_query_embedding(operation)
            if query_embedding:
                logger.info(f"[RAG] Embedding 生成成功 (dim={len(query_embedding)})")
            else:
                logger.warning("[RAG] Embedding 返回为空，将仅使用 BM25 检索")
        except Exception as e:
            logger.warning(f"[RAG] Embedding 生成异常: {e}，将仅使用 BM25 检索")

        # ---- Step 2: 按接口类型分别检索 ----
        all_results: List[Dict[str, Any]] = []

        for doc_type in doc_types:
            try:
                chunk_type = "command" if doc_type == "ipmi" else "resource"
                results = await self._retriever.search_with_rewrite(
                    query=operation,
                    query_embedding=query_embedding,
                    top_k=top_k,
                    chunk_type=chunk_type,
                    doc_type=doc_type,
                )
                logger.info(f"[RAG] {doc_type} 检索完成: {len(results)} 条结果")
                all_results.extend(results)
            except Exception as e:
                logger.error(f"[RAG] {doc_type} 检索异常: {type(e).__name__}: {e}")
                continue

        # 按分数排序合并结果
        all_results.sort(key=lambda r: r.get("score", 0), reverse=True)
        all_results = all_results[:top_k * len(doc_types)]

        if not all_results:
            logger.warning("[RAG] 未检索到任何结果，请检查知识库是否已构建")
            return self._rag_fallback_keyword(operation, interface_type)

        # ---- Step 3: 详细日志 ----
        logger.info(f"[RAG] 合并后共 {len(all_results)} 条结果")
        for i, r in enumerate(all_results[:6], 1):
            meta = r.get("metadata", {})
            score = r.get("score", 0)
            dt = meta.get("doc_type", "-")
            ct = meta.get("chunk_type", "-")
            section = meta.get("section", "-")
            if dt == "redfish":
                uri = meta.get("resource_uri", "-")
                method = meta.get("http_method", "-")
                logger.info(
                    f"[RAG]   [{i}] score={score:.4f} | {dt}/{ct} | "
                    f"URI={uri} | Method={method}"
                )
            else:
                cmd_name = meta.get("command_name", "-")
                netfn = meta.get("netfn", "-")
                cmd = meta.get("cmd", "-")
                logger.info(
                    f"[RAG]   [{i}] score={score:.4f} | {dt}/{ct} | "
                    f"command={cmd_name} | NetFn={netfn}, Cmd={cmd}"
                )

        # ---- Step 4: 格式化并返回 ----
        return self._format_rag_results(operation, all_results, interface_type)

    def _resolve_doc_types(self, operation: str, interface_type: str) -> List[str]:
        """
        根据 interface_type 参数和查询内容决定搜索哪些文档类型.

        Args:
            operation: 查询文本
            interface_type: "auto" / "ipmi" / "redfish" / "both"

        Returns:
            需要搜索的 doc_type 列表，如 ["ipmi"] / ["redfish"] / ["ipmi", "redfish"]
        """
        if interface_type == "ipmi":
            return ["ipmi"]
        if interface_type == "redfish":
            return ["redfish"]
        if interface_type == "both":
            return ["ipmi", "redfish"]

        # interface_type == "auto": 关键词检测
        if not self._rag_auto_detect:
            return ["ipmi", "redfish"]

        query_lower = operation.lower()

        # 明确的 Redfish 特征词
        redfish_strong = [
            "/redfish", "redfish", "rest api", "restful",
            "get ", "post ", "patch ", "delete ",
            "uri", "endpoint", "json", "https://",
            "odatatype", "odata",
        ]
        # 明确的 IPMI 特征词
        ipmi_strong = [
            "ipmi", "ipmitool", "netfn", "raw 0x",
            "sel ", "sdr ", "fru ", "mc info", "mc guid",
            "chassis ", "sensor list",
        ]
        # 偏向 Redfish 的场景词
        redfish_weak = [
            "账户管理", "会话管理", "用户角色", "redfish",
            "事件订阅", "更新服务", "任务服务", "证书",
            "ethernetinterface", "ip地址配置", "网络接口",
            "virtualmedia", "虚拟媒体", "固件升级",
        ]
        # 偏向 IPMI 的场景词
        ipmi_weak = [
            "raw命令", "原始命令", "ipmi命令",
            "风扇模式", "sdr仓库", "传感器读数",
            "机箱状态", "机箱电源",
        ]

        redfish_score = 0
        ipmi_score = 0

        for kw in redfish_strong:
            if kw in query_lower:
                redfish_score += 2
        for kw in ipmi_strong:
            if kw in query_lower:
                ipmi_score += 2
        for kw in redfish_weak:
            if kw in query_lower:
                redfish_score += 1
        for kw in ipmi_weak:
            if kw in query_lower:
                ipmi_score += 1

        logger.info(
            f"[RAG] 自动检测: redfish_score={redfish_score}, "
            f"ipmi_score={ipmi_score}"
        )

        # 只有明显偏向某一方时才过滤，否则搜索两者
        threshold = 2
        if redfish_score >= threshold and ipmi_score < threshold:
            return ["redfish"]
        if ipmi_score >= threshold and redfish_score < threshold:
            return ["ipmi"]
        return ["ipmi", "redfish"]

    def _format_rag_results(
        self, operation: str, results: List[Dict[str, Any]], interface_type: str
    ) -> str:
        """
        格式化 RAG 检索结果为结构化字符串，便于 LLM 直接使用。

        自动识别结果类型（IPMI / Redfish），按对应格式输出:
        - IPMI 结果: command_name, netfn, cmd, description
        - Redfish 结果: resource_uri, http_method, description, schema_name
        """
        if not results:
            return json.dumps(
                {
                    "operation_description": operation,
                    "interface_type": interface_type,
                    "total_matches": 0,
                    "results": [],
                    "hint": "RAG 未检索到匹配结果，请根据操作描述自行推测命令，"
                            "或尝试使用更具体的关键词重新调用 bmc_command_rag",
                },
                ensure_ascii=False,
                indent=2,
            )

        formatted_items = []
        for i, r in enumerate(results):
            meta = r.get("metadata", {})
            doc = r.get("document", "")
            score = r.get("score", 0.0)
            doc_type = meta.get("doc_type", "ipmi")

            if doc_type == "redfish":
                item = {
                    "rank": i + 1,
                    "doc_type": "redfish",
                    "resource_uri": meta.get("resource_uri", ""),
                    "http_method": meta.get("http_method", ""),
                    "schema_name": meta.get("schema_name", ""),
                    "chinese_name": meta.get("chinese_name", ""),
                    "english_name": meta.get("english_name", ""),
                    "description": meta.get("description", ""),
                    "score": round(score, 4),
                    "document_snippet": doc[:500] if doc else "",
                }
            else:
                # IPMI 结果
                item = {
                    "rank": i + 1,
                    "doc_type": "ipmi",
                    "command_name": meta.get("command_name", ""),
                    "section": meta.get("section", ""),
                    "description": meta.get("description", ""),
                    "netfn": meta.get("netfn", ""),
                    "cmd": meta.get("cmd", ""),
                    "score": round(score, 4),
                    "notes": meta.get("notes", ""),
                    "document_snippet": doc[:500] if doc else "",
                }
            formatted_items.append(item)

        # 统计各类型结果数
        ipmi_count = sum(1 for r in formatted_items if r["doc_type"] == "ipmi")
        redfish_count = sum(1 for r in formatted_items if r["doc_type"] == "redfish")

        return json.dumps(
            {
                "operation_description": operation,
                "interface_type": interface_type,
                "total_matches": len(formatted_items),
                "ipmi_results": ipmi_count,
                "redfish_results": redfish_count,
                "results": formatted_items,
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _rag_fallback_keyword(operation: str, interface_type: str) -> str:
        """RAG 未启用时的关键词匹配降级方案（IPMI + Redfish 双接口）。"""
        templates = []
        op_lower = operation.lower()

        # -- 搜索范围控制 --
        search_ipmi = interface_type in ("ipmi", "auto", "both", "any")
        search_redfish = interface_type in ("redfish", "auto", "both", "any")

        # ---- IPMI 模板 ----
        if search_ipmi:
            if ("用户" in operation or "user" in op_lower
                    or "账户" in operation or "account" in op_lower):
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "ipmi",
                    "command_name": "Set User Name",
                    "netfn": "0x06",
                    "cmd": "0x45",
                    "description": "设置用户名",
                    "score": 0.0,
                    "notes": "添加用户前需检查用户数量上限（15个），"
                             "后续还需 Set User Password + Enable User",
                })
            if "电源" in operation or "power" in op_lower or "上下电" in operation:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "ipmi",
                    "command_name": "Chassis Power Control",
                    "netfn": "0x00",
                    "cmd": "0x02",
                    "description": "机箱电源控制（开机/关机/复位）",
                    "score": 0.0,
                    "notes": "参数: 0x00=关机, 0x01=开机, 0x02=复位, 0x03=硬关机",
                })
            if "风扇" in operation or "fan" in op_lower:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "ipmi",
                    "command_name": "Set Fan Mode / Set Fan Speed",
                    "netfn": "0x2e",
                    "cmd": "0x07",
                    "description": "设置风扇运行模式和转速",
                    "score": 0.0,
                    "notes": "自动/手动模式切换，手动模式下可设定目标转速",
                })
            if "SEL" in operation or "日志" in operation or "log" in op_lower:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "ipmi",
                    "command_name": "Clear SEL",
                    "netfn": "0x0a",
                    "cmd": "0x47",
                    "description": "清除系统事件日志",
                    "score": 0.0,
                    "notes": "清除前建议先备份日志（Get SEL）",
                })
            if ("固件" in operation or "版本" in operation or "firmware" in op_lower
                    or "version" in op_lower):
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "ipmi",
                    "command_name": "Get Device ID",
                    "netfn": "0x06",
                    "cmd": "0x01",
                    "description": "获取设备信息（制造商、固件版本、IPMI版本）",
                    "score": 0.0,
                    "notes": "返回包含固件版本号的设备信息",
                })

        # ---- Redfish 模板 ----
        if search_redfish:
            if ("用户" in operation or "account" in op_lower):
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "redfish",
                    "resource_uri": "/redfish/v1/AccountService/Accounts",
                    "http_method": "GET,POST",
                    "schema_name": "AccountService",
                    "chinese_name": "账户管理",
                    "description": "查询/创建用户账户",
                    "score": 0.0,
                })
            if "电源" in operation or "power" in op_lower:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "redfish",
                    "resource_uri": "/redfish/v1/Systems/{SystemId}",
                    "http_method": "GET",
                    "schema_name": "ComputerSystem",
                    "chinese_name": "系统资源",
                    "description": "查询系统电源状态（PowerState字段）",
                    "score": 0.0,
                })
            if ("固件" in operation or "版本" in operation or "firmware" in op_lower
                    or "version" in op_lower):
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "redfish",
                    "resource_uri": "/redfish/v1/Managers/{ManagerId}",
                    "http_method": "GET",
                    "schema_name": "Manager",
                    "chinese_name": "管理控制器",
                    "description": "查询 BMC 固件版本（FirmwareVersion 字段）",
                    "score": 0.0,
                })
            if "传感器" in operation or "温度" in operation or "sensor" in op_lower:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "redfish",
                    "resource_uri": "/redfish/v1/Chassis/{ChassisId}/Thermal",
                    "http_method": "GET",
                    "schema_name": "Thermal",
                    "chinese_name": "散热管理",
                    "description": "查询温度和风扇传感器数据",
                    "score": 0.0,
                })
            if "网络" in operation or "ip地址" in operation or "网络" in operation:
                templates.append({
                    "rank": len(templates) + 1,
                    "doc_type": "redfish",
                    "resource_uri": "/redfish/v1/Managers/{ManagerId}/EthernetInterfaces",
                    "http_method": "GET,PATCH",
                    "schema_name": "EthernetInterface",
                    "chinese_name": "网络接口",
                    "description": "查询/修改 BMC 网络接口配置",
                    "score": 0.0,
                })

        if not templates:
            templates.append({
                "rank": 1,
                "doc_type": "unknown",
                "command_name": "(未找到匹配)",
                "interface_type": interface_type if interface_type != "auto" else "unknown",
                "description": "RAG 未启用，关键词匹配未命中",
                "score": 0.0,
                "notes": "请启用 RAG 以获得更准确的命令推荐",
            })

        ipmi_count = sum(1 for t in templates if t.get("doc_type") == "ipmi")
        redfish_count = sum(1 for t in templates if t.get("doc_type") == "redfish")

        return json.dumps(
            {
                "operation_description": operation,
                "interface_type": interface_type,
                "total_matches": len(templates),
                "ipmi_results": ipmi_count,
                "redfish_results": redfish_count,
                "results": templates,
                "fallback": True,
                "message": "RAG 未启用，使用关键词匹配降级方案",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ==================================================================
    # 输出解析
    # ==================================================================

    def _parse_record(self, content: str, case: dict, started_at: datetime) -> ExecutionRecord:
        """
        从 LLM 输出解析 ExecutionRecord。

        尝试链：提取 JSON -> 清理 -> 直接解析 -> 清理后解析 -> 修复解析 -> fallback
        """
        json_str = extract_json_from_response(content)

        if json_str:
            # 第一轮：直接解析
            try:
                data = json.loads(json_str)
                record = ExecutionRecord.model_validate(data)
                record = self._force_override_timestamps(record, started_at)
                logger.info("成功解析 ExecutionRecord")
                return record
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"直接解析失败: {e}")

            # 第二轮：清理后解析
            cleaned = _sanitize_json_string(json_str)
            if cleaned != json_str:
                try:
                    data = json.loads(cleaned)
                    record = ExecutionRecord.model_validate(data)
                    record = self._force_override_timestamps(record, started_at)
                    logger.info("清理后解析成功")
                    return record
                except (json.JSONDecodeError, ValidationError):
                    pass

            # 第三轮：修复解析
            try:
                data = json.loads(cleaned if cleaned else json_str)
            except json.JSONDecodeError:
                # 最后一次尝试：用更宽松的方式解析
                data = None

            if data is None:
                # 尝试修复不可解析的 JSON
                data = self._try_fix_malformed_json(cleaned if cleaned else json_str)

            if data:
                record = self._repair_and_validate(data, case, started_at)
                if record:
                    logger.info("修复后解析成功")
                    return record

        # 所有解析尝试失败，生成 fallback
        logger.warning("未找到有效 JSON，生成 fallback 记录")
        return self._build_failure_record(
            case, started_at,
            raw_output=content,
            step_desc="Agent 输出解析失败，原始输出已保存",
            error_msg="模型输出无法解析为 ExecutionRecord JSON",
        )

    def _force_override_timestamps(self, record: ExecutionRecord, started_at: datetime) -> ExecutionRecord:
        """强制覆盖时间戳和 execution_id（不信任 LLM）。"""
        now = datetime.now()
        record.execution_id = f"exec_{now.strftime('%Y%m%d_%H%M%S')}"
        record.started_at = started_at
        record.completed_at = now
        return record

    def _try_fix_malformed_json(self, raw: str) -> Optional[dict]:
        """
        尝试修复严重格式错误的 JSON。

        策略：单引号替换、属性名加引号、宽松解析。
        """
        s = raw

        # 尝试替换单引号为双引号（谨慎处理，避免破坏字符串内容）
        # 只替捓名值对中的单引号
        s = re.sub(r":\s*'([^']*)'", r': "\1"', s)

        # 尝试给裸属性名加引号（如 {name: "value"} -> {"name": "value"}）
        s = re.sub(r"(\{|,)\s*([a-zA-Z_]\w*)\s*:", r'\1 "\2":', s)

        try:
            return json.loads(s)
        except (json.JSONDecodeError, Exception):
            return None

    def _repair_and_validate(self, data: dict, case: dict, started_at: datetime) -> Optional[ExecutionRecord]:
        """补全/修复字段后验证。时间戳等关键字段强制使用真实值。"""

        # 强制覆盖：时间戳和 ID 由框架控制
        now = datetime.now()
        data["execution_id"] = f"exec_{now.strftime('%Y%m%d_%H%M%S')}"
        data["started_at"] = started_at.isoformat()
        data["completed_at"] = now.isoformat()

        # 补全缺失字段
        data.setdefault("case_id", case.get("case_id", case.get("用例_编号", "unknown")))
        data.setdefault("case_name", case.get("name", case.get("用例_名称", "unknown")))
        data.setdefault("overall_status", "completed")
        data.setdefault("environment", {})
        data.setdefault("test_case_info", {})
        data.setdefault("steps", [])

        # 修复 environment
        if not isinstance(data["environment"], dict):
            data["environment"] = {"raw": str(data["environment"])}

        # 修复 prerequisites: 字符串 -> dict
        prereqs = data.get("prerequisites", [])
        repaired_prereqs = []
        for p in prereqs:
            if isinstance(p, str):
                repaired_prereqs.append({"name": p, "status": "checked"})
            elif isinstance(p, dict):
                repaired_prereqs.append(p)
        data["prerequisites"] = repaired_prereqs

        # 修复 steps
        for step in data.get("steps", []):
            if not isinstance(step, dict):
                continue
            self._repair_step(step)

        # 确保 steps 非空（Pydantic 可能要求至少一个 step）
        if not data["steps"]:
            data["steps"] = [{
                "step_id": "step_001",
                "description": "自动生成的空步骤（原始输出解析失败）",
                "tool": "unknown",
                "expected": "ExecutionRecord JSON",
                "actual": "解析失败",
                "status": "failed",
                "started_at": started_at.isoformat(),
                "completed_at": now.isoformat(),
            }]

        try:
            return ExecutionRecord.model_validate(data)
        except ValidationError as e:
            logger.error(f"修复后仍无法解析: {e}")
            return None

    def _repair_step(self, step: dict) -> None:
        """修复单个 step 中常见的格式问题。"""
        # 修复 evidence 格式
        ev_list = step.get("evidence", [])
        if not isinstance(ev_list, list):
            ev_list = []
        repaired_ev = []
        for idx, ev in enumerate(ev_list):
            if not isinstance(ev, dict):
                continue
            repaired_ev.append(self._repair_evidence(ev, step.get("step_id", "unknown"), idx))
        step["evidence"] = repaired_ev

        # 修复 status：枚举值兼容
        status_raw = step.get("status", "completed")
        if isinstance(status_raw, str):
            status_lower = status_raw.lower()
            if status_lower in ("completed", "success", "ok", "pass"):
                step["status"] = "completed"
            elif status_lower in ("failed", "failure", "error", "fail"):
                step["status"] = "failed"
            elif status_lower in ("skipped", "skip"):
                step["status"] = "skipped"
            else:
                step["status"] = "completed"

        # 确保必填字段存在
        step.setdefault("tool", "unknown")
        step.setdefault("expected", "")

        # IPMI 步骤强制修复：命令失败时 LLM 可能遗漏 tool/interface_preference
        self._fix_ipmi_step_fields(step)

    def _fix_ipmi_step_fields(self, step: dict) -> None:
        """
        强制修复 IPMI 步骤的 tool / interface_preference / actual 字段。

        当 ipmi_command 执行失败时，LLM 生成的 ExecutionRecord 中
        step.tool 和 step.interface_preference 经常为空或 "N/A"，
        导致 Markdown 报告中工具/接口显示 N/A，影响故障定位。

        检测条件（满足任一即视为 IPMI 步骤）：
        - command 字段包含 "ipmitool" 或 "ipmi" 关键字
        - tool 字段已标注为 "ipmi" 或 "ipmi_command"
        - description 包含 IPMI 相关描述
        """
        command = str(step.get("command") or "").lower()
        tool = str(step.get("tool") or "").lower()
        description = str(step.get("description") or "").lower()
        raw_stdout = str(step.get("raw_stdout") or "")
        raw_stderr = str(step.get("raw_stderr") or "")
        error_message = str(step.get("error_message") or "")

        is_ipmi = (
            "ipmitool" in command
            or "ipmi" in command
            or tool in ("ipmi", "ipmi_command")
            or "ipmi" in description
        )

        if not is_ipmi:
            return

        # 强制设置 tool 和 interface_preference
        step["tool"] = "ipmi_command"
        step["interface_preference"] = "ipmi"

        # 确保 actual 有值（优先 raw_stdout -> error_message -> raw_stderr）
        actual = step.get("actual")
        if not actual or str(actual).strip().upper() in ("N/A", "", "NONE", "NULL"):
            step["actual"] = raw_stdout or error_message or raw_stderr or "N/A"

    def _repair_evidence(self, ev: dict, step_id: str, idx: int) -> dict:
        """修复单个 evidence 对象，确保 5 个必填字段都存在。"""
        ev.setdefault("evidence_id", f"{step_id}_ev_{idx + 1:03d}")
        ev.setdefault("step_id", step_id)
        # evidence_type: 多种 LLM 写法兼容
        if "evidence_type" not in ev:
            ev["evidence_type"] = ev.get("type", ev.get("evidenceType", "unknown"))
        # content: 多种 LLM 写法兼容
        if "content" not in ev:
            content = ev.get("data", ev.get("description", ev.get("value", "")))
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            ev["content"] = str(content)
        ev.setdefault("captured_at", datetime.now().isoformat())
        ev.setdefault("metadata", {})
        return ev

    # ==================================================================
    # Failure 记录 & 持久化
    # ==================================================================

    def _build_failure_record(
        self,
        case: dict,
        started_at: datetime,
        raw_output: str = "",
        step_desc: str = "执行过程中发生异常",
        error_msg: str = "",
    ) -> ExecutionRecord:
        """构建 fallback / error 记录。"""
        completed_at = datetime.now()
        ts = completed_at.strftime("%Y%m%d_%H%M%S")

        step = StepRecord(
            step_id="step_failure",
            description=step_desc,
            tool="unknown",
            interface_preference="unknown",
            expected="ExecutionRecord JSON",
            actual=f"异常: {error_msg}" if error_msg else "解析失败",
            raw_stdout=raw_output,
            raw_stderr=error_msg,
            evidence=[],
            status=StepStatus.FAILED,
            error_message=error_msg or "执行失败",
            started_at=started_at,
            completed_at=completed_at,
        )

        return ExecutionRecord(
            execution_id=f"exec_{ts}_failure",
            case_id=case.get("case_id", case.get("用例_编号", "unknown")),
            case_name=case.get("name", case.get("用例_名称", "unknown")),
            environment={
                "bmc_host": self.bmc_host,
                "bmc_port": self.bmc_port,
                "ipmi_port": self.ipmi_port,
                "bmc_user": self.bmc_user,
            },
            test_case_info={"source_path": case.get("_source_path", ""), "failure": True},
            prerequisites=[],
            steps=[step],
            started_at=started_at,
            completed_at=completed_at,
            overall_status="failed",
        )

    def _save_record(self, record: ExecutionRecord) -> None:
        """自动保存 ExecutionRecord 到共享目录。"""
        try:
            save_execution_record(record, self.shared_dir)
        except Exception as e:
            logger.error(f"保存记录失败（不影响返回）: {e}")
