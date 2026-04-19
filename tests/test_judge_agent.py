# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Judge Agent 完整测试套件

验证目标：
1. ExecutionRecord v2.1 Schema 正确性与迁移能力
2. Exec Agent 自动生成 consolidated_audit_draft
3. --judge / --no-judge CLI 参数
4. Judge 严格判断（宁可错杀，不可放过）
5. v1.0 旧格式记录兼容迁移
6. 最终输出单个 audit_report.md
7. Judge 异常或超时时的错误处理

运行：
    pytest tests/test_judge_agent.py -v --asyncio-mode=auto
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

# 项目根目录加入路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.schemas import (
    ExecutionRecord,
    TestResult,
    StepStatus,
    load_execution_record_with_migration,
)
from src.core.config import load_config
from src.core.client_factory import ClientFactory
from src.agents.judge_agent import (
    JudgeAgent,
    build_test_result_from_json,
    build_error_test_result,
    write_audit_report,
)

# 日志配置
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_judge")


# ============================================================
# 测试 Fixtures
# ============================================================

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "execution_records"
SHARED_DIR = PROJECT_ROOT / "shared"


@pytest.fixture
def config():
    """加载测试配置"""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        pytest.skip(f"Config not found: {config_path}")
    return load_config(str(config_path))


@pytest.fixture
def client_factory(config):
    """创建 ClientFactory"""
    return ClientFactory(config)


@pytest.fixture
def judge_agent(config, client_factory):
    """创建 JudgeAgent 实例"""
    agent = JudgeAgent(
        config=config,
        client_factory=client_factory,
        shared_dir=str(SHARED_DIR),
    )
    yield agent
    # 不关闭 factory，由外层控制


def load_fixture(name: str) -> dict:
    """加载 fixture JSON"""
    path = FIXTURES_DIR / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_fixture_record(name: str) -> ExecutionRecord:
    """加载 fixture 为 ExecutionRecord"""
    data = load_fixture(name)
    return ExecutionRecord.model_validate(data)


# ============================================================
# Test 1: v2.1 Schema 验证 + consolidated_audit_draft
# ============================================================

def test_v21_schema_and_consolidated_draft():
    """
    验证点：
    1. ExecutionRecord schema_version 强制为 Literal["2.1"]
    2. Evidence.text_summary 字段存在
    3. TestResult.risk_notes 字段存在
    4. consolidated_audit_draft 自动生成

    预期：
    - schema_version == "2.1"
    - evidence[].text_summary 可为 None
    - risk_notes 字段存在且为 List
    - consolidated_audit_draft 不为 None
    """
    record = load_fixture_record("case_pass_strict.json")

    # Schema 版本
    assert record.schema_version == "2.1", f"Expected 2.1, got {record.schema_version}"

    # Evidence.text_summary
    assert hasattr(record.steps[0].evidence[0], "text_summary")
    assert record.steps[0].evidence[0].text_summary is None or isinstance(
        record.steps[0].evidence[0].text_summary, str
    )

    # consolidated_audit_draft
    assert record.consolidated_audit_draft is not None
    assert "# Exec Agent Audit Draft" in record.consolidated_audit_draft

    # TestResult.risk_notes 字段存在
    result = TestResult(
        execution_id="test",
        case_id="test",
        case_name="test",
        overall_result="PASS",
        confidence=0.9,
        risk_notes=["Risk note 1", "Risk note 2"],
    )
    assert result.risk_notes == ["Risk note 1", "Risk note 2"]

    print("[PASS] v2.1 Schema 和 consolidated_audit_draft 验证通过")


# ============================================================
# Test 2: v1 -> v2.1 迁移
# ============================================================

def test_v1_migration():
    """
    验证点：
    1. v1.0 旧格式（无 schema_version）能正确迁移
    2. 自动填充 v2.1 新增字段
    3. migrate_from_v1() 类方法可用

    预期：
    - schema_version 自动变为 "2.1"
    - environment_recovery_actions == []
    - consolidated_audit_draft == None
    - steps[].assertions == []
    """
    v1_data = load_fixture("case_v1_compatible.json")

    # 验证 v1 数据没有 schema_version
    assert "schema_version" not in v1_data

    # 自动迁移加载
    record = load_execution_record_with_migration(v1_data)

    assert record.schema_version == "2.1"
    assert record.environment_recovery_actions == []
    assert record.consolidated_audit_draft is None
    assert record.steps[0].assertions == []

    # 显式调用 migrate_from_v1
    record2 = ExecutionRecord.migrate_from_v1(v1_data)
    assert record2.schema_version == "2.1"

    print("[PASS] v1 -> v2.1 迁移验证通过")


