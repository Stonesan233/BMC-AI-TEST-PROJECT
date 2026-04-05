# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 核心数据结构定义

本模块定义了框架中使用的所有核心数据模型，包括：
- StepStatus: 步骤执行状态枚举
- Evidence: 执行证据模型
- Assertion: 步骤断言模型（v2.1 新增）
- StepRecord: 单步执行记录模型
- ExecutionRecord: 完整执行记录模型（单用例，v2.1）
- StepJudgment: 单步判断结果模型
- TestResult: 测试判断结果模型（v2）
- migrate_v1_to_v2_1: v1 -> v2.1 迁移函数

设计原则：
- 使用 Pydantic v2 BaseModel
- 优先保证灵活性（environment 和 test_case_info 使用 Dict[str, Any]）
- 每个步骤可以独立指定接口优先级
- 必须完整保存 raw_stdout 和 raw_stderr（用于生成人可读报告）
- v2.1 新增 schema_version 字段和 assertions 结构化断言
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ============================================================
# 版本常量
# ============================================================

SCHEMA_VERSION_V1 = "1.0"
SCHEMA_VERSION_V2_1 = "2.1"
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION_V2_1


# ============================================================
# 步骤状态枚举
# ============================================================

class StepStatus(str, Enum):
    """
    步骤执行状态枚举

    Attributes:
        PENDING: 待执行
        RUNNING: 执行中
        COMPLETED: 执行完成（成功）
        FAILED: 执行失败
        SKIPPED: 跳过执行
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ============================================================
# 证据模型
# ============================================================

class Evidence(BaseModel):
    """
    执行证据模型

    用于记录单步执行过程中收集的证据信息。

    Attributes:
        evidence_id: 证据唯一标识符
        step_id: 关联的步骤 ID
        evidence_type: 证据类型（如 "redfish_response", "cli_output", "ipmi_output"）
        content: 证据内容（原始内容，v2.1 强调内联）
        metadata: 证据元数据（如 HTTP 状态码、执行时间等）
        captured_at: 证据采集时间
    """
    evidence_id: str = Field(..., description="证据唯一标识符")
    step_id: str = Field(..., description="关联的步骤 ID")
    evidence_type: str = Field(
        ...,
        description="证据类型，如 'redfish_response', 'cli_output', 'ipmi_output', 'ssh_output', 'error_log'"
    )
    content: str = Field(
        ...,
        description="证据内容，原始数据内联存储（v2.1 要求不使用文件引用）"
    )
    text_summary: Optional[str] = Field(
        default=None,
        description=(
            "多模态证据的文本摘要（v2.1 新增）。"
            "当 evidence_type 为 image/video/screenshot 时，用文本描述视觉内容。"
            "v2.x 版本仅通过文本描述处理，v3.0 将支持完整视觉分析。"
        )
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="证据元数据，如 HTTP 状态码、执行时间、命令参数等"
    )
    captured_at: datetime = Field(
        default_factory=datetime.now,
        description="证据采集时间"
    )


# ============================================================
# 断言模型（v2.1 新增）
# ============================================================

class Assertion(BaseModel):
    """
    结构化断言模型（v2.1 新增）

    每个步骤可以包含一个或多个断言，Judge Agent 逐条验证。
    如果步骤没有明确的 assertions 列表，Judge 会从 expected 字段推导。

    Attributes:
        assertion_id: 断言唯一标识符
        assertion_type: 断言类型
        field_path: 检查的字段路径（JSONPath 风格，如 "body.PowerState"）
        operator: 比较运算符
        expected_value: 预期值
        actual_value: 实际值（Judge 填充）
        passed: 是否通过（Judge 填充）
        note: 补充说明
    """
    assertion_id: str = Field(
        default="",
        description="断言唯一标识符，如 'step_001_assert_001'"
    )
    assertion_type: Literal[
        "field_equals", "field_contains", "field_matches",
        "status_code", "field_exists", "field_type",
        "response_time", "custom"
    ] = Field(
        default="field_equals",
        description="断言类型"
    )
    field_path: str = Field(
        default="",
        description="检查的字段路径，如 'body.PowerState', 'http_status'"
    )
    operator: Literal["eq", "ne", "contains", "not_contains", "matches", "gt", "lt", "gte", "lte", "exists", "type_of"] = Field(
        default="eq",
        description="比较运算符"
    )
    expected_value: Any = Field(
        default=None,
        description="预期值"
    )
    actual_value: Any = Field(
        default=None,
        description="实际值（由 Judge 填充）"
    )
    passed: Optional[bool] = Field(
        default=None,
        description="断言是否通过（由 Judge 填充）"
    )
    note: str = Field(
        default="",
        description="补充说明"
    )


# ============================================================
# 步骤记录模型
# ============================================================

class StepRecord(BaseModel):
    """
    单步执行记录模型

    记录测试用例中单个步骤的完整执行信息，包括执行参数、结果、证据等。

    Attributes:
        step_id: 步骤唯一标识符
        description: 步骤描述（中文）
        tool: 使用的工具名称（如 "redfish", "cli", "ipmi", "ssh"）
        interface_preference: 接口优先级，默认 "redfish"
        endpoint: Redfish 端点路径（仅 Redfish 接口使用）
        method: HTTP 方法（GET/POST/PATCH/DELETE，仅 Redfish 接口使用）
        command: CLI/IPMI 命令（仅 CLI/IPMI 接口使用）
        expected: 预期结果
        actual: 实际结果
        raw_stdout: 完整标准输出（用于生成人可读报告）
        raw_stderr: 完整标准错误（用于生成人可读报告）
        evidence: 证据列表
        assertions: 结构化断言列表（v2.1 新增）
        status: 步骤执行状态
        http_status: HTTP 状态码（仅 Redfish 接口使用）
        error_message: 错误信息（执行失败时记录）
        started_at: 步骤开始时间
        completed_at: 步骤完成时间
        keyword: Robot Framework 关键字预留（v2.1 新增）
    """
    step_id: str = Field(..., description="步骤唯一标识符")
    description: str = Field(..., description="步骤描述（中文）")
    tool: str = Field(
        ...,
        description="使用的工具名称，如 'redfish', 'cli', 'ipmi', 'ssh'"
    )
    interface_preference: str = Field(
        default="redfish",
        description="接口优先级，可选 'redfish', 'cli', 'ipmi'，默认 Redfish 最高优先级"
    )

    # Redfish 接口相关字段
    endpoint: Optional[str] = Field(
        default=None,
        description="Redfish 端点路径，如 '/redfish/v1/Systems/system'"
    )
    method: Optional[str] = Field(
        default=None,
        description="HTTP 方法，如 'GET', 'POST', 'PATCH', 'DELETE'"
    )
    request_body: Optional[Dict[str, Any]] = Field(
        default=None,
        description="请求体（POST/PATCH 时使用）"
    )

    # CLI/IPMI 接口相关字段
    command: Optional[str] = Field(
        default=None,
        description="CLI/IPMI 命令"
    )

    # 执行结果
    expected: Any = Field(
        ...,
        description="预期结果"
    )
    actual: Optional[Any] = Field(
        default=None,
        description="实际结果"
    )

    # 完整输出（用于生成人可读报告）
    raw_stdout: Optional[str] = Field(
        default=None,
        description="完整标准输出，用于生成人可读报告"
    )
    raw_stderr: Optional[str] = Field(
        default=None,
        description="完整标准错误，用于生成人可读报告"
    )

    # 证据和断言
    evidence: List[Evidence] = Field(
        default_factory=list,
        description="证据列表"
    )
    assertions: List[Assertion] = Field(
        default_factory=list,
        description="结构化断言列表（v2.1 新增，Judge 逐条验证）"
    )

    # 状态
    status: StepStatus = Field(
        default=StepStatus.PENDING,
        description="步骤执行状态"
    )
    http_status: Optional[int] = Field(
        default=None,
        description="HTTP 状态码（仅 Redfish 接口）"
    )
    error_message: Optional[str] = Field(
        default=None,
        description="错误信息（执行失败时记录）"
    )

    # 时间戳
    started_at: Optional[datetime] = Field(
        default=None,
        description="步骤开始时间"
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="步骤完成时间"
    )

    # Robot Framework 预留字段
    keyword: Optional[str] = Field(
        default=None,
        description="Robot Framework 关键字名称（预留，供后续集成使用）"
    )


# ============================================================
# 环境恢复记录模型（v2.1 新增）
# ============================================================

class EnvironmentRecoveryAction(BaseModel):
    """
    环境恢复动作记录

    Attributes:
        action_type: 恢复动作类型
        target: 恢复目标（如用户名、配置项）
        status: 恢复状态
        details: 详细信息
    """
    action_type: str = Field(
        ...,
        description="恢复动作类型：delete_user / restore_password / restore_config / other"
    )
    target: str = Field(
        default="",
        description="恢复目标，如用户名、配置项名称"
    )
    status: Literal["completed", "failed", "skipped"] = Field(
        default="completed",
        description="恢复状态"
    )
    details: str = Field(
        default="",
        description="详细信息"
    )


# ============================================================
# ExecutionRecord（v2.1）
# ============================================================

class ExecutionRecord(BaseModel):
    """
    完整执行记录模型（单用例，v2.1）

    记录单个测试用例的完整执行过程，包括环境信息、预置条件、所有步骤的执行记录等。
    支持批量串行执行，但每个 ExecutionRecord 只记录单个用例。

    v2.1 变更：
    - 新增 schema_version 字段（默认 "2.1"）
    - 新增 assertions 字段到 StepRecord
    - 新增 environment_recovery_actions 字段
    - 新增 consolidated_audit_draft 字段（Exec 生成的审计草案）
    - 新增 text_summary 字段（多模态证据的文本摘要）
    - 证据内容内联到 evidence.content，不再使用外部文件引用

    Attributes:
        schema_version: Schema 版本号
        execution_id: 执行记录唯一标识符
        case_id: 测试用例 ID
        case_name: 测试用例名称
        environment: 执行环境信息（灵活字段）
        test_case_info: 测试用例详细信息（灵活字段）
        prerequisites: 预置条件检查结果列表
        steps: 步骤执行记录列表
        environment_recovery_actions: 环境恢复动作记录（v2.1 新增）
        consolidated_audit_draft: Exec 生成的审计草案（v2.1 新增）
        text_summary: 多模态证据的文本摘要（v2.1 新增）
        started_at: 执行开始时间
        completed_at: 执行完成时间
        overall_status: 整体执行状态
    """
    schema_version: Literal["2.1"] = Field(
        default=CURRENT_SCHEMA_VERSION,
        frozen=True,
        description=f"Schema 版本号，强制为 '{CURRENT_SCHEMA_VERSION}'，不可修改"
    )
    execution_id: str = Field(..., description="执行记录唯一标识符")
    case_id: str = Field(..., description="测试用例 ID")
    case_name: str = Field(..., description="测试用例名称")

    # 灵活的环境信息字段
    environment: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "执行环境信息，支持任意字段。"
            "常见字段：bmc_host, bmc_user, bmc_password, os_host, os_user, os_password 等"
        )
    )

    # 灵活的用例信息字段
    test_case_info: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "测试用例详细信息，支持任意字段。"
            "常见字段：用例编号、名称、步骤描述、预期结果、预置条件等"
        )
    )

    # 预置条件
    prerequisites: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="预置条件检查结果列表"
    )

    # 步骤记录
    steps: List[StepRecord] = Field(
        default_factory=list,
        description="步骤执行记录列表"
    )

    # v2.1 新增字段
    environment_recovery_actions: List[EnvironmentRecoveryAction] = Field(
        default_factory=list,
        description="环境恢复动作记录列表（v2.1 新增）"
    )
    consolidated_audit_draft: Optional[str] = Field(
        default=None,
        description=(
            "Exec Agent 生成的审计草案文本，供 Judge 参考（v2.1 新增）。"
            "在 Exec 执行结束时自动生成，包含执行摘要、步骤概览、环境恢复状态。"
        )
    )
    text_summary: Optional[str] = Field(
        default=None,
        description=(
            "多模态证据（图像/视频）的文本摘要。"
            "当前版本（v2.x）仅通过文本描述处理，v3.0 将支持完整视觉分析。"
        )
    )

    # 时间戳
    started_at: datetime = Field(
        default_factory=datetime.now,
        description="执行开始时间"
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="执行完成时间"
    )

    # 整体状态
    overall_status: str = Field(
        default="running",
        description="整体执行状态，可选 'running', 'completed', 'failed', 'interrupted'"
    )

    # ------------------------------------------------------------------
    # v1 -> v2.1 迁移（类方法，支持外部显式调用）
    # ------------------------------------------------------------------

    @classmethod
    def migrate_from_v1(cls, old_data: Dict[str, Any]) -> "ExecutionRecord":
        """
        将 v1.0 格式的 ExecutionRecord 字典迁移为 v2.1 ExecutionRecord 实例。

        v1.0 数据特征：无 schema_version 字段或 schema_version == "1.0"。
        迁移操作：添加 v2.1 新增字段默认值，保留所有 v1 字段不变。

        Args:
            old_data: v1.0 格式的 ExecutionRecord 字典

        Returns:
            ExecutionRecord v2.1 实例
        """
        migrated = migrate_v1_to_v2_1(old_data)
        return cls(**migrated)


# ============================================================
# 判断结果相关模型
# ============================================================

class AssertionJudgment(BaseModel):
    """
    单条断言的判断结果（v2.1 新增）

    Attributes:
        assertion_id: 断言 ID
        passed: 是否通过
        actual_value: Judge 提取的实际值
        reason: 判断理由
    """
    assertion_id: str = Field(..., description="断言 ID")
    passed: bool = Field(..., description="断言是否通过")
    actual_value: Any = Field(default=None, description="Judge 提取的实际值")
    reason: str = Field(default="", description="判断理由（中文）")


class StepJudgment(BaseModel):
    """
    单步判断结果模型

    记录 Judge Agent 对单个步骤的判断结果。

    Attributes:
        step_id: 步骤 ID
        result: 判断结果（PASS/FAIL）
        confidence: 置信度（0.0-1.0）
        reason: 判断理由（中文）
        expected_match: 预期与实际是否匹配
        concerns: 关注点/疑点列表
        assertion_judgments: 断言级判断结果（v2.1 新增）
        evidence_sufficient: 证据是否充分（v2.1 新增）
    """
    step_id: str = Field(..., description="步骤 ID")
    result: str = Field(
        ...,
        description="判断结果，'PASS' 或 'FAIL'"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="置信度，范围 0.0-1.0"
    )
    reason: str = Field(..., description="判断理由（中文）")
    expected_match: bool = Field(
        ...,
        description="预期与实际是否匹配"
    )
    concerns: List[str] = Field(
        default_factory=list,
        description="关注点/疑点列表"
    )
    # v2.1 新增
    assertion_judgments: List[AssertionJudgment] = Field(
        default_factory=list,
        description="断言级判断结果列表（v2.1 新增）"
    )
    evidence_sufficient: bool = Field(
        default=True,
        description="证据是否充分。证据不足时即使其他条件满足也应标为 False"
    )


class TestResult(BaseModel):
    """
    测试结果模型（v2）

    记录 Judge Agent 对整个测试用例的判断结果。

    v2 变更：
    - 新增 schema_version 字段
    - StepJudgment 新增 assertion_judgments 和 evidence_sufficient
    - 新增 judge_model 和 judge_duration_seconds 字段
    - 新增 false_pass_risk 字段（假 PASS 风险评估）

    Attributes:
        schema_version: Schema 版本号
        execution_id: 执行记录 ID
        case_id: 测试用例 ID
        case_name: 测试用例名称
        overall_result: 整体判断结果（PASS/FAIL）
        confidence: 整体置信度（0.0-1.0）
        step_results: 各步骤判断结果列表
        prerequisite_check: 预置条件检查结果
        environment_recovery: 环境恢复结果
        judge_notes: 判断说明列表
        judge_model: 使用的判断模型名称
        judge_duration_seconds: 判断耗时（秒）
        false_pass_risk: 假 PASS 风险评估
        audit_report_markdown: Judge 生成的审计报告 Markdown 内容
    """
    schema_version: str = Field(
        default=CURRENT_SCHEMA_VERSION,
        description=f"Schema 版本号，当前版本: {CURRENT_SCHEMA_VERSION}"
    )
    execution_id: str = Field(..., description="执行记录 ID")
    case_id: str = Field(..., description="测试用例 ID")
    case_name: str = Field(..., description="测试用例名称")
    overall_result: str = Field(
        ...,
        description="整体判断结果，'PASS' 或 'FAIL'"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="整体置信度，范围 0.0-1.0"
    )
    step_results: List[StepJudgment] = Field(
        default_factory=list,
        description="各步骤判断结果列表"
    )
    prerequisite_check: Dict[str, Any] = Field(
        default_factory=dict,
        description="预置条件检查结果"
    )
    environment_recovery: Dict[str, Any] = Field(
        default_factory=dict,
        description="环境恢复结果"
    )
    judge_notes: List[str] = Field(
        default_factory=list,
        description="判断说明列表"
    )
    # v2 新增
    judge_model: str = Field(
        default="",
        description="使用的判断模型名称，如 'Qwen3-235B-A22B', 'glm-5'"
    )
    judge_duration_seconds: float = Field(
        default=0.0,
        description="Judge 判断耗时（秒）"
    )
    false_pass_risk: Literal["none", "low", "medium", "high"] = Field(
        default="none",
        description="假 PASS 风险评估等级"
    )
    risk_notes: List[str] = Field(
        default_factory=list,
        description="假 PASS 风险的详细说明（v2.1 新增），补充 false_pass_risk 的理由"
    )
    audit_report_markdown: Optional[str] = Field(
        default=None,
        description="Judge 生成的完整审计报告 Markdown 内容"
    )


# ============================================================
# v1 -> v2.1 迁移
# ============================================================

def migrate_v1_to_v2_1(v1_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 v1.0 格式的 ExecutionRecord 数据迁移为 v2.1 格式。

    主要变更：
    1. 添加 schema_version = "2.1"
    2. 为每个 step 添加空的 assertions 列表
    3. 添加 environment_recovery_actions 为空列表
    4. 保留所有 v1 字段不变

    Args:
        v1_data: v1.0 格式的 ExecutionRecord 字典

    Returns:
        v2.1 格式的 ExecutionRecord 字典
    """
    v2_1_data = dict(v1_data)
    v2_1_data["schema_version"] = SCHEMA_VERSION_V2_1

    # 为每个步骤添加 assertions 列表（如果不存在）
    steps = v2_1_data.get("steps", [])
    for step in steps:
        if "assertions" not in step:
            step["assertions"] = []
        if "keyword" not in step:
            step["keyword"] = None

    # 添加 v2.1 新增的顶层字段
    if "environment_recovery_actions" not in v2_1_data:
        v2_1_data["environment_recovery_actions"] = []
    if "consolidated_audit_draft" not in v2_1_data:
        v2_1_data["consolidated_audit_draft"] = None
    if "text_summary" not in v2_1_data:
        v2_1_data["text_summary"] = None

    return v2_1_data


def load_execution_record_with_migration(data: Dict[str, Any]) -> ExecutionRecord:
    """
    加载 ExecutionRecord，自动处理版本迁移。

    如果数据中没有 schema_version 字段，视为 v1.0 并自动迁移到 v2.1。

    Args:
        data: ExecutionRecord 原始字典

    Returns:
        ExecutionRecord v2.1 实例
    """
    version = data.get("schema_version", SCHEMA_VERSION_V1)

    if version != CURRENT_SCHEMA_VERSION:
        data = migrate_v1_to_v2_1(data)

    return ExecutionRecord(**data)
