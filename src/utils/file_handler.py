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

    创建子目录：execution_records, test_results, evidence, reports

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
    保存 ExecutionRecord 到 JSON 文件，    """
    root = Path(shared_dir)
    json_path = root / "execution_records" / f"{record.execution_id}.json"

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2, default=str)
        print(f"[OK] ExecutionRecord 已保存: {json_path}")
    except Exception as e:
        print(f"[ERROR] 保存 ExecutionRecord 失败: {e}")
        return ""

    # 导出证据文件
    evidence_dir = root / "evidence" / record.execution_id
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
    保存 TestResult 到 JSON 文件
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
    生成人可读的 Markdown 测试报告
    """
    ensure_shared_dirs(shared_dir)

    report_path = Path(shared_dir) / "reports" / f"{execution_record.execution_id}.md"

    with open(report_path, "w", encoding="utf-8") as f:
        # ==================== 标题和基本信息 ====================
        f.write(f"# 测试报告 - {execution_record.case_name}\n\n")
        f.write(f"**执行 ID**: `{execution_record.execution_id}`\n")
        f.write(f"**用例 ID**: `{execution_record.case_id}`\n")
        f.write(f"**执行时间**: {_fmt(execution_record.started_at)}\n")
        f.write(f"**总体结果**: **{test_result.overall_result}**\n")
        f.write(f"**置信度**: {test_result.confidence:.2f}\n\n")

        # ==================== 环境信息 ====================
        f.write("## 环境信息\n\n")
        env = execution_record.environment
        if env:
            for key, value in env.items():
                if _is_sensitive(key):
                    value = "******"
                f.write(f"- **{key}**: `{value}`\n")
        else:
            f.write("_无环境信息_\n")
        f.write("\n")

        # ==================== 预置条件检查 ====================
        f.write("## 预置条件检查\n\n")
        if execution_record.prerequisites:
            for prereq in execution_record.prerequisites:
                name = prereq.get("name", "-")
                status = prereq.get("status", "-")
                details = prereq.get("details", "")
                icon = "[OK]" if status == "completed" else "[FAIL]"
                f.write(f"- {icon} **{name}**: {status}\n")
                if details:
                    f.write(f"  - 详情: {details}\n")
        else:
            f.write("_无预置条件_\n")
        f.write("\n---\n\n")

        # ==================== 步骤详情（核心）====================
        f.write("## 执行步骤详情\n\n")

        for step in execution_record.steps:
            # 步骤标题
            f.write(f"### Step {step.step_id}: {step.description or '无描述'}\n\n")

            # 步骤元信息
            f.write(f"- **工具**: `{step.tool}`\n")
            f.write(f"- **接口偏好**: `{step.interface_preference}`\n")

            if step.endpoint:
                method = step.method or "GET"
                f.write(f"- **端点**: `{method} {step.endpoint}`\n")

            if step.command:
                f.write(f"- **执行命令**: `{step.command}`\n")

            # 状态
            status_text = step.status.value if hasattr(step.status, 'value') else str(step.status)
            f.write(f"- **状态**: **{status_text}**\n\n")

            # 原始输出（核心）
            if step.raw_stdout:
                f.write("**原始输出 (stdout):**\n")
                f.write("```text\n")
                f.write(step.raw_stdout.strip() + "\n")
                f.write("```\n\n")
            else:
                f.write("**原始输出 (stdout):** _无输出_\n\n")

            # 原始错误
            if step.raw_stderr:
                f.write("**原始错误 (stderr):**\n")
                f.write("```text\n")
                f.write(step.raw_stderr.strip() + "\n")
                f.write("```\n\n")
            else:
                f.write("**原始错误 (stderr):** _无错误输出_\n\n")

            # 错误信息
            if step.error_message:
                f.write(f"**错误信息**: {step.error_message}\n\n")

        # ==================== 判断结果 ====================
        f.write("## 判断结果\n\n")
        f.write(f"**总体结论**: **{test_result.overall_result}**\n\n")

        if test_result.judge_notes:
            f.write("**判断说明:**\n")
            for note in test_result.judge_notes:
                f.write(f"- {note}\n")
            f.write("\n")

        # 单步判断详情
        if test_result.step_results:
            f.write("### 单步判断详情\n\n")
            for sr in test_result.step_results:
                icon = "[PASS]" if sr.result == "PASS" else "[FAIL]"
                f.write(f"- {icon} **Step {sr.step_id}**: {sr.result} - {sr.reason}\n")
                if sr.concerns:
                    f.write(f"  - 关注点: {', '.join(sr.concerns)}\n")
            f.write("\n")

        # 环境恢复
        env_recovery = test_result.environment_recovery
        if env_recovery:
            recovered = env_recovery.get("recovered", False)
            icon = "[OK]" if recovered else "[WARN]"
            f.write(f"**环境恢复**: {icon} {'已恢复' if recovered else '未完全恢复'}\n")
            warnings = env_recovery.get("warnings", [])
            if warnings:
                for w in warnings:
                    f.write(f"  - {w}\n")
            f.write("\n")

        # ==================== 生成时间 ====================
        f.write("---\n\n")
        f.write(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

    print(f"[OK] Markdown 测试报告已生成 -> {report_path}")
    return str(report_path)


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
