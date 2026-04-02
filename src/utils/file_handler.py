# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 文件处理工具模块

负责文件读写和报告生成，是主流程打通的关键模块。
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.schemas import ExecutionRecord, TestResult, Evidence

import yaml

logger = logging.getLogger("file_handler")


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
    生成人可读的 Markdown 测试报告（人工审计友好格式）
    """
    ensure_shared_dirs(shared_dir)

    report_path = Path(shared_dir) / "reports" / f"{execution_record.execution_id}.md"

    with open(report_path, "w", encoding="utf-8") as f:
        # ==================== 头部 ====================
        f.write(f"# 测试报告 - {execution_record.case_name}\n\n")
        f.write(f"**执行 ID**: {execution_record.execution_id}  \n")
        f.write(f"**用例 ID**: {execution_record.case_id}  \n")
        f.write(f"**执行时间**: {_fmt(execution_record.started_at)}  \n")
        overall = test_result.overall_result
        conf = test_result.confidence
        f.write(f"**总体结果**: **{overall}** (置信度: {conf:.2f})\n\n")

        # ==================== 环境信息（表格）====================
        f.write("## 环境信息\n\n")
        env = execution_record.environment
        if env:
            f.write("| 项目 | 值 |\n")
            f.write("|------|----|\n")
            label_map = {
                "bmc_host": "BMC 地址", "bmc_ip": "BMC 地址",
                "bmc_port": "BMC 端口", "redfish_port": "BMC 端口",
                "bmc_user": "BMC 用户", "bmc_username": "BMC 用户",
            }
            for key, value in env.items():
                if _is_sensitive(key):
                    value = "******"
                label = label_map.get(key, key)
                f.write(f"| {label} | {value} |\n")
            # 补充协议行
            port = env.get("bmc_port", env.get("redfish_port", 443))
            proto = "HTTPS (SSL 校验关闭)" if str(port) != "80" else "HTTP"
            f.write(f"| 协议 | {proto} |\n")
        else:
            f.write("_无环境信息_\n")
        f.write("\n")

        # ==================== 预置条件检查 ====================
        f.write("## 预置条件检查\n\n")
        if execution_record.prerequisites:
            for prereq in execution_record.prerequisites:
                name = prereq.get("name", "-")
                status_raw = prereq.get("status", "-")
                status_label = "**PASS**" if status_raw in ("completed", "checked", "pass") else f"**FAIL** ({status_raw})"
                f.write(f"- {name}: {status_label}\n")
        else:
            f.write("_无预置条件_\n")
        f.write("\n")

        # ==================== 执行步骤详情 ====================
        f.write("## 执行步骤详情\n\n")

        for step in execution_record.steps:
            # 步骤编号：step_001 -> 001
            step_num = step.step_id.replace("step_", "").lstrip("0") or "1"
            f.write(f"### Step {step_num} - {step.description or '无描述'}\n\n")

            f.write(f"- **工具**: {step.tool}\n")
            f.write(f"- **接口**: {step.interface_preference}\n")

            if step.endpoint:
                method = step.method or "GET"
                f.write(f"- **命令**: {method} {step.endpoint}\n")
            elif step.command:
                f.write(f"- **命令**: {step.command}\n")

            status_text = step.status.value if hasattr(step.status, 'value') else str(step.status)
            f.write(f"- **状态**: **{status_text}**\n\n")

            # 原始输出：提取 Redfish body（去掉 httpx 包装层）
            stdout_text = _extract_response_body(step.raw_stdout)
            if stdout_text:
                f.write("**原始输出 (stdout)**:\n")
                f.write("```json\n")
                f.write(stdout_text + "\n")
                f.write("```\n\n")

            # 原始错误
            if step.raw_stderr:
                f.write("**原始错误 (stderr)**:\n")
                f.write("```text\n")
                f.write(step.raw_stderr.strip() + "\n")
                f.write("```\n\n")

            if step.error_message:
                f.write(f"**错误信息**: {step.error_message}\n\n")

        # ==================== 判断结果 ====================
        f.write("## 判断结果\n\n")
        f.write(f"**总体结论**: **{test_result.overall_result}**\n\n")

        if test_result.step_results:
            f.write("| 步骤 | 结果 | 置信度 | 判断理由 | 关注点 |\n")
            f.write("|------|------|--------|----------|--------|\n")
            for sr in test_result.step_results:
                step_num = sr.step_id.replace("step_", "").lstrip("0") or "1"
                concerns = ", ".join(sr.concerns) if sr.concerns else "-"
                f.write(
                    f"| Step {step_num} | **{sr.result}** | {sr.confidence:.2f} "
                    f"| {sr.reason} | {concerns} |\n"
                )
            f.write("\n")

        if test_result.judge_notes:
            f.write("**判断说明:**\n")
            for note in test_result.judge_notes:
                f.write(f"- {note}\n")
            f.write("\n")

        # 环境恢复
        env_recovery = test_result.environment_recovery
        if env_recovery:
            recovered = env_recovery.get("recovered", False)
            status_label = "已恢复" if recovered else "未完全恢复"
            f.write(f"**环境恢复**: {status_label}\n")
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


def _extract_response_body(raw_stdout: Optional[str]) -> Optional[str]:
    """
    从 raw_stdout 中提取实际响应体。

    如果 raw_stdout 是 httpx 包装格式（含 http_status/headers/body），
    则只提取 body 部分。否则原样返回。
    """
    if not raw_stdout:
        return None

    text = raw_stdout.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        # httpx 包装格式：{"http_status": ..., "headers": ..., "body": ...}
        if isinstance(data, dict) and "body" in data and "http_status" in data:
            body = data["body"]
            return json.dumps(body, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        pass

    return text


# ============================================================
# Excel -> YAML 用例转换
# ============================================================

# Excel 列名 -> 标准 YAML 字段名的映射
_EXCEL_COLUMN_MAP: Dict[str, List[str]] = {
    "用例_编号": ["用例_编号", "用例编号", "编号", "case_id", "id"],
    "用例_名称": ["用例_名称", "用例名称", "名称", "case_name", "title", "测试名称"],
    "测试类型": ["测试类型", "类型", "test_type", "type"],
    "优先级": ["优先级", "priority", "级别"],
    "预置条件": ["预置条件", "前置条件", "前提条件", "prerequisites", " precondition"],
    "测试步骤": ["测试步骤", "步骤", "steps", "step", "操作步骤", "测试操作"],
    "预期结果": ["预期结果", "期望结果", "expected", "期望", "预期"],
    "notes": ["notes", "备注", "说明", "note", "注释"],
}


def _map_excel_columns(df: "pd.DataFrame") -> Dict[str, str]:
    """
    将 DataFrame 实际列名映射到标准字段名。

    返回: {"用例_编号": "实际列名", ...}
    """
    mapping: Dict[str, str] = {}
    actual_cols = list(df.columns)

    for std_name, aliases in _EXCEL_COLUMN_MAP.items():
        for alias in aliases:
            for col in actual_cols:
                if col.strip().lower() == alias.strip().lower():
                    mapping[std_name] = col
                    break
            if std_name in mapping:
                break

    return mapping


def _safe_filename(text: str) -> str:
    """生成安全的文件名（保留字母、数字、下划线、连字符、中文）"""
    safe = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', text)
    return safe[:80] if len(safe) > 80 else safe


class _BlockScalarStr(str):
    """标记字符串，让 YAML dumper 使用 | 块标量输出（非 |-）"""
    def __new__(cls, value):
        # 确保以换行结尾，这样 YAML 输出为 | 而非 |-
        if value and not value.endswith('\n'):
            value = value + '\n'
        return super().__new__(cls, value)


def _represent_block_scalar(dumper: yaml.Dumper, data: _BlockScalarStr):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')


_YamlDumper = type(
    "YamlDumper",
    (yaml.Dumper,),
    {"increase_indent": lambda self, flow=False, indentless=False: super(type(self), self).increase_indent(flow, False)},
)
_YamlDumper.add_representer(_BlockScalarStr, _represent_block_scalar)


def _cell_text(row, col_name: str) -> str:
    """安全提取单元格文本，NaN 返回空字符串"""
    if col_name not in row.index:
        return ""
    val = row[col_name]
    if val is None or (isinstance(val, float) and val != val):  # NaN check
        return ""
    return str(val).strip()


def _build_case_dict(row, col_map: Dict[str, str], row_index: int) -> Dict[str, Any]:
    """
    将 DataFrame 一行转换为 YAML 用例字典。

    预置条件/测试步骤/预期结果 使用 | 块标量保留原始自然文本。
    """
    def _get(key: str) -> str:
        actual_col = col_map.get(key)
        return _cell_text(row, actual_col) if actual_col else ""

    # 用例_编号（必须）
    case_id = _get("用例_编号") or f"TC-EXCEL-{row_index + 1:03d}"

    # 用例_名称
    case_name = _get("用例_名称") or case_id

    # 测试类型 / 优先级
    test_type = _get("测试类型") or "功能测试"
    priority = _get("优先级") or "P1"

    # 预置条件（块标量）
    preconditions_raw = _get("预置条件")
    preconditions = _BlockScalarStr(preconditions_raw) if preconditions_raw else ""

    # 测试步骤（块标量）
    steps_raw = _get("测试步骤")
    steps = _BlockScalarStr(steps_raw) if steps_raw else ""

    # 预期结果（块标量）
    expected_raw = _get("预期结果")
    expected = _BlockScalarStr(expected_raw) if expected_raw else ""

    # notes
    notes = _get("notes")

    case: Dict[str, Any] = {
        "用例_编号": case_id,
        "用例_名称": case_name,
        "测试类型": test_type,
        "优先级": priority,
    }
    if preconditions:
        case["预置条件"] = preconditions
    if steps:
        case["测试步骤"] = steps
    if expected:
        case["预期结果"] = expected
    if notes:
        case["notes"] = notes

    return case


def _convert_sheet(
    df: "pd.DataFrame",
    sheet_name: str,
    output_dir: Path,
) -> List[str]:
    """转换单个 Sheet，返回生成的文件路径列表"""
    import pandas as pd

    col_map = _map_excel_columns(df)
    logger.info("Sheet '%s': 检测到列映射 %s", sheet_name, col_map)

    # 校验必填列
    _REQUIRED_COLUMNS = ["用例_编号", "用例_名称", "预置条件", "测试步骤", "预期结果"]
    missing = [c for c in _REQUIRED_COLUMNS if c not in col_map]
    if missing:
        print(f"  [WARN] Sheet '{sheet_name}': 缺少必填列 {missing}，跳过")
        logger.warning("Sheet '%s': 缺少必填列 %s，跳过", sheet_name, missing)
        return []

    generated: List[str] = []
    for idx, row in df.iterrows():
        try:
            case = _build_case_dict(row, col_map, idx)
            case_id = case["用例_编号"]
            case_name = case["用例_名称"]

            # 文件名：优先用例_编号，fallback 到 sanitized 用例名称
            base_name = case_id if case_id and not case_id.startswith("TC-EXCEL-") else case_name
            filename = _safe_filename(base_name) + ".yaml"
            output_path = output_dir / filename

            # 文件名冲突时追加序号
            counter = 1
            while output_path.exists():
                output_path = output_dir / f"{_safe_filename(base_name)}_{counter}.yaml"
                counter += 1

            # 写入 YAML
            yaml_str = yaml.dump(
                case,
                Dumper=_YamlDumper,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                width=120,
            )
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(yaml_str)

            generated.append(str(output_path))
            print(f"  [OK] {case_id} -> {output_path.name}")

        except Exception as e:
            logger.error("行 %d 转换失败: %s", idx + 2, e)
            continue

    return generated


def convert_excel_to_yaml(
    excel_path: str,
    output_dir: str = "testcases",
) -> List[str]:
    """
    将 Excel 文件或目录转换为标准 YAML 用例文件。

    自动识别输入类型：
    - 单个 .xlsx/.xls 文件：直接转换
    - 目录：遍历其中所有 Excel 文件逐个转换

    每行生成一个 YAML 文件，预置条件/测试步骤/预期结果使用 | 块标量保留原始自然文本。
    支持多 Sheet，列名自动识别。

    Args:
        excel_path: Excel 文件路径或包含 Excel 文件的目录
        output_dir: YAML 输出目录，默认 testcases/

    Returns:
        生成的 YAML 文件路径列表
    """
    import pandas as pd

    path = Path(excel_path)

    if not path.exists():
        logger.error("路径不存在: %s", excel_path)
        return []

    # 目录：遍历其中所有 Excel 文件
    if path.is_dir():
        excel_files = sorted(path.glob("*.xlsx")) + sorted(path.glob("*.xls"))
        if not excel_files:
            logger.warning("目录中未找到 Excel 文件: %s", excel_path)
            return []
        logger.info("目录模式: 找到 %d 个 Excel 文件", len(excel_files))
        all_generated: List[str] = []
        for ef in excel_files:
            all_generated.extend(
                convert_excel_to_yaml(str(ef), output_dir)
            )
        return all_generated

    # 单文件模式
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    logger.info("读取 Excel: %s", path)

    try:
        xls = pd.ExcelFile(path, engine='openpyxl')
    except Exception as e:
        logger.error("无法读取 Excel 文件: %s", e)
        return []

    all_generated: List[str] = []

    for sheet_name in xls.sheet_names:
        logger.info("处理 Sheet: '%s'", sheet_name)
        try:
            df = pd.read_excel(xls, sheet_name=sheet_name)
            df = df.dropna(how='all')
            if df.empty:
                logger.info("Sheet '%s' 为空，跳过", sheet_name)
                continue
            generated = _convert_sheet(df, sheet_name, out)
            all_generated.extend(generated)
        except Exception as e:
            logger.error("Sheet '%s' 处理失败: %s", sheet_name, e)
            continue

    return all_generated
