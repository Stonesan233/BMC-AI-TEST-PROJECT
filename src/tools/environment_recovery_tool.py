# -*- coding: utf-8 -*-
"""
Environment Recovery Tool - BMC 环境恢复工具

职责:
  - 在测试用例执行完毕后自动恢复 BMC 环境到干净状态
  - 恢复流程: 检查 OS SSH -> Redfish ForcePowerCycle 上电 -> 检查 BMC 认证
               -> 若失败则从 OS 执行 ipmitool 重置 user 2
  - 以脚本形式 tool 供 ExecAgent 调用，而非让 LLM 直接执行命令

用法:
  from src.tools.environment_recovery_tool import EnvironmentRecoveryTool

  tool = EnvironmentRecoveryTool(
      bmc_host="192.168.1.100",
      bmc_port=443,
      bmc_user="Administrator",
      bmc_password="Admin@90000",
      verify_ssl=False,
      os_host="192.168.1.101",       # 可选
      os_user="root",                # 可选
      os_password="xxx",             # 可选
  )
  result = await tool.recover()
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("tools.environment_recovery")


# ======================================================================
# 数据结构
# ======================================================================


@dataclass
class RecoveryStepResult:
    """单个恢复步骤的结果"""

    step_name: str
    success: bool
    message: str = ""
    duration_seconds: float = 0.0
    timestamp: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecoveryResult:
    """完整恢复流程的结果"""

    recovered: bool = False
    steps: list = field(default_factory=list)  # List[RecoveryStepResult]
    total_duration: float = 0.0
    warnings: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""


# ======================================================================
# EnvironmentRecoveryTool
# ======================================================================


class EnvironmentRecoveryTool:
    """
    BMC 环境恢复工具.

    恢复流程（严格顺序）:
      1. 检查 OS SSH 可达性
      2. Redfish ForcePowerCycle 上电（等待 5-10min）
      3. 检查 BMC 认证
      4. 若失败则从 OS 执行 ipmitool 重置 user 2
    """

    # ForcePowerCycle 后等待 BMC 就绪的参数
    POWER_CYCLE_WAIT_MIN = 300   # 最短等待 5 分钟
    POWER_CYCLE_WAIT_MAX = 600   # 最长等待 10 分钟
    BMC_READY_CHECK_INTERVAL = 30  # 每 30 秒检查一次 BMC 是否就绪

    def __init__(
        self,
        bmc_host: str,
        bmc_port: int = 443,
        bmc_user: str = "Administrator",
        bmc_password: str = "",
        verify_ssl: bool = False,
        os_host: Optional[str] = None,
        os_user: Optional[str] = None,
        os_password: Optional[str] = None,
        ipmi_host: Optional[str] = None,
        ipmi_port: int = 623,
    ):
        self.bmc_host = bmc_host
        self.bmc_port = bmc_port
        self.bmc_user = bmc_user
        self.bmc_password = bmc_password
        self.verify_ssl = verify_ssl

        # OS SSH（可选，用于 ipmitool 重置）
        self.os_host = os_host
        self.os_user = os_user or "root"
        self.os_password = os_password or ""

        # IPMI（用于备用恢复路径）
        self.ipmi_host = ipmi_host or bmc_host
        self.ipmi_port = ipmi_port

    # ==================================================================
    # 主入口
    # ==================================================================

    async def recover(self) -> RecoveryResult:
        """
        执行完整环境恢复流程.

        Returns:
            RecoveryResult 包含每一步的详细结果
        """
        started_at = datetime.now()
        result = RecoveryResult(started_at=started_at.isoformat())
        overall_start = time.monotonic()

        logger.info("=" * 60)
        logger.info("[Recovery] 开始环境恢复流程")
        logger.info("=" * 60)

        # Step 1: 检查 OS SSH 可达性
        step1 = await self._check_os_ssh()
        result.steps.append(step1)
        os_ssh_ok = step1.success
        if not os_ssh_ok:
            result.warnings.append("OS SSH 不可达，跳过 OS 端恢复路径")

        # Step 2: Redfish ForcePowerCycle 上电
        step2 = await self._redfish_force_power_cycle()
        result.steps.append(step2)
        if not step2.success:
            result.warnings.append(
                f"Redfish ForcePowerCycle 失败: {step2.message}"
            )
            # 如果 OS SSH 可达，尝试从 OS 端执行 ipmitool power reset
            if os_ssh_ok:
                logger.warning("[Recovery] 尝试从 OS 端执行 ipmitool chassis power cycle")
                step2b = await self._os_ipmitool_power_cycle()
                result.steps.append(step2b)
                if not step2b.success:
                    result.warnings.append(f"OS ipmitool power cycle 也失败: {step2b.message}")

        # Step 3: 等待 BMC 就绪并检查认证
        step3 = await self._wait_and_check_bmc_auth()
        result.steps.append(step3)
        bmc_auth_ok = step3.success

        # Step 4: 若 BMC 认证失败，从 OS 执行 ipmitool 重置 user 2
        if not bmc_auth_ok and os_ssh_ok:
            logger.warning("[Recovery] BMC 认证失败，尝试从 OS 重置 user 2")
            step4 = await self._os_ipmitool_reset_user2()
            result.steps.append(step4)
            if not step4.success:
                result.warnings.append(
                    f"OS ipmitool 重置 user 2 失败: {step4.message}"
                )

        # 汇总
        result.total_duration = time.monotonic() - overall_start
        result.completed_at = datetime.now().isoformat()

        # 判定恢复是否成功: BMC 认证可通过即视为成功
        result.recovered = bmc_auth_ok or any(
            s.step_name == "os_ipmitool_reset_user2" and s.success
            for s in result.steps
        )

        status = "[OK]" if result.recovered else "[FAIL]"
        logger.info("=" * 60)
        logger.info(f"[Recovery] 环境恢复{status} | 耗时 {result.total_duration:.0f}s")
        if result.warnings:
            for w in result.warnings:
                logger.warning(f"[Recovery] 警告: {w}")
        logger.info("=" * 60)

        return result

    # ==================================================================
    # Step 1: 检查 OS SSH
    # ==================================================================

    async def _check_os_ssh(self) -> RecoveryStepResult:
        """检查 OS SSH 是否可达。"""
        start = time.monotonic()
        step_name = "check_os_ssh"
        logger.info(f"[Recovery] Step 1: 检查 OS SSH ({self.os_host})")

        if not self.os_host:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message="未配置 os_host，跳过 OS SSH 检查",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

        try:
            from src.tools.ssh_tool import SSHTool

            ssh = SSHTool(
                host=self.os_host,
                port=22,
                user=self.os_user,
                password=self.os_password,
                connect_timeout=15,
            )
            res = await ssh.execute("echo ok", timeout=10)
            ok = res.success and "ok" in res.raw_stdout.lower()
            return RecoveryStepResult(
                step_name=step_name,
                success=ok,
                message="OS SSH 可达" if ok else f"SSH 执行失败: {res.error}",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
                details={"host": self.os_host, "user": self.os_user},
            )
        except Exception as e:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message=f"OS SSH 检查异常: {e}",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

    # ==================================================================
    # Step 2: Redfish ForcePowerCycle
    # ==================================================================

    async def _redfish_force_power_cycle(self) -> RecoveryStepResult:
        """
        执行 Redfish Oem ForcePowerCycle 上电.

        POST /redfish/v1/Systems/1/Actions/Oem/Huawei/ComputerSystem.FruControl
        Header: X-Auth-Token, Content-Type: application/json
        Body: {"FruControlType": "ForcePowerCycle", "FruID": 0}
        """
        start = time.monotonic()
        step_name = "redfish_force_power_cycle"
        logger.info("[Recovery] Step 2: Redfish ForcePowerCycle 上电")

        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl,
                timeout=30.0,
                trust_env=False,
            ) as client:
                # 2a. 先获取 Session Token
                token = await self._get_redfish_session_token(client)
                if not token:
                    return RecoveryStepResult(
                        step_name=step_name,
                        success=False,
                        message="无法获取 Redfish Session Token",
                        duration_seconds=time.monotonic() - start,
                        timestamp=datetime.now().isoformat(),
                    )

                # 2b. 发送 ForcePowerCycle
                endpoint = (
                    "/redfish/v1/Systems/1/Actions/"
                    "Oem/Huawei/ComputerSystem.FruControl"
                )
                headers = {
                    "X-Auth-Token": token,
                    "Content-Type": "application/json",
                }
                body = {
                    "FruControlType": "ForcePowerCycle",
                    "FruID": 0,
                }

                resp = await client.post(endpoint, headers=headers, json=body)

                # 注销 Session
                await self._delete_redfish_session(client, token)

                if resp.status_code in (200, 201, 204):
                    logger.info(
                        f"[Recovery] ForcePowerCycle 成功 "
                        f"(HTTP {resp.status_code}), 等待 BMC 重启..."
                    )
                    # 2c. 等待 BMC 就绪
                    await self._wait_for_bmc_ready(client)
                    return RecoveryStepResult(
                        step_name=step_name,
                        success=True,
                        message=f"ForcePowerCycle 成功 (HTTP {resp.status_code})",
                        duration_seconds=time.monotonic() - start,
                        timestamp=datetime.now().isoformat(),
                        details={"http_status": resp.status_code},
                    )
                else:
                    resp_text = resp.text[:500]
                    return RecoveryStepResult(
                        step_name=step_name,
                        success=False,
                        message=(
                            f"ForcePowerCycle 失败: HTTP {resp.status_code}, "
                            f"响应: {resp_text}"
                        ),
                        duration_seconds=time.monotonic() - start,
                        timestamp=datetime.now().isoformat(),
                        details={"http_status": resp.status_code, "body": resp_text},
                    )

        except Exception as e:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message=f"Redfish ForcePowerCycle 异常: {e}",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

    # ==================================================================
    # Step 2b (fallback): OS 端 ipmitool power cycle
    # ==================================================================

    async def _os_ipmitool_power_cycle(self) -> RecoveryStepResult:
        """从 OS SSH 执行 ipmitool chassis power cycle."""
        start = time.monotonic()
        step_name = "os_ipmitool_power_cycle"
        logger.info("[Recovery] Step 2b: OS ipmitool power cycle")

        try:
            from src.tools.ssh_tool import SSHTool

            ssh = SSHTool(
                host=self.os_host,
                port=22,
                user=self.os_user,
                password=self.os_password,
                connect_timeout=15,
            )

            # power cycle
            cmd = (
                f"ipmitool -H {self.ipmi_host} -U {self.bmc_user} "
                f"-P {self.bmc_password} chassis power cycle"
            )
            res = await ssh.execute(cmd, timeout=30)

            if res.success:
                logger.info("[Recovery] ipmitool power cycle 已发送, 等待 BMC 重启...")
                await asyncio.sleep(self.POWER_CYCLE_WAIT_MIN)
                return RecoveryStepResult(
                    step_name=step_name,
                    success=True,
                    message="ipmitool power cycle 成功",
                    duration_seconds=time.monotonic() - start,
                    timestamp=datetime.now().isoformat(),
                )
            else:
                return RecoveryStepResult(
                    step_name=step_name,
                    success=False,
                    message=f"ipmitool power cycle 失败: {res.error}",
                    duration_seconds=time.monotonic() - start,
                    timestamp=datetime.now().isoformat(),
                )
        except Exception as e:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message=f"OS ipmitool power cycle 异常: {e}",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

    # ==================================================================
    # Step 3: 等待 BMC 就绪 + 检查认证
    # ==================================================================

    async def _wait_and_check_bmc_auth(self) -> RecoveryStepResult:
        """等待 BMC 重启完成并验证认证。"""
        start = time.monotonic()
        step_name = "check_bmc_auth"
        logger.info(
            f"[Recovery] Step 3: 等待 BMC 就绪 "
            f"(最久 {self.POWER_CYCLE_WAIT_MAX}s)..."
        )

        deadline = time.monotonic() + self.POWER_CYCLE_WAIT_MAX
        last_error = ""

        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(
                    base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                    verify=self.verify_ssl,
                    timeout=10.0,
                    trust_env=False,
                ) as client:
                    # 尝试获取 Session
                    token = await self._get_redfish_session_token(client)
                    if token:
                        # 认证成功，注销 session
                        await self._delete_redfish_session(client, token)
                        return RecoveryStepResult(
                            step_name=step_name,
                            success=True,
                            message="BMC 认证成功",
                            duration_seconds=time.monotonic() - start,
                            timestamp=datetime.now().isoformat(),
                            details={"host": self.bmc_host},
                        )

                    # Redfish 可达但认证失败
                    last_error = "Redfish 可达但认证失败（密码可能被改）"
                    logger.warning(f"[Recovery] {last_error}")
                    break  # 不再重试，交给 Step 4 重置密码

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = str(e)
                elapsed = time.monotonic() - start
                logger.info(
                    f"[Recovery] BMC 未就绪 ({elapsed:.0f}s), "
                    f"等待 {self.BMC_READY_CHECK_INTERVAL}s 后重试..."
                )
                await asyncio.sleep(self.BMC_READY_CHECK_INTERVAL)
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(self.BMC_READY_CHECK_INTERVAL)

        return RecoveryStepResult(
            step_name=step_name,
            success=False,
            message=f"BMC 认证检查失败: {last_error}",
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now().isoformat(),
        )

    # ==================================================================
    # Step 4: 从 OS 端 ipmitool 重置 user 2
    # ==================================================================

    async def _os_ipmitool_reset_user2(self) -> RecoveryStepResult:
        """
        从 OS SSH 执行 ipmitool 重置 BMC user 2 (Administrator).

        流程:
          1. ipmitool user set name 2 Administrator
          2. ipmitool user set password 2 <password>
          3. ipmitool user enable 2
          4. ipmitool channel setaccess 1 2 privilege=4
        """
        start = time.monotonic()
        step_name = "os_ipmitool_reset_user2"
        logger.info("[Recovery] Step 4: 从 OS 重置 BMC user 2")

        if not self.os_host:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message="未配置 os_host，无法执行 ipmitool 重置",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

        try:
            from src.tools.ssh_tool import SSHTool

            ssh = SSHTool(
                host=self.os_host,
                port=22,
                user=self.os_user,
                password=self.os_password,
                connect_timeout=15,
            )

            ipmi_base = (
                f"ipmitool -H {self.ipmi_host} -U {self.bmc_user} "
                f"-P {self.bmc_password}"
            )
            results = {}

            # 1. 设置用户名
            cmd_name = f"{ipmi_base} user set name 2 {self.bmc_user}"
            res = await ssh.execute(cmd_name, timeout=15)
            results["set_name"] = {"success": res.success, "output": res.raw_stdout[:200]}

            # 2. 设置密码
            cmd_pw = f"{ipmi_base} user set password 2 {self.bmc_password}"
            res = await ssh.execute(cmd_pw, timeout=15)
            results["set_password"] = {"success": res.success, "output": res.raw_stdout[:200]}

            # 3. 启用用户
            cmd_enable = f"{ipmi_base} user enable 2"
            res = await ssh.execute(cmd_enable, timeout=15)
            results["enable"] = {"success": res.success, "output": res.raw_stdout[:200]}

            # 4. 设置权限级别 4 (Administrator)
            cmd_priv = f"{ipmi_base} channel setaccess 1 2 privilege=4"
            res = await ssh.execute(cmd_priv, timeout=15)
            results["set_privilege"] = {"success": res.success, "output": res.raw_stdout[:200]}

            all_ok = all(r["success"] for r in results.values())
            return RecoveryStepResult(
                step_name=step_name,
                success=all_ok,
                message=(
                    "user 2 重置成功" if all_ok
                    else f"部分步骤失败: {json.dumps(results, ensure_ascii=False)}"
                ),
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
                details=results,
            )

        except Exception as e:
            return RecoveryStepResult(
                step_name=step_name,
                success=False,
                message=f"OS ipmitool 重置 user 2 异常: {e}",
                duration_seconds=time.monotonic() - start,
                timestamp=datetime.now().isoformat(),
            )

    # ==================================================================
    # Redfish Session 管理
    # ==================================================================

    async def _get_redfish_session_token(
        self, client: httpx.AsyncClient
    ) -> Optional[str]:
        """通过 POST /redfish/v1/SessionService/Sessions 获取 Token。"""
        try:
            import base64

            credentials = f"{self.bmc_user}:{self.bmc_password}"
            auth_header = "Basic " + base64.b64encode(credentials.encode()).decode()

            resp = await client.post(
                "/redfish/v1/SessionService/Sessions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": auth_header,
                },
                json={
                    "UserName": self.bmc_user,
                    "Password": self.bmc_password,
                },
            )
            if resp.status_code in (200, 201):
                token = resp.headers.get("X-Auth-Token", "")
                if token:
                    return token
                # 某些实现把 token 放在 body 里
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return body.get("Id", "")
            return None
        except Exception:
            return None

    async def _delete_redfish_session(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        """注销 Redfish Session。"""
        try:
            await client.delete(
                "/redfish/v1/SessionService/Sessions/" + token,
                headers={"X-Auth-Token": token},
            )
        except Exception:
            pass

    async def _wait_for_bmc_ready(self, client: httpx.AsyncClient) -> None:
        """
        ForcePowerCycle 后轮询等待 BMC 就绪.

        先等待最短时间，然后开始检查，直到最长等待或就绪。
        """
        logger.info(
            f"[Recovery] 等待 BMC 重启: "
            f"最短 {self.POWER_CYCLE_WAIT_MIN}s, "
            f"最长 {self.POWER_CYCLE_WAIT_MAX}s"
        )
        await asyncio.sleep(self.POWER_CYCLE_WAIT_MIN)

        deadline = time.monotonic() + self.POWER_CYCLE_WAIT_MAX - self.POWER_CYCLE_WAIT_MIN
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    "/redfish/v1",
                    headers={"Accept": "application/json"},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    elapsed = self.POWER_CYCLE_WAIT_MIN + (
                        self.POWER_CYCLE_WAIT_MAX - self.POWER_CYCLE_WAIT_MIN
                        - (deadline - time.monotonic())
                    )
                    logger.info(f"[Recovery] BMC 已就绪 (耗时 {elapsed:.0f}s)")
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(self.BMC_READY_CHECK_INTERVAL)

        logger.warning(
            f"[Recovery] BMC 在 {self.POWER_CYCLE_WAIT_MAX}s 内未就绪"
        )
