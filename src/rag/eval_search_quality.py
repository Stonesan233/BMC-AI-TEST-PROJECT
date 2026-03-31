# -*- coding: utf-8 -*-
"""
RAG 检索质量评估脚本

从 Chroma 向量数据库中提取所有 IPMI 命令的元数据,
构造多维度查询用例，自动评估检索准确率。

依赖: chromadb, openai, python-dotenv, tqdm
"""

import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI
import chromadb
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ENV_KEY_NAME = "DASHSCOPE_API_KEY"
CHROMA_PATH = "./shared/rag_index"
COLLECTION_NAME = "openubmc_rag"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024


# ---------------------------------------------------------------------------
# 嵌入查询
# ---------------------------------------------------------------------------
async def embed_queries(
    client: AsyncOpenAI,
    queries: List[str],
    batch_size: int = 10,
) -> Dict[str, List[float]]:
    """批量嵌入查询文本"""
    result: Dict[str, List[float]] = {}
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        try:
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
                dimensions=EMBEDDING_DIM,
            )
            for j in range(len(batch)):
                result[queries[i + j]] = resp.data[j].embedding
        except Exception as e:
            print(f"[WARN] Embedding batch {i // batch_size} failed: {e}")
            for q in batch:
                try:
                    resp = await client.embeddings.create(
                        model=EMBEDDING_MODEL,
                        input=[q],
                        dimensions=EMBEDDING_DIM,
                    )
                    result[q] = resp.data[0].embedding
                except Exception:
                    result[q] = None
    return result


# ---------------------------------------------------------------------------
# 评估指标
# ---------------------------------------------------------------------------
def precision_at_k(results: List[Dict], k: int) -> float:
    """计算 Precision@K: top-K 中至少有一个相关结果的比例"""
    hits = 0
    for r in results:
        metas = r["metadatas"][0]
        top_k = metas[:k]
        if any(_is_relevant(r["query"], m) for m in top_k):
            hits += 1
    return hits / len(results) if results else 0.0


def mrr_at_k(results: List[Dict], k: int) -> float:
    """计算 MRR@K (Mean Reciprocal Rank)"""
    total_reciprocal_rank = 0.0
    for r in results:
        metas = r["metadatas"][0]
        for rank in range(1, min(k, len(metas)) + 1):
            if _is_relevant(r["query"], metas[rank - 1]):
                total_reciprocal_rank += 1.0 / rank
                break
    return total_reciprocal_rank / len(results) if results else 0.0


def _is_relevant(query: str, metadata: Dict) -> bool:
    """判断检索结果是否与查询相关"""
    q = query.lower()
    section = metadata.get("section", "").lower()
    description = metadata.get("description", "").lower()
    all_text = f"{section} {description}"

    keywords = query.lower().split()
    matched = sum(1 for kw in keywords if kw in all_text)
    return matched >= len(keywords) * 0.5


def mean_recall_at_k(results: List[Dict], k: int) -> float:
    """计算 Mean Recall@K: top-K 中相关结果占所有相关结果的比例"""
    total_recall = 0.0
    for r in results:
        metas = r["metadatas"][0]
        top_k = metas[:k]
        relevant_count = sum(1 for m in top_k if _is_relevant(r["query"], m))
        total_recall += relevant_count / k
    return total_recall / len(results) if results else 0.0


