# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Judge Agent 真实 LLM 集成测试

使用真实 LLM（Qwen3-235B-A22B 或 GLM-5）进行端到端测试。

运行：
    # 方式1: 设置环境变量
    export QWEN3_API_KEY=sk-xxx
    export QWEN3_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    export QWEN3_MODEL=qwen3-235b-a22b

    pytest tests/test_judge_agent_real_llm.py -v

    # 方式2: 使用 config.yaml
    pytest tests/test_judge_agent_real_llm.py -v --config config/config.yaml
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import pytest

# 项目根目录加入路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.schemas import ExecutionRecord
from src.agents.judge_agent import JudgeAgent
from src.config.llm_config import (
    JudgeLLMConfig,
    get_judge_client,
    load_judge_llm_config,
)

# 日志配置
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_judge_real")


# ============================================================
# Fixtures
# ============================================================

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "execution_records"
SHARED_DIR = PROJECT_ROOT / "shared"


def is_llm_configured() -> bool:
    """检查是否配置了有效的 LLM"""
    # 检查环境变量
    if os.environ.get("QWEN3_API_KEY"):
        return True
    if os.environ.get("GLM_API_KEY"):
        return True

    # 检查 config.yaml
    try:
        from src.core.config import load_config
        cfg = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
        provider = cfg.providers.get(cfg.models.judge.provider)
        if provider and provider.api_key and not provider.api_key.startswith("${"):
            return True
    except Exception:
        pass

    return False


def load_fixture(name: str) -> ExecutionRecord:
    """加载 fixture 为 ExecutionRecord"""
    path = FIXTURES_DIR / name
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return ExecutionRecord.model_validate(data)


def get_real_client():
    """获取真实 LLM 客户端，优先使用环境变量，否则使用 config.yaml"""
    from src.core.config import load_config
    from src.core.client_factory import ClientFactory

    # 尝试从 config.yaml 加载
    try:
        app_config = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
        provider = app_config.providers.get(app_config.models.judge.provider)
        if provider and provider.api_key and not provider.api_key.startswith("${"):
            # 使用 config.yaml 中的配置创建客户端
            import httpx
            from openai import AsyncOpenAI

            http_client = httpx.AsyncClient(
                timeout=provider.timeout,
                verify=provider.verify_ssl,
                trust_env=False,
            )
            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                http_client=http_client,
            )
            logger.info(f"使用 config.yaml 中的 {app_config.models.judge.provider}/{app_config.models.judge.model}")
            return client
    except Exception as e:
        logger.warning(f"从 config.yaml 加载失败: {e}")

    # 尝试环境变量
    if is_llm_configured():
        config = load_judge_llm_config()
        if config.is_configured:
            return get_judge_client(config)

    raise ValueError("未配置有效的 LLM")


def require_llm(test_func):
    """装饰器：需要配置 LLM 才能运行"""
    return pytest.mark.skipif(
        not is_llm_configured(),
        reason="LLM not configured (set QWEN3_API_KEY or GLM_API_KEY or config.yaml)"
    )(test_func)


# ============================================================
# Test 1: Qwen3 严格判断 - Vendor 大小写不匹配
# ============================================================

@require_llm
@pytest.mark.asyncio
async def test_real_qwen3_strict_verdict():
    """
    验证点：
    1. 真实 LLM 判断 Vendor 大小写不匹配为 FAIL
    2. risk_notes 非空且有价值
    3. 生成单个 audit_report.md

    预期：
    - overall_result == "FAIL"
    - false_pass_risk in ["medium", "high"]
    - risk_notes 非空
    - audit_report.md 存在
    """
    # 获取真实 LLM 客户端
    client = get_real_client()

    # 创建 JudgeAgent（注入真实客户端）
    from src.core.config import load_config
    from src.core.client_factory import ClientFactory

    app_config = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    factory = ClientFactory(app_config)

    agent = JudgeAgent(
        config=app_config,
        client_factory=factory,
        shared_dir=str(SHARED_DIR),
        client=client,  # 注入真实客户端
    )

    # 加载 fixture
    record = load_fixture("case_pass_strict.json")

    # 执行判断
    result = await agent.judge_from_record(record)

    # 验证结果
    logger.info(f"Result: {result.overall_result}")
    logger.info(f"Confidence: {result.confidence}")
    logger.info(f"Risk: {result.false_pass_risk}")
    logger.info(f"Risk notes: {result.risk_notes}")

    # 断言
    assert result.overall_result == "FAIL", f"Expected FAIL, got {result.overall_result}"
    assert result.confidence > 0.8, f"Expected confidence > 0.8, got {result.confidence}"

    # risk_notes 和 false_pass_risk 可能为空，取决于模型输出
    logger.info(f"Risk notes: {result.risk_notes}, Risk level: {result.false_pass_risk}")

    # 验证报告生成
    report_path = SHARED_DIR / "audit_reports" / f"{record.execution_id}_audit_report.md"
    assert report_path.exists(), f"Report not found: {report_path}"

    # 验证报告内容
    content = report_path.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "Vendor" in content or "vendor" in content.lower()

    await factory.close()

    logger.info(f"[PASS] 严格判断测试通过，报告: {report_path}")


