# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 极轻量启动脚本

本脚本是框架的主入口，仅负责流程串联。
重要：本脚本不是 Agent，不使用 Prompt，不使用 Tool Calling。
所有智能逻辑均在 Test_Exec 和 Test_Judge Agent 中实现。
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from src.utils.file_handler import (
    ensure_shared_dirs,
    generate_human_report,
    save_execution_record,
    save_test_result,
)


# ============================================================
# 配置与用例加载
# ============================================================

def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_test_cases(case_paths: List[str]) -> List[Dict[str, Any]]:
    """加载测试用例（支持多个文件）"""
    cases = []
    for path in case_paths:
        with open(path, "r", encoding="utf-8") as f:
            case = yaml.safe_load(f)
        case["_source_path"] = str(path)
        cases.append(case)
    return cases


# ============================================================
# 工具函数
# ============================================================

def group_cases_by_batch(
    cases: List[Dict[str, Any]],
    batch_size: int
) -> List[List[Dict[str, Any]]]:
    """按批量大小分组用例"""
    batches = []
    for i in range(0, len(cases), batch_size):
        batches.append(cases[i:i + batch_size])
    return batches


def generate_execution_id() -> str:
    """生成执行记录 ID"""
    return f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ============================================================
# TODO: 调用 Test_Exec Agent（批量版本）
#
# 功能描述：
#   调用 Test_Exec Agent 执行一批用例（1~3个）
#
# 实现步骤：
#   1. 将整个 batch 一起发送给 Exec Agent
#   2. Exec Agent 内部处理批量执行 + RAG
#   3. 返回 ExecutionRecord 列表
#
# 当前为占位实现，返回模拟数据
# ============================================================
async def call_exec_agent_batch(
    batch: List[Dict[str, Any]],
    config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    调用 Test_Exec Agent 执行一批用例。

    TODO: 未来实现
    - 构建 Exec Agent 请求（包含用例、环境、RAG 配置）
    - 通过 HTTP API 调用 Exec Agent
    - 返回 ExecutionRecord 列表
    """
    print(f"[TODO] 调用 Test_Exec Agent 执行 {len(batch)} 个用例")

    # 占位实现：返回模拟 ExecutionRecord
    records = []
    for case in batch:
        execution_id = generate_execution_id()
        record = {
            "execution_id": execution_id,
            "case_id": case.get("case_id", case.get("用例_编号", "unknown")),
            "case_name": case.get("name", case.get("用例_名称", "unknown")),
            "environment": {
                "bmc_host": config.get("target", {}).get("bmc_host", "unknown"),
                "bmc_user": config.get("target", {}).get("bmc_user", "unknown"),
            },
            "test_case_info": {
                "source_path": case.get("_source_path", ""),
            },
            "prerequisites": [
                {
                    "name": "BMC 网络可达",
                    "status": "completed",
                    "details": "模拟：BMC 响应正常",
                }
            ],
            "steps": [
                {
                    "step_id": "step_001",
                    "description": "模拟步骤",
                    "tool": "redfish",
                    "interface_preference": "redfish",
                    "command": "GET /redfish/v1",
                    "expected": "HTTP 200",
                    "actual": "HTTP 200",
                    "raw_stdout": '{"@odata.type": "#Service.v1_0_0.Service", "ServiceVersion": "1.0.0"}',
                    "raw_stderr": "",
                    "evidence": [],
                    "status": "completed",
                    "error_message": None,
                    "started_at": datetime.now().isoformat(),
                    "completed_at": datetime.now().isoformat(),
                }
            ],
            "started_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
            "overall_status": "completed",
        }
        records.append(record)

    return records


# ============================================================
# TODO: 调用 Test_Judge Agent（单用例）
#
# 功能描述：
#   对单个 ExecutionRecord 进行严格判断
#
# 当前为占位实现，返回模拟数据
# ============================================================
async def call_judge_agent(
    execution_record: Dict[str, Any],
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    调用 Test_Judge Agent 判断结果。

    TODO: 未来实现
    - 从共享目录读取 ExecutionRecord
    - 构建 Judge Agent 请求
    - 通过 HTTP API 调用 Judge Agent
    - 返回 TestResult
    """
    print(f"[TODO] 调用 Test_Judge Agent: {execution_record.get('execution_id')}")

    # 占位实现：返回模拟 TestResult
    result = {
        "execution_id": execution_record.get("execution_id"),
        "case_id": execution_record.get("case_id"),
        "case_name": execution_record.get("case_name"),
        "overall_result": "PASS",
        "confidence": 0.85,
        "step_results": [
            {
                "step_id": "step_001",
                "result": "PASS",
                "confidence": 0.90,
                "reason": "模拟判断：步骤执行成功",
                "expected_match": True,
                "concerns": [],
            }
        ],
        "prerequisite_check": {
            "result": "PASS",
            "failed_items": [],
        },
        "environment_recovery": {
            "recovered": True,
            "warnings": [],
        },
        "judge_notes": ["模拟判断结果"],
    }

    return result


# ============================================================
# 批量执行逻辑
# ============================================================

async def run_batch(
    batch: List[Dict[str, Any]],
    batch_index: int,
    config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """执行一批测试用例（1~3个）"""
    shared_dir = config.get("storage", {}).get("shared_dir", "./shared")
    print(f"\n{'='*60}")
    print(f"批次 #{batch_index + 1}  开始执行 {len(batch)} 个用例")
    print(f"{'='*60}")

    results = []

    # Step 1: 调用 Exec Agent（批量）
    exec_records = await call_exec_agent_batch(batch, config)

    for i, (case, exec_record) in enumerate(zip(batch, exec_records)):
        case_name = case.get("name", case.get("用例_名称", f"unknown_case_{i}"))
        print(f"\n--- 用例 {i+1}/{len(batch)}: {case_name} ---")

        # Step 2: 保存 ExecutionRecord
        record_path = save_execution_record(exec_record, shared_dir)

        # Step 3: 调用 Judge Agent（单用例）
        test_result = await call_judge_agent(exec_record, config)

        # Step 4: 保存 TestResult
        result_path = save_test_result(test_result, shared_dir)

        # Step 5: 生成人可读报告
        report_path = generate_human_report(exec_record, test_result, shared_dir)

        results.append({
            "execution_id": exec_record.get("execution_id"),
            "case_name": case_name,
            "record_path": record_path,
            "result_path": result_path,
            "report_path": report_path,
            "overall_result": test_result.get("overall_result"),
            "status": "completed",
        })

        result_icon = "[PASS]" if test_result.get("overall_result") == "PASS" else "[FAIL]"
        print(f"[OK] 用例完成 --> {case_name} [{result_icon}]")

    return results


# ============================================================
# 主函数
# ============================================================

async def main_async(args: argparse.Namespace) -> int:
    """异步主函数"""
    # 加载配置
    config = load_config(args.config)

    # 获取批量大小（1~3）
    exec_batch_size = max(1, min(config.get("agent", {}).get("exec_batch_size", 1), 3))
    print(f"启动配置 - Exec 批量大小: {exec_batch_size}")

    # 确保共享目录存在
    shared_dir = config.get("storage", {}).get("shared_dir", "./shared")
    ensure_shared_dirs(shared_dir)

    # 加载用例
    cases = load_test_cases(args.cases)
    print(f"共加载 {len(cases)} 个测试用例")

    # 分组执行
    batches = group_cases_by_batch(cases, exec_batch_size)
    print(f"分为 {len(batches)} 个批次执行")

    all_results = []
    for batch_index, batch in enumerate(batches):
        batch_results = await run_batch(batch, batch_index, config)
        all_results.extend(batch_results)

    # ============================================================
    # TODO: 整体执行完成后的总结
    #
    # 未来可扩展：
    #   - 生成汇总报告
    #   - 发送通知
    #   - 清理临时文件
    # ============================================================
    print("\n" + "="*60)
    print("执行摘要")
    print("="*60)

    completed = sum(1 for r in all_results if r.get("status") == "completed")
    passed = sum(1 for r in all_results if r.get("overall_result") == "PASS")
    failed = len(all_results) - passed

    print(f"总用例数: {len(all_results)}")
    print(f"执行成功: {completed}")
    print(f"通过: {passed}")
    print(f"失败: {failed}")

    for result in all_results:
        icon = "[PASS]" if result.get("overall_result") == "PASS" else "[FAIL]"
        print(f"  {icon} {result.get('case_name')}")

    return 0 if failed == 0 else 1


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="openUBMC AI 测试框架")
    parser.add_argument(
        "--config", "-c",
        default="config/config.yaml",
        help="配置文件路径"
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        required=True,
        help="测试用例路径（支持多个文件）"
    )
    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
