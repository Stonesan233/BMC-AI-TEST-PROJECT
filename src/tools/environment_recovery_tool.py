# -*- coding: utf-8 -*-
"""
Environment Recovery Tool - v1.0.1 精简版

恢复流程（决策树）:
  纯 BMC 模式（无 os_host）:
    -> 只做 BMC 认证检查，绝不执行 ForcePowerCycle
  OS + BMC 模式:
    OS 可达 -> 跳过 ForcePowerCycle -> 检查 BMC 认证 -> 失败则 ipmitool 重置 user 2
    OS 不可达 -> ForcePowerCycle 上电 -> 等待主机启动 -> 检查 BMC 认证 -> 失败则重置 user 2

注意: ForcePowerCycle 只对主机 Power Cycle，不会重启 BMC，不会使 Redfish Session 失效。
脚本形式 tool，供 ExecAgent 调用。
"""

import asyncio
import base64
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("tools.environment_recovery")


class RecoveryResult:
    __slots__ = ("recovered", "warnings")

    def __init__(self, recovered: bool = False, warnings: Optional[List[str]] = None):
        self.recovered = recovered
        self.warnings = warnings or []


class EnvironmentRecoveryTool:
    """BMC 环境恢复工具（精简版）。"""

    def __init__(self, config: Dict[str, Any]):
        self.bmc_host = config["bmc_host"]
        self.bmc_port = config.get("bmc_port", 443)
        self.bmc_user = config.get("bmc_user", "Administrator")
        self.bmc_password = config.get("bmc_password", "")
        self.verify_ssl = config.get("verify_ssl", False)
        self.os_host = config.get("os_host")
        self.os_user = config.get("os_user", "root")
        self.os_password = config.get("os_password", "")
        self.ipmi_host = config.get("ipmi_host", self.bmc_host)
        self.ipmi_port = config.get("ipmi_port", 623)
        self.wait_min = config.get("recovery_wait_min", 300)
        self.wait_max = config.get("recovery_wait_max", 600)

    async def recover(self) -> RecoveryResult:
        warnings: List[str] = []
        t0 = time.monotonic()
        logger.info("[Recovery] === 开始 ===")

        if not self.os_host:
            # ---- 纯 BMC 模式：无 OS 配置，绝不执行 ForcePowerCycle ----
            logger.info("[Recovery] Pure BMC mode, skip power cycle, only check auth")
            auth_ok = await self._check_bmc_auth()
            if not auth_ok:
                warnings.append("BMC 认证失败且无 OS 端恢复路径")

        else:
            os_ok = await self._check_os_ssh()
            if os_ok:
                # ---- OS 可达：跳过 ForcePowerCycle ----
                logger.info("[Recovery] OS reachable, skip power cycle")
                auth_ok = await self._check_bmc_auth()
                if not auth_ok:
                    auth_ok = await self._os_ipmitool_reset_user2()
            else:
                # ---- OS 不可达：执行 ForcePowerCycle 上电（对主机） ----
                logger.warning("[Recovery] OS unreachable, execute Redfish ForcePowerCycle")
                power_ok = await self._redfish_force_power_cycle()
                if power_ok:
                    logger.info(f"[Recovery] 等待主机启动 ({self.wait_min}s)")
                    await asyncio.sleep(self.wait_min)

                auth_ok = await self._check_bmc_auth()
                if not auth_ok and self.os_host:
                    auth_ok = await self._os_ipmitool_reset_user2()

        tag = "[OK]" if auth_ok else "[FAIL]"
        logger.info(f"[Recovery] === {tag} {time.monotonic() - t0:.0f}s ===")
        return RecoveryResult(recovered=auth_ok, warnings=warnings)

    async def _check_os_ssh(self) -> bool:
        if not self.os_host:
            return False
        return await self._os_ssh_exec("echo ok")

    async def _check_bmc_auth(self) -> bool:
        """单次 BMC 认证检查（不等待，用于纯 BMC 模式和 OS 正常场景）。"""
        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl, timeout=10.0, trust_env=False,
            ) as c:
                token = await self._redfish_login(c)
                if token:
                    await self._redfish_logout(c, token)
                    logger.info("[Recovery] BMC 认证成功")
                    return True
                logger.warning("[Recovery] BMC 认证失败（密码可能被改）")
                return False
        except Exception as e:
            logger.warning(f"[Recovery] BMC 不可达: {e}")
            return False

    async def _redfish_force_power_cycle(self) -> bool:
        """POST Oem/Huawei/ComputerSystem.FruControl + ForcePowerCycle"""
        logger.info("[Recovery] Step 2: Redfish ForcePowerCycle")
        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl, timeout=30.0, trust_env=False,
            ) as c:
                token = await self._redfish_login(c)
                if not token:
                    return False
                resp = await c.post(
                    "/redfish/v1/Systems/1/Actions/Oem/Huawei/ComputerSystem.FruControl",
                    headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                    json={"FruControlType": "ForcePowerCycle", "FruID": 0},
                )
                await self._redfish_logout(c, token)
                ok = resp.status_code in (200, 201, 204)
                logger.info(f"[Recovery] ForcePowerCycle {'OK' if ok else 'FAIL'} "
                            f"(HTTP {resp.status_code})")
                return ok
        except Exception as e:
            logger.warning(f"[Recovery] ForcePowerCycle 异常: {e}")
            return False

    async def _wait_and_check_bmc_auth(self) -> bool:
        logger.info(f"[Recovery] Step 3: 等待 BMC 就绪 (最久 {self.wait_max}s)")
        await asyncio.sleep(self.wait_min)
        deadline = time.monotonic() + (self.wait_max - self.wait_min)
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(
                    base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                    verify=self.verify_ssl, timeout=10.0, trust_env=False,
                ) as c:
                    token = await self._redfish_login(c)
                    if token:
                        await self._redfish_logout(c, token)
                        logger.info("[Recovery] BMC 认证成功")
                        return True
                    return False  # 可达但认证失败 -> Step 4
            except (httpx.ConnectError, httpx.TimeoutException):
                await asyncio.sleep(30)
            except Exception:
                await asyncio.sleep(30)
        logger.warning(f"[Recovery] BMC 在 {self.wait_max}s 内未就绪")
        return False

    async def _os_ipmitool_reset_user2(self) -> bool:
        if not self.os_host:
            return False
        ipmi = f"ipmitool -H {self.ipmi_host} -U {self.bmc_user} -P {self.bmc_password}"
        cmds = [
            f"{ipmi} user set name 2 {self.bmc_user}",
            f"{ipmi} user set password 2 {self.bmc_password}",
            f"{ipmi} user enable 2",
            f"{ipmi} channel setaccess 1 2 privilege=4",
        ]
        for cmd in cmds:
            if not await self._os_ssh_exec(cmd):
                logger.warning(f"[Recovery] 重置失败: {cmd[:80]}")
                return False
        logger.info("[Recovery] user 2 重置成功")
        return True

    async def _os_ssh_exec(self, cmd: str, capture: bool = False) -> Any:
        if not self.os_host:
            return "" if capture else False
        try:
            from src.tools.ssh_tool import SSHTool
            ssh = SSHTool(host=self.os_host, port=22,
                          user=self.os_user, password=self.os_password,
                          connect_timeout=15)
            res = await ssh.execute(cmd, timeout=15)
            if capture:
                return res.raw_stdout if res.success else ""
            return res.success
        except Exception as e:
            logger.debug(f"[Recovery] SSH exec 异常: {e}")
            return "" if capture else False

    async def _redfish_login(self, client: httpx.AsyncClient) -> Optional[str]:
        resp = await client.post(
            "/redfish/v1/SessionService/Sessions",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(
                    f"{self.bmc_user}:{self.bmc_password}".encode()
                ).decode(),
            },
            json={"UserName": self.bmc_user, "Password": self.bmc_password},
        )
        if resp.status_code in (200, 201):
            return resp.headers.get("X-Auth-Token") or resp.json().get("Id")
        return None

    async def _redfish_logout(self, client: httpx.AsyncClient, token: str) -> None:
        try:
            await client.delete(
                f"/redfish/v1/SessionService/Sessions/{token}",
                headers={"X-Auth-Token": token},
            )
        except Exception:
            pass
