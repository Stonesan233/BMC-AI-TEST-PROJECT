# -*- coding: utf-8 -*-
"""
Environment Recovery Tool - v2.0.0 (Hybrid Mode)

Recovery flow (decision tree):
  Phase 1 (Rules Engine - deterministic):
    Pure BMC mode (no os_host):
      -> Force cleanup users via ipmitool binary -> check BMC auth
    OS + BMC mode:
      OS reachable -> skip ForcePowerCycle -> check BMC auth -> fail then ipmitool reset user 2
      OS unreachable -> ForcePowerCycle -> wait for host boot -> check BMC auth -> fail then reset user 2

  Phase 2 (LLM Assisted - when Phase 1 fails and mode is HYBRID):
    -> Collect failure context -> LLM diagnosis -> validate repair actions -> execute repair

Recovery modes:
  RULES_ONLY:  Phase 1 only (original behavior)
  LLM_ONLY:   Skip Phase 1, go directly to LLM diagnosis
  HYBRID:     Phase 1 first, Phase 2 if Phase 1 fails (default)

Note: ForcePowerCycle only cycles the host power, does NOT restart BMC,
does NOT invalidate Redfish sessions.

Script-form tool, called by ExecAgent.
"""

import asyncio
import base64
import enum
import json
import logging
import re
import shlex
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("tools.environment_recovery")


# ======================================================================
# Recovery Mode
# ======================================================================

class RecoveryMode(enum.Enum):
    """Environment recovery mode selection."""
    RULES_ONLY = "rules_only"   # Phase 1 only: deterministic rules engine
    LLM_ONLY = "llm_only"      # Phase 2 only: LLM diagnosis (skip rules)
    HYBRID = "hybrid"           # Phase 1 + Phase 2 fallback (default)


class RecoveryResult:
    __slots__ = ("recovered", "warnings", "phase", "diagnosis")

    def __init__(
        self,
        recovered: bool = False,
        warnings: Optional[List[str]] = None,
        phase: str = "",
        diagnosis: Optional[Dict[str, Any]] = None,
    ):
        self.recovered = recovered
        self.warnings = warnings or []
        self.phase = phase
        self.diagnosis = diagnosis


# ======================================================================
# LLM Diagnostic Prompt Template
# ======================================================================

LLM_DIAGNOSTIC_PROMPT = """\
You are a BMC (Baseboard Management Controller) environment recovery diagnostician.
Analyze the following recovery failure context and provide a diagnosis with repair actions.

## Failure Context

- Errors encountered: {errors}
- BMC status: {bmc_status}
- Actions already tried: {tried_actions}
- Environment info: host={bmc_host}, port={bmc_port}, ipmi_host={ipmi_host}, os_host={os_host}

## Your Task

Diagnose the root cause and propose repair actions. Output ONLY a JSON object with this schema:

{{
  "diagnosis": "Brief description of the root cause",
  "repair_actions": [
    {{
      "action": "Action type (one of: ipmi_command, ssh_command, redfish_request, power_cycle, wait_retry)",
      "params": {{}},
      "description": "What this action does"
    }}
  ],
  "risk_level": "low|medium|high",
  "fallback": "What to do if all repair actions fail"
}}

## Constraints

1. Allowed action types: ipmi_command, ssh_command, redfish_request, power_cycle, wait_retry
2. ipmi_command params: {{"command": "user set name 2 Administrator"}}
3. ssh_command params: {{"command": "ipmitool user set password 2 PASSWORD"}}
4. redfish_request params: {{"endpoint": "/redfish/v1/...", "method": "POST", "body": {{}}}}
5. power_cycle: no params needed (uses Redfish ForcePowerCycle)
6. wait_retry params: {{"seconds": 30}}
7. Do NOT propose destructive actions like factory reset or firmware update
8. Do NOT propose actions that modify BMC network configuration
9. Keep repair_actions minimal (1-3 actions)
10. If the failure is likely transient (network glitch, BMC busy), propose wait_retry
"""


