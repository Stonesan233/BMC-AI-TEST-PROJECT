# -*- coding: utf-8 -*-
"""
集成测试 - IPMI MC Info 查询 BMC 版本号

前置条件：本地 openUBMC QEMU 环境已启动，IPMI RMCP+ 服务在 127.0.0.1:10623

运行方式：
    python -m pytest tests/integration/test_ipmitool_mc_info.py -v -s

验证内容：
    1. 框架能成功加载 IPMI 用例并执行完整流程（Exec -> Judge -> Report）
    2. overall_result 为 PASS
    3. 报告中包含 "mc info" 的原始输出
    4. raw_stdout 中能看到 BMC 版本相关信息（Manufacturer ID / Product ID）
"""

import json
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

CASE_FILE = PROJECT_ROOT / "testcases" / "test_ipmitool_mc_info.yaml"
CONFIG_FILE = PROJECT_ROOT / "config" / "config_ipmi_test.yaml"
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
    通过 main.py 执行完整测试流程，返回 subprocess.CompletedProcess。

    使用 config_ipmi_test.yaml 配置（ipmi_port=10623, bmc_password=Admin@90000）。
    """
    ensure_shared_dirs(str(SHARED_DIR))

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--config", str(CONFIG_FILE),
        "--cases", str(CASE_FILE),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=120,
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
        data = json.load(f)

    return ExecutionRecord.model_validate(data)


@pytest.fixture(scope="module")
def test_result(run_result):
    """从 shared/test_results/ 中读取最新的 TestResult"""
    results_dir = SHARED_DIR / "test_results"
    json_files = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    if not json_files:
        pytest.skip("未找到 TestResult 文件")

    latest = json_files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data


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

    def test_case_file_exists(self):
        """测试用例文件存在"""
        assert CASE_FILE.exists(), f"用例文件不存在: {CASE_FILE}"

    def test_config_file_exists(self):
        """测试配置文件存在"""
        assert CONFIG_FILE.exists(), f"配置文件不存在: {CONFIG_FILE}"

    def test_case_has_steps(self, case_data):
        """用例包含至少一个测试步骤"""
        steps = case_data.get("测试步骤", [])
        assert len(steps) >= 1, "用例中无测试步骤"

    def test_case_command_is_mc_info(self, case_data):
        """用例步骤命令为 mc info"""
        steps = case_data.get("测试步骤", [])
        assert steps[0].get("command") == "mc info", (
            f"命令不正确: {steps[0].get('command')}"
        )


# ------------------------------------------------------------------
# 测试：ExecutionRecord
# ------------------------------------------------------------------

class TestExecutionRecord:
    """验证 ExecutionRecord 内容"""

    def test_record_exists(self, execution_record):
        """ExecutionRecord 成功生成"""
        assert execution_record is not None

    def test_has_steps(self, execution_record):
        """至少有一个步骤"""
        assert len(execution_record.steps) >= 1

    def test_step_tool_is_ipmi(self, execution_record):
        """步骤使用了 ipmi 工具"""
        first_step = execution_record.steps[0]
        assert first_step.tool in ("ipmi", "ipmi_command"), (
            f"工具类型不正确: {first_step.tool}"
        )

    def test_step_command_is_mc_info(self, execution_record):
        """步骤命令包含 mc info"""
        first_step = execution_record.steps[0]
        assert first_step.command and "mc info" in first_step.command, (
            f"命令不正确: {first_step.command}"
        )

    def test_step_has_raw_stdout(self, execution_record):
        """步骤包含 raw_stdout"""
        first_step = execution_record.steps[0]
        assert first_step.raw_stdout, "raw_stdout 为空"

    def test_raw_stdout_has_bmc_info(self, execution_record):
        """raw_stdout 包含 BMC 版本相关信息"""
        first_step = execution_record.steps[0]
        stdout_text = first_step.raw_stdout or ""
        has_bmc_info = any(
            kw in stdout_text
            for kw in ("Manufacturer ID", "Product ID", "Device ID", "Firmware")
        )
        assert has_bmc_info, (
            f"raw_stdout 中未找到 BMC 版本信息: {stdout_text[:300]}"
        )


# ------------------------------------------------------------------
# 测试：判断结果
# ------------------------------------------------------------------

class TestJudgeResult:
    """验证判断结果"""

    def test_overall_result_is_pass(self, test_result):
        """overall_result 为 PASS"""
        assert test_result.get("overall_result") == "PASS", (
            f"overall_result 不为 PASS: {test_result.get('overall_result')}"
        )

    def test_step_result_is_pass(self, test_result):
        """步骤判断结果为 PASS"""
        step_results = test_result.get("step_results", [])
        assert len(step_results) >= 1, "无步骤判断结果"
        assert step_results[0].get("result") == "PASS", (
            f"步骤结果不为 PASS: {step_results[0].get('result')}"
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
        assert "IPMI" in report_content or "MC Info" in report_content

    def test_report_has_mc_info_output(self, report_content):
        """报告中包含 mc info 原始输出"""
        has_mc_info = any(
            kw in report_content
            for kw in ("Manufacturer ID", "Product ID", "Device ID", "mc info")
        )
        assert has_mc_info, "报告中未找到 mc info 输出"

    def test_report_has_bmc_version_info(self, report_content):
        """报告中包含 BMC 版本信息"""
        has_version = any(
            kw in report_content
            for kw in ("Manufacturer", "Product", "Firmware", "BMC")
        )
        assert has_version, "报告中未找到 BMC 版本相关信息"


# ------------------------------------------------------------------
# 测试：IPMI 连通性（可选，需要 QEMU 环境运行）
# ------------------------------------------------------------------

class TestIPMIConnectivity:
    """
    验证真实 IPMI 服务连通性。

    此测试类需要本地 QEMU 环境运行中。
    如果环境不可达，测试将被 skip。
    """

    @pytest.fixture(autouse=True)
    def check_ipmi(self):
        """检查 IPMI 服务是否可达"""
        ipmitool_bin = self._get_ipmitool_bin()
        if not ipmitool_bin:
            pytest.skip("ipmitool 二进制未找到")

        try:
            result = subprocess.run(
                [
                    str(ipmitool_bin),
                    "-H", "127.0.0.1",
                    "-U", "Administrator",
                    "-P", "Admin@90000",
                    "-p", "10623",
                    "-I", "lanplus",
                    "-C", "17",
                    "mc", "info",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                pytest.skip("IPMI 服务不可达 (127.0.0.1:10623)")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pytest.skip("IPMI 服务不可达 (127.0.0.1:10623)")

    @staticmethod
    def _get_ipmitool_bin() -> Path:
        """获取当前平台的 ipmitool 二进制路径"""
        import platform
        if platform.system() == "Windows":
            return PROJECT_ROOT / "tools" / "ipmitool" / "windows" / "ipmitool.exe"
        return PROJECT_ROOT / "tools" / "ipmitool" / "linux" / "ipmitool"

    def _run_mc_info(self) -> subprocess.CompletedProcess:
        """执行 ipmitool mc info"""
        ipmitool_bin = self._get_ipmitool_bin()
        return subprocess.run(
            [
                str(ipmitool_bin),
                "-H", "127.0.0.1",
                "-U", "Administrator",
                "-P", "Admin@90000",
                "-p", "10623",
                "-I", "lanplus",
                "-C", "17",
                "mc", "info",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_ipmi_mc_info_exit_code(self):
        """ipmitool mc info 执行成功（exit code 0）"""
        result = self._run_mc_info()
        assert result.returncode == 0, (
            f"mc info 执行失败: {result.stderr}"
        )

    def test_ipmi_mc_info_has_manufacturer(self):
        """mc info 输出包含 Manufacturer ID"""
        result = self._run_mc_info()
        assert "Manufacturer ID" in result.stdout, (
            f"输出中未找到 Manufacturer ID: {result.stdout[:200]}"
        )

    def test_ipmi_mc_info_has_product_id(self):
        """mc info 输出包含 Product ID"""
        result = self._run_mc_info()
        assert "Product ID" in result.stdout, (
            f"输出中未找到 Product ID: {result.stdout[:200]}"
        )

    def test_ipmi_mc_info_has_device_id(self):
        """mc info 输出包含 Device ID"""
        result = self._run_mc_info()
        assert "Device ID" in result.stdout, (
            f"输出中未找到 Device ID: {result.stdout[:200]}"
        )