# ---------------------------------------------------------------------------
# 评估主流程
# ---------------------------------------------------------------------------
async def run_evaluation(output_file: str) -> None:
    load_dotenv()
    api_key = os.getenv(ENV_KEY_NAME, "").strip()
    if not api_key:
        print("[ERROR] DASHSCOPE_API_KEY not set")
        return

    # 连接 Chroma
    chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = chroma_client.get_collection(COLLECTION_NAME)
    total_chunks = collection.count()
    print(f"[OK] Collection: {COLLECTION_NAME}, {total_chunks} chunks")

    # 提取所有 metadata
    all_metas = []
    all_docs = []
    batch = 100
    offset = 0
    while offset < total_chunks:
        result = collection.get(include=["metadatas", "documents"], limit=batch, offset=offset)
        all_metas.extend(result["metadatas"])
        all_docs.extend(result["documents"])
        offset += batch
    print(f"[OK] Loaded {len(all_metas)} metadata entries")

    # 构造查询用例
    test_queries = _build_test_queries()
    print(f"[OK] Built {len(test_queries)} test queries")

    # 嵌入查询
    openai_client = AsyncOpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)
    print("[INFO] Embedding queries...")
    query_texts = [q["query"] for q in test_queries]
    query_embeddings = await embed_queries(openai_client, query_texts)

    # 执行检索
    print("[INFO] Running searches...")
    all_results = []
    for tq in tqdm(test_queries, desc="Searching"):
        q = tq["query"]
        emb = query_embeddings.get(q)
        if emb is None:
            continue
        results = collection.query(
            query_embeddings=[emb],
            n_results=5,
            where={"chunk_type": "command"},
        )
        results["query"] = q
        results["category"] = tq["category"]
        results["expected_keywords"] = tq["expected_keywords"]
        all_results.append(results)

    # 计算指标
    print("\n[INFO] Computing metrics...")
    p1 = precision_at_k(all_results, 1)
    p3 = precision_at_k(all_results, 3)
    mrr1 = mrr_at_k(all_results, 1)
    mrr3 = mrr_at_k(all_results, 3)
    recall1 = mean_recall_at_k(all_results, 1)
    recall3 = mean_recall_at_k(all_results, 3)

    # 按类别统计
    by_category: Dict[str, List] = defaultdict(list)
    for r in all_results:
        by_category[r["category"]].append(r)

    report: Dict[str, Any] = {}
    for cat in sorted(by_category.keys()):
        cat_results = by_category[cat]
        report[cat] = {
            "count": len(cat_results),
            "P@1": round(precision_at_k(cat_results, 1), 4),
            "P@3": round(precision_at_k(cat_results, 3), 4),
            "MRR@1": round(mrr_at_k(cat_results, 1), 4),
            "MRR@3": round(mrr_at_k(cat_results, 3), 4),
        }

    # 输出结果
    print(f"\n{'=' * 60}")
    print("RAG RETRIEVAL QUALITY EVALUATION REPORT")
    print(f"{'=' * 60}")
    print(f"  Total chunks in DB:  {total_chunks}")
    print(f"  Total queries:    {len(all_results)}")
    print(f"  P@1:              {p1:.1%}")
    print(f"  P@3:              {p3:.1%}")
    print(f"  MRR@1:             {mrr1:.4f}")
    print(f"  MRR@3:             {mrr3:.4f}")
    print(f"  Recall@1:          {recall1:.2f}")
    print(f"  Recall@3:          {recall3:.2f}")
    print(f"\n  By Category:")
    for cat in sorted(report.keys()):
        r = report[cat]
        print(f"    {cat:20s}  n={r['count']:2d}  P@1={r['P@1']:.1%}  P@3={r['P@3']:.1%}  MRR@1={r['MRR@1']:.4f}")

    print(f"\n  Top-3 Examples:")
    for r in all_results[:5]:
        print(f"    [{r['category']:20s}] Q=\"{r['query']}\"")
        for i in range(min(3, len(r["metadatas"][0]))):
            m = r["metadatas"][0][i]
            d = r["distances"][0][i]
            print(f"      {i+1}. {m.get('section', '')[:60]}  dist={d:.4f}")

    # 保存 JSON 报告
    if output_file:
        output_data = {
            "total_chunks": total_chunks,
            "total_queries": len(all_results),
            "P@1": round(p1, 4),
            "P@3": round(p3, 4),
            "MRR@1": round(mrr1, 4),
            "MRR@3": round(mrr3, 4),
            "Recall@1": round(recall1, 4),
            "Recall@3": round(recall3, 4),
            "by_category": report,
            "detailed_results": [
                {
                    "query": r["query"],
                    "category": r["category"],
                    "top1_section": r["metadatas"][0][0].get("section", "")[:100],
                    "top1_distance": r["distances"][0][0],
                }
                for r in all_results
            ],
        }
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] Report saved to: {output_file}")

    await openai_client.close()


