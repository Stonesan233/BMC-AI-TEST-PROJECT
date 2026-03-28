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
from itertools import count
from pathlib import Path
from typing import Any, Dict, List

import yaml

from src.core.schemas import (
    ExecutionRecord,
    TestResult,
    StepRecord,
    StepJudgment,
    StepStatus,
    Evidence,
)
from src.utils.file_handler import (
    ensure_shared_dirs,
    generate_human_report,
    save_execution_record,
    save_test_result,
)

# 全局执行计数器，确保 execution_id 唯一
_execution_counter = count(1)


# ============================================================
# 配置与用例加载
# ============================================================

def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件"""
    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] 配置文件不存在: {config_path}")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"[ERROR] 配置文件格式错误: {e}")
        sys.exit(1)


def load_test_cases(case_paths: List[str]) -> List[Dict[str, Any]]:
    """加载测试用例（支持多个文件）"""
    cases = []
    for path_str in case_paths:
        path = Path(path_str)
        if not path.exists():
            print(f"[ERROR] 用例文件不存在: {path_str}")
            sys.exit(1)
        try:
            with open(path, "r", encoding="utf-8") as f:
                case = yaml.safe_load(f)
            case["_source_path"] = str(path)
            cases.append(case)
        except yaml.YAMLError as e:
            print(f"[ERROR] 用例文件格式错误 ({path_str}): {e}")
            sys.exit(1)
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
    """生成唯一执行记录 ID（含毫秒 + 计数器）"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    seq = next(_execution_counter)
    return f"exec_{ts}_{seq:03d}"


# ============================================================
# TODO: 调用 Test_Exec Agent（批量版本）
#
# 未来实现位置: src/exec_agent.py
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
) -> List[ExecutionRecord]:
    """
    调用 Test_Exec Agent 执行一批用例。

    TODO: 未来实现
    - 构建 Exec Agent 请求（包含用例、环境、RAG 配置）
    - 通过 HTTP API 调用 Exec Agent（MiniMax-M2.5 @ 192.168.1.100）
    - 返回 ExecutionRecord 列表
    """
    print(f"[TODO] 调用 Test_Exec Agent 执行 {len(batch)} 个用例")

    records = []
    for case in batch:
        execution_id = generate_execution_id()
        now = datetime.now()

        mock_step = StepRecord(
            step_id="step_001",
            description="模拟步骤：查询 BMC 信息",
            tool="redfish",
            interface_preference="redfish",
            endpoint="/redfish/v1",
            method="GET",
            expected="HTTP 200",
            actual="HTTP 200",
            raw_stdout='{"@odata.type": "#Service.v1_0_0.Service", "ServiceVersion": "1.0.0"}',
            raw_stderr="",
            evidence=[],
            status=StepStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        )

        record = ExecutionRecord(
            execution_id=execution_id,
            case_id=case.get("case_id", case.get("用例_编号", "unknown")),
            case_name=case.get("name", case.get("用例_名称", "unknown")),
            environment={
                "bmc_host": config.get("target", {}).get("bmc_host", "unknown"),
                "bmc_user": config.get("target", {}).get("bmc_user", "unknown"),
            },
            test_case_info={
                "source_path": case.get("_source_path", ""),
            },
            prerequisites=[
                {
                    "name": "BMC 网络可达",
                    "status": "completed",
                    "details": "模拟：BMC 响应正常",
                }
            ],
            steps=[mock_step],
            started_at=now,
            completed_at=now,
            overall_status="completed",
        )
        records.append(record)

    return records


# ============================================================
# TODO: 调用 Test_Judge Agent（单用例）
#
# 未来实现位置: src/judge_agent.py
#
# 功能描述：
#   对单个 ExecutionRecord 进行严格判断
#
# 当前为占位实现，返回模拟数据
# ============================================================
async def call_judge_agent(
    execution_record: ExecutionRecord,
    config: Dict[str, Any]
) -> TestResult:
    """
    调用 Test_Judge Agent 判断结果。

    TODO: 未来实现
    - 从共享目录读取 ExecutionRecord
    - 构建 Judge Agent 请求（三层 Prompt）
    - 通过 HTTP API 调用 Judge Agent（Qwen3-235B-A22B @ 192.168.1.101）
    - 解析返回 JSON 为 TestResult
    """
    print(f"[TODO] 调用 Test_Judge Agent: {execution_record.execution_id}")

    mock_judgment = StepJudgment(
        step_id="step_001",
        result="PASS",
        confidence=0.90,
        reason="模拟判断：步骤执行成功，响应符合预期",
        expected_match=True,
        concerns=[],
    )

    result = TestResult(
        execution_id=execution_record.execution_id,
        case_id=execution_record.case_id,
        case_name=execution_record.case_name,
        overall_result="PASS",
        confidence=0.85,
        step_results=[mock_judgment],
        prerequisite_check={
            "result": "PASS",
            "failed_items": [],
        },
        environment_recovery={
            "recovered": True,
            "warnings": [],
        },
        judge_notes=["模拟判断结果：所有步骤通过"],
    )

    return result


