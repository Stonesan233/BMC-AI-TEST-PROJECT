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
import os
import re
import ssl
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from jinja2 import Template
from openai import AsyncOpenAI
from pydantic import ValidationError

from src.core.schemas import ExecutionRecord, StepRecord, StepStatus
from src.tools.ipmi_tool import IPMITool
from src.utils.file_handler import save_execution_record


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
                },
                "required": ["endpoint", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ipmi_command",
            "description": "执行 IPMI 命令。用于传统 BMC 管理操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "IPMI 命令，如 'chassis status'",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
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
            "description": "通过 SSH 在远程主机上执行命令。用于 BMC Shell 或主机控制台操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "目标主机 IP"},
                    "port": {
                        "type": "integer",
                        "description": "SSH 端口（BMC Shell 默认 22，主机控制台默认 2200）",
                        "default": 22,
                    },
                    "user": {"type": "string", "description": "用户名"},
                    "password": {"type": "string", "description": "密码"},
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
                    },
                },
                "required": ["host", "user", "password", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bmc_command_rag",
            "description": (
                "根据自然语言描述检索最匹配的 BMC 命令模板。"
                "当步骤描述模糊、缺少具体命令或参数时，必须优先调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_description": {
                        "type": "string",
                        "description": "操作的自然语言描述，如 '使用 CLI 新增用户'",
                    },
                    "interface_hint": {
                        "type": "string",
                        "enum": ["redfish", "cli", "ipmi", "any"],
                        "default": "any",
                        "description": "接口类型提示",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 3,
                        "description": "返回结果数量",
                    },
                },
                "required": ["operation_description"],
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
    MAX_TOOL_ROUNDS = 15

    def __init__(self, config: dict):
        exec_cfg = config.get("agents", {}).get("exec", {})
        if not exec_cfg:
            raise ValueError("config 中缺少 agents.exec 配置段，请检查 config.yaml")

        # 必填字段校验
        for field in ("base_url", "model"):
            if not exec_cfg.get(field):
                raise ValueError(f"config[agents.exec].{field} 不能为空，请检查 config.yaml")

        # API Key: 支持环境变量引用（如 ${GLM_API_KEY}）或 .env 文件
        api_key_raw = exec_cfg.get("api_key", "")
        if api_key_raw.startswith("${") and api_key_raw.endswith("}"):
            env_var = api_key_raw[2:-1].strip("}")
            api_key = os.environ.get(env_var, "")
            if not api_key:
                # 尝试从 .env 文件加载
                api_key = self._load_dotenv(env_var)
            if not api_key:
                raise ValueError(
                    f"环境变量 {env_var} 未设置，请编辑项目根目录 .env 文件或设置环境变量 {env_var}"
                )
        else:
            api_key = api_key_raw

        self.base_url = exec_cfg["base_url"]
        self.api_key = api_key
        self.model = exec_cfg["model"]
        self.temperature = float(exec_cfg.get("temperature", 0.1))
        self.max_tokens = int(exec_cfg.get("max_tokens", 8192))

        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )
        self.config = config
        self.shared_dir = config.get("storage", {}).get("shared_dir", "./shared")

        # 预加载 system prompt 模板
        self._system_template = Template(
            self.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )

        # BMC 连接信息
        target = config.get("target", {})
        self.bmc_host = target.get("bmc_host", "127.0.0.1")
        self.bmc_port = target.get("bmc_port", 443)
        self.bmc_user = target.get("bmc_user", "Administrator")
        self.bmc_password = target.get("bmc_password", "")
        self.ipmi_port = target.get("ipmi_port", 623)

        # httpx 客户端（禁用 SSL 验证，适配自签证书）
        self._http_client: Optional[httpx.AsyncClient] = None

        # IPMI Tool 实例（pyghmi 后端）
        self._ipmi_tool = IPMITool(
            host=self.bmc_host,
            port=self.ipmi_port,
            user=self.bmc_user,
            password=self.bmc_password,
            cipher_suite=17,
        )

        # Tool 分发表
        self._tool_handlers = {
            "redfish_request": self._tool_redfish_request,
            "ipmi_command": self._tool_ipmi_command,
            "ssh_exec": self._tool_ssh_exec,
            "bmc_command_rag": self._tool_bmc_command_rag,
        }

        print(f"[Exec Agent] 使用模型: {self.model} | base_url: {self.base_url}")
        print(f"[Exec Agent] 参数: temperature={self.temperature}, max_tokens={self.max_tokens}")
        print(f"[Exec Agent] 目标 BMC: {self.bmc_host}:{self.bmc_port} | IPMI port: {self.ipmi_port}")

    # ==================================================================
    # .env 文件加载
    # ==================================================================

    @staticmethod
    def _load_dotenv(key_name: str) -> str:
        """从项目根目录 .env 文件中读取指定变量"""
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if not env_path.exists():
            return ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key_name:
                return v.strip().strip("'\"")
        return ""

    # ==================================================================
    # httpx 生命周期
    # ==================================================================

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 异步客户端（懒初始化）。"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=False,     # 自签证书环境
                timeout=30.0,
                trust_env=False,  # 禁用系统代理，避免 Windows 代理干扰
            )
        return self._http_client

    async def close(self):
        """清理资源。"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._ipmi_tool.close()

    # ==================================================================
    # 公开接口
    # ==================================================================

    async def execute(self, case: dict, config: dict) -> ExecutionRecord:
        """
        执行单个测试用例。

        流程：渲染 prompt -> LLM 对话（含 Tool Calling）-> 解析 -> 保存
        """
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        print(f"\n[Exec] 开始执行: {case_name}")

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
            print(f"[Exec] 执行异常: {e}")
            record = self._build_failure_record(case, started_at, error_msg=str(e))
            self._save_record(record)
            return record

        # 解析 ExecutionRecord
        record = self._parse_record(final_content, case, started_at)

        # 自动保存
        self._save_record(record)

        print(f"[Exec] 执行完成: {case_name} -> {record.overall_status}")
        return record

    async def execute_batch(self, cases: list, config: dict) -> list:
        """批量执行测试用例（串行）。"""
        print(f"\n[Exec] 批量执行 {len(cases)} 个用例")

        records = []
        for i, case in enumerate(cases):
            print(f"\n[Exec] --- 用例 {i + 1}/{len(cases)} ---")
            try:
                records.append(await self.execute(case, config))
            except Exception as e:
                print(f"[Exec] 用例执行失败: {e}")
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
            print(f"\n[Exec] --- 第 {round_num + 1} 轮 ---")

            text, tool_calls, finish_reason = await self._stream_response(messages)

            # 无 tool call -> 返回
            if finish_reason != "tool_calls" or not tool_calls:
                return text

            # 执行 tool calls 并注入结果
            await self._process_tool_calls(messages, text, tool_calls)

        print("[Exec] 达到最大对话轮次限制")
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

            # 文本 -> 实时打印
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
            print(f"  [Exec] [Tool Call] {tool_name}({args_preview})")

            result = await self._dispatch_tool(tool_name, args)
            print(f"  [Exec] [Tool Result] {str(result)[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    # ==================================================================
    # Tool 分发
    # ==================================================================

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
        真实 IPMI 命令（pyghmi 后端）。

        通过 IPMITool (pyghmi) 发送 IPMI 命令。
        支持 cipher_suite=17（openUBMC 必须）。
        """
        command = args.get("command", "")
        timeout = args.get("timeout", 30)

        if not command.strip():
            return json.dumps(
                {"error": "IPMI 命令为空"},
                ensure_ascii=False,
            )

        print(f"  [IPMI] cmd: {command} | host={self.bmc_host}:{self.ipmi_port} | cipher=17")

        try:
            result = await self._ipmi_tool.execute(command, timeout=timeout)
        except Exception as e:
            return json.dumps(
                {"error": f"IPMI 执行异常: {e}"},
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

    async def _tool_ssh_exec(self, args: dict) -> str:
        """
        真实 SSH 执行。

        通过 subprocess 调用系统 ssh 命令。
        使用 -o StrictHostKeyChecking=no 禁用主机密钥检查。
        """
        host = args.get("host", self.bmc_host)
        port = args.get("port", 22)
        user = args.get("user", self.bmc_user)
        password = args.get("password", self.bmc_password)
        command = args.get("command", "")
        timeout = args.get("timeout", 30)

        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}",
            "-p", str(port),
            f"{user}@{host}",
            command,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=password.encode() + b"\n"),
                timeout=timeout,
            )

            return json.dumps(
                {
                    "host": host,
                    "port": port,
                    "command": command,
                    "exit_code": proc.returncode,
                    "stdout": stdout.decode("utf-8", errors="replace").strip(),
                    "stderr": stderr.decode("utf-8", errors="replace").strip(),
                },
                ensure_ascii=False,
            )
        except FileNotFoundError:
            return json.dumps(
                {"error": "ssh 命令未找到"},
                ensure_ascii=False,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return json.dumps(
                {"error": f"SSH 连接超时 ({timeout}s): {host}:{port}"},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"error": f"SSH 执行异常: {e}"},
                ensure_ascii=False,
            )

    async def _tool_bmc_command_rag(self, args: dict) -> str:
        """
        BMC 命令 RAG（关键词匹配模拟）。

        TODO: 集成 src/tools/rag.py（向量数据库 + Embedding）
        """
        operation = args.get("operation_description", "")
        hint = args.get("interface_hint", "any")
        templates = []

        if "用户" in operation or "user" in operation.lower() or "账户" in operation or "account" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "POST /redfish/v1/AccountService/Accounts",
                "parameters": {"UserName": "test_user", "Password": "Test@123", "RoleId": "Operator"},
                "notes": "RoleId 可选 Administrator / Operator / ReadOnly",
            })
            if hint in ("cli", "any"):
                templates.append({
                    "interface_type": "cli",
                    "recommended_command": "account adduser --username test_user --password Test@123 --role Operator",
                    "notes": "添加前必须检查用户数量上限（15个）",
                })
        if "电源" in operation or "power" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "POST /redfish/v1/Systems/system/Actions/ComputerSystem.Reset",
                "parameters": {"ResetType": "On"},
                "notes": "ResetType 可选 On/Off/GracefulShutdown/ForceRestart",
            })
        if "传感器" in operation or "sensor" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "GET /redfish/v1/Chassis/1/Sensors",
                "notes": "传感器列表查询",
            })
        if "SEL" in operation or "日志" in operation or "event" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "GET /redfish/v1/Systems/system/LogServices/LogEntries",
                "notes": "SEL 日志查询",
            })
        if not templates:
            templates.append({
                "interface_type": hint if hint != "any" else "redfish",
                "recommended_command": "（未找到匹配模板）",
                "notes": "RAG 知识库中暂无匹配",
            })

        return json.dumps(
            {"operation_description": operation, "results": templates, "total_matches": len(templates)},
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
                print("[Exec] 成功解析 ExecutionRecord")
                return record
            except (json.JSONDecodeError, ValidationError) as e:
                print(f"[Exec] 直接解析失败: {e}")

            # 第二轮：清理后解析
            cleaned = _sanitize_json_string(json_str)
            if cleaned != json_str:
                try:
                    data = json.loads(cleaned)
                    record = ExecutionRecord.model_validate(data)
                    record = self._force_override_timestamps(record, started_at)
                    print("[Exec] 清理后解析成功")
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
                    print("[Exec] 修复后解析成功")
                    return record

        # 所有解析尝试失败，生成 fallback
        print("[Exec] 未找到有效 JSON，生成 fallback 记录")
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
            print(f"[Exec] 修复后仍无法解析: {e}")
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
            print(f"[Exec] 保存记录失败（不影响返回）: {e}")