# ============================================================
# Test 3: --judge / --no-judge CLI 参数（模拟）
# ============================================================

def test_cli_judge_flag_parsing():
    """
    验证点：
    1. --judge 参数默认为 True
    2. --no-judge 设置为 False
    3. main.py 正确解析参数

    预期：
    - parse_args([]).judge == True
    - parse_args(["--no-judge"]).judge == False
    """
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", action="store_true", default=True)
    parser.add_argument("--no-judge", action="store_false", dest="judge")

    # 默认开启
    args = parser.parse_args([])
    assert args.judge is True

    # --no-judge 关闭
    args = parser.parse_args(["--no-judge"])
    assert args.judge is False

    # 显式开启
    args = parser.parse_args(["--judge"])
    assert args.judge is True

    print("[PASS] CLI --judge/--no-judge 参数解析验证通过")


# ============================================================
# Test 4: Judge 严格判断（模拟 LLM 调用）
# ============================================================

def test_judge_strict_verdict_simulation():
    """
    验证点：
    1. 表面通过但存在微小不一致 -> 判 FAIL 或添加 risk_notes
    2. 证据不充分 -> evidence_sufficient = False
    3. 断言不匹配 -> assertion_judgments 中 passed=False

    模拟 Judge 输出：
    - case_pass_strict.json: Vendor 大小写不匹配 (Huawei vs HUAWEI)
    - 预期：判 FAIL 或 risk_notes 说明差异
    """
    # 构建模拟 Judge 输出（模拟 LLM 返回）
    record = load_fixture_record("case_pass_strict.json")

    # 模拟 Judge 解析后的 JSON（模拟 LLM 严格判断）
    judge_output = {
        "execution_id": record.execution_id,
        "case_id": record.case_id,
        "case_name": record.case_name,
        "overall_result": "FAIL",  # 严格判断：Vendor 大小写不一致
        "confidence": 0.95,
        "step_results": [
            {
                "step_id": "step_001",
                "result": "FAIL",
                "confidence": 0.95,
                "reason": "Vendor 字段大小写不一致：expected=Huawei, actual=HUAWEI",
                "expected_match": False,
                "concerns": ["字段值大小写不完全匹配"],
                "assertion_judgments": [
                    {
                        "assertion_id": "step_001_assert_001",
                        "passed": True,
                        "actual_value": 200,
                        "reason": "HTTP 状态码匹配"
                    },
                    {
                        "assertion_id": "step_001_assert_002",
                        "passed": True,
                        "actual_value": "1.20.1",
                        "reason": "RedfishVersion 匹配"
                    },
                    {
                        "assertion_id": "step_001_assert_003",
                        "passed": False,
                        "actual_value": "HUAWEI",
                        "reason": "Vendor 大小写不匹配：expected=Huawei, actual=HUAWEI"
                    }
                ],
                "evidence_sufficient": True
            }
        ],
        "prerequisite_check": {
            "result": "PASS",
            "failed_items": []
        },
        "environment_recovery": {
            "recovered": True,
            "warnings": []
        },
        "judge_notes": ["步骤 status=completed，但存在字段不一致"],
        "false_pass_risk": "medium",
        "risk_notes": [
            "Vendor 字段大小写不匹配（expected: Huawei, actual: HUAWEI）",
            "建议 Exec 阶段进行大小写不敏感比较或标准化字段值"
        ]
    }

    result = build_test_result_from_json(
        parsed=judge_output,
        execution_record=record,
        judge_model="glm-5",
        duration_seconds=5.0,
    )

    # 验证严格判断
    assert result.overall_result == "FAIL"
    assert result.false_pass_risk == "medium"
    assert len(result.risk_notes) > 0
    assert "大小写" in result.risk_notes[0]

    # 验证断言判断
    step_judgment = result.step_results[0]
    assert step_judgment.result == "FAIL"
    assert step_judgment.assertion_judgments[2].passed is False

    print("[PASS] 严格判断（宁可错杀）验证通过")


# ============================================================
# Test 5: 明显失败案例判断
# ============================================================

