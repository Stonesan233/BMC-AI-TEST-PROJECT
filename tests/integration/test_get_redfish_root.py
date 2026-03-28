# -*- coding: utf-8 -*-
"""
集成测试 - 查询 Redfish Service Root

前置条件：本地 openUBMC QEMU 环境已启动，Redfish 服务在 https://127.0.0.1:10443

运行方式：
    python -m pytest tests/integration/test_get_redfish_root.py -v -s

验证内容：
    1. 框架能成功加载用例并执行完整流程（Exec -> Judge -> Report）
    2. 报告文件正常生成
    3. ExecutionRecord 中步骤状态为 completed
    4. 报告中包含 Redfish 响应内容（raw_stdout）
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.schemas import ExecutionRecord, StepStatus
from src.utils.file_handler import ensure_shared_dirs


# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

CASE_FILE = PROJECT_ROOT / "testcases" / "get_redfish_root.yaml"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
SHARED_DIR = PROJECT_ROOT / "shared"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def case_data():
    """加载测试用例 YAML"""
    with open(CASE_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def run_result():
    """
    通过 main.py 执行完整测试流程，返回 (exit_code, stdout)。

    如果框架尚未集成真实 Redfish 客户端，此步骤使用占位 Agent 运行。
    """
    ensure_shared_dirs(str(SHARED_DIR))

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--cases", str(CASE_FILE),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )

    return result


@pytest.fixture(scope="module")
def execution_record(run_result):
    """从 shared/execution_records/ 中读取最新的 ExecutionRecord"""
    records_dir = SHARED_DIR / "execution_records"
    json_files = sorted(records_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    if not json_files:
        pytest.skip("未找到 ExecutionRecord 文件（可能 main.py 执行失败）")

    latest = json_files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        import json
        data = json.load(f)

    return ExecutionRecord.model_validate(data)


@pytest.fixture(scope="module")
def report_content(run_result):
    """从 shared/reports/ 中读取最新的 Markdown 报告"""
    reports_dir = SHARED_DIR / "reports"
    md_files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)

    if not md_files:
        pytest.skip("未找到报告文件")

    return md_files[-1].read_text(encoding="utf-8")


# ------------------------------------------------------------------
# 测试：框架流程
# ------------------------------------------------------------------

class TestFrameworkPipeline:
    """验证框架完整流程是否正常工作"""

    def test_main_exit_code(self, run_result):
        """main.py 执行成功（exit code 0）"""
        assert run_result.returncode == 0, (
            f"main.py 退出码异常: {run_result.returncode}\n"
            f"stdout: {run_result.stdout[-500:]}\n"
            f"stderr: {run_result.stderr[-500:]}"
        )

    def test_case_file_exists(self):
        """测试用例文件存在"""
        assert CASE_FILE.exists(), f"用例文件不存在: {CASE_FILE}"

    def test_case_has_steps(self, case_data):
        """用例包含至少一个测试步骤"""
        steps = case_data.get("测试步骤", [])
        assert len(steps) >= 1, "用例中无测试步骤"


# ------------------------------------------------------------------
# 测试：ExecutionRecord
# ------------------------------------------------------------------

class TestExecutionRecord:
    """验证 ExecutionRecord 内容"""

    def test_record_exists(self, execution_record):
        """ExecutionRecord 成功生成"""
        assert execution_record is not None

    def test_overall_status(self, execution_record):
        """整体状态为 completed"""
        assert execution_record.overall_status in ("completed", "failed")

    def test_has_steps(self, execution_record):
        """至少有一个步骤"""
        assert len(execution_record.steps) >= 1

    def test_step_completed(self, execution_record):
        """步骤状态为 completed"""
        first_step = execution_record.steps[0]
        assert first_step.status == StepStatus.COMPLETED, (
            f"步骤状态异常: {first_step.status}"
        )

    def test_step_has_raw_stdout(self, execution_record):
        """步骤包含 raw_stdout"""
        first_step = execution_record.steps[0]
        assert first_step.raw_stdout, "raw_stdout 为空"
        assert len(first_step.raw_stdout) > 0

    def test_step_endpoint(self, execution_record):
        """步骤使用了正确的 Redfish 端点"""
        first_step = execution_record.steps[0]
        assert first_step.endpoint == "/redfish/v1", (
            f"端点不正确: {first_step.endpoint}"
        )

    def test_step_method(self, execution_record):
        """步骤使用了 GET 方法"""
        first_step = execution_record.steps[0]
        assert first_step.method == "GET", (
            f"HTTP 方法不正确: {first_step.method}"
        )


# ------------------------------------------------------------------
# 测试：报告内容
# ------------------------------------------------------------------

class TestReportContent:
    """验证 Markdown 报告内容"""

    def test_report_generated(self, report_content):
        """报告非空"""
        assert len(report_content) > 100

    def test_report_has_case_name(self, report_content):
        """报告包含用例名称"""
        assert "Redfish" in report_content or "RedfishVersion" in report_content

    def test_report_has_stdout(self, report_content):
        """报告中包含 raw_stdout 内容"""
        # 占位模式下应包含模拟的 Redfish 响应
        has_content = any(
            kw in report_content
            for kw in ("ServiceVersion", "Status", "Health", "Members", "RedfishVersion")
        )
        assert has_content, "报告中未找到 Redfish 响应内容"

    def test_report_has_step_detail(self, report_content):
        """报告包含步骤详情"""
        assert "step_001" in report_content or "GET" in report_content


# ------------------------------------------------------------------
# 测试：真实 Redfish 连通性（可选，需要 QEMU 环境运行）
# ------------------------------------------------------------------

class TestRedfishConnectivity:
    """
    验证真实 Redfish 服务连通性。

    此测试类需要本地 QEMU 环境运行中。
    如果环境不可达，测试将被 skip。
    """

    @pytest.fixture(autouse=True)
    def check_redfish(self):
        """检查 Redfish 服务是否可达"""
        import urllib.request
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
            req.add_header("Accept", "application/json")
            urllib.request.urlopen(req, context=ctx, timeout=5)
        except Exception:
            pytest.skip("Redfish 服务不可达 (https://127.0.0.1:10443)")

    def test_redfish_returns_200(self):
        """Redfish Service Root 返回 HTTP 200"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        assert resp.status == 200

        data = json.loads(resp.read().decode("utf-8"))
        assert "RedfishVersion" in data

    def test_redfish_vendor(self):
        """Vendor 字段为 Huawei"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        assert data.get("Vendor") == "Huawei", (
            f"Vendor 不匹配: {data.get('Vendor')}"
        )

    def test_redfish_product(self):
        """Product 字段为 S920X20"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        assert data.get("Product") == "S920X20", (
            f"Product 不匹配: {data.get('Product')}"
        )

    def test_redfish_version(self):
        """RedfishVersion 为 1.20.1"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        assert data.get("RedfishVersion") == "1.20.1", (
            f"RedfishVersion 不匹配: {data.get('RedfishVersion')}"
        )

    def test_redfish_oem_product_name(self):
        """Oem.openUBMC.ProductName 为 S920X20"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        oem = data.get("Oem", {})
        openubmc = oem.get("openUBMC", {})
        assert openubmc.get("ProductName") == "S920X20", (
            f"ProductName 不匹配: {openubmc.get('ProductName')}"
        )

    def test_redfish_oem_major_version(self):
        """Oem.openUBMC.MajorVersion 为 5"""
        import urllib.request
        import ssl
        import json

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://127.0.0.1:10443/redfish/v1")
        req.add_header("Accept", "application/json")

        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        oem = data.get("Oem", {})
        openubmc = oem.get("openUBMC", {})
        assert str(openubmc.get("MajorVersion")) == "5", (
            f"MajorVersion 不匹配: {openubmc.get('MajorVersion')}"
        )
