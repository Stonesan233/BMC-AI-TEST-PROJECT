# -*- coding: utf-8 -*-
"""端到端 SSH 测试 - 使用 SSHTool 类"""
import asyncio
import yaml
from src.tools.ssh_tool import SSHTool

async def main():
    cfg = yaml.safe_load(open("config/config_ipmi_test.yaml", encoding="utf-8"))
    t = cfg["target"]

    ssh_host = t.get("ssh_host", t["bmc_host"])
    ssh_port = t.get("ssh_port", 10022)

    print(f"=== SSHTool e2e test: {ssh_host}:{ssh_port} ===\n")

    tool = SSHTool(
        host=ssh_host,
        port=ssh_port,
        user=t["bmc_user"],
        password=t["bmc_password"],
    )

    # Test 1: 非交互式
    print("--- Test 1: ipmcget -d version (shell mode) ---")
    r = await tool.execute("ipmcget -d version", timeout=30)
    print(f"success: {r.success}")
    print(f"mode: {r.mode}")
    print(f"exit_code: {r.exit_code}")
    if r.error:
        print(f"error: {r.error}")
    if r.raw_stdout:
        print(f"stdout ({len(r.raw_stdout)} chars):")
        print(r.raw_stdout[:500])
    if r.evidence:
        print(f"evidence_type: {r.evidence['evidence_type']}")

    # Test 2: 另一个命令
    print("\n--- Test 2: ipmcget -d userlist ---")
    r2 = await tool.execute("ipmcget -d userlist", timeout=20)
    print(f"success: {r2.success}")
    if r2.raw_stdout:
        print(r2.raw_stdout[:300])

    tool.close()
    print("\n=== DONE ===")

asyncio.run(main())
