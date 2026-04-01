# -*- coding: utf-8 -*-
"""
Query Rewriter: LLM 驱动的查询扩展模块

将模糊/场景化查询扩展为多条精准查询，提升检索召回率。

用法:
  import asyncio
  from src.rag.query_rewriter import QueryRewriter

  rw = QueryRewriter()
  results = asyncio.run(rw.rewrite("怎么控制风扇转速"))
  for r in results:
      print(f"  - {r}")

依赖: openai>=1.0.0, python-dotenv, httpx
"""

import asyncio
import json
import logging
import os
import re
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

logger = logging.getLogger("rag.query_rewriter")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ENV_KEY_NAME = "DASHSCOPE_API_KEY"
REWRITE_MODEL_ENV = "DASHSCOPE_REWRITE_MODEL"
DEFAULT_MODEL = "qwen3.5-plus"

MAX_RETRIES = 2
RETRY_DELAYS = [1, 2]

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
你是 IPMI/BMC 命令检索系统的查询扩展引擎。

你的任务：将用户的模糊/场景化查询扩展为 3~5 条精准的检索查询。
扩展查询应覆盖不同角度：英文命令名、中文命令名、功能关键词、NetFn/CMD 编码等。

规则：
1. 必须输出一个 JSON 数组，包含 3~5 个字符串
2. 不要输出任何其他内容（不要解释、不要标注）
3. 每条查询应该短小精悍（2~8 个词）
4. 包含原始查询中未明确写出但相关的精确术语
"""

# ---------------------------------------------------------------------------
# Few-shot 示例
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
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

    调用 DashScope 兼容 API，将模糊查询扩展为多条精准查询。
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        base_url: str = DASHSCOPE_BASE_URL,
    ):
        load_dotenv()

        # 模型优先级: 参数 > 环境变量 > 默认
        env_model = os.getenv(REWRITE_MODEL_ENV, "").strip()
        self.model = model if model != DEFAULT_MODEL else (env_model or DEFAULT_MODEL)

        # API Key
        resolved_key = api_key or os.getenv(ENV_KEY_NAME, "").strip()
        if not resolved_key:
            raise ValueError(f"API Key 未设置: 请设置 {ENV_KEY_NAME} 环境变量或传入 api_key 参数")

        self._http_client = httpx.AsyncClient(timeout=120.0)
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            http_client=self._http_client,
        )

        logger.info(f"QueryRewriter 初始化: model={self.model}")

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
