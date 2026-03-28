# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 文件处理工具模块

负责文件读写和报告生成，是主流程打通的关键模块。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from src.core.schemas import ExecutionRecord, TestResult, Evidence


# ============================================================
# 目录管理
# ============================================================

def ensure_shared_dirs(shared_dir: str) -> None:
    """
    确保共享目录结构存在。

    创建以下子目录：
    - execution_records/ : ExecutionRecord JSON 文件
    - test_results/      : TestResult JSON 文件
    - evidence/          : 证据文件（按 execution_id 组织）
    - reports/           : Markdown 报告

    Args:
        shared_dir: 共享目录根路径
    """
    root = Path(shared_dir)
    subdirs = ["execution_records", "test_results", "evidence", "reports"]

    for subdir in subdirs:
        (root / subdir).mkdir(parents=True, exist_ok=True)


# ============================================================
# ExecutionRecord 保存
# ============================================================

def save_execution_record(record: ExecutionRecord, shared_dir: str) -> str:
    """
    保存 ExecutionRecord 到 JSON 文件，并导出证据文件。

    保存路径：
    - JSON: {shared_dir}/execution_records/{execution_id}.json
    - 证据: {shared_dir}/evidence/{execution_id}/{evidence_id}.txt

    Args:
        record: ExecutionRecord 对象
        shared_dir: 共享目录根路径

    Returns:
        保存的 JSON 文件路径
    """
    root = Path(shared_dir)
    execution_id = record.execution_id

    # 保存 JSON 文件
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
    """
    保存单个证据文件（内部函数）。

    只保存文本类型的证据，二进制证据暂不处理。
    """
    content = evidence.content
    if not content or not isinstance(content, str):
        return

    ext = _get_evidence_extension(evidence.evidence_type)
    file_path = evidence_dir / f"{evidence.evidence_id}{ext}"

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[OK] 证据已保存: {file_path.name}")
    except Exception as e:
        print(f"[WARN] 证据保存失败: {evidence.evidence_id} - {e}")


def _get_evidence_extension(evidence_type: str) -> str:
    """根据证据类型获取文件扩展名"""
    type_map = {
        "redfish_response": ".json",
        "cli_output": ".txt",
        "ipmi_output": ".txt",
        "ssh_output": ".txt",
        "http_response": ".json",
    }
    return type_map.get(evidence_type, ".txt")


# ============================================================
# TestResult 保存
# ============================================================

