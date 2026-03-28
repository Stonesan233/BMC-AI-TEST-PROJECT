# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 文件处理工具模块

提供共享目录管理、执行记录保存、测试结果保存、人可读报告生成等功能。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


# ============================================================
# 目录管理
# ============================================================

def ensure_shared_dirs(shared_dir: str) -> None:
    """
    确保共享目录结构存在。

    创建以下子目录：
    - execution_records/ : ExecutionRecord JSON 文件
    - test_results/      : TestResult JSON 文件
    - evidence/          : 证据文件
    - reports/           : Markdown 报告

    Args:
        shared_dir: 共享目录根路径
    """
    root = Path(shared_dir)
    subdirs = ["execution_records", "test_results", "evidence", "reports"]

    for subdir in subdirs:
        (root / subdir).mkdir(parents=True, exist_ok=True)


# ============================================================
# JSON 文件保存
# ============================================================

def save_execution_record(record: Dict[str, Any], shared_dir: str) -> str:
    """
    保存 ExecutionRecord 到 JSON 文件。

    Args:
        record: ExecutionRecord 字典
        shared_dir: 共享目录根路径

    Returns:
        保存的文件路径
    """
    execution_id = record.get("execution_id", "unknown")
    file_path = Path(shared_dir) / "execution_records" / f"{execution_id}.json"

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    return str(file_path)


def save_test_result(result: Dict[str, Any], shared_dir: str) -> str:
    """
    保存 TestResult 到 JSON 文件。

    Args:
        result: TestResult 字典
        shared_dir: 共享目录根路径

    Returns:
        保存的文件路径
    """
    execution_id = result.get("execution_id", "unknown")
    file_path = Path(shared_dir) / "test_results" / f"{execution_id}.json"

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    return str(file_path)


# ============================================================
# Markdown 报告生成
# ============================================================

