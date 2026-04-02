# -*- coding: utf-8 -*-
"""
RAG 索引构建工具

将文档解析、向量化并存入 Chroma 向量数据库。

用法:
  # 使用 config.yaml 中的 embedding 配置
  python -m src.rag.build_index --file_path "C:/Codes/docs_word/iBMC IPMI 接口说明 03.docx"

  # 批量处理目录
  python -m src.rag.build_index --directory "C:/Codes/docs_word/" --collection_name openubmc_rag

  # 强制指定文档类型
  python -m src.rag.build_index --file_path "doc.docx" --doc_type ipmi

依赖:
  - openai>=1.0.0      (AsyncOpenAI, OpenAI-compatible API)
  - chromadb>=0.4.0    (向量数据库)
  - tqdm>=4.60.0       (进度条)
  - python-docx>=0.8   (DOCX 解析, 由 Parser 间接依赖)
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from openai import AsyncOpenAI
from tqdm import tqdm

from src.core.config import load_config
from src.core.client_factory import ClientFactory
from src.rag.parsers import get_parser

logger = logging.getLogger("rag.build_index")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_COLLECTION = "openubmc_rag"
DEFAULT_DIMENSION = 1024
DEFAULT_PERSIST_DIR = "./shared/rag_index"

BATCH_SIZE = 10       # Embedding 每批最大数量
MAX_RETRIES = 3       # API 调用最大重试次数
RETRY_DELAYS = [1, 2, 4]  # 重试间隔 (秒)


# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RAG 索引构建工具 - 将文档解析、向量化并存入 Chroma"
    )
    parser.add_argument(
        "--config_path", type=str, default="config/config.yaml",
        help="配置文件路径 (默认: config/config.yaml)",
    )
    parser.add_argument(
        "--file_path", type=str, default=None,
        help="处理单个文件 (与 --directory 二选一)",
    )
    parser.add_argument(
        "--directory", type=str, default=None,
        help="批量处理目录下的文档文件",
    )
    parser.add_argument(
        "--collection_name", type=str, default=DEFAULT_COLLECTION,
        help=f"Chroma collection 名称 (默认: {DEFAULT_COLLECTION})",
    )
    parser.add_argument(
        "--dimension", type=int, default=DEFAULT_DIMENSION,
        help=f"Embedding 维度 (默认: {DEFAULT_DIMENSION})",
    )
    parser.add_argument(
        "--doc_type", type=str, default=None,
        help="强制指定文档类型 (ipmi / redfish / cli)",
    )
    parser.add_argument(
        "--persist_dir", type=str, default=DEFAULT_PERSIST_DIR,
        help=f"Chroma 持久化目录 (默认: {DEFAULT_PERSIST_DIR})",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 文件解析
# ---------------------------------------------------------------------------
def _resolve_input_files(
    file_path: Optional[str], directory: Optional[str]
) -> List[str]:
    """从 --file_path 或 --directory 解析输入文件列表。"""
    files: List[str] = []

    if file_path:
        p = Path(file_path)
        if not p.exists():
            print(f"[ERROR] 文件不存在: {file_path}")
            sys.exit(1)
        files.append(str(p.resolve()))

    elif directory:
        d = Path(directory)
        if not d.is_dir():
            print(f"[ERROR] 目录不存在: {directory}")
            sys.exit(1)
        supported_ext = [".docx"]  # TODO: 未来新增 ".pdf", ".html"
        for ext in supported_ext:
            files.extend(str(f.resolve()) for f in d.rglob(f"*{ext}"))
    else:
        print("[ERROR] 必须指定 --file_path 或 --directory")
        sys.exit(1)

    return sorted(files)


# ---------------------------------------------------------------------------
# Embedding (带重试 + fallback)
# ---------------------------------------------------------------------------
async def _embed_batch_with_retry(
    client: AsyncOpenAI,
    texts: List[str],
    model: str,
    dimension: int,
) -> Optional[List[List[float]]]:
    """
    批量 Embedding，带重试逻辑。

    Returns:
        向量列表，或在 model not found 时返回 None (触发 fallback)
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.embeddings.create(
                model=model,
                input=texts,
                dimensions=dimension,
            )
            # 按 index 排序确保与输入顺序一致
            sorted_data = sorted(resp.data, key=lambda x: x.index)
            return [item.embedding for item in sorted_data]

        except Exception as e:
            error_str = str(e).lower()

            # 不可重试: 400 参数错误 -> 直接抛出 (如 batch size 超限)
            if "400" in error_str or "invalidparameter" in error_str:
                logger.error(f"参数错误，不可重试: {e}")
                raise

            # 不可重试: 模型不存在 -> 返回 None 触发 fallback
            if "does not exist" in error_str or "not found" in error_str:
                logger.warning(f"模型不可用: {model} ({e})")
                return None

            # 不可重试: 认证失败 -> 直接抛出
            if "auth" in error_str or "401" in error_str or "api key" in error_str:
                raise

            # 可重试: 限流、服务端错误等
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    f"Embedding 重试 {attempt + 1}/{MAX_RETRIES} "
                    f"(model={model}, delay={delay}s): {e}"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"Embedding 失败，已重试 {MAX_RETRIES} 次: {e}")
                raise

    return None


