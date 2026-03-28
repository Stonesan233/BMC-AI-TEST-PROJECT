# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - 核心数据结构定义

本模块定义了框架中使用的所有核心数据模型，包括：
- StepStatus: 步骤执行状态枚举
- Evidence: 执行证据模型
- StepRecord: 单步执行记录模型
- ExecutionRecord: 完整执行记录模型（单用例）

设计原则：
- 使用 Pydantic v2 BaseModel
- 优先保证灵活性（environment 和 test_case_info 使用 Dict[str, Any]）
- 每个步骤可以独立指定接口优先级
- 必须完整保存 raw_stdout 和 raw_stderr（用于生成人可读报告）
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    """
    步骤执行状态枚举

    Attributes:
        PENDING: 待执行
        RUNNING: 执行中
        COMPLETED: 执行完成（成功）
        FAILED: 执行失败
        SKIPP: 跳过执行
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Evidence(BaseModel):
    """
    执行证据模型

    用于记录单步执行过程中收集的证据信息。

    Attributes:
        evidence_id: 证据唯一标识符
        step_id: 关联的步骤 ID
        evidence_type: 证据类型（如 "redfish_response", "cli_output", "ipmi_output"）
        content: 证据内容（原始内容或文件路径）
        metadata: 证据元数据（如 HTTP 状态码、执行时间等）
        captured_at: 证据采集时间
    """
    evidence_id: str = Field(..., description="证据唯一标识符")
    step_id: str = Field(..., description="关联的步骤 ID")
    evidence_type: str = Field(
        ...,
        description="证据类型，如 'redfish_response', 'cli_output', 'ipmi_output', 'ssh_output'"
    )
    content: str = Field(
        ...,
        description="证据内容，可以是原始内容或文件路径"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="证据元数据，如 HTTP 状态码、执行时间、命令参数等"
    )
    captured_at: datetime = Field(
        default_factory=datetime.now,
        description="证据采集时间"
    )


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
        status: 步骤执行状态
        http_status: HTTP 状态码（仅 Redfish 接口使用）
        error_message: 错误信息（执行失败时记录）
        started_at: 步骤开始时间
        completed_at: 步骤完成时间
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

    # 证据和状态
    evidence: List[Evidence] = Field(
        default_factory=list,
        description="证据列表"
    )
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


class ExecutionRecord(BaseModel):
    """
    完整执行记录模型（单用例）

    记录单个测试用例的完整执行过程，包括环境信息、预置条件、所有步骤的执行记录等。
    支持批量串行执行，但每个 ExecutionRecord 只记录单个用例。

    Attributes:
        execution_id: 执行记录唯一标识符
        case_id: 测试用例 ID
        case_name: 测试用例名称
        environment: 执行环境信息（灵活字段）
        test_case_info: 测试用例详细信息（灵活字段）
        prerequisites: 预置条件检查结果列表
        steps: 步骤执行记录列表
        started_at: 执行开始时间
        completed_at: 执行完成时间
        overall_status: 整体执行状态
    """
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


# ============================================================
# 判断结果相关模型
# ============================================================

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


class TestResult(BaseModel):
    """
    测试结果模型

    记录 Judge Agent 对整个测试用例的判断结果。

    Attributes:
        execution_id: 执行记录 ID
        case_id: 测试用例 ID
        case_name: 测试用例名称
        overall_result: 整体判断结果（PASS/FAIL）
        confidence: 整体置信度（0.0-1.0）
        step_results: 各步骤判断结果列表
        prerequisite_check: 预置条件检查结果
        environment_recovery: 环境恢复结果
        judge_notes: 判断说明列表
    """
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
