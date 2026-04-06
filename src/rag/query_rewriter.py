# -*- coding: utf-8 -*-
"""
Query Rewriter: LLM 驱动的查询扩展模块

将模糊/场景化查询扩展为多条精准查询，提升检索召回率。

用法:
  import asyncio
  from src.rag.query_rewriter import QueryRewriter

  rw = QueryRewriter(
      model="qwen3.5-plus",
      api_key="sk-xxx",
      base_url="https://your-openai-service/api/v1/",
  )
  results = asyncio.run(rw.rewrite("怎么控制风扇转速"))
  for r in results:
      print(f"  - {r}")

依赖: openai>=1.0.0, httpx
"""

import asyncio
import json
import logging
import re
from typing import List, Optional

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger("rag.query_rewriter")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "qwen3.5-plus"
DEFAULT_TIMEOUT = 120.0

MAX_RETRIES = 2
RETRY_DELAYS = [1, 2]

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
你是 IPMI/BMC/Redfish 命令检索系统的查询扩展引擎。

你的任务：将用户的模糊/场景化查询扩展为 3~5 条精准的检索查询。
扩展查询应覆盖不同角度：英文命令名、中文命令名、功能关键词、Redfish URI 路径、HTTP 方法等。

规则：
1. 必须输出一个 JSON 数组，包含 3~5 个字符串
2. 不要输出任何其他内容（不要解释、不要标注）
3. 每条查询应该短小精悍（2~8 个词）
4. 包含原始查询中未明确写出但相关的精确术语
5. 如果查询涉及 Redfish/REST 操作，扩展结果应包含 URI 路径和 HTTP 方法
6. 如果查询中包含 Redfish URI 路径（如 /redfish/v1/...），必须原样保留该路径
"""

# ---------------------------------------------------------------------------
# Few-shot 示例
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    # --- IPMI 示例 ---
    {
        "input": "怎么控制风扇转速",
        "output": ["Set Fan Speed", "设置风扇转速", "风扇控制命令", "Fan Speed Control", "风扇调速策略"],
    },
    {
        "input": "BMC重启",
        "output": ["Cold Reset", "BMC 冷复位", "Warm Reset", "NetFn 06h CMD 02h", "BMC Reset"],
    },
    {
        "input": "添加一个IPMI用户",
        "output": ["Set User Name", "创建用户", "Set User Password", "Enable User", "Add IPMI User"],
    },
    {
        "input": "查看CPU温度",
        "output": ["Get CPU Reading", "获取CPU读数", "CPU温度", "Sensor Reading", "CPU Temperature"],
    },
    # --- Redfish 场景示例 ---
    {
        "input": "查看服务器电源状态",
        "output": ["PowerState GET", "/redfish/v1/Systems Power", "查询电源状态", "System Power", "电源控制"],
    },
    {
        "input": "重启系统",
        "output": [
            "ComputerSystem.Reset POST",
            "/redfish/v1/Systems/Actions/Reset",
            "GracefulRestart ForceRestart",
            "系统重启 ResetType",
        ],
    },
    {
        "input": "配置网络接口IP地址",
        "output": [
            "EthernetInterface PATCH",
            "/redfish/v1/Managers/EthernetInterfaces",
            "修改网卡IP地址",
            "IPv4Address Static",
            "网络配置 IPAddress",
        ],
    },
    {
        "input": "查看所有传感器读数",
        "output": [
            "Thermal GET /redfish/v1/Chassis/Thermal",
            "传感器 Temperature Fan",
            "Sensor Reading",
            "Chassis Thermal Sensors",
        ],
    },
    {
        "input": "创建一个新用户",
        "output": [
            "AccountService POST",
            "/redfish/v1/AccountService/Accounts",
            "添加用户 Create User",
            "UserName Password RoleId",
        ],
    },
    {
        "input": "查看BMC固件版本",
        "output": [
            "Manager GET FirmwareVersion",
            "/redfish/v1/Managers firmware",
            "查询固件版本",
            "BMC Version UpdateService",
        ],
    },
    # --- Redfish exact_uri 示例 ---
    {
        "input": "/redfish/v1/Systems/{SystemId}",
        "output": [
            "/redfish/v1/Systems GET PATCH",
            "ComputerSystem 查询系统资源",
            "服务器信息 Processor Memory",
            "Systems PowerState Status Boot",
        ],
    },
    {
        "input": "/redfish/v1/Managers/{ManagerId}",
        "output": [
            "/redfish/v1/Managers GET PATCH",
            "Manager 管理控制器 BMC",
            "FirmwareVersion DateTime",
            "管理模块信息查询",
        ],
    },
    {
        "input": "/redfish/v1/Chassis/{ChassisId}",
        "output": [
            "/redfish/v1/Chassis GET",
            "Chassis 机箱信息",
            "Thermal Power Sensors",
            "机箱状态 传感器",
        ],
    },
    # --- Redfish 场景扩展示例 ---
    {
        "input": "重启BMC管理控制器",
        "output": [
            "Manager.Reset POST",
            "/redfish/v1/Managers/Actions/Reset",
            "GracefulRestart ForceRestart",
            "BMC重启 管理控制器复位",
        ],
    },
    {
        "input": "查看服务器的CPU和内存配置",
        "output": [
            "Processor GET /redfish/v1/Systems/Processors",
            "Memory GET /redfish/v1/Systems/Memory",
            "CPU处理器 内存DIMM",
            "处理器型号 核心数 主频",
        ],
    },
    {
        "input": "导出系统日志",
        "output": [
            "LogService GET /redfish/v1/Systems/LogServices",
            "日志服务 DownloadLog",
            "系统日志 SEL 事件日志",
            "LogEntry 事件记录",
        ],
    },
    {
        "input": "修改BMC的IP地址",
        "output": [
            "EthernetInterface PATCH",
            "/redfish/v1/Managers/EthernetInterfaces",
            "IPv4StaticAddresses IPAddress",
            "网络配置 修改IP 静态地址",
        ],
    },
]


def _build_few_shot_messages() -> list:
    """构造 few-shot 消息对。"""
    messages = []
    for ex in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": ex["input"]})
        messages.append({"role": "assistant", "content": json.dumps(ex["output"], ensure_ascii=False)})
    return messages


# ---------------------------------------------------------------------------
# QueryRewriter
# ---------------------------------------------------------------------------
class QueryRewriter:
    """
    LLM 驱动的查询扩展器.

    调用 OpenAI-compatible API，将模糊查询扩展为多条精准查询。
    base_url 和 api_key 由调用方通过 ProviderConfig 传入，不再硬编码。
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        base_url: str = "https://your-openai-service/api/v1/",
        timeout: float = DEFAULT_TIMEOUT,
        verify_ssl: bool = True,
        trust_env: bool = False,
    ):
        if not api_key:
            raise ValueError(
                "QueryRewriter 需要有效的 api_key，"
                "请通过 config.yaml providers 配置或在初始化时传入"
            )

        self.model = model

        self._http_client = httpx.AsyncClient(
            timeout=timeout,
            verify=verify_ssl,
            trust_env=trust_env,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=self._http_client,
        )

        logger.info(
            f"QueryRewriter 初始化: model={model}, base_url={base_url}, "
            f"verify_ssl={verify_ssl}, trust_env={trust_env}"
        )

    async def close(self):
        """释放资源。"""
        await self._client.close()
        await self._http_client.aclose()

    async def rewrite(self, query: str, top_k: int = 5) -> List[str]:
        """
        将模糊查询扩展为多条精准查询.

        Args:
            query: 原始查询文本
            top_k: 最多返回条数

        Returns:
            扩展后的查询列表（至少包含原始查询）
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_build_few_shot_messages())
        messages.append({"role": "user", "content": query})

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=256,
                )
                content = resp.choices[0].message.content.strip()
                return self._parse_response(content, query, top_k)

            except Exception as e:
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(
                        f"Query rewrite 重试 {attempt + 1}/{MAX_RETRIES} "
                        f"(delay={delay}s): {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Query rewrite 全部重试失败: {e}")
                    return [query]

    # ==================================================================
    # 响应解析
    # ==================================================================

    @staticmethod
    def _parse_response(content: str, original_query: str, top_k: int) -> List[str]:
        """
        解析 LLM 响应为查询列表.

        三级 fallback:
          1. JSON 解析
          2. 正则提取 ["...", "..."]
          3. 返回 [原始查询]
        """
        # Level 1: JSON 解析
        try:
            result = json.loads(content)
            if isinstance(result, list):
                queries = [str(item).strip() for item in result if str(item).strip()]
                if queries:
                    # 确保原始查询在列表中
                    if original_query not in queries:
                        queries.insert(0, original_query)
                    return queries[:top_k]
        except json.JSONDecodeError:
            pass

        # Level 2: 正则提取 JSON 数组
        match = re.search(r'\[\s*"[^"]*"(?:\s*,\s*"[^"]*")*\s*\]', content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list) and result:
                    queries = [str(item).strip() for item in result if str(item).strip()]
                    if original_query not in queries:
                        queries.insert(0, original_query)
                    return queries[:top_k]
            except json.JSONDecodeError:
                pass

        # Level 3: Fallback
        logger.warning(f"Query rewrite 解析失败，使用原始查询: {content[:100]}")
        return [original_query]
