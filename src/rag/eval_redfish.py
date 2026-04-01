# -*- coding: utf-8 -*-
"""
Redfish RAG 检索质量评估脚本

基于现有 eval_search_quality.py 架构，专门评估 Redfish API 文档的检索准确率。

评估维度 (5 类 40 条):
  - exact_uri:    精确 URI 路径查询
  - exact_name:   精确中英文 API 名称
  - fuzzy:        模糊功能描述 (模糊测试用例风格)
  - http_method:  HTTP 方法 + 资源类型查询
  - scenario:     场景化查询 (最贴近实际使用)

检索模式 (4 种):
  - vector:     纯向量 (alpha=1.0)
  - hybrid_a07: 混合 70/30 (alpha=0.7)
  - hybrid_a05: 混合 50/50 (alpha=0.5)
  - keyword:    纯 BM25 (alpha=0.0)

用法:
  python -m src.rag.eval_redfish --output ./shared/rag_eval_redfish.json

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

logger = logging.getLogger("rag.eval_redfish")

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
# Embedding (复用自 eval_search_quality)
# ---------------------------------------------------------------------------
async def _embed_single(
    client: AsyncOpenAI,
    text: str,
    max_retries: int = 3,
) -> Optional[List[float]]:
    for attempt in range(max_retries):
        try:
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[text],
                dimensions=EMBEDDING_DIM,
            )
            return resp.data[0].embedding
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                logger.warning(f"Embedding retry {attempt + 1}/{max_retries} ({delay}s): {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Embedding failed: {text[:50]}... ({e})")
                return None


async def embed_queries(
    client: AsyncOpenAI,
    queries: List[str],
    batch_size: int = 8,
) -> Dict[str, List[float]]:
    result: Dict[str, Optional[List[float]]] = {}
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
            logger.warning(f"Batch {i // batch_size} failed: {e}, retrying individually")
            for q in batch:
                if q not in result:
                    result[q] = await _embed_single(client, q)

    failed = [q for q in queries if result.get(q) is None]
    if failed:
        logger.info(f"Retrying {len(failed)} failed embeddings...")
        for q in failed:
            result[q] = await _embed_single(client, q)

    ok = {q: emb for q, emb in result.items() if emb is not None}
    logger.info(f"Embedding done: {len(ok)}/{len(queries)}")
    return ok


# ---------------------------------------------------------------------------
# 相关性判断 (Redfish 专用)
# ---------------------------------------------------------------------------
def _is_relevant(expected_keywords: List[str], metadata: Dict) -> bool:
    """
    判断检索结果是否与 Redfish 查询相关。

    检查字段: section, description, chinese_name, english_name, full_title,
              resource_uri, http_method, schema_name
    注意: doc_type 过滤已在检索层完成, 此处不再检查
    """
    fields = [
        metadata.get("section", ""),
        metadata.get("description", ""),
        metadata.get("chinese_name", ""),
        metadata.get("english_name", ""),
        metadata.get("full_title", ""),
        metadata.get("resource_uri", ""),
        metadata.get("http_method", ""),
        metadata.get("schema_name", ""),
    ]
    all_text = " ".join(f.lower() for f in fields if f)

    matched = sum(1 for kw in expected_keywords if kw.lower() in all_text)
    return matched >= max(len(expected_keywords) * 0.5, 1)


# ---------------------------------------------------------------------------
# 评估指标
# ---------------------------------------------------------------------------
def precision_at_k(results: List[Dict], k: int) -> float:
    hits = sum(
        1 for r in results
        if any(_is_relevant(r["expected_keywords"], m) for m in r["top_metas"][:k])
    )
    return hits / len(results) if results else 0.0


def mrr_at_k(results: List[Dict], k: int) -> float:
    total = 0.0
    for r in results:
        for rank in range(1, min(k, len(r["top_metas"])) + 1):
            if _is_relevant(r["expected_keywords"], r["top_metas"][rank - 1]):
                total += 1.0 / rank
                break
    return total / len(results) if results else 0.0


# ---------------------------------------------------------------------------
# 构造 Redfish 测试查询
# ---------------------------------------------------------------------------
def _build_test_queries() -> List[Dict]:
    """构造 Redfish API 检索测试查询 (5 类 40 条)."""
    queries = []

    # Category 1: 精确 URI 路径查询
    exact_uri = [
        {"query": "/redfish/v1/Systems/system_id", "expected_keywords": ["/redfish/v1/systems"], "category": "exact_uri"},
        {"query": "/redfish/v1/Managers/1", "expected_keywords": ["/redfish/v1/managers"], "category": "exact_uri"},
        {"query": "/redfish/v1/Chassis/1", "expected_keywords": ["/redfish/v1/chassis"], "category": "exact_uri"},
        {"query": "/redfish/v1/SessionService/Sessions", "expected_keywords": ["sessionservice", "sessions"], "category": "exact_uri"},
        {"query": "/redfish/v1/AccountService/Accounts", "expected_keywords": ["accountservice", "accounts"], "category": "exact_uri"},
        {"query": "/redfish/v1/EventService", "expected_keywords": ["eventservice"], "category": "exact_uri"},
        {"query": "/redfish/v1/UpdateService", "expected_keywords": ["updateservice"], "category": "exact_uri"},
        {"query": "/redfish/v1/TaskService/Tasks", "expected_keywords": ["taskservice", "tasks"], "category": "exact_uri"},
    ]
    queries.extend(exact_uri)

    # Category 2: 精确中英文 API 名称
    exact_name = [
        {"query": "查询系统信息 ComputerSystem", "expected_keywords": ["system", "系统"], "category": "exact_name"},
        {"query": "电源控制 Power Control", "expected_keywords": ["power", "电源", "控制"], "category": "exact_name"},
        {"query": "用户管理 AccountService", "expected_keywords": ["accountservice", "用户"], "category": "exact_name"},
        {"query": "会话管理 SessionService", "expected_keywords": ["session", "会话"], "category": "exact_name"},
        {"query": "查询Manager信息", "expected_keywords": ["manager"], "category": "exact_name"},
        {"query": "查询Chassis传感器", "expected_keywords": ["chassis", "传感器"], "category": "exact_name"},
        {"query": "事件订阅 EventService", "expected_keywords": ["event", "事件"], "category": "exact_name"},
        {"query": "固件更新 UpdateService", "expected_keywords": ["update", "固件", "更新"], "category": "exact_name"},
    ]
    queries.extend(exact_name)

    # Category 3: 模糊功能描述 (模糊测试用例风格)
    fuzzy = [
        {"query": "怎么查看服务器电源状态", "expected_keywords": ["power", "电源", "状态"], "category": "fuzzy"},
        {"query": "如何重启BMC", "expected_keywords": ["reset", "重启", "manager"], "category": "fuzzy"},
        {"query": "查一下系统序列号和型号", "expected_keywords": ["serial", "model", "系统", "型号"], "category": "fuzzy"},
        {"query": "创建一个新用户", "expected_keywords": ["account", "用户", "创建"], "category": "fuzzy"},
        {"query": "修改网络配置", "expected_keywords": ["network", "网络", "ethernet"], "category": "fuzzy"},
        {"query": "查看当前登录的会话", "expected_keywords": ["session", "会话", "登录"], "category": "fuzzy"},
        {"query": "查看风扇转速", "expected_keywords": ["fan", "风扇"], "category": "fuzzy"},
        {"query": "配置SNMP告警", "expected_keywords": ["snmp", "告警"], "category": "fuzzy"},
        {"query": "升级BMC固件", "expected_keywords": ["firmware", "固件", "update"], "category": "fuzzy"},
        {"query": "查看温度传感器数据", "expected_keywords": ["temperature", "温度", "sensor"], "category": "fuzzy"},
    ]
    queries.extend(fuzzy)

    # Category 4: HTTP 方法 + 资源类型
    http_method = [
        {"query": "GET 服务器系统资源信息", "expected_keywords": ["systems", "get", "系统"], "category": "http_method"},
        {"query": "PATCH 修改Manager属性", "expected_keywords": ["managers", "patch"], "category": "http_method"},
        {"query": "POST 创建用户会话", "expected_keywords": ["session", "post", "创建"], "category": "http_method"},
        {"query": "DELETE 删除用户账号", "expected_keywords": ["account", "delete", "删除"], "category": "http_method"},
        {"query": "POST 系统重启操作", "expected_keywords": ["reset", "post", "重启"], "category": "http_method"},
        {"query": "GET 查询日志服务", "expected_keywords": ["logservice", "get", "日志"], "category": "http_method"},
        {"query": "PATCH 修改网卡配置", "expected_keywords": ["ethernet", "patch", "网络"], "category": "http_method"},
    ]
    queries.extend(http_method)

    # Category 5: 场景化查询 (最贴近实际使用)
    scenario = [
        {"query": "我想通过Redfish接口远程给服务器开机", "expected_keywords": ["power", "on", "开机", "控制"], "category": "scenario"},
        {"query": "如何使用Redfish添加一个管理员账户", "expected_keywords": ["account", "用户", "add", "管理员"], "category": "scenario"},
        {"query": "查询服务器的CPU和内存信息", "expected_keywords": ["processor", "memory", "cpu", "处理器"], "category": "scenario"},
        {"query": "查看BMC的固件版本号", "expected_keywords": ["firmware", "version", "版本", "manager"], "category": "scenario"},
        {"query": "配置BMC的IP地址", "expected_keywords": ["ip", "address", "ethernet", "网络"], "category": "scenario"},
        {"query": "导出系统日志用于排查问题", "expected_keywords": ["log", "日志", "导出"], "category": "scenario"},
        {"query": "强制重启卡死的系统", "expected_keywords": ["force", "restart", "重启", "reset"], "category": "scenario"},
    ]
    queries.extend(scenario)

    return queries


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

    http_client = httpx.AsyncClient(timeout=120.0)
    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url=DASHSCOPE_BASE_URL,
        http_client=http_client,
    )

    # 构造测试查询
    test_queries = _build_test_queries()
    print(f"[OK] {len(test_queries)} Redfish test queries")

    # 嵌入查询
    print("[INFO] Embedding queries...")
    query_texts = [q["query"] for q in test_queries]
    query_embeddings = await embed_queries(openai_client, query_texts)
    print(f"[OK] {len(query_embeddings)}/{len(query_texts)} queries embedded")

    # ------------------------------------------------------------------
    # 多模式检索 (doc_type=redfish 过滤, 解决 IPMI 抢占问题)
    # ------------------------------------------------------------------
    modes = {
        "vector":     1.0,
        "hybrid_a07": 0.7,
        "hybrid_a05": 0.5,
        "keyword":    0.0,
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
                doc_type="redfish",
                chunk_type="resource",
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
    print("REDFISH RAG RETRIEVAL QUALITY EVALUATION")
    print(f"{'=' * 70}")
    print(f"  Total queries: {len(test_queries)}")
    print(f"  Embedded:      {len(query_embeddings)}")
    print(f"  DB total:      IPMI 847 + Redfish 585 = 1432")

    report_modes: Dict[str, Any] = {}

    for mode_name in modes:
        results = all_mode_results[mode_name]

        p1 = precision_at_k(results, 1)
        p3 = precision_at_k(results, 3)
        p5 = precision_at_k(results, 5)
        m1 = mrr_at_k(results, 1)
        m3 = mrr_at_k(results, 3)

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
                "P@5": round(precision_at_k(cr, 5), 4),
                "MRR@1": round(mrr_at_k(cr, 1), 4),
                "MRR@3": round(mrr_at_k(cr, 3), 4),
            }

        report_modes[mode_name] = {
            "total": len(results),
            "P@1": round(p1, 4),
            "P@3": round(p3, 4),
            "P@5": round(p5, 4),
            "MRR@1": round(m1, 4),
            "MRR@3": round(m3, 4),
            "by_category": cat_report,
        }

        print(f"\n  [{mode_name:15s}] P@1={p1:.1%}  P@3={p3:.1%}  P@5={p5:.1%}  "
              f"MRR@1={m1:.4f}  MRR@3={m3:.4f}")
        for cat in sorted(cat_report.keys()):
            cr = cat_report[cat]
            print(f"    {cat:20s}  n={cr['count']:2d}  "
                  f"P@1={cr['P@1']:.1%}  P@3={cr['P@3']:.1%}  "
                  f"MRR@1={cr['MRR@1']:.4f}")

    # Top-5 示例 (best mode)
    best_mode = max(report_modes, key=lambda m: report_modes[m]["P@1"])
    print(f"\n  Best mode: {best_mode} (P@1={report_modes[best_mode]['P@1']:.1%})")
    print(f"\n  Top-5 Examples ({best_mode}):")
    for r in all_mode_results[best_mode][:10]:
        hit = any(
            _is_relevant(r["expected_keywords"], m)
            for m in r["top_metas"][:3]
        )
        mark = "[OK]" if hit else "[MISS]"
        print(f"    {mark} [{r['category']:12s}] Q=\"{r['query']}\"")
        for i, sec in enumerate(r["top_sections"][:2]):
            print(f"      {i + 1}. {sec[:80]}")

    # ------------------------------------------------------------------
    # 保存报告
    # ------------------------------------------------------------------
    if output_file:
        output_data = {
            "description": "Redfish RAG retrieval quality evaluation",
            "db_stats": {
                "ipmi_chunks": 847,
                "redfish_chunks": 585,
                "total": 1432,
            },
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
# 入口
# ---------------------------------------------------------------------------
def main():
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Redfish RAG retrieval quality evaluation")
    parser.add_argument(
        "--output", type=str, default="./shared/rag_eval_redfish.json",
        help="Output report file path",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(run_evaluation(args.output))


if __name__ == "__main__":
    main()
