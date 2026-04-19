"""
DashScope (阿里云百炼) OpenAI 兼容接口连通性测试脚本

功能:
  1. 列出可用模型 (models.list)
  2. Chat Completions 测试 (Qwen3.5 系列)
  3. Embedding 测试 (text-embedding-v4)

使用方法:
  1. 设置环境变量:
     export DASHSCOPE_API_KEY="sk-xxx"
     或在项目根目录的 .env 文件中添加:
       DASHSCOPE_API_KEY=sk-xxx
  2. 安装依赖:
     pip install openai python-dotenv
  3. 运行:
     python test_dashscope.py

常用模型名称:
  Judge 推荐:   qwen-plus, qwen-turbo, qwen-max, qwen3-235b-a22b
  Embedding 推荐: text-embedding-v3, text-embedding-v4
"""

import asyncio
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ENV_KEY_NAME = "DASHSCOPE_API_KEY"

# 常用模型名称参考 (实际可用以 models.list 输出为准)
# Judge 候选: qwen-plus, qwen-turbo, qwen-max, qwen3-235b-a22b
# Embedding 候选: text-embedding-v3, text-embedding-v4
CHAT_MODEL = "qwen-plus"
EMBEDDING_MODEL = "text-embedding-v3"

# ---------------------------------------------------------------------------
# 打印辅助
# ---------------------------------------------------------------------------
SEP = "=" * 70
SUB_SEP = "-" * 50


def banner(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def info(label: str, value: str | int | float | None) -> None:
    print(f"  {label}: {value}")


# ---------------------------------------------------------------------------
# 初始化客户端
# ---------------------------------------------------------------------------
def create_client() -> AsyncOpenAI:
    load_dotenv()
    api_key = os.getenv(ENV_KEY_NAME, "").strip()
    if not api_key:
        print(f"[FAIL] 环境变量 {ENV_KEY_NAME} 未设置或为空")
        print(f"  请执行: export {ENV_KEY_NAME}='sk-xxx'")
        print(f"  或在 .env 文件中添加: {ENV_KEY_NAME}=sk-xxx")
        sys.exit(1)
    print(f"[OK] API Key 已加载 (长度={len(api_key)}, 前缀={api_key[:8]}...)")
    return AsyncOpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)


# ---------------------------------------------------------------------------
# 测试 1: 列出模型
# ---------------------------------------------------------------------------
async def test_list_models(client: AsyncOpenAI) -> list[str]:
    banner("[测试 1] 列出可用模型 (models.list)")
    try:
        t0 = time.perf_counter()
        resp = await client.models.list()
        elapsed = time.perf_counter() - t0

        models = [m.id for m in resp.data]
        models.sort()

        info("可用模型总数", len(models))
        info("耗时", f"{elapsed:.2f}s")

        # 分类打印
        keywords_judge = ["qwen", "qwq"]
        keywords_embed = ["embed", "text-embedding"]

        judge_models = [m for m in models if any(k in m.lower() for k in keywords_judge)]
        embed_models = [m for m in models if any(k in m.lower() for k in keywords_embed)]

        if judge_models:
            print(f"\n  [Judge 候选模型] ({len(judge_models)} 个):")
            for m in judge_models:
                print(f"    - {m}")

        if embed_models:
            print(f"\n  [Embedding 候选模型] ({len(embed_models)} 个):")
            for m in embed_models:
                print(f"    - {m}")

        other = [m for m in models if m not in judge_models and m not in embed_models]
        if other:
            print(f"\n  [其他模型] ({len(other)} 个):")
            for m in other:
                print(f"    - {m}")

        print(f"\n[OK] 模型列表获取成功")
        return models

    except Exception as e:
        print(f"[FAIL] 获取模型列表失败: {e}")
        return []


# ---------------------------------------------------------------------------
# 测试 2: Chat Completions
# ---------------------------------------------------------------------------
async def test_chat_completion(client: AsyncOpenAI, model: str) -> None:
    banner(f"[测试 2] Chat Completions (model={model})")
    try:
        t0 = time.perf_counter()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "你是谁？请用中文简短回复。"},
            ],
            max_tokens=256,
            temperature=0.7,
        )
        elapsed = time.perf_counter() - t0

        choice = resp.choices[0]
        content = choice.message.content
        finish_reason = choice.finish_reason

        info("模型", resp.model)
        info("回复内容", content)
        info("finish_reason", finish_reason)
        info("耗时", f"{elapsed:.2f}s")

        if resp.usage:
            info("prompt_tokens", resp.usage.prompt_tokens)
            info("completion_tokens", resp.usage.completion_tokens)
            info("total_tokens", resp.usage.total_tokens)

        print(f"\n[OK] Chat Completions 测试通过")

    except Exception as e:
        print(f"[FAIL] Chat Completions 测试失败: {e}")