# ============================================================
# 单用例执行（Exec -> Save -> Judge -> Save -> Report）
# ============================================================

async def run_single_case(
    case: Dict[str, Any],
    exec_record: ExecutionRecord,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """执行单个用例的完整流程：保存记录 -> Judge -> 保存结果 -> 生成报告"""
    shared_dir = config.get("storage", {}).get("shared_dir", "./shared")
    case_name = exec_record.case_name

    # Step 1: 保存 ExecutionRecord
    record_path = save_execution_record(exec_record, shared_dir)

    # Step 2: 调用 Judge Agent
    test_result = await call_judge_agent(exec_record, config)

    # Step 3: 保存 TestResult
    result_path = save_test_result(test_result, shared_dir)

    # Step 4: 生成 Markdown 报告
    report_path = generate_human_report(exec_record, test_result, shared_dir)

    result_icon = "[PASS]" if test_result.overall_result == "PASS" else "[FAIL]"
    print(f"[OK] 用例完成 --> {case_name} [{result_icon}]")

    return {
        "case_name": case_name,
        "execution_id": exec_record.execution_id,
        "record_path": record_path,
        "result_path": result_path,
        "report_path": report_path,
        "overall_result": test_result.overall_result,
        "status": "completed",
    }


# ============================================================
# 批量执行逻辑
# ============================================================

async def run_batch(
    batch: List[Dict[str, Any]],
    batch_index: int,
    config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """执行一批测试用例（1~3个）"""
    print(f"\n{'='*60}")
    print(f"批次 #{batch_index + 1}  开始执行 {len(batch)} 个用例")
    print(f"{'='*60}")

    # Step 1: 调用 Exec Agent（批量）
    try:
        exec_records = await call_exec_agent_batch(batch, config)
    except Exception as e:
        print(f"[ERROR] Exec Agent 调用失败: {e}")
        return [
            {"case_name": c.get("name", c.get("用例_名称", "unknown")), "status": "error", "overall_result": "FAIL"}
            for c in batch
        ]

    # Step 2: 逐用例执行 Judge + Save + Report
    results = []
    for i, (case, exec_record) in enumerate(zip(batch, exec_records)):
        case_name = exec_record.case_name
        print(f"\n--- 用例 {i+1}/{len(batch)}: {case_name} ---")

        try:
            result = await run_single_case(case, exec_record, config)
            results.append(result)
        except Exception as e:
            print(f"[ERROR] 用例执行失败: {case_name} - {e}")
            results.append({
                "case_name": case_name,
                "execution_id": exec_record.execution_id,
                "status": "error",
                "overall_result": "FAIL",
            })

    return results


# ============================================================
# 主函数
# ============================================================

async def main_async(args: argparse.Namespace) -> int:
    """异步主函数"""
    start_time = datetime.now()

    # 1. 加载配置
    config = load_config(args.config)

    # 2. 确定批量大小（1~3）
    exec_batch_size = max(1, min(config.get("agent", {}).get("exec_batch_size", 1), 3))

    # 3. 确保共享目录存在
    shared_dir = config.get("storage", {}).get("shared_dir", "./shared")
    ensure_shared_dirs(shared_dir)

    # 4. 加载用例
    cases = load_test_cases(args.cases)

    # 启动信息
    print(f"\nopenUBMC AI 测试框架")
    print(f"{'='*60}")
    print(f"  配置文件:   {args.config}")
    print(f"  用例数量:   {len(cases)}")
    print(f"  批量大小:   {exec_batch_size}")
    print(f"  共享目录:   {shared_dir}")
    print(f"{'='*60}")

    if not cases:
        print("[WARN] 未加载到任何测试用例，退出")
        return 0

    # 5. 分组执行
    batches = group_cases_by_batch(cases, exec_batch_size)
    print(f"分为 {len(batches)} 个批次执行\n")

    all_results: List[Dict[str, Any]] = []
    for batch_index, batch in enumerate(batches):
        batch_results = await run_batch(batch, batch_index, config)
        all_results.extend(batch_results)

    # 6. 执行摘要
    elapsed = (datetime.now() - start_time).total_seconds()
    passed = sum(1 for r in all_results if r.get("overall_result") == "PASS")
    failed = sum(1 for r in all_results if r.get("overall_result") == "FAIL")
    errors = sum(1 for r in all_results if r.get("status") == "error")

    print(f"\n{'='*60}")
    print(f"执行摘要  (耗时 {elapsed:.1f}s)")
    print(f"{'='*60}")
    print(f"  总用例数: {len(all_results)}")
    print(f"  通过:     {passed}")
    print(f"  失败:     {failed}")
    if errors:
        print(f"  异常:     {errors}")

    for r in all_results:
        icon = "[PASS]" if r.get("overall_result") == "PASS" else "[FAIL]"
        name = r.get("case_name", "unknown")
        print(f"  {icon} {name}")

    print(f"{'='*60}")

    return 0 if (failed == 0 and errors == 0) else 1


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="openUBMC AI 测试框架")
    parser.add_argument(
        "--config", "-c",
        default="config/config.yaml",
        help="配置文件路径 (默认: config/config.yaml)"
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
