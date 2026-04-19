# -*- coding: utf-8 -*-
"""
快速验证 RAG 双接口 (IPMI + Redfish) 检索功能

用法:
  python test_rag_dual_interface.py

测试内容:
  1. 自动检测: 模糊查询自动判断 IPMI / Redfish
  2. 指定 IPMI: 仅搜索 IPMI 文档
  3. 指定 Redfish: 仅搜索 Redfish 文档
  4. 混合检索: 同时搜索两种文档并合并
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI

from src.rag.retriever import HybridRetriever

load_dotenv()

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("test_dual")


# ======================================================================
# 测试查询集
# ======================================================================
TEST_QUERIES = [
    # (查询文本, 期望检测类型, 说明)
    ("查看BMC固件版本号", "both", "模糊 - 应检测为 both"),
    ("通过Redfish接口查询系统电源状态", "redfish", "明确Redfish"),
    ("用IPMI raw命令读取传感器数据", "ipmi", "明确IPMI"),
    ("怎么给服务器远程开机", "both", "模糊场景"),
    ("/redfish/v1/Managers/1", "redfish", "Redfish URI"),
    ("NetFn 0x06 Cmd 0x01 获取设备ID", "ipmi", "IPMI NetFn/Cmd"),
    ("添加一个管理员账户", "both", "账户管理 - 可能两种都有"),
    ("配置BMC的IP地址", "redfish", "偏向Redfish（ethernetinterface）"),
    ("SEL事件日志怎么查看", "ipmi", "SEL 是IPMI特征"),
    ("查询CPU温度和风扇转速", "both", "传感器 - 两种都可能"),
]


# ======================================================================
# Embedding 辅助
# ======================================================================
async def get_embedding(client: AsyncOpenAI, text: str, model: str = "text-embedding-v4", dim: int = 1024):
    try:
        resp = await client.embeddings.create(model=model, input=[text], dimensions=dim)
        return resp.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return None


# ======================================================================
# 自动检测关键词评分 (从 exec_agent.py 复制核心逻辑)
# ======================================================================
def auto_detect_doc_types(query: str) -> dict:
    """关键词评分自动检测."""
    q = query.lower()

    redfish_strong = ["/redfish", "redfish", "rest api", "restful",
                      "uri", "endpoint", "json", "https://", "odata"]
    ipmi_strong = ["ipmi", "ipmitool", "netfn", "raw 0x",
                   "sel ", "sdr ", "fru ", "mc info", "mc guid",
                   "chassis ", "sensor list"]
    redfish_weak = ["账户管理", "会话管理", "用户角色", "事件订阅", "更新服务",
                    "任务服务", "证书", "ethernetinterface", "ip地址配置",
                    "网络接口", "虚拟媒体", "固件升级"]
    ipmi_weak = ["raw命令", "原始命令", "ipmi命令", "风扇模式", "sdr仓库",
                 "传感器读数", "机箱状态", "机箱电源"]

    rs = sum(2 for kw in redfish_strong if kw in q) + sum(1 for kw in redfish_weak if kw in q)
    is_ = sum(2 for kw in ipmi_strong if kw in q) + sum(1 for kw in ipmi_weak if kw in q)

    threshold = 2
    if rs >= threshold and is_ < threshold:
        detected = ["redfish"]
    elif is_ >= threshold and rs < threshold:
        detected = ["ipmi"]
    else:
        detected = ["ipmi", "redfish"]

    return {"redfish_score": rs, "ipmi_score": is_, "detected": detected}


# ======================================================================
# 格式化单条结果
# ======================================================================
def format_result(i: int, r: dict) -> str:
    meta = r.get("metadata", {})
    score = r.get("score", 0)
    dt = meta.get("doc_type", "?")
    ct = meta.get("chunk_type", "?")

    if dt == "redfish":
        uri = meta.get("resource_uri", "-")
        method = meta.get("http_method", "-")
        cn = meta.get("chinese_name", meta.get("full_title", ""))[:40]
        return f"    [{i}] score={score:.4f} | {dt}/{ct} | {method:6s} {uri} | {cn}"
    else:
        sec = meta.get("section", "")[:30]
        cmd = meta.get("command_name", "-")
        netfn = meta.get("netfn", "-")
        cmd_byte = meta.get("cmd", "-")
        return f"    [{i}] score={score:.4f} | {dt}/{ct} | NetFn={netfn} Cmd={cmd_byte} | {cmd}"


# ======================================================================
# 主流程
# ======================================================================
async def main():
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] DASHSCOPE_API_KEY not set")
        return

    # 初始化组件
    retriever = HybridRetriever(
        chroma_path="./shared/rag_index",
        collection_name="openubmc_rag",
        alpha=0.7,
        enable_rewrite=False,  # 测试时不启用 rewrite，加快速度
    )

    embed_http = httpx.AsyncClient(timeout=120.0)
    embed_client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=embed_http,
    )

    print(f"{'=' * 80}")
    print("RAG DUAL-INTERFACE TEST (IPMI + Redfish)")
    print(f"{'=' * 80}")
    print(f"  DB chunks: {retriever.collection.count()}")
    print(f"  Documents loaded: {len(retriever.documents)}")
    print()

    passed = 0
    failed = 0

    for query, expected, desc in TEST_QUERIES:
        # 自动检测
        detect = auto_detect_doc_types(query)
        detected = detect["detected"]

        # 生成 embedding
        emb = await get_embedding(embed_client, query)

        # 按检测到的 doc_type 分别检索
        all_results = []
        for doc_type in detected:
            chunk_type = "command" if doc_type == "ipmi" else "resource"
            results = retriever.search(
                query=query,
                query_embedding=emb,
                top_k=3,
                chunk_type=chunk_type,
                doc_type=doc_type,
            )
            all_results.extend(results)

        all_results.sort(key=lambda r: r.get("score", 0), reverse=True)
        top3 = all_results[:3]

        # 判断测试结果
        ok = len(top3) > 0
        status = "[OK]" if ok else "[MISS]"
        if ok:
            passed += 1
        else:
            failed += 1

        print(f"  {status} Q=\"{query}\"")
        print(f"       {desc}")
        print(f"       detect: redfish={detect['redfish_score']}, ipmi={detect['ipmi_score']} -> {detected}")
        print(f"       results: {len(top3)} hits")
        for i, r in enumerate(top3, 1):
            print(format_result(i, r))
        print()

    # 再测试指定接口模式
    print(f"{'=' * 80}")
    print("FIXED INTERFACE MODE TEST")
    print(f"{'=' * 80}")
    print()

    fixed_tests = [
        ("查询BMC固件版本", "ipmi", "强制仅搜索 IPMI"),
        ("查询BMC固件版本", "redfish", "强制仅搜索 Redfish"),
    ]

    query_emb = await get_embedding(embed_client, "查询BMC固件版本")

    for query, iface, desc in fixed_tests:
        chunk_type = "command" if iface == "ipmi" else "resource"
        results = retriever.search(
            query=query,
            query_embedding=query_emb,
            top_k=3,
            chunk_type=chunk_type,
            doc_type=iface,
        )
        print(f"  Q=\"{query}\" | interface={iface} | {desc}")
        print(f"  results: {len(results)} hits")
        for i, r in enumerate(results[:3], 1):
            print(format_result(i, r))
        print()

    # 汇总
    print(f"{'=' * 80}")
    print(f"  Auto-detect: {passed} passed, {failed} failed / {len(TEST_QUERIES)} total")
    print(f"{'=' * 80}")

    await embed_client.close()
    await embed_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
