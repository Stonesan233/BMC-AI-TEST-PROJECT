# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 极轻量启动脚本

本脚本是框架的主入口，仅负责流程串联。
重要：本脚本不是 Agent，不使用 Prompt，不使用 Tool Calling。
所有智能逻辑均在 Test_Exec 和 Test_Judge Agent 中实现。
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 控制台 UTF-8 输出（避免 LLM 输出中的 Unicode 字符导致 GBK 编码错误）
if sys.stdout:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr:
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

import yaml

from src.core.config import load_config as load_app_config, AppConfig
from src.core.client_factory import ClientFactory
from src.core.schemas import (
    ExecutionRecord,
    TestResult,
    StepJudgment,
    StepStatus,
)
from src.utils.file_handler import (
    convert_excel_to_yaml,
    ensure_shared_dirs,
    generate_human_report,
    save_execution_record,
    save_test_result,
)
from src.agents.exec_agent import ExecAgent
from src.agents.judge_agent import JudgeAgent


# ============================================================
# 日志配置
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)


# ============================================================
# 配置与用例加载
# ============================================================

def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件（原始 dict 格式，用于 Exec Agent 兼容）"""
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

def _resolve_excel_paths(
    excel_args: List[str],
    shared_dir: str,
) -> List[str]:
    """处理 --excel 参数，返回转换后的 YAML 文件路径列表"""
    excel_yaml_dir = Path(shared_dir) / "excel_cases"
    generated: List[str] = []
    for ep in excel_args:
        p = Path(ep)
        if not p.exists():
            print(f"[ERROR] Excel 路径不存在: {ep}")
            continue
        generated.extend(convert_excel_to_yaml(str(p), str(excel_yaml_dir)))

    if generated:
        print(f"[Excel] 已转换 {len(generated)} 个用例为 YAML，保存至 {excel_yaml_dir}/")
    else:
        print("[Excel] 未生成任何 YAML 文件")
    return generated


def group_cases_by_batch(
    cases: List[Dict[str, Any]],
    batch_size: int
) -> List[List[Dict[str, Any]]]:
    """按批量大小分组用例"""
    batches = []
    for i in range(0, len(cases), batch_size):
        batches.append(cases[i:i + batch_size])
    return batches


# ============================================================
# 调用 Test_Exec Agent（真实 LLM 版本）
# ============================================================

async def call_exec_agent_batch(
    agent: ExecAgent,
    batch: List[Dict[str, Any]],
    config: Dict[str, Any]
) -> List[ExecutionRecord]:
    """
    调用真实 Test_Exec Agent 执行一批用例。

    通过 AsyncOpenAI + Tool Calling 与 LLM 交互，
    使用 Redfish/IPMI/SSH 工具实际执行 BMC 操作。
    """
    print(f"[Exec] 调用 Test_Exec Agent 执行 {len(batch)} 个用例")
    return await agent.execute_batch(batch, config)


# ============================================================
# 调用 Test_Judge Agent（真实 LLM 版本）
# ============================================================

async def call_judge_agent(
    execution_record: ExecutionRecord,
    config: Dict[str, Any],
    judge_agent: JudgeAgent,
) -> TestResult:
    """
    调用真实 Test_Judge Agent 判断结果。

    通过 AsyncOpenAI 与 Qwen3/GLM 模型交互，
    使用三层 Prompt（System + Domain + Task）进行严格判断。

    Args:
        execution_record: ExecutionRecord 实例
        config: 原始配置字典（用于获取 shared_dir）
        judge_agent: JudgeAgent 实例
    """
    print(f"[Judge] 调用 Test_Judge Agent: {execution_record.execution_id}")
    return await judge_agent.judge_from_record(execution_record)


# ============================================================
# 单用例执行（Exec -> Save -> Judge -> Save -> Report）
# ============================================================

async def run_single_case(
    case: Dict[str, Any],
    exec_record: ExecutionRecord,
    config: Dict[str, Any],
    judge_agent: Optional[JudgeAgent],
) -> Dict[str, Any]:
    """
    执行单个用例的完整流程：保存记录 -> (Judge) -> 保存结果 -> 生成报告。

    当 judge_agent 为 None 时（--no-judge 模式），跳过 Judge 步骤，
    基于 Exec 步骤状态生成模拟 TestResult。
    Judge 调用失败时整体标记为 ERROR，但保留 partial report。
    """
    shared_dir = config.get("storage", {}).get("shared_dir", "./shared")
    case_name = exec_record.case_name

    # Step 1: 保存 ExecutionRecord
    record_path = save_execution_record(exec_record, shared_dir)

    # Step 2: 调用 Judge Agent（如果启用）
    if judge_agent is not None:
        try:
            test_result = await call_judge_agent(exec_record, config, judge_agent)
        except Exception as e:
            # Judge 失败 -> 构建 ERROR 级别 TestResult，保留 partial report
            print(f"[ERROR] Judge Agent 调用异常: {e}")
            from src.agents.judge_agent import build_error_test_result
            test_result = build_error_test_result(
                execution_record=exec_record,
                judge_model=getattr(judge_agent, '_judge_comp', None),
                error_message=str(e),
            )
            # 仍然保存 partial report
            test_result.audit_report_markdown = (
                f"# Judge Error - {case_name}\n\n"
                f"Judge Agent 调用异常，结果不可信。\n\n**Error**: {e}\n"
            )
    else:
        # --no-judge 模式：基于 Exec 步骤状态生成简单 TestResult
        test_result = _build_skip_judge_result(exec_record)

    # Step 3: 保存 TestResult
    result_path = save_test_result(test_result, shared_dir)

    # Step 4: 生成 Markdown 报告（基于 file_handler 的报告 + Judge 审计报告）
    report_path = generate_human_report(exec_record, test_result, shared_dir)

    result_icon = "[PASS]" if test_result.overall_result == "PASS" else "[FAIL]"
    confidence_str = f"{test_result.confidence:.2f}"
    risk_str = test_result.false_pass_risk
    judge_tag = "" if judge_agent else " (no-judge)"
    print(f"[OK] 用例完成 --> {case_name} [{result_icon}] (conf={confidence_str}, risk={risk_str}){judge_tag}")

    return {
        "case_name": case_name,
        "execution_id": exec_record.execution_id,
        "record_path": record_path,
        "result_path": result_path,
        "report_path": report_path,
        "overall_result": test_result.overall_result,
        "status": "completed",
    }


def _build_skip_judge_result(exec_record: ExecutionRecord) -> TestResult:
    """
    --no-judge 模式下，基于 Exec 步骤状态生成简单的 TestResult。

    注意：此结果未经 LLM 严格判断，假 PASS 风险为 high。
    """
    step_judgments = []
    all_pass = True
    for step in exec_record.steps:
        is_pass = step.status == StepStatus.COMPLETED
        if not is_pass:
            all_pass = False
        step_judgments.append(StepJudgment(
            step_id=step.step_id,
            result="PASS" if is_pass else "FAIL",
            confidence=0.5,
            reason=f"未启用 Judge，基于步骤状态自动判断: {step.status.value}",
            expected_match=is_pass,
            concerns=["no-judge 模式，未经 LLM 严格验证"],
            evidence_sufficient=bool(step.evidence),
        ))

    return TestResult(
        execution_id=exec_record.execution_id,
        case_id=exec_record.case_id,
        case_name=exec_record.case_name,
        overall_result="PASS" if all_pass else "FAIL",
        confidence=0.5,
        step_results=step_judgments,
        prerequisite_check={"result": "SKIP", "failed_items": []},
        environment_recovery={"recovered": True, "warnings": []},
        judge_notes=["no-judge 模式，未经 Judge Agent 严格判断，结果仅供参考"],
        judge_model="none (no-judge)",
        judge_duration_seconds=0.0,
        false_pass_risk="high",
        risk_notes=["未启用 Judge Agent，所有判断基于步骤状态自动推导"],
    )


# ============================================================
# 批量执行逻辑
# ============================================================

async def run_batch(
    exec_agent: ExecAgent,
    batch: List[Dict[str, Any]],
    batch_index: int,
    config: Dict[str, Any],
    judge_agent: Optional[JudgeAgent],
) -> List[Dict[str, Any]]:
    """执行一批测试用例（1~3个）"""
    print(f"\n{'='*60}")
    print(f"批次 #{batch_index + 1}  开始执行 {len(batch)} 个用例")
    print(f"{'='*60}")

    # Step 1: 调用 Exec Agent（批量）
    try:
        exec_records = await call_exec_agent_batch(exec_agent, batch, config)
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
            result = await run_single_case(case, exec_record, config, judge_agent)
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

    # 1. 加载配置（原始 dict 格式，用于 Exec Agent）
    raw_config = load_config(args.config)

    # 1b. 加载 AppConfig（Pydantic 格式，用于 Judge Agent + ClientFactory）
    try:
        app_config = load_app_config(args.config)
    except Exception as e:
        print(f"[ERROR] AppConfig 加载失败: {e}")
        return 1

    # 2. 确定批量大小（1~3）
    exec_batch_size = max(1, min(raw_config.get("agent", {}).get("exec_batch_size", 1), 3))

    # 3. 确保共享目录存在
    shared_dir = raw_config.get("storage", {}).get("shared_dir", "./shared")
    ensure_shared_dirs(shared_dir)

    # 3b. 确保审计报告目录存在
    audit_reports_dir = Path(shared_dir) / "audit_reports"
    audit_reports_dir.mkdir(parents=True, exist_ok=True)

    # 4. Excel 用例转换（如果提供 --excel）
    case_paths = list(args.cases) if args.cases else []
    if args.excel:
        case_paths.extend(_resolve_excel_paths(args.excel, shared_dir))

    if not case_paths:
        print("[ERROR] 未提供任何用例（--cases 或 --excel 至少需要一个）")
        return 1

    # 5. 加载用例
    cases = load_test_cases(case_paths)

    # 启动信息
    judge_model = f"{app_config.models.judge.provider}/{app_config.models.judge.model}"
    judge_status = "ON" if args.judge else "OFF"
    print(f"\nopenUBMC AI 测试框架 (Judge v2.1)")
    print(f"{'='*60}")
    print(f"  配置文件:   {args.config}")
    print(f"  用例数量:   {len(cases)}")
    print(f"  批量大小:   {exec_batch_size}")
    print(f"  共享目录:   {shared_dir}")
    print(f"  Exec 模型:  {app_config.models.exec.provider}/{app_config.models.exec.model}")
    print(f"  Judge 模型: {judge_model} [{judge_status}]")
    print(f"{'='*60}")

    if not cases:
        print("[WARN] 未加载到任何测试用例，退出")
        return 0

    # 6. 初始化 ClientFactory
    client_factory = ClientFactory(app_config)

    # 7. 初始化 Exec Agent
    try:
        exec_agent = ExecAgent(raw_config)
    except ValueError as e:
        print(f"[ERROR] Exec Agent 初始化失败: {e}")
        await client_factory.close()
        return 1

    # 8. 初始化 Judge Agent（如果 --judge 启用）
    judge_agent: Optional[JudgeAgent] = None
    if args.judge:
        try:
            judge_agent = JudgeAgent(
                config=app_config,
                client_factory=client_factory,
                shared_dir=shared_dir,
            )
            print(f"[OK] Judge Agent 初始化成功 (model={judge_model})")
        except Exception as e:
            print(f"[WARN] Judge Agent 初始化失败: {e}，将以 --no-judge 模式运行")
            judge_agent = None
    else:
        print("[INFO] --no-judge 模式，跳过 Judge Agent 初始化")

    # 9. 分组执行
    batches = group_cases_by_batch(cases, exec_batch_size)
    print(f"分为 {len(batches)} 个批次执行\n")

    all_results: List[Dict[str, Any]] = []
    try:
        for batch_index, batch in enumerate(batches):
            batch_results = await run_batch(
                exec_agent, batch, batch_index, raw_config, judge_agent
            )
            all_results.extend(batch_results)
    finally:
        await exec_agent.close()
        await client_factory.close()

    # 10. 执行摘要
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

    # 11. 指出审计报告位置
    print(f"\n审计报告位置: {audit_reports_dir}/")
    for r in all_results:
        eid = r.get("execution_id", "")
        if eid:
            print(f"  {eid}_audit_report.md")

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
        default=[],
        help="YAML 测试用例路径（支持多个文件）"
    )
    parser.add_argument(
        "--excel",
        nargs="+",
        help="Excel 用例路径（.xlsx 文件或目录，自动转换为 YAML）"
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        default=True,
        help="启用 Judge Agent 进行严格判断（默认开启）"
    )
    parser.add_argument(
        "--no-judge",
        action="store_false",
        dest="judge",
        help="禁用 Judge Agent，仅执行不判断"
    )
    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