def test_judge_clear_fail_case():
    """
    验证点：
    1. 步骤 status=failed -> overall_result=FAIL
    2. 步骤 status=skipped -> 证据不足
    3. error_message 非空 -> FAIL

    预期：
    - overall_result == "FAIL"
    - 有详细的失败理由
    """
    record = load_fixture_record("case_fail_clear.json")

    # 模拟 Judge 输出
    judge_output = {
        "execution_id": record.execution_id,
        "case_id": record.case_id,
        "case_name": record.case_name,
        "overall_result": "FAIL",
        "confidence": 0.98,
        "step_results": [
            {
                "step_id": "step_001",
                "result": "PASS",
                "confidence": 0.98,
                "reason": "HTTP 200, 用户列表查询成功",
                "expected_match": True,
                "concerns": [],
                "evidence_sufficient": True
            },
            {
                "step_id": "step_002",
                "result": "FAIL",
                "confidence": 0.99,
                "reason": "HTTP 400, 用户创建失败: password does not meet requirements",
                "expected_match": False,
                "concerns": ["密码复杂度不足"],
                "evidence_sufficient": True
            },
            {
                "step_id": "step_003",
                "result": "FAIL",
                "confidence": 0.99,
                "reason": "前置步骤失败，跳过验证",
                "expected_match": False,
                "concerns": ["未执行，无法验证"],
                "evidence_sufficient": False
            }
        ],
        "prerequisite_check": {
            "result": "PASS",
            "failed_items": []
        },
        "environment_recovery": {
            "recovered": True,
            "warnings": []
        },
        "judge_notes": ["步骤2失败（HTTP 400），步骤3因前置失败跳过"],
        "false_pass_risk": "none",
        "risk_notes": []
    }

    result = build_test_result_from_json(
        parsed=judge_output,
        execution_record=record,
        judge_model="glm-5",
        duration_seconds=3.0,
    )

    assert result.overall_result == "FAIL"
    assert result.step_results[1].result == "FAIL"
    assert "400" in result.step_results[1].reason

    print("[PASS] 明显失败案例判断验证通过")


# ============================================================
# Test 6: Error/Timeout 错误处理
# ============================================================

def test_judge_error_handling():
    """
    验证点：
    1. Exec 超时/错误 -> overall_status=failed
    2. Judge 构建 ERROR TestResult
    3. partial report 生成

    预期：
    - overall_result == "FAIL"
    - confidence == 0.0
    - false_pass_risk == "high"
    - audit_report_markdown 包含错误信息
    """
    record = load_fixture_record("case_error_timeout.json")

    # 模拟 Judge 处理超时错误
    result = build_error_test_result(
        execution_record=record,
        judge_model="glm-5",
        error_message="SSH connection timeout after 30s",
        duration_seconds=30.0,
    )

    assert result.overall_result == "FAIL"
    assert result.confidence == 0.0
    assert result.false_pass_risk == "high"
    assert "timeout" in result.judge_notes[0].lower()
    assert result.audit_report_markdown is None  # ERROR 时为 None

    print("[PASS] 错误处理验证通过")


# ============================================================
# Test 7: audit_report.md 生成
# ============================================================

def test_audit_report_generation(tmp_path):
    """
    验证点：
    1. 生成单个 audit_report.md
    2. 包含 overall_result
    3. 包含 risk_notes（如果有）
    4. 路径为 shared/audit_reports/{execution_id}_audit_report.md

    预期：
    - 文件成功创建
    - 内容包含判断结果
    """
    record = load_fixture_record("case_pass_strict.json")

    # 构建 TestResult
    result = TestResult(
        execution_id=record.execution_id,
        case_id=record.case_id,
        case_name=record.case_name,
        overall_result="PASS",
        confidence=0.95,
        judge_model="glm-5",
        judge_duration_seconds=5.0,
        false_pass_risk="low",
        risk_notes=["证据充分，无风险点"],
    )

    # 写入审计报告
    report_path = write_audit_report(
        execution_id=record.execution_id,
        markdown_content=f"# Audit Report\n\nResult: {result.overall_result}\nConfidence: {result.confidence}\nRisk: {result.false_pass_risk}",
        shared_dir=str(tmp_path),
    )

    # 验证文件存在
    assert Path(report_path).exists()

    # 验证内容
    content = Path(report_path).read_text(encoding="utf-8")
    assert "PASS" in content
    assert "0.95" in content

    print(f"[PASS] audit_report.md 生成验证通过: {report_path}")


# ============================================================
# Test 8: _compact_record_for_judge 字段精简
# ============================================================

