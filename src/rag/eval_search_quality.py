# -*- coding: utf-8 -*-
"""
RAG 检索质量评估脚本 (v2 -- Hybrid Search)

对比 4 种检索模式:
  - vector:     纯向量 (alpha=1.0)
  - hybrid_a07: 混合 70/30 (alpha=0.7)
  - hybrid_a05: 混合 50/50 (alpha=0.5)
  - keyword:    纯 BM25 (alpha=0.0)

评估维度:
  - exact_en:   英文精确命令名
  - exact_cn:   中文精确命令名
  - fuzzy:      模糊功能描述
  - netfn_cmd:  NetFn+CMD 查询
  - scenario:   场景化查询

用法:
  python -m src.rag.eval_search_quality --output ./shared/rag_eval_report_v2.json

依赖: chromadb, openai, python-dotenv, tqdm, httpx, jieba
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from tqdm import tqdm

from src.rag.retriever import HybridRetriever

logger = logging.getLogger("rag.eval")

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
# Embedding (带重试 + 回退)
# ---------------------------------------------------------------------------
async def _embed_single(
    client: AsyncOpenAI,
    text: str,
    max_retries: int = 3,
) -> Optional[List[float]]:
    """嵌入单条文本, 带指数退避重试."""
    for attempt in range(max_retries):
        try:
            # 不传 dimensions，使用模型原生维度（避免 vLLM-ascend bug）
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[text],
            )
            return resp.data[0].embedding
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                logger.warning(f"Embedding 重试 {attempt + 1}/{max_retries} ({delay}s): {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Embedding 失败: {text[:50]}... ({e})")
                return None


async def embed_queries(
    client: AsyncOpenAI,
    queries: List[str],
    batch_size: int = 8,
) -> Dict[str, List[float]]:
    """
    批量嵌入查询, 失败时逐条重试.

    batch_size 降到 8 避免超时.
    """
    result: Dict[str, Optional[List[float]]] = {}

    # 批量嵌入
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        try:
            # 不传 dimensions，使用模型原生维度（避免 vLLM-ascend bug）
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
            )
            for j in range(len(batch)):
                result[queries[i + j]] = resp.data[j].embedding
        except Exception as e:
            logger.warning(f"Batch {i // batch_size} 失败: {e}, 逐条重试")
            for q in batch:
                if q not in result:
                    result[q] = await _embed_single(client, q)

    # 二次重试失败项
    failed = [q for q in queries if result.get(q) is None]
    if failed:
        logger.info(f"二次重试 {len(failed)} 条失败 embedding...")
        for q in failed:
            result[q] = await _embed_single(client, q)

    ok = {q: emb for q, emb in result.items() if emb is not None}
    logger.info(f"Embedding 完成: {len(ok)}/{len(queries)}")
    return ok


# ---------------------------------------------------------------------------
# 相关性判断
# ---------------------------------------------------------------------------
def _is_relevant(expected_keywords: List[str], metadata: Dict) -> bool:
    """
    基于预期关键词判断检索结果是否相关.

    检查 section, description, chinese_name, english_name, full_command,
    netfn, cmd 等字段.
    """
    fields = [
        metadata.get("section", ""),
        metadata.get("description", ""),
        metadata.get("chinese_name", ""),
        metadata.get("english_name", ""),
        metadata.get("full_command", ""),
        metadata.get("netfn", ""),
        metadata.get("cmd", ""),
    ]
    all_text = " ".join(f.lower() for f in fields if f)

    matched = sum(1 for kw in expected_keywords if kw.lower() in all_text)
    return matched >= max(len(expected_keywords) * 0.5, 1)


# ---------------------------------------------------------------------------
# 评估指标
# ---------------------------------------------------------------------------
def precision_at_k(results: List[Dict], k: int) -> float:
    """P@K: top-K 中至少有一个相关结果的比例."""
    hits = sum(
        1 for r in results
        if any(_is_relevant(r["expected_keywords"], m) for m in r["top_metas"][:k])
    )
    return hits / len(results) if results else 0.0


def mrr_at_k(results: List[Dict], k: int) -> float:
    """MRR@K: Mean Reciprocal Rank."""
    total = 0.0
    for r in results:
        for rank in range(1, min(k, len(r["top_metas"])) + 1):
            if _is_relevant(r["expected_keywords"], r["top_metas"][rank - 1]):
                total += 1.0 / rank
                break
    return total / len(results) if results else 0.0


# ---------------------------------------------------------------------------
# 评估主流程
# ---------------------------------------------------------------------------
async def run_evaluation(output_file: str) -> None:
    load_dotenv()
    api_key = os.getenv(ENV_KEY_NAME, "").strip()
    if not api_key:
        print("[ERROR] DASHSCOPE_API_KEY not set")
        return

    # 初始化 HybridRetriever
    retriever = HybridRetriever(
        chroma_path=CHROMA_PATH,
        collection_name=COLLECTION_NAME,
    )

    # 初始化 Embedding 客户端 (120s 超时, 匹配 build_index.py)
    http_client = httpx.AsyncClient(timeout=120.0)
    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url=DASHSCOPE_BASE_URL,
        http_client=http_client,
    )

    # 构造测试查询
    test_queries = _build_test_queries()
    print(f"[OK] {len(test_queries)} test queries")

    # 嵌入查询
    print("[INFO] Embedding queries...")
    query_texts = [q["query"] for q in test_queries]
    query_embeddings = await embed_queries(openai_client, query_texts)
    print(f"[OK] {len(query_embeddings)}/{len(query_texts)} queries embedded")

    # ------------------------------------------------------------------
    # 多模式检索
    # ------------------------------------------------------------------
    modes = {
        "vector":     1.0,   # 纯向量
        "hybrid_a07": 0.7,   # 混合 70/30
        "hybrid_a05": 0.5,   # 混合 50/50
        "keyword":    0.0,   # 纯 BM25
    }

    all_mode_results: Dict[str, List[Dict]] = {m: [] for m in modes}

    for tq in tqdm(test_queries, desc="Searching"):
        q = tq["query"]
        emb = query_embeddings.get(q)
        eks = tq["expected_keywords"]

        for mode_name, alpha in modes.items():
            results = retriever.search(
                query=q,
                query_embedding=emb,
                top_k=5,
                alpha=alpha,
                chunk_type="command",
            )
            top_metas = [r["metadata"] for r in results]
            all_mode_results[mode_name].append({
                "query": q,
                "category": tq["category"],
                "expected_keywords": eks,
                "top_metas": top_metas,
                "top_sections": [
                    m.get("section", "")[:80] for m in top_metas[:3]
                ],
            })

    # ------------------------------------------------------------------
    # 计算指标
    # ------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print("RAG RETRIEVAL QUALITY EVALUATION (Hybrid Search)")
    print(f"{'=' * 70}")
    print(f"  Total queries: {len(test_queries)}")
    print(f"  Embedded:      {len(query_embeddings)}")

    report_modes: Dict[str, Any] = {}

    for mode_name in modes:
        results = all_mode_results[mode_name]

        p1 = precision_at_k(results, 1)
        p3 = precision_at_k(results, 3)
        m1 = mrr_at_k(results, 1)
        m3 = mrr_at_k(results, 3)

        # 按类别统计
        by_cat: Dict[str, List] = defaultdict(list)
        for r in results:
            by_cat[r["category"]].append(r)

        cat_report: Dict[str, Any] = {}
        for cat in sorted(by_cat.keys()):
            cr = by_cat[cat]
            cat_report[cat] = {
                "count": len(cr),
                "P@1": round(precision_at_k(cr, 1), 4),
                "P@3": round(precision_at_k(cr, 3), 4),
                "MRR@1": round(mrr_at_k(cr, 1), 4),
                "MRR@3": round(mrr_at_k(cr, 3), 4),
            }

        report_modes[mode_name] = {
            "total": len(results),
            "P@1": round(p1, 4),
            "P@3": round(p3, 4),
            "MRR@1": round(m1, 4),
            "MRR@3": round(m3, 4),
            "by_category": cat_report,
        }

        print(f"\n  [{mode_name:15s}] P@1={p1:.1%}  P@3={p3:.1%}  "
              f"MRR@1={m1:.4f}  MRR@3={m3:.4f}")
        for cat in sorted(cat_report.keys()):
            cr = cat_report[cat]
            print(f"    {cat:20s}  n={cr['count']:2d}  "
                  f"P@1={cr['P@1']:.1%}  P@3={cr['P@3']:.1%}  "
                  f"MRR@1={cr['MRR@1']:.4f}")

    # Top-3 示例 (hybrid_a07)
    print(f"\n  Top-3 Examples (hybrid_a07):")
    for r in all_mode_results["hybrid_a07"][:8]:
        print(f"    [{r['category']:20s}] Q=\"{r['query']}\"")
        for i, sec in enumerate(r["top_sections"]):
            print(f"      {i + 1}. {sec}")

    # ------------------------------------------------------------------
    # 保存报告
    # ------------------------------------------------------------------
    if output_file:
        output_data = {
            "modes": report_modes,
            "test_queries": [
                {
                    "query": q["query"],
                    "category": q["category"],
                    "expected_keywords": q["expected_keywords"],
                }
                for q in test_queries
            ],
        }
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] Report saved: {output_file}")

    await openai_client.close()
    await http_client.aclose()


# ---------------------------------------------------------------------------
# 构造查询用例
# ---------------------------------------------------------------------------
def _build_test_queries() -> List[Dict]:
    """构造多维度测试查询 (5 类 40 条)."""
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
        {"query": "Get Power Reading", "expected_keywords": ["power", "reading"], "category": "exact_en"},
    ]
    queries.extend(exact_en)

    # Category 2: 精确命令名 (中文)
    exact_cn = [
        {"query": "机箱控制", "expected_keywords": ["机箱", "控制"], "category": "exact_cn"},
        {"query": "获取设备ID", "expected_keywords": ["设备", "id"], "category": "exact_cn"},
        {"query": "获取SEL时间", "expected_keywords": ["sel", "时间"], "category": "exact_cn"},
        {"query": "查询黑名单", "expected_keywords": ["黑名单"], "category": "exact_cn"},
        {"query": "获取BMC信息", "expected_keywords": ["bmc"], "category": "exact_cn"},
        {"query": "设置BIOS版本", "expected_keywords": ["bios", "版本"], "category": "exact_cn"},
        {"query": "获取机柜信息", "expected_keywords": ["机柜", "机柜信息"], "category": "exact_cn"},
        {"query": "获取电源功率", "expected_keywords": ["电源", "功率"], "category": "exact_cn"},
    ]
    queries.extend(exact_cn)

    # Category 3: 模糊功能描述
    fuzzy = [
        {"query": "怎么查看CPU温度", "expected_keywords": ["cpu"], "category": "fuzzy"},
        {"query": "如何控制服务器上下电", "expected_keywords": ["电源", "控制", "chassis"], "category": "fuzzy"},
        {"query": "配置告警通知", "expected_keywords": ["告警", "alert"], "category": "fuzzy"},
        {"query": "查看系统日志", "expected_keywords": ["日志", "sel"], "category": "fuzzy"},
        {"query": "管理用户账号", "expected_keywords": ["用户", "user"], "category": "fuzzy"},
        {"query": "配置网络IP地址", "expected_keywords": ["网络", "ip", "lan"], "category": "fuzzy"},
        {"query": "风扇调速策略", "expected_keywords": ["风扇", "fan"], "category": "fuzzy"},
        {"query": "查看传感器状态", "expected_keywords": ["传感器", "sensor"], "category": "fuzzy"},
        {"query": "固件升级", "expected_keywords": ["固件", "firmware"], "category": "fuzzy"},
        {"query": "BMC重启", "expected_keywords": ["bmc", "重启", "reset"], "category": "fuzzy"},
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
        {"query": "添加一个新的IPMI用户", "expected_keywords": ["用户", "user", "add"], "category": "scenario"},
        {"query": "查看所有传感器读数", "expected_keywords": ["传感器", "sensor"], "category": "scenario"},
        {"query": "远程开关机", "expected_keywords": ["电源", "power", "chassis"], "category": "scenario"},
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
    parser.add_argument(
        "--output", type=str, default="./shared/rag_eval_report_v2.json",
        help="Output report file path",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(run_evaluation(args.output))


if __name__ == "__main__":
    main()