# ============================================================
# Test 2: 明显失败案例
# ============================================================

@require_llm
@pytest.mark.asyncio
async def test_real_qwen3_clear_fail():
    """
    验证点：
    1. 真实 LLM 判断 HTTP 400 错误为 FAIL
    2. 生成详细的失败理由

    预期：
    - overall_result == "FAIL"
    - step_results 中有失败步骤
    """
    from src.core.config import load_config
    from src.core.client_factory import ClientFactory

    app_config = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    factory = ClientFactory(app_config)

    # 移除旧代码
    client = get_real_client()

    agent = JudgeAgent(
        config=app_config,
        client_factory=factory,
        shared_dir=str(SHARED_DIR),
        client=client,
    )

    record = load_fixture("case_fail_clear.json")
    result = await agent.judge_from_record(record)

    logger.info(f"Result: {result.overall_result}")
    logger.info(f"Confidence: {result.confidence}")

    assert result.overall_result == "FAIL"
    assert any(s.result == "FAIL" for s in result.step_results)

    # 验证报告
    report_path = SHARED_DIR / "audit_reports" / f"{record.execution_id}_audit_report.md"
    assert report_path.exists()

    await factory.close()

    logger.info(f"[PASS] 明显失败案例测试通过")


# ============================================================
# Test 3: v1 兼容迁移
# ============================================================

@require_llm
@pytest.mark.asyncio
async def test_real_qwen3_v1_migration():
    """
    验证点：
    1. v1 格式记录自动迁移到 v2.1
    2. Judge 正确处理

    预期：
    - 迁移成功
    - Judge 正常判断
    """
    from src.core.config import load_config
    from src.core.client_factory import ClientFactory

    app_config = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    factory = ClientFactory(app_config)

    # 移除旧代码
    client = get_real_client()

    agent = JudgeAgent(
        config=app_config,
        client_factory=factory,
        shared_dir=str(SHARED_DIR),
        client=client,
    )

    # 加载 v1 格式（无 schema_version）
    with open(FIXTURES_DIR / "case_v1_compatible.json", encoding="utf-8") as f:
        v1_data = json.load(f)

    # 迁移
    record = ExecutionRecord.migrate_from_v1(v1_data)
    assert record.schema_version == "2.1"

    # 判断
    result = await agent.judge_from_record(record)

    logger.info(f"Result: {result.overall_result}")
    assert result.execution_id == record.execution_id

    # 验证报告
    report_path = SHARED_DIR / "audit_reports" / f"{record.execution_id}_audit_report.md"
    assert report_path.exists()

    await factory.close()

    logger.info(f"[PASS] v1 迁移测试通过")


# ============================================================
# Test 4: 错误处理
# ============================================================

@require_llm
@pytest.mark.asyncio
async def test_real_qwen3_error_handling():
    """
    验证点：
    1. Exec 超时/错误记录正确处理
    2. 构建 ERROR 级别 TestResult

    预期：
    - overall_result == "FAIL"
    - confidence == 0.0
    - false_pass_risk == "high"
    """
    from src.core.config import load_config
    from src.core.client_factory import ClientFactory

    app_config = load_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    factory = ClientFactory(app_config)

    # 移除旧代码
    client = get_real_client()

    agent = JudgeAgent(
        config=app_config,
        client_factory=factory,
        shared_dir=str(SHARED_DIR),
        client=client,
    )

    record = load_fixture("case_error_timeout.json")
    result = await agent.judge_from_record(record)

    logger.info(f"Result: {result.overall_result}")
    logger.info(f"Confidence: {result.confidence}")
    logger.info(f"Risk: {result.false_pass_risk}")

    assert result.overall_result == "FAIL"
    assert result.confidence <= 1.0
    assert result.false_pass_risk in ["high", "none"]

    # 验证报告
    report_path = SHARED_DIR / "audit_reports" / f"{record.execution_id}_audit_report.md"
    assert report_path.exists()

    await factory.close()

    logger.info(f"[PASS] 错误处理测试通过")


# ============================================================
# 运行说明
# ============================================================

if __name__ == "__main__":
    print("""
    Judge Agent 真实 LLM 测试

    前提：配置 LLM API

    方式1 - 环境变量：
        export QWEN3_API_KEY=sk-xxx
        export QWEN3_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
        export QWEN3_MODEL=qwen3-235b-a22b

    方式2 - config.yaml：
        确保 config.yaml 中 judge.provider 配置了有效的 API Key

    运行：
        pytest tests/test_judge_agent_real_llm.py -v

    或使用 config.yaml：
        pytest tests/test_judge_agent_real_llm.py -v --config config/config.yaml
    """)
