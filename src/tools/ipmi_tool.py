# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - IPMI Tool

使用 pyghmi 作为后端，对外提供 ipmitool 风格的命令接口。

设计:
- 内部统一使用 pyghmi raw_command（兼容所有 pyghmi 版本）
- 对外保持 ipmitool 风格的命令接口（mc info, user list, chassis power status 等）
- 支持 cipher_suite=17（华为 openUBMC 必须）
- 返回结构化结果 + Evidence 数据
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

try:
    from pyghmi.ipmi import command as ipmi_command

    HAS_PYGHMI = True
except ImportError:
    HAS_PYGHMI = False


# ======================================================================
# 数据结构
# ======================================================================


@dataclass
class IPMIResult:
    """IPMI 命令执行结果"""

    success: bool
    command: str
    exit_code: int = 0
    raw_output: str = ""
    parsed_data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ======================================================================
# IPMITool
# ======================================================================


class IPMITool:
    """
    BMC IPMI 命令工具

    使用 pyghmi raw_command 作为后端，对外提供 ipmitool 风格的命令接口。
    支持 cipher_suite=17（华为 openUBMC 环境必须）。
    """

    def __init__(
        self,
        host: str,
        port: int = 10623,
        user: str = "Administrator",
        password: str = "",
        cipher_suite: int = 17,
    ):
        if not HAS_PYGHMI:
            raise RuntimeError(
                "pyghmi 未安装，请执行: pip install pyghmi"
            )

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.cipher_suite = cipher_suite
        self._conn = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _connect(self) -> "ipmi_command.Command":
        """建立或复用 IPMI RMCP+ 连接"""
        if self._conn is None:
            self._conn = ipmi_command.Command(
                bmc=self.host,
                userid=self.user,
                password=self.password,
                port=self.port,
                cipher=self.cipher_suite,
            )
        return self._conn

    def close(self):
        """关闭 IPMI 连接"""
        if self._conn is not None:
            try:
                self._conn.ipmi_session.logout()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def execute(self, command: str, timeout: int = 30) -> IPMIResult:
        """
        执行 ipmitool 风格的命令（异步）。

        Args:
            command: ipmitool 子命令，如 "mc info", "chassis power status"
            timeout: 超时秒数

        Returns:
            IPMIResult 结构化结果
        """
        started_at = datetime.now()

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, command),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            completed_at = datetime.now()
            return IPMIResult(
                success=False,
                command=command,
                exit_code=-1,
                error=f"IPMI 命令超时 ({timeout}s): {command}",
                started_at=started_at,
                completed_at=completed_at,
            )
        except Exception as e:
            completed_at = datetime.now()
            return IPMIResult(
                success=False,
                command=command,
                exit_code=-1,
                error=f"IPMI 连接/执行异常: {e}",
                started_at=started_at,
                completed_at=completed_at,
            )

        result.started_at = started_at
        result.completed_at = datetime.now()
        return result

    def _execute_sync(self, command_str: str) -> IPMIResult:
        """同步执行 IPMI 命令（在工作线程中运行）"""
        conn = self._connect()
        cmd = command_str.strip()
        # 兼容业界习惯：自动去除 ipmitool / ipmi 前缀
        # LLM 经常生成 "ipmi chassis status" 或 "ipmitool mc info"
        if cmd.lower().startswith("ipmitool "):
            cmd = cmd[len("ipmitool "):].strip()
        elif cmd.lower().startswith("ipmi "):
            cmd = cmd[len("ipmi "):].strip()
        cmd_lower = cmd.lower()

        # 命令路由
        handler = self._resolve_handler(cmd_lower)
        if handler:
            return handler(conn, command_str)

        # raw 命令格式: "raw 0x06 0x01 [data...]"
        if cmd_lower.startswith("raw "):
            return self._handle_raw_command(conn, command_str)

        return IPMIResult(
            success=False,
            command=command_str,
            exit_code=1,
            error=f"不支持的 IPMI 命令: {command_str}",
        )

    # ------------------------------------------------------------------
    # 命令路由表
    # ------------------------------------------------------------------

    def _resolve_handler(self, cmd_lower: str) -> Optional[Callable]:
        """将 ipmitool 风格命令字符串映射到处理方法"""

        EXACT_MAP = {
            "mc info": self._handle_mc_info,
            "mc guid": self._handle_mc_guid,
            "mc reset cold": self._handle_mc_reset_cold,
            "mc reset warm": self._handle_mc_reset_warm,
            "chassis status": self._handle_chassis_status,
            "chassis power status": self._handle_chassis_power_status,
            "sel info": self._handle_sel_info,
            "sdr info": self._handle_sdr_info,
            "sensor list": self._handle_sensor_list,
            "fru list": self._handle_fru_list,
            "user list": self._handle_user_list,
            "user summary": self._handle_user_summary,
        }

        if cmd_lower in EXACT_MAP:
            return EXACT_MAP[cmd_lower]

        PREFIX_HANDLERS = [
            ("chassis power ", self._handle_chassis_power_control),
            ("sel list", self._handle_sel_list),
            ("sdr list", self._handle_sdr_list),
        ]

        for prefix, handler in PREFIX_HANDLERS:
            if cmd_lower.startswith(prefix):
                return handler

        return None

    # ------------------------------------------------------------------
    # raw_command 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_data(resp: Any) -> Optional[bytes]:
        """
        从 raw_command 响应中提取 data 字节。

        pyghmi raw_command 返回格式因版本而异:
        - dict: {"data": bytes, "error": str} 或 {"data": bytes}
        - bytes/bytearray: 直接返回
        """
        if isinstance(resp, (bytes, bytearray)):
            return bytes(resp)
        if isinstance(resp, dict):
            if "error" in resp:
                return None
            return resp.get("data")
        return None

    @staticmethod
    def _check_error(resp: Any) -> Optional[str]:
        """检查 raw_command 响应是否包含错误"""
        if isinstance(resp, dict) and "error" in resp:
            return str(resp["error"])
        return None

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    def _handle_mc_info(self, conn, command_str: str) -> IPMIResult:
        """
        mc info -- 查询 BMC 设备信息

        IPMI spec 20.1: Get Device ID
        Request:  netfn=0x06, command=0x01
        Response: data bytes:
          [0]    Device ID
          [1]    Device Revision (bits 3:0) + provides SDRs (bit 7) + available (bit 6)
          [2]    Major Firmware Revision
          [3]    Minor Firmware Revision (bits 6:0) + device available (bit 7)
          [4]    IPMI Version (BCD: 0x02 = 2.0)
          [5]    Additional Device Support (bitfield)
          [6:9]  Manufacturer ID (3 bytes, little-endian)
          [9:11] Product ID (2 bytes, little-endian)
        """
        try:
            resp = conn.raw_command(netfn=0x06, command=0x01)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw = self._extract_data(resp)
            if raw is None:
                return IPMIResult(
                    success=False,
                    command=command_str,
                    exit_code=1,
                    error=f"mc info 返回空数据: {resp}",
                )

            parsed = {}
            lines = []

            if len(raw) >= 1:
                parsed["device_id"] = raw[0]
            if len(raw) >= 2:
                parsed["device_revision"] = raw[1] & 0x0F
                parsed["provides_device_sdrs"] = bool(raw[1] & 0x80)
                parsed["device_available"] = not bool(raw[1] & 0x20)
            if len(raw) >= 4:
                major = raw[2]
                minor = raw[3] & 0x7F
                parsed["firmware_revision"] = f"{major}.{minor}"
            if len(raw) >= 5:
                ipmi_ver = raw[4]
                parsed["ipmi_version"] = f"{ipmi_ver >> 4}.{ipmi_ver & 0x0F}"
            if len(raw) >= 8:
                manufacturer_id = raw[5] | (raw[6] << 8) | (raw[7] << 16)
                parsed["manufacturer_id"] = manufacturer_id
            if len(raw) >= 10:
                product_id = raw[8] | (raw[9] << 8)
                parsed["product_id"] = product_id
            if len(raw) >= 11:
                aux = raw[10]
                parsed["auxiliary_revision"] = (
                    f"{(aux >> 4) & 0x0F}.{aux & 0x0F}"
                )

            display_map = {
                "device_id": "Device ID",
                "device_revision": "Device Revision",
                "firmware_revision": "Firmware Revision",
                "ipmi_version": "IPMI Version",
                "manufacturer_id": "Manufacturer ID",
                "product_id": "Product ID",
                "device_available": "Device Available",
                "provides_device_sdrs": "Provides Device SDRs",
                "auxiliary_revision": "Auxiliary Revision",
            }

            for key, label in display_map.items():
                if key in parsed:
                    value = parsed[key]
                    if isinstance(value, bool):
                        value_str = "yes" if value else "no"
                    else:
                        value_str = str(value)
                    lines.append(f"{label:27s}: {value_str}")

            raw_output = "\n".join(lines) if lines else f"mc info (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data=parsed,
                evidence=self._build_evidence(command_str, raw_output, parsed),
            )

        except Exception as e:
            return self._make_error(command_str, "mc info", e)

    def _handle_mc_guid(self, conn, command_str: str) -> IPMIResult:
        """
        mc guid -- 查询系统 GUID

        Get System GUID: netfn=0x06, command=0x08
        Response: 16 bytes GUID
        """
        try:
            resp = conn.raw_command(netfn=0x06, command=0x08)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw = self._extract_data(resp)
            if raw and len(raw) >= 16:
                # 格式化为标准 UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
                h = raw.hex()
                guid_str = (
                    f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
                )
            else:
                guid_str = str(resp)

            raw_output = f"System GUID: {guid_str}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"system_guid": guid_str},
                evidence=self._build_evidence(
                    command_str, raw_output, {"system_guid": guid_str}
                ),
            )
        except Exception as e:
            return self._make_error(command_str, "mc guid", e)

    def _handle_mc_reset_cold(self, conn, command_str: str) -> IPMIResult:
        """mc reset cold (netfn=0x06, command=0x02)"""
        try:
            conn.raw_command(netfn=0x06, command=0x02)
            raw_output = "MC Cold Reset sent"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"action": "cold_reset"},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "mc reset cold", e)

    def _handle_mc_reset_warm(self, conn, command_str: str) -> IPMIResult:
        """mc reset warm (netfn=0x06, command=0x03)"""
        try:
            conn.raw_command(netfn=0x06, command=0x03)
            raw_output = "MC Warm Reset sent"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"action": "warm_reset"},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "mc reset warm", e)

    def _handle_chassis_status(self, conn, command_str: str) -> IPMIResult:
        """
        chassis status -- 查询机箱状态

        Get Chassis Status: netfn=0x00, command=0x01
        Response:
          [0]  current power state (bit 0 = power on, bit 1 = overload, ...)
          [1]  last power event
          [2]  misc. chassis state
          [3]  front panel button disable mask (optional)
        """
        try:
            resp = conn.raw_command(netfn=0x00, command=0x01)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw = self._extract_data(resp)
            parsed = {}
            lines = []

            if raw and len(raw) >= 1:
                byte0 = raw[0]
                parsed["power_on"] = bool(byte0 & 0x01)
                parsed["power_overload"] = bool(byte0 & 0x02)
                parsed["power_interlock"] = bool(byte0 & 0x04)
                parsed["power_fault"] = bool(byte0 & 0x08)
                parsed["power_control_fault"] = bool(byte0 & 0x10)

                lines.append(
                    f"{'System Power':27s}: "
                    f"{'on' if parsed['power_on'] else 'off'}"
                )
                lines.append(
                    f"{'Power Overload':27s}: "
                    f"{'true' if parsed['power_overload'] else 'false'}"
                )
                lines.append(
                    f"{'Power Interlock':27s}: "
                    f"{'active' if parsed['power_interlock'] else 'inactive'}"
                )
                lines.append(
                    f"{'Power Fault':27s}: "
                    f"{'true' if parsed['power_fault'] else 'false'}"
                )
                lines.append(
                    f"{'Power Control Fault':27s}: "
                    f"{'true' if parsed['power_control_fault'] else 'false'}"
                )

                # Power restore policy: bits 6:5 of byte 0
                prp = (byte0 >> 5) & 0x03
                prp_map = {0: "always-off", 1: "always-on", 2: "previous", 3: "unknown"}
                parsed["power_restore_policy"] = prp_map.get(prp, "unknown")
                lines.append(
                    f"{'Power Restore Policy':27s}: {parsed['power_restore_policy']}"
                )

            if raw and len(raw) >= 2:
                parsed["last_power_event"] = raw[1]
                lines.append(f"{'Last Power Event':27s}: {raw[1]:#04x}")

            if raw and len(raw) >= 3:
                parsed["misc_chassis_state"] = raw[2]
                lines.append(f"{'Misc. Chassis State':27s}: {raw[2]:#04x}")

            raw_output = (
                "\n".join(lines) if lines else f"chassis status (raw): {resp}"
            )

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data=parsed,
                evidence=self._build_evidence(command_str, raw_output, parsed),
            )
        except Exception as e:
            return self._make_error(command_str, "chassis status", e)

    def _handle_chassis_power_status(self, conn, command_str: str) -> IPMIResult:
        """chassis power status -- 查询电源状态（复用 Get Chassis Status）"""
        try:
            resp = conn.raw_command(netfn=0x00, command=0x01)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw = self._extract_data(resp)
            power_state = "Unknown"

            if raw and len(raw) >= 1:
                power_state = "on" if (raw[0] & 0x01) else "off"

            raw_output = f"Chassis Power is {power_state}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"power_state": power_state},
                evidence=self._build_evidence(
                    command_str, raw_output, {"power_state": power_state}
                ),
            )
        except Exception as e:
            return self._make_error(command_str, "chassis power status", e)

    def _handle_chassis_power_control(self, conn, command_str: str) -> IPMIResult:
        """
        chassis power on/off/reset/cycle/soft

        Chassis Control: netfn=0x00, command=0x02
        Data: [0] = action (0=off, 1=on, 2=cycle, 3=reset, 4=diag, 5=soft)
        """
        parts = command_str.strip().lower().split()
        if len(parts) >= 3 and parts[1] == "power":
            action = parts[2]
        else:
            return IPMIResult(
                success=False,
                command=command_str,
                exit_code=1,
                error=f"无法解析 power 命令: {command_str}",
            )

        action_map = {
            "on": 0x01,
            "off": 0x00,
            "cycle": 0x02,
            "reset": 0x03,
            "diag": 0x04,
            "soft": 0x05,
            "nmi": 0x04,
        }

        action_byte = action_map.get(action)
        if action_byte is None:
            return IPMIResult(
                success=False,
                command=command_str,
                exit_code=1,
                error=f"未知的 power action: {action}",
            )

        try:
            resp = conn.raw_command(
                netfn=0x00, command=0x02, data=[action_byte]
            )

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw_output = f"Chassis Power Control: {action}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"action": action, "action_byte": action_byte},
                evidence=self._build_evidence(
                    command_str, raw_output, {"action": action}
                ),
            )
        except Exception as e:
            return self._make_error(command_str, f"chassis power {action}", e)

    def _handle_user_list(self, conn, command_str: str) -> IPMIResult:
        """
        user list -- 列出所有 IPMI 用户

        Get User Access: netfn=0x06, cmd=0x44, data=[channel, user_id]
        Get User Name:  netfn=0x06, cmd=0x45, data=[user_id]
        """
        try:
            users = []

            # 获取最大用户数
            resp = conn.raw_command(netfn=0x06, command=0x44, data=[0x01, 0x01])

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw_data = self._extract_data(resp)
            max_users = 0

            if raw_data and len(raw_data) >= 3:
                # byte 1: max_uid_count(upper nibble) | enabled_count(lower nibble)
                max_users = (raw_data[1] >> 4) & 0x0F
                if max_users == 0:
                    max_users = raw_data[2] & 0x3F
                if max_users == 0:
                    max_users = 15

            # 遍历每个用户 ID
            for uid in range(1, min(max_users + 1, 64)):
                try:
                    # Get User Name
                    name_resp = conn.raw_command(
                        netfn=0x06, command=0x45, data=[uid]
                    )
                    name_bytes = self._extract_data(name_resp)
                    username = ""
                    if name_bytes:
                        username = (
                            bytes(name_bytes)
                            .rstrip(b"\x00")
                            .decode("utf-8", errors="replace")
                        )

                    # Get User Access
                    access_resp = conn.raw_command(
                        netfn=0x06, command=0x44, data=[0x01, uid]
                    )
                    access_raw = self._extract_data(access_resp)

                    # 解析权限等级
                    privilege = "NO ACCESS"
                    if access_raw and len(access_raw) >= 2:
                        priv_byte = access_raw[1] & 0x0F
                        priv_map = {
                            0: "NO ACCESS",
                            1: "CALLBACK",
                            2: "USER",
                            3: "OPERATOR",
                            4: "ADMINISTRATOR",
                            5: "OEM",
                        }
                        privilege = priv_map.get(priv_byte, f"LEVEL_{priv_byte}")

                    users.append(
                        {
                            "id": uid,
                            "name": username,
                            "privilege": privilege,
                        }
                    )
                except Exception:
                    users.append(
                        {"id": uid, "name": "???", "privilege": "error"}
                    )

            # 格式化输出（类似 ipmitool user list）
            lines = [
                "ID  Name               Callin  Link Auth  IPMI Msg  Channel Priv Limit"
            ]
            for u in users:
                name_display = u["name"] if u["name"] else "(empty)"
                lines.append(
                    f"{u['id']:2d}  {name_display:18s} true      true       true      {u['privilege']}"
                )

            raw_output = "\n".join(lines)

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"users": users, "user_count": len(users)},
                evidence=self._build_evidence(
                    command_str, raw_output, {"user_count": len(users)}
                ),
            )
        except Exception as e:
            return self._make_error(command_str, "user list", e)

    def _handle_user_summary(self, conn, command_str: str) -> IPMIResult:
        """user summary -- 用户摘要信息"""
        try:
            resp = conn.raw_command(netfn=0x06, command=0x44, data=[0x01, 0x01])

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw_data = self._extract_data(resp)
            parsed = {}
            lines = []

            if raw_data and len(raw_data) >= 3:
                max_uid = (raw_data[1] >> 4) & 0x0F
                enabled_count = raw_data[1] & 0x0F
                fixed_count = (raw_data[2] >> 4) & 0x0F
                max_uid_alt = raw_data[2] & 0x3F

                if max_uid == 0:
                    max_uid = max_uid_alt

                parsed["max_user_ids"] = max_uid
                parsed["enabled_users"] = enabled_count
                parsed["fixed_users"] = fixed_count

                lines.append(f"{'Max User IDs':27s}: {max_uid}")
                lines.append(f"{'Enabled Users':27s}: {enabled_count}")
                lines.append(f"{'Fixed Users':27s}: {fixed_count}")

            raw_output = (
                "\n".join(lines)
                if lines
                else f"user summary (raw): {resp}"
            )

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data=parsed,
                evidence=self._build_evidence(command_str, raw_output, parsed),
            )
        except Exception as e:
            return self._make_error(command_str, "user summary", e)

    def _handle_sel_info(self, conn, command_str: str) -> IPMIResult:
        """sel info -- SEL 信息 (netfn=0x0a, command=0x40)"""
        try:
            resp = conn.raw_command(netfn=0x0A, command=0x40)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw_data = self._extract_data(resp)
            parsed = {}
            lines = []

            if raw_data and len(raw_data) >= 15:
                version = raw_data[0]
                entry_count = raw_data[1] | (raw_data[2] << 8)
                parsed["sel_version"] = version
                parsed["entry_count"] = entry_count
                lines.append(f"{'SEL Version':27s}: {version:#04x}")
                lines.append(f"{'Entry Count':27s}: {entry_count}")

            raw_output = (
                "\n".join(lines)
                if lines
                else f"SEL Info (raw): {resp}"
            )

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data=parsed,
                evidence=self._build_evidence(command_str, raw_output, parsed),
            )
        except Exception as e:
            return self._make_error(command_str, "sel info", e)

    def _handle_sel_list(self, conn, command_str: str) -> IPMIResult:
        """sel list -- 列出 SEL 条目"""
        try:
            resp = conn.raw_command(netfn=0x0A, command=0x43)
            raw_output = f"SEL List (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"raw_response": str(resp)},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "sel list", e)

    def _handle_sdr_info(self, conn, command_str: str) -> IPMIResult:
        """sdr info -- SDR 信息 (netfn=0x0a, command=0x20)"""
        try:
            resp = conn.raw_command(netfn=0x0A, command=0x20)
            raw_output = f"SDR Info (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"raw_response": str(resp)},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "sdr info", e)

    def _handle_sdr_list(self, conn, command_str: str) -> IPMIResult:
        """sdr list -- SDR 列表"""
        try:
            resp = conn.raw_command(netfn=0x0A, command=0x23)
            raw_output = f"SDR List (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"raw_response": str(resp)},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "sdr list", e)

    def _handle_sensor_list(self, conn, command_str: str) -> IPMIResult:
        """sensor list -- 传感器列表"""
        try:
            resp = conn.raw_command(netfn=0x04, command=0x2D)
            raw_output = f"Sensor List (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"raw_response": str(resp)},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "sensor list", e)

    def _handle_fru_list(self, conn, command_str: str) -> IPMIResult:
        """fru list -- FRU 信息"""
        try:
            resp = conn.raw_command(netfn=0x0A, command=0x10)
            raw_output = f"FRU List (raw): {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={"raw_response": str(resp)},
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "fru list", e)

    def _handle_raw_command(self, conn, command_str: str) -> IPMIResult:
        """raw 命令 -- 发送原始 IPMI 命令 (raw <netfn> <command> [data...])"""
        try:
            parts = command_str.strip().split()
            if len(parts) < 3:
                return IPMIResult(
                    success=False,
                    command=command_str,
                    exit_code=1,
                    error="raw 命令格式: raw <netfn> <command> [data...]",
                )

            netfn = int(parts[1], 0)
            cmd = int(parts[2], 0)
            data = [int(x, 0) for x in parts[3:]] if len(parts) > 3 else []

            resp = conn.raw_command(netfn=netfn, command=cmd, data=data)

            err = self._check_error(resp)
            if err:
                return IPMIResult(
                    success=False, command=command_str, exit_code=1, error=err
                )

            raw_output = f"raw {netfn:#04x} {cmd:#04x}: {resp}"

            return IPMIResult(
                success=True,
                command=command_str,
                exit_code=0,
                raw_output=raw_output,
                parsed_data={
                    "netfn": netfn,
                    "command": cmd,
                    "response": str(resp),
                },
                evidence=self._build_evidence(command_str, raw_output),
            )
        except Exception as e:
            return self._make_error(command_str, "raw command", e)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _make_error(
        command_str: str, operation: str, exception: Exception
    ) -> IPMIResult:
        """快速构建错误结果"""
        return IPMIResult(
            success=False,
            command=command_str,
            exit_code=1,
            error=f"{operation} 执行失败: {exception}",
        )

    def _build_evidence(
        self,
        command: str,
        raw_output: str,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        构建 Evidence 字典 (evidence_type="ipmi_output")。

        返回 dict 而非 Evidence 对象，方便 JSON 序列化后由 ExecAgent 使用。
        """
        return {
            "evidence_type": "ipmi_output",
            "content": raw_output,
            "metadata": {
                "command": command,
                "host": self.host,
                "port": self.port,
                "cipher_suite": self.cipher_suite,
                **(extra_data or {}),
            },
            "captured_at": datetime.now().isoformat(),
        }

    @staticmethod
    def to_json(result: IPMIResult) -> str:
        """将 IPMIResult 序列化为 JSON 字符串"""
        data = {
            "success": result.success,
            "command": result.command,
            "exit_code": result.exit_code,
            "raw_output": result.raw_output,
            "parsed_data": result.parsed_data,
        }
        if result.error:
            data["error"] = result.error
        if result.evidence:
            data["evidence"] = result.evidence
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
