# -*- coding: utf-8 -*-
"""
Environment Recovery Tool - v1.0.1

Recovery flow (decision tree):
  Pure BMC mode (no os_host):
    -> Force cleanup users via ipmitool binary -> check BMC auth
  OS + BMC mode:
    OS reachable -> skip ForcePowerCycle -> check BMC auth -> fail then ipmitool reset user 2
    OS unreachable -> ForcePowerCycle -> wait for host boot -> check BMC auth -> fail then reset user 2

Note: ForcePowerCycle only cycles the host power, does NOT restart BMC,
does NOT invalidate Redfish sessions.

Script-form tool, called by ExecAgent.
"""

import asyncio
import base64
import logging
import shlex
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
    """BMC environment recovery tool (v1.0.1)."""

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
        logger.info("[Recovery] === START ===")

        # Force cleanup: runs on EVERY recover, ensures clean environment
        await self._force_restore_admin_user2()
        await self._delete_users_3_to_17()

        if not self.os_host:
            # ---- Pure BMC mode: no OS, never execute ForcePowerCycle ----
            logger.info("[Recovery] Pure BMC mode, skip power cycle, only check auth")
            auth_ok = await self._check_bmc_auth()
            if not auth_ok:
                warnings.append("BMC auth failed and no OS-side recovery path")

        else:
            os_ok = await self._check_os_ssh()
            if os_ok:
                # ---- OS reachable: skip ForcePowerCycle ----
                logger.info("[Recovery] OS reachable, skip power cycle")
                auth_ok = await self._check_bmc_auth()
                if not auth_ok:
                    auth_ok = await self._os_ipmitool_reset_user2()
            else:
                # ---- OS unreachable: execute ForcePowerCycle (on host) ----
                logger.warning("[Recovery] OS unreachable, execute Redfish ForcePowerCycle")
                power_ok = await self._redfish_force_power_cycle()
                if power_ok:
                    logger.info(f"[Recovery] waiting for host boot ({self.wait_min}s)")
                    await asyncio.sleep(self.wait_min)

                auth_ok = await self._check_bmc_auth()
                if not auth_ok and self.os_host:
                    auth_ok = await self._os_ipmitool_reset_user2()

        tag = "[OK]" if auth_ok else "[FAIL]"
        logger.info(f"[Recovery] === {tag} {time.monotonic() - t0:.0f}s ===")
        return RecoveryResult(recovered=auth_ok, warnings=warnings)

    # ------------------------------------------------------------------
    # Force cleanup (runs on EVERY recover)
    # ------------------------------------------------------------------

    async def _force_restore_admin_user2(self) -> bool:
        """Force restore user 2 as Administrator (runs on every recover).

        OS mode: in-band via SSH (ipmitool on host, NO -H/-U/-P, no auth needed).
        Pure BMC mode: out-of-band via local ipmitool binary (-H/-U/-P required).
        """
        cmds = [
            "user set name 2 Administrator",
            f"user set password 2 {self.bmc_password}",
            "user enable 2",
            "user priv 2 4",
        ]
        if self.os_host:
            # In-band: OS host has local ipmitool, talks to BMC via /dev/ipmi0
            for cmd in cmds:
                if not await self._os_ssh_exec(f"ipmitool {cmd}"):
                    logger.warning(f"[Recovery] user 2 in-band restore failed: {cmd}")
                    return False
        else:
            # Out-of-band: test server -> BMC via lanplus, needs credentials
            for cmd in cmds:
                if not await self._ipmi_binary_exec(cmd):
                    logger.warning(f"[Recovery] user 2 out-of-band restore failed: {cmd}")
                    return False
        logger.info("[Recovery] user 2 force restored (Administrator, priv=4)")
        return True

    async def _delete_users_3_to_17(self) -> int:
        """Delete users 3-17 (runs on every recover), return count cleaned.

        OS mode: in-band via SSH (no auth needed).
        Pure BMC mode: out-of-band via local ipmitool binary.
        """
        cleaned = 0
        for uid in range(3, 18):
            if self.os_host:
                # In-band: single compound command via SSH
                ok = await self._os_ssh_exec(
                    f"ipmitool user disable {uid} && ipmitool user set name {uid} ''"
                )
            else:
                # Out-of-band: two separate calls
                ok = await self._ipmi_binary_exec(f"user disable {uid}")
                if ok:
                    ok = await self._ipmi_binary_exec(f"user set name {uid} ''")
            if ok:
                cleaned += 1
            else:
                logger.debug(f"[Recovery] cleanup uid={uid} failed (may not exist)")
        logger.info(f"[Recovery] user cleanup done, cleaned {cleaned}/15 users")
        return cleaned

    # ------------------------------------------------------------------
    # Execution backends: SSH (OS side) vs local binary (pure BMC)
    # ------------------------------------------------------------------

    async def _ipmi_binary_exec(self, ipmi_subcmd: str) -> bool:
        """Run ipmitool binary locally (fallback when no OS SSH available)."""
        try:
            cmd_parts = shlex.split(ipmi_subcmd)
        except ValueError:
            return False
        if not cmd_parts:
            return False
        args = [
            "/usr/bin/ipmitool",
            "-I", "lanplus",
            "-H", self.ipmi_host,
            "-U", self.bmc_user,
            "-P", self.bmc_password,
            "-p", str(self.ipmi_port),
        ] + cmd_parts
        logger.debug(
            "[Recovery] binary exec: /usr/bin/ipmitool -I lanplus "
            "-H %s -U %s -P *** -p %s %s",
            self.ipmi_host, self.bmc_user, self.ipmi_port, ipmi_subcmd,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except FileNotFoundError:
            logger.warning("[Recovery] ipmitool binary not found at /usr/bin/ipmitool")
            return False
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            logger.warning(f"[Recovery] ipmitool binary timeout: {ipmi_subcmd}")
            return False
        except Exception as e:
            logger.debug(f"[Recovery] ipmitool binary exec error: {e}")
            return False

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            logger.debug(
                "[Recovery] ipmitool binary rc=%d stderr=%s",
                proc.returncode, stderr_text,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # OS SSH checks
    # ------------------------------------------------------------------

    async def _check_os_ssh(self) -> bool:
        if not self.os_host:
            return False
        return await self._os_ssh_exec("echo ok")

    async def _check_bmc_auth(self) -> bool:
        """Single BMC auth check (no waiting)."""
        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl, timeout=10.0, trust_env=False,
            ) as c:
                token = await self._redfish_login(c)
                if token:
                    await self._redfish_logout(c, token)
                    logger.info("[Recovery] BMC auth OK")
                    return True
                logger.warning("[Recovery] BMC auth failed (password may have been changed)")
                return False
        except Exception as e:
            logger.warning(f"[Recovery] BMC unreachable: {e}")
            return False

    # ------------------------------------------------------------------
    # Redfish operations
    # ------------------------------------------------------------------

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
            logger.warning(f"[Recovery] ForcePowerCycle error: {e}")
            return False

    async def _wait_and_check_bmc_auth(self) -> bool:
        logger.info(f"[Recovery] Step 3: wait for BMC ready (max {self.wait_max}s)")
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
                        logger.info("[Recovery] BMC auth OK")
                        return True
                    return False  # reachable but auth failed -> Step 4
            except (httpx.ConnectError, httpx.TimeoutException):
                await asyncio.sleep(30)
            except Exception:
                await asyncio.sleep(30)
        logger.warning(f"[Recovery] BMC not ready within {self.wait_max}s")
        return False

    # ------------------------------------------------------------------
    # OS-side ipmitool reset (fallback in decision tree)
    # ------------------------------------------------------------------

    async def _os_ipmitool_reset_user2(self) -> bool:
        """Reset user 2 via OS SSH (in-band, no BMC auth needed)."""
        if not self.os_host:
            return False
        cmds = [
            f"ipmitool user set name 2 {self.bmc_user}",
            f"ipmitool user set password 2 {self.bmc_password}",
            "ipmitool user enable 2",
            "ipmitool user priv 2 4",
        ]
        for cmd in cmds:
            if not await self._os_ssh_exec(cmd):
                logger.warning(f"[Recovery] in-band reset failed: {cmd}")
                return False
        logger.info("[Recovery] user 2 in-band reset OK")
        return True

    # ------------------------------------------------------------------
    # SSH / Redfish helpers
    # ------------------------------------------------------------------

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
            logger.debug(f"[Recovery] SSH exec error: {e}")
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