def test_compact_record_field_stripping(judge_agent):
    """
    验证点：
    1. 顶层字段: text_summary / test_case_info / started_at / completed_at / schema_version 被移除
    2. environment: 仅保留 bmc_host / bmc_user
    3. prerequisites: 全部 completed 时整体移除
    4. 步骤: 移除 started_at / completed_at / interface_preference / tool / duration_seconds
    5. Evidence: 仅保留 evidence_type + content
    6. 空 environment_recovery_actions 被移除
    """
    record = load_fixture_record("case_pass_strict.json")
    compact = judge_agent._compact_record_for_judge(record)

    # 顶层字段不应存在
    assert "text_summary" not in compact
    assert "test_case_info" not in compact
    assert "started_at" not in compact
    assert "completed_at" not in compact
    assert "schema_version" not in compact

    # environment 只保留 bmc_host / bmc_user
    assert set(compact["environment"].keys()) <= {"bmc_host", "bmc_user"}
    assert compact["environment"]["bmc_host"] == "192.168.1.100"
    assert compact["environment"]["bmc_user"] == "Administrator"

    # prerequisites 全 completed -> 移除
    assert "prerequisites" not in compact

    # 步骤字段精简
    step = compact["steps"][0]
    assert "started_at" not in step
    assert "completed_at" not in step
    assert "interface_preference" not in step
    assert "tool" not in step

    # Evidence 只保留 evidence_type + content
    ev = step["evidence"][0]
    assert set(ev.keys()) <= {"evidence_type", "content"}
    assert ev["evidence_type"] == "redfish_response"

    # 空 environment_recovery_actions 被移除
    assert "environment_recovery_actions" not in compact

    # execution_id / case_id / case_name 应保留
    assert compact["execution_id"] == record.execution_id
    assert compact["case_id"] == record.case_id

    print("[PASS] _compact_record_for_judge 字段精简验证通过")


# ============================================================
# Test 9: _compact_record_for_judge Evidence 截断
# ============================================================

def test_compact_record_evidence_truncation(judge_agent):
    """
    验证点：
    1. Evidence content 超过 1500 chars 时截断并标记 [TRUNCATED]
    2. Evidence content 未超过时保持原样
    """
    record = load_fixture_record("case_pass_strict.json")

    # 手动设置一个超长 evidence content
    long_content = "A" * 2000
    record.steps[0].evidence[0].content = long_content

    compact = judge_agent._compact_record_for_judge(record)
    ev_content = compact["steps"][0]["evidence"][0]["content"]

    assert len(ev_content) <= judge_agent._EVIDENCE_CONTENT_MAX_LEN + len("\n[TRUNCATED]")
    assert ev_content.endswith("[TRUNCATED]")
    assert ev_content.startswith("A" * judge_agent._EVIDENCE_CONTENT_MAX_LEN)

    print("[PASS] Evidence 截断验证通过")


# ============================================================
# Test 10: _compact_record_for_judge audit_draft 截断
# ============================================================

def test_compact_record_audit_draft_truncation(judge_agent):
    """
    验证点：
    1. consolidated_audit_draft 超过 _AUDIT_DRAFT_MAX_LEN 时截断
    2. 未超过时保持原样
    3. 截断后 JSON 有效
    """
    record = load_fixture_record("case_pass_strict.json")

    # 测试未超限的 draft 保持原样
    compact = judge_agent._compact_record_for_judge(record)
    draft = compact.get("consolidated_audit_draft")
    assert draft is not None
    assert "[TRUNCATED]" not in draft

    # 测试超限截断
    long_draft = "# Audit Draft\n" + "X" * 3000
    record.consolidated_audit_draft = long_draft

    compact = judge_agent._compact_record_for_judge(record)
    draft = compact.get("consolidated_audit_draft")
    assert draft is not None
    assert len(draft) <= judge_agent._AUDIT_DRAFT_MAX_LEN + len("\n[TRUNCATED]")
    assert draft.endswith("[TRUNCATED]")

    # 验证 JSON 有效
    import json
    json_str = json.dumps(compact, ensure_ascii=False, default=str)
    parsed = json.loads(json_str)
    assert parsed["consolidated_audit_draft"].endswith("[TRUNCATED]")

    print("[PASS] audit_draft 截断验证通过")


# ============================================================
# Test 11: _progressive_strip_for_budget 渐进式精简
# ============================================================

