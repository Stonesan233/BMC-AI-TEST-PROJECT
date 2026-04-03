# -*- coding: utf-8 -*-
"""
CLI RAG 检索质量评估脚本

评估 CLI 命令在 5 个维度上的检索精度:
  - exact_name:  精确命令名查询 (英文/中文)
  - subcommand:  子命令查询
  - fuzzy:       模糊功能描述
  - scenario:    场景化查询
  - interactive: 交互式命令查询

用法:
  python -m src.rag.eval_cli_search
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

from src.rag.retriever import HybridRetriever

logger = logging.getLogger("rag.eval_cli")

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ENV_KEY_NAME = "DASHSCOPE_API_KEY"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024


async def _embed(client: AsyncOpenAI, texts: List[str]) -> Dict[str, List[float]]:
    result = {}
    for i in range(0, len(texts), 8):
        batch = texts[i:i+8]
        try:
            # 不传 dimensions，使用模型原生维度（避免 vLLM-ascend bug）
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL, input=batch,
            )
            for j, t in enumerate(batch):
                result[t] = resp.data[j].embedding
        except Exception as e:
            logger.warning(f"Batch fail: {e}")
            for t in batch:
                try:
                    r = await client.embeddings.create(
                        model=EMBEDDING_MODEL, input=[t],
                    )
                    result[t] = r.data[0].embedding
                except:
                    pass
    return result


def _is_relevant(expected: List[str], meta: Dict) -> bool:
    if meta.get("doc_type") != "cli":
        return False
    fields = [
        meta.get("section", ""),
        meta.get("description", ""),
        meta.get("chinese_name", ""),
        meta.get("english_name", ""),
        meta.get("full_command", ""),
        meta.get("command_name", ""),
        meta.get("subcommand", ""),
    ]
    all_text = " ".join(f.lower() for f in fields if f)
    matched = sum(1 for kw in expected if kw.lower() in all_text)
    return matched >= max(len(expected) * 0.4, 1)


def _build_queries() -> List[Dict]:
    queries = []

    # 1. 精确命令名 (英文)
    queries.extend([
        {"query": "ipmcset -d adduser", "expected": ["ipmcset", "adduser"], "cat": "exact_name"},
        {"query": "ipmcget -d userlist", "expected": ["ipmcget", "userlist"], "cat": "exact_name"},
        {"query": "ipmcset -d password", "expected": ["ipmcset", "password"], "cat": "exact_name"},
        {"query": "ipmcset -d deluser", "expected": ["ipmcset", "deluser"], "cat": "exact_name"},
        {"query": "ipmcset -d privilege", "expected": ["ipmcset", "privilege"], "cat": "exact_name"},
        {"query": "ipmcset -d ipaddr", "expected": ["ipmcset", "ipaddr"], "cat": "exact_name"},
        {"query": "ipmcget -d ipinfo", "expected": ["ipmcget", "ipinfo"], "cat": "exact_name"},
        {"query": "ipmcset -d reset", "expected": ["ipmcset", "reset"], "cat": "exact_name"},
        {"query": "ipmcset -d fanlevel", "expected": ["ipmcset", "fanlevel"], "cat": "exact_name"},
        {"query": "ipmcget -d sel", "expected": ["ipmcget", "sel"], "cat": "exact_name"},
    ])

    # 2. 中文精确命令名
    queries.extend([
        {"query": "添加新用户", "expected": ["adduser", "添加"], "cat": "exact_cn"},
        {"query": "删除用户", "expected": ["deluser", "删除"], "cat": "exact_cn"},
        {"query": "修改用户密码", "expected": ["password", "密码"], "cat": "exact_cn"},
        {"query": "查询用户列表", "expected": ["userlist", "用户"], "cat": "exact_cn"},
        {"query": "设置用户权限", "expected": ["privilege", "权限"], "cat": "exact_cn"},
        {"query": "设置IP地址", "expected": ["ipaddr"], "cat": "exact_cn"},
        {"query": "查询IP信息", "expected": ["ipinfo"], "cat": "exact_cn"},
        {"query": "重启iBMC", "expected": ["reset", "重启"], "cat": "exact_cn"},
    ])

    # 3. 子命令/复合命令查询
    queries.extend([
        {"query": "service -d state", "expected": ["service", "state"], "cat": "subcommand"},
        {"query": "ntp -d preferredserver", "expected": ["ntp", "preferredserver"], "cat": "subcommand"},
        {"query": "trap -d port", "expected": ["trap", "port"], "cat": "subcommand"},
        {"query": "syslog -d address", "expected": ["syslog", "address"], "cat": "subcommand"},
        {"query": "vnc -d password", "expected": ["vnc", "password"], "cat": "subcommand"},
        {"query": "sol -d activate", "expected": ["sol", "activate"], "cat": "subcommand"},
        {"query": "securityenhance -d updatemasterkey", "expected": ["updatemasterkey"], "cat": "subcommand"},
        {"query": "user -d lock", "expected": ["lock", "锁定"], "cat": "subcommand"},
    ])

    # 4. 模糊功能描述
    queries.extend([
        {"query": "怎么给BMC添加一个新用户", "expected": ["adduser", "添加"], "cat": "fuzzy"},
        {"query": "如何查看服务器所有用户", "expected": ["userlist", "用户"], "cat": "fuzzy"},
        {"query": "修改用户权限为管理员", "expected": ["privilege", "权限"], "cat": "fuzzy"},
        {"query": "配置BMC管理网口IP", "expected": ["ipaddr", "ip"], "cat": "fuzzy"},
        {"query": "查看BMC版本信息", "expected": ["version", "版本"], "cat": "fuzzy"},
        {"query": "风扇转速设置", "expected": ["fan", "风扇"], "cat": "fuzzy"},
        {"query": "查看系统日志", "expected": ["sel", "日志"], "cat": "fuzzy"},
        {"query": "升级BMC固件", "expected": ["upgrade", "升级"], "cat": "fuzzy"},
        {"query": "设置NTP时间同步", "expected": ["ntp"], "cat": "fuzzy"},
        {"query": "SNMP trap告警配置", "expected": ["trap", "snmp"], "cat": "fuzzy"},
        {"query": "SSL证书导入", "expected": ["certificate", "ssl", "import"], "cat": "fuzzy"},
        {"query": "服务器上下电控制", "expected": ["power", "frucontrol"], "cat": "fuzzy"},
    ])

    # 5. 场景化/交互式
    queries.extend([
        {"query": "通过CLI创建一个管理员用户并设置密码", "expected": ["adduser"], "cat": "scenario"},
        {"query": "删除一个不需要的BMC用户", "expected": ["deluser"], "cat": "scenario"},
        {"query": "锁定一个可疑的用户账号", "expected": ["lock", "锁定"], "cat": "scenario"},
        {"query": "解锁被锁定的用户", "expected": ["unlock", "解锁"], "cat": "scenario"},
        {"query": "配置远程VNC连接", "expected": ["vnc"], "cat": "scenario"},
        {"query": "设置syslog服务器地址", "expected": ["syslog", "address"], "cat": "scenario"},
        {"query": "恢复BMC出厂设置", "expected": ["restore", "恢复"], "cat": "scenario"},
        {"query": "查看RAID控制器信息", "expected": ["ctrlinfo", "raid"], "cat": "scenario"},
        {"query": "修改SNMPv3用户加密密码", "expected": ["snmpprivacypassword"], "cat": "scenario"},
        {"query": "检查网络连通性", "expected": ["ping"], "cat": "scenario"},
    ])

    return queries


async def run_eval():
    load_dotenv()
    api_key = os.getenv(ENV_KEY_NAME, "").strip()
    if not api_key:
        print("[ERROR] DASHSCOPE_API_KEY not set")
        return

    retriever = HybridRetriever(chroma_path="./shared/rag_index", collection_name="openubmc_rag")

    http_client = httpx.AsyncClient(timeout=120.0)
    openai_client = AsyncOpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL, http_client=http_client)

    queries = _build_queries()
    print(f"[OK] {len(queries)} test queries")

    # Embed
    print("[INFO] Embedding queries...")
    q_texts = [q["query"] for q in queries]
    embeddings = await _embed(openai_client, q_texts)
    print(f"[OK] {len(embeddings)}/{len(q_texts)} embedded")

    # Search with multiple modes
    modes = {"vector": 1.0, "hybrid_a07": 0.7, "keyword": 0.0}

    all_results = {m: [] for m in modes}

    for q in queries:
        emb = embeddings.get(q["query"])
        for mode, alpha in modes.items():
            results = retriever.search(
                query=q["query"],
                query_embedding=emb,
                top_k=5,
                alpha=alpha,
                doc_type="cli",
            )
            top_metas = [r["metadata"] for r in results]
            all_results[mode].append({
                "query": q["query"],
                "cat": q["cat"],
                "expected": q["expected"],
                "top_metas": top_metas,
                "top_sections": [m.get("section","")[:80] for m in top_metas[:3]],
            })

    # Compute metrics
    print(f"\n{'='*70}")
    print("CLI RAG RETRIEVAL QUALITY EVALUATION")
    print(f"{'='*70}")
    print(f"  Total queries: {len(queries)}")

    for mode in modes:
        results = all_results[mode]
        p1 = sum(1 for r in results if any(_is_relevant(r["expected"], m) for m in r["top_metas"][:1])) / len(results)
        p3 = sum(1 for r in results if any(_is_relevant(r["expected"], m) for m in r["top_metas"][:3])) / len(results)
        p5 = sum(1 for r in results if any(_is_relevant(r["expected"], m) for m in r["top_metas"][:5])) / len(results)

        mrr = 0.0
        for r in results:
            for rank in range(1, min(5, len(r["top_metas"]))+1):
                if _is_relevant(r["expected"], r["top_metas"][rank-1]):
                    mrr += 1.0 / rank
                    break
        mrr /= len(results)

        print(f"\n  [{mode:15s}] P@1={p1:.1%}  P@3={p3:.1%}  P@5={p5:.1%}  MRR={mrr:.4f}")

        # By category
        by_cat = defaultdict(list)
        for r in results:
            by_cat[r["cat"]].append(r)
        for cat in sorted(by_cat.keys()):
            cr = by_cat[cat]
            cp1 = sum(1 for r in cr if any(_is_relevant(r["expected"], m) for m in r["top_metas"][:1])) / len(cr)
            cp3 = sum(1 for r in cr if any(_is_relevant(r["expected"], m) for m in r["top_metas"][:3])) / len(cr)
            print(f"    {cat:15s}  n={len(cr):2d}  P@1={cp1:.1%}  P@3={cp3:.1%}")

    # Show examples
    print(f"\n  --- Query Examples (hybrid_a07, top-3) ---")
    for r in all_results["hybrid_a07"][:15]:
        hit = any(_is_relevant(r["expected"], m) for m in r["top_metas"][:1])
        mark = "[OK]" if hit else "[MISS]"
        print(f"    {mark} [{r['cat']:12s}] Q=\"{r['query'][:50]}\"")
        for i, sec in enumerate(r["top_sections"][:2]):
            print(f"      {i+1}. {sec[:100]}")

    await openai_client.close()
    await http_client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    asyncio.run(run_eval())