# ======================================================================
# Repair Action Whitelist
# ======================================================================

# Allowed action types
_ALLOWED_ACTION_TYPES = {
    "ipmi_command", "ssh_command", "redfish_request",
    "power_cycle", "wait_retry",
}

# Allowed IPMI subcommand prefixes (lowercase)
_ALLOWED_IPMI_PREFIXES = [
    "user ", "mc info", "mc guid", "chassis status",
    "chassis power ", "sel ", "sdr ", "sensor ",
    "fru ",
]

# Blocked keywords in any command (case-insensitive)
_BLOCKED_KEYWORDS = [
    "factory", "reset to default", "firmware update", "flash",
    "tftp", "dhcp", "network config", "lan set",
    "rm -rf", "mkfs", "fdisk", "format",
]


class EnvironmentRecoveryTool:
    """BMC environment recovery tool (v2.0.0 - Hybrid Mode)."""

    def __init__(
        self,
        config: Dict[str, Any],
        llm_client: Optional[Any] = None,
        llm_model: str = "",
        mode: RecoveryMode = RecoveryMode.HYBRID,
    ):
        """
        Initialize recovery tool.

        Args:
            config: BMC connection config dict.
            llm_client: AsyncOpenAI-compatible client for LLM diagnosis (Phase 2).
                        None means LLM-only and hybrid modes degrade to rules-only.
            llm_model: Model name for LLM diagnosis.
            mode: Recovery mode (RULES_ONLY / LLM_ONLY / HYBRID).
        """
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

        # LLM integration (Phase 2)
        self._llm_client = llm_client
        self._llm_model = llm_model
        self._mode = mode

        # Tracking for LLM context
        self._tried_actions: List[str] = []
        self._errors: List[str] = []

        mode_name = self._mode.value if isinstance(self._mode, RecoveryMode) else str(self._mode)
        llm_status = "available" if self._llm_client else "not configured"
        logger.info(
            f"[Recovery] mode={mode_name}, llm={llm_status}"
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def recover(self) -> RecoveryResult:
        """
        Execute environment recovery based on configured mode.

        RULES_ONLY:  Run Phase 1 only (deterministic rules engine).
        LLM_ONLY:    Skip Phase 1, run Phase 2 (LLM diagnosis) directly.
        HYBRID:      Phase 1 first; if failed, fallback to Phase 2.
        """
        warnings: List[str] = []
        t0 = time.monotonic()
        logger.info(f"[Recovery] === START (mode={self._mode.value}) ===")

        self._tried_actions = []
        self._errors = []

        if self._mode == RecoveryMode.LLM_ONLY:
            result = await self._llm_assisted_recovery(None)
            tag = "[OK]" if result.recovered else "[FAIL]"
            logger.info(
                f"[Recovery] === {tag} (LLM_ONLY) "
                f"{time.monotonic() - t0:.0f}s ==="
            )
            return result

        # RULES_ONLY or HYBRID: start with Phase 1
        rules_result = await self._rules_based_recovery()
        if rules_result.recovered:
            tag = "[OK]"
            logger.info(
                f"[Recovery] === {tag} (Phase 1) "
                f"{time.monotonic() - t0:.0f}s ==="
            )
            return RecoveryResult(
                recovered=True,
                warnings=rules_result.warnings,
                phase="phase1_rules",
            )

        # Phase 1 failed
        warnings.extend(rules_result.warnings)

        if self._mode == RecoveryMode.RULES_ONLY:
            tag = "[FAIL]"
            logger.info(
                f"[Recovery] === {tag} (RULES_ONLY) "
                f"{time.monotonic() - t0:.0f}s ==="
            )
            return RecoveryResult(
                recovered=False,
                warnings=warnings,
                phase="phase1_rules",
            )

        # HYBRID: try Phase 2
        logger.info("[Recovery] Phase 1 failed, starting Phase 2 (LLM assisted)")
        llm_result = await self._llm_assisted_recovery(rules_result)
        if llm_result.recovered:
            tag = "[OK]"
            logger.info(
                f"[Recovery] === {tag} (Phase 2 LLM) "
                f"{time.monotonic() - t0:.0f}s ==="
            )
            return llm_result

        # Both phases failed
        warnings.extend(llm_result.warnings)
        tag = "[FAIL]"
        logger.info(
            f"[Recovery] === {tag} (Phase 1 + Phase 2 exhausted) "
            f"{time.monotonic() - t0:.0f}s ==="
        )
        return RecoveryResult(
            recovered=False,
            warnings=warnings,
            phase="phase2_llm",
            diagnosis=llm_result.diagnosis,
        )

    # ------------------------------------------------------------------
    # Phase 1: Rules-based recovery (original logic)
    # ------------------------------------------------------------------

    async def _rules_based_recovery(self) -> RecoveryResult:
        """Phase 1: Deterministic rules engine (original recovery logic)."""
        warnings: List[str] = []

        # Force cleanup: runs on EVERY recover, ensures clean environment
        self._tried_actions.append("force_restore_admin_user2")
        user2_ok = await self._force_restore_admin_user2()
        if not user2_ok:
            self._errors.append("force_restore_admin_user2 failed")

        self._tried_actions.append("delete_users_3_to_17")
        await self._delete_users_3_to_17()

        auth_ok = False

        if not self.os_host:
            # Pure BMC mode: no OS, never execute ForcePowerCycle
            logger.info("[Recovery] Pure BMC mode, skip power cycle, only check auth")
            self._tried_actions.append("check_bmc_auth")
            auth_ok = await self._check_bmc_auth()
            if not auth_ok:
                self._errors.append("BMC auth failed, no OS-side recovery path")
                warnings.append("BMC auth failed and no OS-side recovery path")
        else:
            self._tried_actions.append("check_os_ssh")
            os_ok = await self._check_os_ssh()
            if os_ok:
                # OS reachable: skip ForcePowerCycle
                logger.info("[Recovery] OS reachable, skip power cycle")
                self._tried_actions.append("check_bmc_auth")
                auth_ok = await self._check_bmc_auth()
                if not auth_ok:
                    self._errors.append("BMC auth failed, trying OS-side ipmitool reset")
                    self._tried_actions.append("os_ipmitool_reset_user2")
                    auth_ok = await self._os_ipmitool_reset_user2()
                    if not auth_ok:
                        self._errors.append("OS-side ipmitool reset user 2 failed")
            else:
                # OS unreachable: execute ForcePowerCycle (on host)
                self._errors.append("OS unreachable")
                self._tried_actions.append("redfish_force_power_cycle")
                logger.warning("[Recovery] OS unreachable, execute Redfish ForcePowerCycle")
                power_ok = await self._redfish_force_power_cycle()
                if power_ok:
                    logger.info(f"[Recovery] waiting for host boot ({self.wait_min}s)")
                    self._tried_actions.append(f"wait_boot_{self.wait_min}s")
                    await asyncio.sleep(self.wait_min)
                else:
                    self._errors.append("ForcePowerCycle failed")

                self._tried_actions.append("check_bmc_auth")
                auth_ok = await self._check_bmc_auth()
                if not auth_ok and self.os_host:
                    self._errors.append("BMC auth failed after power cycle, trying OS-side reset")
                    self._tried_actions.append("os_ipmitool_reset_user2")
                    auth_ok = await self._os_ipmitool_reset_user2()
                    if not auth_ok:
                        self._errors.append("OS-side ipmitool reset user 2 failed after power cycle")

        return RecoveryResult(
            recovered=auth_ok,
            warnings=warnings,
            phase="phase1_rules",
        )

    # ------------------------------------------------------------------
    # Phase 2: LLM-assisted recovery
    # ------------------------------------------------------------------

    async def _llm_assisted_recovery(
        self, failed_result: Optional[RecoveryResult]
    ) -> RecoveryResult:
        """
        Phase 2: LLM-assisted recovery.

        When Phase 1 fails, collect failure context and ask LLM for diagnosis.
        LLM proposes repair actions which are validated against a whitelist
        before execution.
        """
        if not self._llm_client:
            logger.warning("[Recovery] LLM client not configured, Phase 2 unavailable")
            return RecoveryResult(
                recovered=False,
                warnings=["LLM client not configured, Phase 2 unavailable"],
                phase="phase2_llm",
            )

        # Step 1: Collect current BMC status
        logger.info("[Recovery] Phase 2: collecting BMC status for LLM diagnosis")
        bmc_status = await self._collect_bmc_status()

        # Step 2: Build prompt and call LLM
        prompt = self._build_diagnostic_prompt(bmc_status)
        diagnosis = await self._call_llm_diagnosis(prompt)

        if not diagnosis:
            return RecoveryResult(
                recovered=False,
                warnings=["LLM diagnosis returned no result"],
                phase="phase2_llm",
            )

        logger.info(
            f"[Recovery] LLM diagnosis: {diagnosis.get('diagnosis', 'N/A')}, "
            f"risk_level={diagnosis.get('risk_level', 'N/A')}, "
            f"repair_actions={len(diagnosis.get('repair_actions', []))}"
        )

        # Step 3: Validate repair actions
        repair_actions = diagnosis.get("repair_actions", [])
        validated = self._validate_repair_actions(repair_actions)

        if not validated:
            fallback = diagnosis.get("fallback", "No fallback available")
            logger.warning(
                f"[Recovery] No valid repair actions after whitelist filter. "
                f"LLM fallback: {fallback}"
            )
            return RecoveryResult(
                recovered=False,
                warnings=[
                    "All LLM repair actions rejected by whitelist",
                    f"LLM fallback suggestion: {fallback}",
                ],
                phase="phase2_llm",
                diagnosis=diagnosis,
            )

        # Step 4: Execute validated repair actions
        logger.info(f"[Recovery] Executing {len(validated)} validated repair actions")
        repair_ok = await self._execute_repair_actions(validated)

        # Step 5: Verify recovery
        if repair_ok:
            self._tried_actions.append("verify_after_llm_repair")
            auth_ok = await self._check_bmc_auth()
            if auth_ok:
                logger.info("[Recovery] Phase 2: LLM repair succeeded, BMC auth OK")
                return RecoveryResult(
                    recovered=True,
                    warnings=[],
                    phase="phase2_llm",
                    diagnosis=diagnosis,
                )
            self._errors.append("BMC auth still failed after LLM repair actions")

        return RecoveryResult(
            recovered=False,
            warnings=["Phase 2 LLM repair actions did not resolve the issue"],
            phase="phase2_llm",
            diagnosis=diagnosis,
        )

    async def _collect_bmc_status(self) -> Dict[str, Any]:
        """Collect current BMC status for LLM diagnosis context."""
        status: Dict[str, Any] = {
            "bmc_host": self.bmc_host,
            "bmc_port": self.bmc_port,
            "os_host": self.os_host,
            "ipmi_host": self.ipmi_host,
        }

        # Check OS SSH reachability
        if self.os_host:
            os_ok = await self._check_os_ssh()
            status["os_reachable"] = os_ok
        else:
            status["os_reachable"] = False

        # Check BMC auth (quick check, no retry)
        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl, timeout=10.0, trust_env=False,
            ) as c:
                token = await self._redfish_login(c)
                if token:
                    await self._redfish_logout(c, token)
                    status["bmc_auth"] = "ok"
                else:
                    status["bmc_auth"] = "failed"
        except httpx.ConnectError:
            status["bmc_auth"] = "unreachable"
        except httpx.TimeoutException:
            status["bmc_auth"] = "timeout"
        except Exception as e:
            status["bmc_auth"] = f"error: {e}"

        return status

    def _build_diagnostic_prompt(self, bmc_status: Dict[str, Any]) -> str:
        """Build LLM diagnostic prompt from failure context."""
        return LLM_DIAGNOSTIC_PROMPT.format(
            errors=json.dumps(self._errors, ensure_ascii=False),
            bmc_status=json.dumps(bmc_status, ensure_ascii=False),
            tried_actions=json.dumps(self._tried_actions, ensure_ascii=False),
            bmc_host=self.bmc_host,
            bmc_port=self.bmc_port,
            ipmi_host=self.ipmi_host,
            os_host=self.os_host or "N/A",
        )

    async def _call_llm_diagnosis(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Call LLM for diagnosis, return parsed JSON or None."""
        try:
            resp = await self._llm_client.chat.completions.create(
                model=self._llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a BMC environment recovery expert. "
                            "Output only valid JSON, no extra text."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,
            )
            content = resp.choices[0].message.content or ""
            logger.debug(f"[Recovery] LLM raw response: {content[:500]}")

            # Extract JSON from response
            parsed = self._extract_diagnosis_json(content)
            if parsed:
                return parsed

            logger.warning("[Recovery] Failed to parse LLM diagnosis as JSON")
            return None

        except Exception as e:
            logger.error(f"[Recovery] LLM diagnosis call failed: {e}")
            return None

    @staticmethod
    def _extract_diagnosis_json(text: str) -> Optional[Dict[str, Any]]:
        """Extract diagnosis JSON from LLM response text."""
        # Strategy 1: ```json ... ``` block
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
        else:
            # Strategy 2: find outermost { }
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                candidate = text[start:end + 1]
            else:
                return None

        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "diagnosis" in data:
                return data
        except json.JSONDecodeError:
            pass

        # Try cleanup
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "diagnosis" in data:
                return data
        except json.JSONDecodeError:
            pass

        return None

    # ------------------------------------------------------------------
    # Security whitelist validation
    # ------------------------------------------------------------------

    def _validate_repair_actions(
        self, actions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Validate repair actions against security whitelist.

        Filters out:
        - Unknown action types
        - Commands containing blocked keywords
        - IPMI commands with non-whitelisted prefixes
        """
        validated: List[Dict[str, Any]] = []

        for action in actions:
            action_type = action.get("action", "")
            params = action.get("params", {})
            desc = action.get("description", "")

            # Check action type
            if action_type not in _ALLOWED_ACTION_TYPES:
                logger.warning(
                    f"[Recovery] Rejected action: unknown type '{action_type}' "
                    f"({desc})"
                )
                continue

            # Check blocked keywords in command strings
            command_str = ""
            if action_type in ("ipmi_command", "ssh_command"):
                command_str = params.get("command", "")
            elif action_type == "redfish_request":
                command_str = (
                    params.get("endpoint", "")
                    + " " + json.dumps(params.get("body", {}), ensure_ascii=False)
                )

            command_lower = command_str.lower()
            blocked = False
            for keyword in _BLOCKED_KEYWORDS:
                if keyword in command_lower:
                    logger.warning(
                        f"[Recovery] Rejected action: blocked keyword "
                        f"'{keyword}' in '{command_str}' ({desc})"
                    )
                    blocked = True
                    break
            if blocked:
                continue

            # Validate IPMI command prefix
            if action_type == "ipmi_command":
                cmd = params.get("command", "").strip().lower()
                prefix_ok = any(cmd.startswith(p) for p in _ALLOWED_IPMI_PREFIXES)
                if not prefix_ok:
                    logger.warning(
                        f"[Recovery] Rejected IPMI action: non-whitelisted "
                        f"prefix '{cmd[:40]}' ({desc})"
                    )
                    continue

            # Validate wait_retry seconds range
            if action_type == "wait_retry":
                seconds = params.get("seconds", 0)
                if not isinstance(seconds, (int, float)) or seconds < 1 or seconds > 600:
                    logger.warning(
                        f"[Recovery] Rejected wait_retry: invalid seconds={seconds}"
                    )
                    continue

            logger.info(f"[Recovery] Accepted action: {action_type} - {desc}")
            validated.append(action)

        return validated

    # ------------------------------------------------------------------
    # Execute repair actions (LLM-proposed, whitelist-validated)
    # ------------------------------------------------------------------

    async def _execute_repair_actions(
        self, actions: List[Dict[str, Any]]
    ) -> bool:
        """
        Execute a list of validated repair actions sequentially.

        Returns True if all actions executed without error (does NOT mean
        the recovery was successful; caller must verify).
        """
        for i, action in enumerate(actions):
            action_type = action.get("action", "")
            params = action.get("params", {})
            desc = action.get("description", "")
            self._tried_actions.append(f"llm_repair_{i}_{action_type}")

            logger.info(
                f"[Recovery] Executing repair [{i+1}/{len(actions)}]: "
                f"{action_type} - {desc}"
            )

            try:
                ok = await self._execute_single_repair(action_type, params)
                if not ok:
                    self._errors.append(f"LLM repair action failed: {action_type} - {desc}")
                    logger.warning(
                        f"[Recovery] Repair action failed: {action_type} - {desc}"
                    )
                    # Continue with remaining actions
            except Exception as e:
                self._errors.append(
                    f"LLM repair action exception: {action_type} - {e}"
                )
                logger.error(
                    f"[Recovery] Repair action exception: {action_type} - {e}"
                )

        return True  # All actions attempted

    async def _execute_single_repair(
        self, action_type: str, params: Dict[str, Any]
    ) -> bool:
        """Execute a single validated repair action."""
        if action_type == "ipmi_command":
            cmd = params.get("command", "")
            if self.os_host:
                return await self._os_ssh_exec(f"ipmitool {cmd}")
            return await self._ipmi_binary_exec(cmd)

        if action_type == "ssh_command":
            cmd = params.get("command", "")
            return await self._os_ssh_exec(cmd)

        if action_type == "redfish_request":
            return await self._execute_redfish_repair(params)

        if action_type == "power_cycle":
            return await self._redfish_force_power_cycle()

        if action_type == "wait_retry":
            seconds = params.get("seconds", 30)
            logger.info(f"[Recovery] Wait retry: {seconds}s")
            await asyncio.sleep(seconds)
            return True

        logger.warning(f"[Recovery] Unknown repair action type: {action_type}")
        return False

    async def _execute_redfish_repair(self, params: Dict[str, Any]) -> bool:
        """Execute a Redfish repair action."""
        endpoint = params.get("endpoint", "")
        method = params.get("method", "GET").upper()
        body = params.get("body")

        if not endpoint:
            return False

        try:
            async with httpx.AsyncClient(
                base_url=f"https://{self.bmc_host}:{self.bmc_port}",
                verify=self.verify_ssl, timeout=30.0, trust_env=False,
            ) as c:
                token = await self._redfish_login(c)
                if not token:
                    return False

                headers = {
                    "X-Auth-Token": token,
                    "Content-Type": "application/json",
                }
                if method == "GET":
                    resp = await c.get(endpoint, headers=headers)
                elif method == "POST":
                    resp = await c.post(endpoint, headers=headers, json=body)
                elif method == "PATCH":
                    resp = await c.patch(endpoint, headers=headers, json=body)
                elif method == "DELETE":
                    resp = await c.delete(endpoint, headers=headers)
                else:
                    await self._redfish_logout(c, token)
                    return False

                await self._redfish_logout(c, token)
                ok = resp.status_code in (200, 201, 204)
                logger.info(
                    f"[Recovery] Redfish repair {method} {endpoint}: "
                    f"HTTP {resp.status_code} {'OK' if ok else 'FAIL'}"
                )
                return ok
        except Exception as e:
            logger.warning(f"[Recovery] Redfish repair error: {e}")
            return False

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