def generate_human_report(
    execution_record: Dict[str, Any],
    test_result: Dict[str, Any],
    shared_dir: str
) -> str:
    """
    生成人可读的 Markdown 测试报告。

    报告结构：
    1. 标题和基本信息
    2. 环境信息
    3. 预置条件检查结果
    4. 步骤执行详情（含 raw_stdout 和 raw_stderr）
    5. 判断结果

    Args:
        execution_record: ExecutionRecord 字典
        test_result: TestResult 字典
        shared_dir: 共享目录根路径

    Returns:
        报告文件路径
    """
    execution_id = execution_record.get("execution_id", "unknown")
    report_path = Path(shared_dir) / "reports" / f"{execution_id}.md"

    lines = []

    # ---------- 标题 ----------
    case_name = execution_record.get("case_name", "未知用例")
    overall_result = test_result.get("overall_result", "-")
    result_icon = "[PASS]" if overall_result == "PASS" else "[FAIL]"

    lines.append(f"# {result_icon} 测试报告: {case_name}")
    lines.append("")

    # ---------- 基本信息 ----------
    lines.append("## 基本信息")
    lines.append("")
    lines.append(f"| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 用例 ID | `{execution_record.get('case_id', '-')}` |")
    lines.append(f"| 用例名称 | {case_name} |")
    lines.append(f"| 执行 ID | `{execution_id}` |")
    lines.append(f"| 整体结果 | **{overall_result}** |")
    lines.append(f"| 置信度 | {test_result.get('confidence', '-'):.2f} |")
    lines.append(f"| 开始时间 | {execution_record.get('started_at', '-')} |")
    lines.append(f"| 完成时间 | {execution_record.get('completed_at', '-')} |")
    lines.append("")

    # ---------- 环境信息 ----------
    lines.append("## 环境信息")
    lines.append("")
    env = execution_record.get("environment", {})
    if env:
        lines.append(f"| 配置项 | 值 |")
        lines.append("|--------|-----|")
        for key, value in env.items():
            # 敏感信息脱敏
            if "password" in key.lower() or "pwd" in key.lower():
                value = "******"
            lines.append(f"| {key} | `{value}` |")
        lines.append("")
    else:
        lines.append("_无环境信息_")
        lines.append("")

    # ---------- 预置条件检查 ----------
    lines.append("## 预置条件检查")
    lines.append("")
    prerequisites = execution_record.get("prerequisites", [])
    if prerequisites:
        lines.append(f"| 条件 | 状态 | 详情 |")
        lines.append("|------|------|------|")
        for prereq in prerequisites:
            name = prereq.get("name", "-")
            status = prereq.get("status", "-")
            status_icon = "[OK]" if status == "completed" else "[FAIL]"
            details = prereq.get("details", "-")
            lines.append(f"| {name} | {status_icon} {status} | {details} |")
        lines.append("")
    else:
        lines.append("_无预置条件_")
        lines.append("")

    # ---------- 步骤执行详情 ----------
    lines.append("## 步骤执行详情")
    lines.append("")

    steps = execution_record.get("steps", [])
    step_results = {sr.get("step_id"): sr for sr in test_result.get("step_results", [])}

    for step in steps:
        step_id = step.get("step_id", "-")
        description = step.get("description", "-")
        tool = step.get("tool", "-")
        status = step.get("status", "-")
        status_icon = "[OK]" if status == "completed" else "[FAIL]"

        # 步骤标题
        lines.append(f"### Step {step_id}: {description}")
        lines.append("")

        # 步骤元信息
        lines.append(f"- **工具**: `{tool}`")
        lines.append(f"- **状态**: {status_icon} {status}")

        # 判断结果（如有）
        step_result = step_results.get(step_id, {})
        if step_result:
            result = step_result.get("result", "-")
            reason = step_result.get("reason", "-")
            result_icon = "[PASS]" if result == "PASS" else "[FAIL]"
            lines.append(f"- **判断**: {result_icon} **{result}** - {reason}")

        lines.append("")

        # 执行的命令
        command = step.get("command")
        if command:
            lines.append("**执行的命令:**")
            lines.append("```")
            lines.append(command)
            lines.append("```")
            lines.append("")

        # 预期与实际
        expected = step.get("expected")
        actual = step.get("actual")
        if expected is not None or actual is not None:
            lines.append("| 预期 | 实际 |")
            lines.append("|------|------|")
            lines.append(f"| `{expected}` | `{actual}` |")
            lines.append("")

        # 完整标准输出（核心内容）
        raw_stdout = step.get("raw_stdout")
        if raw_stdout:
            lines.append("**标准输出 (raw_stdout):**")
            lines.append("```")
            lines.append(str(raw_stdout))
            lines.append("```")
            lines.append("")

        # 完整标准错误（核心内容）
        raw_stderr = step.get("raw_stderr")
        if raw_stderr:
            lines.append("**标准错误 (raw_stderr):**")
            lines.append("```")
            lines.append(str(raw_stderr))
            lines.append("```")
            lines.append("")

        # 错误信息
        error_message = step.get("error_message")
        if error_message:
            lines.append(f"**错误信息:** {error_message}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ---------- 判断说明 ----------
    judge_notes = test_result.get("judge_notes", [])
    if judge_notes:
        lines.append("## 判断说明")
        lines.append("")
        for note in judge_notes:
            lines.append(f"- {note}")
        lines.append("")

    # ---------- 环境恢复状态 ----------
    env_recovery = test_result.get("environment_recovery", {})
    if env_recovery:
        lines.append("## 环境恢复")
        lines.append("")
        recovered = env_recovery.get("recovered", False)
        status_text = "[OK] 已恢复" if recovered else "[WARN] 未完全恢复"
        lines.append(f"**状态**: {status_text}")
        warnings = env_recovery.get("warnings", [])
        if warnings:
            lines.append("")
            lines.append("**警告:**")
            for warning in warnings:
                lines.append(f"- {warning}")
        lines.append("")

    # 写入文件
    report_content = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return str(report_path)


# ============================================================
# 批量报告生成
# ============================================================

def generate_batch_summary_report(
    execution_records: List[Dict[str, Any]],
    test_results: List[Dict[str, Any]],
    shared_dir: str
) -> str:
    """
    生成批量执行汇总报告。

    Args:
        execution_records: ExecutionRecord 列表
        test_results: TestResult 列表
        shared_dir: 共享目录根路径

    Returns:
        汇总报告文件路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(shared_dir) / "reports" / f"batch_summary_{timestamp}.md"

    lines = []
    lines.append("# 批量执行汇总报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().isoformat()}")
    lines.append(f"**用例总数**: {len(execution_records)}")
    lines.append("")

    # 统计
    passed = sum(1 for r in test_results if r.get("overall_result") == "PASS")
    failed = len(test_results) - passed

    lines.append("## 执行统计")
    lines.append("")
    lines.append(f"| 结果 | 数量 |")
    lines.append(f"|------|------|")
    lines.append(f"| [PASS] | {passed} |")
    lines.append(f"| [FAIL] | {failed} |")
    lines.append("")

    # 用例列表
    lines.append("## 用例详情")
    lines.append("")
    lines.append(f"| # | 用例名称 | 结果 | 置信度 |")
    lines.append(f"|---|----------|------|--------|")

    for i, (exec_rec, test_res) in enumerate(zip(execution_records, test_results)):
        case_name = exec_rec.get("case_name", "-")
        result = test_res.get("overall_result", "-")
        confidence = test_res.get("confidence", 0)
        result_icon = "[PASS]" if result == "PASS" else "[FAIL]"
        lines.append(f"| {i+1} | {case_name} | {result_icon} {result} | {confidence:.2f} |")

    lines.append("")

    # 写入文件
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return str(report_path)