# ---------------------------------------------------------------------------
# 测试 3: 流式 Chat Completions
# ---------------------------------------------------------------------------
async def test_chat_streaming(client: AsyncOpenAI, model: str) -> None:
    banner(f"[测试 3] 流式 Chat Completions (model={model})")
    try:
        t0 = time.perf_counter()
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "用一句话描述什么是 BMC。"},
            ],
            max_tokens=128,
            temperature=0.7,
            stream=True,
        )

        collected = []
        print("  [流式响应] ", end="", flush=True)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                collected.append(delta.content)
                print(delta.content, end="", flush=True)

        elapsed = time.perf_counter() - t0
        full_text = "".join(collected)
        print(f"\n\n  完整内容: {full_text}")
        info("耗时 (含流式输出)", f"{elapsed:.2f}s")
        info("总字符数", len(full_text))

        print(f"\n[OK] 流式 Chat 测试通过")

    except Exception as e:
        print(f"[FAIL] 流式 Chat 测试失败: {e}")


# ---------------------------------------------------------------------------
# 测试 4: Embedding
# ---------------------------------------------------------------------------
async def test_embedding(client: AsyncOpenAI, model: str) -> None:
    banner(f"[测试 4] Embedding (model={model})")
    try:
        t0 = time.perf_counter()
        resp = await client.embeddings.create(
            model=model,
            input="openUBMC 是基于微组件架构的 BMC 管理软件",
        )
        elapsed = time.perf_counter() - t0

        embedding_data = resp.data[0]
        vector = embedding_data.embedding
        dim = len(vector)

        info("模型", resp.model)
        info("向量维度", dim)
        info("向量前 5 维", str(vector[:5]))
        info("耗时", f"{elapsed:.2f}s")

        if resp.usage:
            info("prompt_tokens", resp.usage.prompt_tokens)
            info("total_tokens", resp.usage.total_tokens)

        print(f"\n[OK] Embedding 测试通过 (维度={dim})")

    except Exception as e:
        print(f"[FAIL] Embedding 测试失败: {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main() -> None:
    print(f"DashScope 连通性测试  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base URL: {DASHSCOPE_BASE_URL}")

    client = create_client()

    # 测试 1: 列出模型
    models = await test_list_models(client)

    # 自动选择可用的模型
    chat_model = CHAT_MODEL
    embed_model = EMBEDDING_MODEL

    if models:
        # 优先匹配可用模型
        qwen_candidates = [m for m in models if "qwen" in m.lower() and "embedding" not in m.lower()]
        embed_candidates = [m for m in models if "embedding" in m.lower() or "embed" in m.lower()]

        if qwen_candidates and chat_model not in models:
            chat_model = qwen_candidates[0]
            print(f"\n  [INFO] {CHAT_MODEL} 不可用, 自动选择: {chat_model}")

        if embed_candidates and embed_model not in models:
            embed_model = embed_candidates[0]
            print(f"  [INFO] {EMBEDDING_MODEL} 不可用, 自动选择: {embed_model}")

    # 测试 2: Chat Completions
    await test_chat_completion(client, chat_model)

    # 测试 3: 流式 Chat
    await test_chat_streaming(client, chat_model)

    # 测试 4: Embedding
    await test_embedding(client, embed_model)

    # 汇总
    banner("[汇总] 测试完成")
    info("Base URL", DASHSCOPE_BASE_URL)
    info("Chat 模型", chat_model)
    info("Embedding 模型", embed_model)
    print(SEP)

    await client.close()


# ---------------------------------------------------------------------------
# 同步版本入口 (如需同步调用, 取消下方注释即可)
# ---------------------------------------------------------------------------
# def run_sync():
#     """同步版本的测试入口"""
#     from openai import OpenAI
#     load_dotenv()
#     api_key = os.getenv(ENV_KEY_NAME, "").strip()
#     if not api_key:
#         print(f"[FAIL] 请设置 {ENV_KEY_NAME}")
#         sys.exit(1)
#     client = OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)
#
#     # Chat 测试
#     resp = client.chat.completions.create(
#         model=CHAT_MODEL,
#         messages=[{"role": "user", "content": "你是谁？请用中文简短回复。"}],
#         max_tokens=256,
#     )
#     print(resp.choices[0].message.content)
#
#     # Embedding 测试
#     resp = client.embeddings.create(
#         model=EMBEDDING_MODEL,
#         input="测试文本",
#     )
#     print(f"向量维度: {len(resp.data[0].embedding)}")


if __name__ == "__main__":
    asyncio.run(main())