def save_test_result(result: TestResult, shared_dir: str) -> str:
    """
    保存 TestResult 到 JSON 文件。

    保存路径：{shared_dir}/test_results/{execution_id}.json

    Args:
        result: TestResult 对象
        shared_dir: 共享目录根路径

    Returns:
        保存的文件路径
    """
    root = Path(shared_dir)
    execution_id = result.execution_id
    json_path = root / "test_results" / f"{execution_id}.json"

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

    保存路径：{shared_dir}/reports/{execution_id}.md

    报告结构：
    1. 标题：测试报告 - {case_name}
    2. 执行基本信息（ID、时间、总体结果）
    3. 环境信息（过滤密码字段）
    4. 预置条件检查结果
    5. 步骤详情（含 raw_stdout、raw_stderr）
    6. 判断结果和 judge_notes
    7. 生成时间

    Args:
        execution_record: ExecutionRecord 对象
        test_result: TestResult 对象
        shared_dir: 共享目录根路径

    Returns:
        报告文件路径
    """
    root = Path(shared_dir)
    execution_id = execution_record.execution_id
    report_path = root / "reports" / f"{execution_id}.md"

    lines = []

    # ==================== 标题 ====================
    case_name = execution_record.case_name
    overall_result = test_result.overall_result
    result_mark = "**[PASS]**" if overall_result == "PASS" else "**[FAIL]**"

    lines.append(f"# 测试报告: {case_name}")
    lines.append("")
    lines.append(f"整体结果: {result_mark}")
    lines.append("")

    # ==================== 基本信息 ====================
    lines.append("## 基本信息")
    lines.append("")
    lines.append(f"| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 用例 ID | `{execution_record.case_id}` |")
    lines.append(f"| 用例名称 | {case_name} |")
    lines.append(f"| 执行 ID | `{execution_id}` |")
    lines.append(f"| 整体结果 | {overall_result} |")
    lines.append(f"| 置信度 | {test_result.confidence:.2f} |")
    lines.append(f"| 开始时间 | {_format_time(execution_record.started_at)} |")
    lines.append(f"| 完成时间 | {_format_time(execution_record.completed_at)} |")
    lines.append("")

    # ==================== 环境信息 ====================
    lines.append("## 环境信息")
    lines.append("")
    env = execution_record.environment
    if env:
        lines.append(f"| 配置项 | 值 |")
        lines.append("|--------|-----|")
        for key, value in env.items():
            # 敏感信息脱敏
            if _is_sensitive_field(key):
                value = "******"
            lines.append(f"| {key} | `{value}` |")
        lines.append("")
    else:
        lines.append("_无环境信息_")
        lines.append("")

    # ==================== 预置条件检查 ====================
    lines.append("## 预置条件检查")
    lines.append("")
    prerequisites = execution_record.prerequisites
    if prerequisites:
        lines.append(f"| 条件 | 状态 | 详情 |")
        lines.append("|------|------|------|")
        for prereq in prerequisites:
            name = prereq.get("name", "-")
            status = prereq.get("status", "-")
            details = prereq.get("details", "-")
            status_mark = "[OK]" if status == "completed" else "[FAIL]"
            lines.append(f"| {name} | {status_mark} {status} | {details} |")
        lines.append("")
    else:
        lines.append("_无预置条件_")
        lines.append("")

    # ==================== 步骤执行详情 ====================
    lines.append("## 步骤执行详情")
    lines.append("")

    # 构建步骤判断结果映射
    step_judgments = {sr.step_id: sr for sr in test_result.step_results}

    for step in execution_record.steps:
        # 步骤标题
        lines.append(f"### Step {step.step_id}: {step.description}")
        lines.append("")

        # 步骤元信息
        status_mark = "[OK]" if step.status.value == "completed" else "[FAIL]"
        lines.append(f"- **工具**: `{step.tool}`")
        lines.append(f"- **接口偏好**: `{step.interface_preference}`")
        lines.append(f"- **状态**: {status_mark} {step.status.value}")

        # Redfish 相关信息
        if step.endpoint:
            method = step.method or "GET"
            lines.append(f"- **端点**: `{method} {step.endpoint}`")

        # 判断结果
        judgment = step_judgments.get(step.step_id)
        if judgment:
            j_mark = "[PASS]" if judgment.result == "PASS" else "[FAIL]"
            lines.append(f"- **判断**: {j_mark} **{judgment.result}** (置信度: {judgment.confidence:.2f})")
            lines.append(f"- **理由**: {judgment.reason}")
            if judgment.concerns:
                lines.append(f"- **关注点**: {', '.join(judgment.concerns)}")

        lines.append("")

        # 执行的命令
        if step.command:
            lines.append("**执行的命令:**")
            lines.append("```bash")
            lines.append(step.command)
            lines.append("```")
            lines.append("")

        # 完整标准输出（完整显示，不截断）
        if step.raw_stdout:
            lines.append("**标准输出 (raw_stdout):**")
            lines.append("```")
            lines.append(step.raw_stdout)
            lines.append("```")
            lines.append("")

        # 完整标准错误（完整显示，不截断）
        if step.raw_stderr:
            lines.append("**标准错误 (raw_stderr):**")
            lines.append("```")
            lines.append(step.raw_stderr)
            lines.append("```")
            lines.append("")

        # 错误信息
        if step.error_message:
            lines.append(f"**错误信息**: {step.error_message}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ==================== 判断说明 ====================
    if test_result.judge_notes:
        lines.append("## 判断说明")
        lines.append("")
        for note in test_result.judge_notes:
            lines.append(f"- {note}")
        lines.append("")

    # ==================== 环境恢复状态 ====================
    env_recovery = test_result.environment_recovery
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

    # ==================== 生成时间 ====================
    lines.append("---")
    lines.append("")
    lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    # 写入文件
    try:
        report_content = "\n".join(lines)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        print(f"[OK] 测试报告已生成: {report_path}")
        return str(report_path)
    except Exception as e:
        print(f"[ERROR] 生成报告失败: {e}")
        return ""


# ============================================================
# 辅助函数
# ============================================================

def _format_time(dt: Optional[datetime]) -> str:
    """格式化时间显示"""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_sensitive_field(field_name: str) -> bool:
    """判断是否为敏感字段"""
    sensitive_keywords = ["password", "passwd", "pwd", "secret", "token", "key"]
    field_lower = field_name.lower()
    return any(kw in field_lower for kw in sensitive_keywords)