async def _embed_and_store(
    chunks: List[Dict[str, Any]],
    client: AsyncOpenAI,
    collection,
    model_name: str,
    dimension: int,
) -> None:
    """批量 Embedding 分块并存入 Chroma。"""
    # 安全截断线: 中文约 1 字符 ~ 1-2 tokens, 取 6000 字符
    MAX_TEXT_CHARS = 6000

    texts = [c["text"] for c in chunks]
    metadatas = [c["metadata"] for c in chunks]
    ids = [m["chunk_id"] for m in metadatas]

    # 截断超长文本，过滤空文本
    filtered_texts, filtered_ids, filtered_metadatas = [], [], []
    for text, cid, meta in zip(texts, ids, metadatas):
        text = text.strip()
        if not text:
            logger.warning(f"跳过空 chunk: {cid}")
            continue
        if len(text) > MAX_TEXT_CHARS:
            logger.warning(f"截断超长 chunk ({len(text)} -> {MAX_TEXT_CHARS}): {cid}")
            text = text[:MAX_TEXT_CHARS]
        filtered_texts.append(text)
        filtered_ids.append(cid)
        filtered_metadatas.append(meta)
    texts, ids, metadatas = filtered_texts, filtered_ids, filtered_metadatas

    # 分批处理
    batches = [
        texts[i : i + BATCH_SIZE]
        for i in range(0, len(texts), BATCH_SIZE)
    ]

    pbar = tqdm(total=len(texts), desc="Embedding", unit="chunk")

    for batch_idx, batch in enumerate(batches):
        batch_start = batch_idx * BATCH_SIZE
        batch_ids = ids[batch_start : batch_start + len(batch)]
        batch_texts = texts[batch_start : batch_start + len(batch)]
        batch_metas = metadatas[batch_start : batch_start + len(batch)]

        embeddings = await _embed_batch_with_retry(
            client, batch, model_name, dimension
        )

        if embeddings is None:
            raise RuntimeError(
                f"Embedding 模型 {model_name} 失败 "
                f"(batch {batch_idx + 1}/{len(batches)})"
            )

        # 逐批存入 Chroma (增量 upsert，避免全部完成后才存储)
        try:
            collection.upsert(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_texts,
                metadatas=batch_metas,
            )
        except Exception as e:
            logger.error(f"Chroma upsert 失败 (batch {batch_idx + 1}): {e}")
            raise

        pbar.update(len(batch))

    pbar.close()
    logger.info(
        f"已存储 {len(ids)} 个 chunks 到 collection '{collection.name}' "
        f"(model={model_name})"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    """异步主入口。"""
    # 1. 收集输入文件
    files = _resolve_input_files(args.file_path, args.directory)
    if not files:
        print("[ERROR] 未找到可处理的文件")
        return 1

    print(f"[INFO] 待处理文件: {len(files)} 个")
    for f in files:
        print(f"  - {f}")

    # 2. 加载配置 -> 创建 Embedding 客户端
    cfg = load_config(args.config_path)
    factory = ClientFactory(cfg)
    openai_client = factory.create_for("embedding")

    # 通过 get_component_config 苿合并后的完整参数（无需硬编码 dimension)
    comp = get_component_config(cfg, "embedding")
    embed_dimension = comp.dimension or args.dimension
    print(f"[OK] Embedding 客户端已创建")
    print(f"     provider:  {comp.provider_name}")
    print(f"     base_url:  {comp.base_url}")
    print(f"     model:     {comp.model}")
    print(f"     dimension: {embed_dimension}")

    # 3. 初始化 Chroma
    persist_dir = args.persist_dir
    Path(persist_dir).mkdir(parents=True, exist_ok=True)

    chroma_client = chromadb.PersistentClient(path=persist_dir)
    collection = chroma_client.get_or_create_collection(
        name=args.collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"[OK] Chroma collection '{args.collection_name}' 就绪 "
          f"(现有 {collection.count()} 条)")

    # 4. 逐文件处理
    total_chunks = 0
    t_start = time.perf_counter()

    for file_path in files:
        file_name = Path(file_path).name
        print(f"\n{'=' * 60}")
        print(f"[处理] {file_name}")
        print(f"{'=' * 60}")

        try:
            # 解析
            parser = get_parser(file_path, args.doc_type)
            chunks = await parser.parse(file_path)
            print(f"[OK] 解析完成: {len(chunks)} 个 chunks")

            if not chunks:
                print(f"[WARN] 无有效分块，跳过")
                continue

            # Embedding + 存储
            await _embed_and_store(
                chunks, openai_client, collection,
                model_name=comp.model,
                dimension=embed_dimension,
            )
            total_chunks += len(chunks)

        except Exception as e:
            logger.error(f"处理文件失败 [{file_name}]: {e}", exc_info=True)
            print(f"[FAIL] 处理失败: {e}")
            continue

    # 5. 汇总
    elapsed = time.perf_counter() - t_start
    print(f"\n{'=' * 60}")
    print(f"[完成] 索引构建结束")
    print(f"  处理文件数:  {len(files)}")
    print(f"  新增 chunks: {total_chunks}")
    print(f"  DB 总 chunks: {collection.count()}")
    print(f"  Collection:  {args.collection_name}")
    print(f"  持久化目录:  {persist_dir}")
    print(f"  总耗时:      {elapsed:.1f}s")
    print(f"{'=' * 60}")

    await factory.close()
    return 0


def main() -> int:
    """同步入口。"""
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