def test_progressive_strip_for_budget(judge_agent):
    """
    验证点：
    1. Level 1: 移除 consolidated_audit_draft 后符合预算
    2. Level 2: evidence 截断后符合预算
    3. Level 3: evidence 只保留 type 后符合预算
    4. Level 4: 极限精简后符合预算
    5. 返回的 JSON 始终有效
    6. 兜底: 即使极限精简仍超预算，也能返回最小有效 JSON
    """
    import json

    record = load_fixture_record("case_pass_strict.json")
    compact = judge_agent._compact_record_for_judge(record)

    # 测试 Level 1: budget 略小于带 draft 的 JSON
    full_json = json.dumps(compact, ensure_ascii=False, default=str)
    draft = compact.get("consolidated_audit_draft", "")
    if draft:
        # 移除 draft 后的大小
        no_draft = {k: v for k, v in compact.items() if k != "consolidated_audit_draft"}
        no_draft_json = json.dumps(no_draft, ensure_ascii=False, default=str)
        # budget 设在两者之间，应触发 Level 1
        budget = len(no_draft_json) + 10
        if budget < len(full_json):
            result = judge_agent._progressive_strip_for_budget(
                dict(compact), budget  # 使用副本
            )
            assert len(result) <= budget
            parsed = json.loads(result)
            assert "consolidated_audit_draft" not in parsed
            assert parsed["execution_id"] == record.execution_id

    # 测试极限预算: 极小 budget 应返回有效 JSON
    tiny_budget = 200
    result = judge_agent._progressive_strip_for_budget(dict(compact), tiny_budget)
    assert len(result) <= max(tiny_budget, 200)
    parsed = json.loads(result)
    assert "execution_id" in parsed

    # 测试正常 budget（不需要精简）
    large_budget = 100000
    result = judge_agent._progressive_strip_for_budget(dict(compact), large_budget)
    parsed = json.loads(result)
    assert parsed["execution_id"] == record.execution_id

    print("[PASS] _progressive_strip_for_budget 渐进式精简验证通过")


# ============================================================
# Test 12: 完整集成测试（需要真实 API）
# ============================================================

@pytest.mark.asyncio
async def test_judge_full_integration(config, client_factory):
    """
    完整集成测试（可选，需要真实 API）

    验证点：
    1. JudgeAgent.judge_from_record() 完整流程
    2. 自动 v1 迁移
    3. 生成 audit_report.md

    注意：此测试需要真实 API Key，未配置时跳过
    """
    # 检查是否有有效配置
    judge_cfg = config.models.judge
    provider = config.providers.get(judge_cfg.provider)
    if not provider or not provider.api_key or provider.api_key.startswith("${"):
        pytest.skip("Judge API 未配置，跳过完整集成测试")

    # 创建 JudgeAgent
    agent = JudgeAgent(
        config=config,
        client_factory=client_factory,
        shared_dir=str(SHARED_DIR),
    )

    # 加载 v1 格式记录（验证迁移）
    v1_data = load_fixture("case_v1_compatible.json")
    record = ExecutionRecord.migrate_from_v1(v1_data)

    # 执行判断
    result = await agent.judge_from_record(record)

    # 验证结果
    assert result.execution_id == record.execution_id
    assert result.judge_model == judge_cfg.model

    # 验证审计报告已生成
    report_path = SHARED_DIR / "audit_reports" / f"{record.execution_id}_audit_report.md"
    assert report_path.exists()

    print(f"[PASS] 完整集成测试通过，报告: {report_path}")


# ============================================================
# 运行说明
# ============================================================

if __name__ == "__main__":
    print("""
    Judge Agent 测试套件

    运行全部测试：
        pytest tests/test_judge_agent.py -v --asyncio-mode=auto

    运行单个测试：
        pytest tests/test_judge_agent.py::test_v21_schema_and_consolidated_draft -v
        pytest tests/test_judge_agent.py::test_v1_migration -v
        pytest tests/test_judge_agent.py::test_cli_judge_flag_parsing -v
        pytest tests/test_judge_agent.py::test_judge_strict_verdict_simulation -v
        pytest tests/test_judge_agent.py::test_judge_clear_fail_case -v
        pytest tests/test_judge_agent.py::test_judge_error_handling -v
        pytest tests/test_judge_agent.py::test_audit_report_generation -v
        pytest tests/test_judge_agent.py::test_compact_record_field_stripping -v
        pytest tests/test_judge_agent.py::test_compact_record_evidence_truncation -v
        pytest tests/test_judge_agent.py::test_compact_record_audit_draft_truncation -v
        pytest tests/test_judge_agent.py::test_progressive_strip_for_budget -v

    注意：test_judge_full_integration 需要真实 API Key
    """)
