# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 文件处理工具模块

负责文件读写和报告生成，是主流程打通的关键模块。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from src.core.schemas import ExecutionRecord, TestResult, Evidence


# ============================================================
# 目录管理
# ============================================================

def ensure_shared_dirs(shared_dir: str) -> None:
    """
    确保共享目录结构存在。

    创建以下子目录：execution_records, test_results, evidence, reports

    Args:
        shared_dir: 共享目录根路径
    """
    root = Path(shared_dir)
    for subdir in ["execution_records", "test_results", "evidence", "reports"]:
        (root / subdir).mkdir(parents=True, exist_ok=True)


# ============================================================
# ExecutionRecord 保存
# ============================================================

def save_execution_record(record: ExecutionRecord, shared_dir: str) -> str:
    """
    保存 ExecutionRecord 到 JSON 文件，并导出证据文件。

    Args:
        record: ExecutionRecord 对象
        shared_dir: 共享目录根路径

    Returns:
        保存的 JSON 文件路径
    """
    root = Path(shared_dir)
    execution_id = record.execution_id
    json_path = root / "execution_records" / f"{execution_id}.json"

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2, default=str)
        print(f"[OK] ExecutionRecord 已保存: {json_path}")
    except Exception as e:
        print(f"[ERROR] 保存 ExecutionRecord 失败: {e}")
        return ""

    # 导出证据文件
    evidence_dir = root / "evidence" / execution_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for step in record.steps:
        for evidence in step.evidence:
            _save_evidence_file(evidence, evidence_dir)

    return str(json_path)


def _save_evidence_file(evidence: Evidence, evidence_dir: Path) -> None:
    """保存单个证据文件"""
    content = evidence.content
    if not content or not isinstance(content, str) or len(content) > 100 * 1024:
        return

    file_path = evidence_dir / f"{evidence.evidence_id}.txt"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"[WARN] 证据保存失败: {evidence.evidence_id} - {e}")


# ============================================================
# TestResult 保存
# ============================================================

def save_test_result(result: TestResult, shared_dir: str) -> str:
    """
    保存 TestResult 到 JSON 文件。

    Args:
        result: TestResult 对象
        shared_dir: 共享目录根路径

    Returns:
        保存的文件路径
    """
    root = Path(shared_dir)
    json_path = root / "test_results" / f"{result.execution_id}.json"

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.model_dump(mode="json"), f, ensure_ascii=False, indent=2, default=str)
        print(f"[OK] TestResult 已保存: {json_path}")
        return str(json_path)
    except Exception as e:
        print(f"[ERROR] 保存 TestResult 失败: {e}")
        return ""


# ============================================================
# Markdown 报告生成
# ============================================================