# ---------------------------------------------------------------------------
# 构造查询用例
# ---------------------------------------------------------------------------
def _build_test_queries() -> List[Dict]:
    """构造多维度测试查询"""
    queries = []

    # Category 1: 精确命令名 (英文)
    exact_en = [
        {"query": "Get CPU Reading", "expected_keywords": ["cpu", "reading"], "category": "exact_en"},
        {"query": "Set Fan Speed", "expected_keywords": ["fan", "speed"], "category": "exact_en"},
        {"query": "Chassis Control", "expected_keywords": ["chassis", "control"], "category": "exact_en"},
        {"query": "Get Device ID", "expected_keywords": ["device", "id"], "category": "exact_en"},
        {"query": "Get SEL Time", "expected_keywords": ["sel", "time"], "category": "exact_en"},
        {"query": "Get BMC Info", "expected_keywords": ["bmc"], "category": "exact_en"},
        {"query": "Set BIOS Version", "expected_keywords": ["bios"], "category": "exact_en"},
        {"query": "Get Rack Info", "expected_keywords": ["rack"], "category": "exact_en"},
        {"query": "Get Power Reading", "expected_keywords": ["power"], "category": "exact_en"},
    ]
    queries.extend(exact_en)

    # Category 2: 精确命令名 (中文)
    exact_cn = [
        {"query": "机箱控制", "expected_keywords": ["机箱", "控制"], "category": "exact_cn"},
        {"query": "获取设备ID", "expected_keywords": ["设备"], "category": "exact_cn"},
        {"query": "获取SEL时间", "expected_keywords": ["sel"], "category": "exact_cn"},
        {"query": "查询黑名单", "expected_keywords": ["黑名单"], "category": "exact_cn"},
        {"query": "获取BMC信息", "expected_keywords": ["bmc"], "category": "exact_cn"},
        {"query": "设置BIOS版本", "expected_keywords": ["bios"], "category": "exact_cn"},
        {"query": "获取机柜信息", "expected_keywords": ["机柜"], "category": "exact_cn"},
        {"query": "获取电源功率", "expected_keywords": ["电源", "功率"], "category": "exact_cn"},
    ]
    queries.extend(exact_cn)

    # Category 3: 模糊功能描述
    fuzzy = [
        {"query": "怎么查看CPU温度", "expected_keywords": ["cpu", "温度"], "category": "fuzzy"},
        {"query": "如何控制服务器上下电", "expected_keywords": ["电源", "控制"], "category": "fuzzy"},
        {"query": "配置告警通知", "expected_keywords": ["告警"], "category": "fuzzy"},
        {"query": "查看系统日志", "expected_keywords": ["日志", "sel"], "category": "fuzzy"},
        {"query": "管理用户账号", "expected_keywords": ["用户"], "category": "fuzzy"},
        {"query": "配置网络IP地址", "expected_keywords": ["网络", "ip"], "category": "fuzzy"},
        {"query": "风扇调速策略", "expected_keywords": ["风扇", "调速"], "category": "fuzzy"},
        {"query": "查看传感器状态", "expected_keywords": ["传感器"], "category": "fuzzy"},
        {"query": "固件升级", "expected_keywords": ["固件", "升级"], "category": "fuzzy"},
        {"query": "BMC重启", "expected_keywords": ["bmc", "重启"], "category": "fuzzy"},
    ]
    queries.extend(fuzzy)

    # Category 4: NetFn+CMD 查询
    netfn_cmd = [
        {"query": "NetFn 30h CMD 92h", "expected_keywords": ["30h", "92h"], "category": "netfn_cmd"},
        {"query": "NetFn 0Ch CMD 02h", "expected_keywords": ["0ch", "02h"], "category": "netfn_cmd"},
        {"query": "NetFn 2Ch CMD 10h", "expected_keywords": ["2ch", "10h"], "category": "netfn_cmd"},
        {"query": "NetFn 06 CMD 01", "expected_keywords": ["06", "01"], "category": "netfn_cmd"},
        {"query": "NetFn 32h CMD 39h", "expected_keywords": ["32h", "39h"], "category": "netfn_cmd"},
    ]
    queries.extend(netfn_cmd)

    # Category 5: 场景化查询
    scenario = [
        {"query": "添加一个新的IPMI用户", "expected_keywords": ["用户", "添加"], "category": "scenario"},
        {"query": "查看所有传感器读数", "expected_keywords": ["传感器"], "category": "scenario"},
        {"query": "远程开关机", "expected_keywords": ["电源", "开机", "关机"], "category": "scenario"},
        {"query": "设置SNMP告警上报", "expected_keywords": ["snmp", "告警"], "category": "scenario"},
        {"query": "读取电子标签信息", "expected_keywords": ["电子标签"], "category": "scenario"},
        {"query": "配置VNC远程控制", "expected_keywords": ["vnc"], "category": "scenario"},
        {"query": "清除系统事件日志SEL", "expected_keywords": ["sel", "清除"], "category": "scenario"},
        {"query": "设置MAC地址", "expected_keywords": ["mac"], "category": "scenario"},
    ]
    queries.extend(scenario)

    return queries


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="RAG retrieval quality evaluation")
    parser.add_argument("--output", type=str, default="./shared/rag_eval_report.json",
                        help="Output report file path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(run_evaluation(args.output))


if __name__ == "__main__":
    main()