def generate_human_report(
    execution_record: ExecutionRecord,
    test_result: TestResult,
    shared_dir: str
) -> str:
    """
    生成人可读的 Markdown 测试报告。

    报告结构：
    1. 标题
    2. 执行基本信息
    3. 环境信息
    4. 预置条件检查
    5. 步骤详情（核心：完整展示 raw_stdout 和 raw_stderr）
    6. 判断结果
    7. 生成时间

    Args:
        execution_record: ExecutionRecord 对象
        test_result: TestResult 对象
        shared_dir: 共享目录根路径

    Returns:
        报告文件路径
    """
    root = Path(shared_dir)
    report_path = root / "reports" / f"{execution_record.execution_id}.md"

    lines = []
    case_name = execution_record.case_name
    overall_result = test_result.overall_result
    result_icon = "[PASS]" if overall_result == "PASS" else "[FAIL]"

    # ==================== 标题 ====================
    lines.append(f"# 测试报告 - {case_name}")
    lines.append("")
    lines.append(f"**整体结果**: {result_icon} **{overall_result}**  |  **置信度**: {test_result.confidence:.2f}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ==================== 执行基本信息 ====================
    lines.append("## 执行基本信息")
    lines.append("")
    lines.append(f"- **执行 ID**: `{execution_record.execution_id}`")
    lines.append(f"- **用例 ID**: `{execution_record.case_id}`")
    lines.append(f"- **开始时间**: {_fmt(execution_record.started_at)}")
    lines.append(f"- **完成时间**: {_fmt(execution_record.completed_at)}")
    lines.append("")

    # ==================== 环境信息 ====================
    lines.append("## 环境信息")
    lines.append("")
    env = execution_record.environment
    if env:
        for key, value in env.items():
            if _is_sensitive(key):
                value = "******"
            lines.append(f"- **{key}**: `{value}`")
    else:
        lines.append("_无环境信息_")
    lines.append("")

    # ==================== 预置条件检查 ====================
    lines.append("## 预置条件检查")
    lines.append("")
    prerequisites = execution_record.prerequisites
    if prerequisites:
        for prereq in prerequisites:
            name = prereq.get("name", "-")
            status = prereq.get("status", "-")
            details = prereq.get("details", "")
            icon = "[OK]" if status == "completed" else "[FAIL]"
            lines.append(f"- {icon} **{name}**: {status}")
            if details:
                lines.append(f"  - 详情: {details}")
    else:
        lines.append("_无预置条件_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ==================== 步骤详情（核心）====================
    lines.append("## 步骤执行详情")
    lines.append("")

    # 构建判断结果映射
    judgments = {sr.step_id: sr for sr in test_result.step_results}

    for step in execution_record.steps:
        # ----- 步骤标题 -----
        desc = step.description or "无描述"
        lines.append(f"### Step {step.step_id}: {desc}")
        lines.append("")

        # ----- 步骤元信息 -----
        status_icon = "[OK]" if step.status.value == "completed" else "[FAIL]"
        lines.append(f"| 项目 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 状态 | {status_icon} `{step.status.value}` |")
        lines.append(f"| 工具 | `{step.tool}` |")
        lines.append(f"| 接口偏好 | `{step.interface_preference}` |")
        if step.endpoint:
            method = step.method or "GET"
            lines.append(f"| 端点 | `{method} {step.endpoint}` |")
        lines.append("")

        # ----- 执行的命令 -----
        if step.command:
            lines.append("**执行的命令:**")
            lines.append("```text")
            lines.append(step.command)
            lines.append("```")
            lines.append("")

        # ----- 标准输出 -----
        lines.append("**标准输出 (raw_stdout):**")
        lines.append("```text")
        lines.append(step.raw_stdout if step.raw_stdout else "（无输出）")
        lines.append("```")
        lines.append("")

        # ----- 标准错误 -----
        lines.append("**标准错误 (raw_stderr):**")
        lines.append("```text")
        lines.append(step.raw_stderr if step.raw_stderr else "（无错误输出）")
        lines.append("```")
        lines.append("")

        # ----- 错误信息 -----
        if step.error_message:
            lines.append(f"**错误信息**: {step.error_message}")
            lines.append("")

        # ----- 判断结果 -----
        judgment = judgments.get(step.step_id)
        if judgment:
            j_icon = "[PASS]" if judgment.result == "PASS" else "[FAIL]"
            lines.append(f"**判断结果**: {j_icon} **{judgment.result}** (置信度: {judgment.confidence:.2f})")
            lines.append(f"- **理由**: {judgment.reason}")
            if judgment.concerns:
                lines.append(f"- **关注点**: {', '.join(judgment.concerns)}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ==================== 判断结果汇总 ====================
    lines.append("## 判断结果汇总")
    lines.append("")
    lines.append(f"- **整体结果**: {result_icon} **{overall_result}**")
    lines.append(f"- **置信度**: {test_result.confidence:.2f}")

    if test_result.judge_notes:
        lines.append("")
        lines.append("**判断说明:**")
        for note in test_result.judge_notes:
            lines.append(f"- {note}")

    # 环境恢复
    env_recovery = test_result.environment_recovery
    if env_recovery:
        lines.append("")
        recovered = env_recovery.get("recovered", False)
        icon = "[OK]" if recovered else "[WARN]"
        lines.append(f"- **环境恢复**: {icon} {'已恢复' if recovered else '未完全恢复'}")
        warnings = env_recovery.get("warnings", [])
        for w in warnings:
            lines.append(f"  - {w}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # ==================== 生成时间 ====================
    lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    # 写入文件
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[OK] 测试报告已生成: {report_path}")
        return str(report_path)
    except Exception as e:
        print(f"[ERROR] 生成报告失败: {e}")
        return ""


# ============================================================
# 辅助函数
# ============================================================

def _fmt(dt: Optional[datetime]) -> str:
    """格式化时间"""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_sensitive(field_name: str) -> bool:
    """判断是否为敏感字段"""
    keywords = ["password", "passwd", "pwd", "secret", "token", "key"]
    return any(kw in field_name.lower() for kw in keywords)
