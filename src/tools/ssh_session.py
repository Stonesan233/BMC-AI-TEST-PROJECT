# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - SSH Session（长连接 + 流式交互）

使用 paramiko invoke_shell() 保持持久连接，
支持 Agent 多轮 Tool Calling 中的动态交互。

典型交互流程:
    session = SSHSession(host, port, user, password)
    session.connect()                          # 建立连接
    session.send_command("ipmcget -d userlist")  # 执行命令
    session.send_command("ipmcset -d adduser -v testuser")  # 启动交互命令
    session.read_until(["[Pp]assword"])         # 等待密码提示
    session.send_line("secret", is_password=True)  # 输入密码
    session.read_until(["[Pp]assword"])         # 等待确认提示
    session.send_line("secret", is_password=True)  # 确认密码
    session.read_until(["#\\s*$"])              # 等待 shell prompt
    result = session.disconnect()              # 关闭连接，获取 Evidence

设计:
- 长连接保持：Session 在 disconnect() 前一直活跃
- 流式读取：read_until / read_available 实时返回输出
- 动态交互：Agent 根据当前输出决定下一步操作
- 完整记录：interaction_history 保存所有交互步骤
- 错误友好：连接断开、超时等场景均有明确错误信息
"""

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko

    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


# ======================================================================
# ANSI 转义码清理（与 ssh_tool.py 共享逻辑）
# ======================================================================

_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ANSI_OTHER = re.compile(r"\x1b\][^\x07]*\x07|\x1b[\(\)][B0UK]")


def _strip_ansi(text: str) -> str:
    """去除终端 ANSI 转义码"""
    text = _ANSI_PATTERN.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return text


# ======================================================================
# SSHSession
# ======================================================================


class SSHSession:
    """
    SSH 长连接会话，支持流式交互。

    使用 paramiko invoke_shell() 保持持久连接，
    支持 Agent 多轮 Tool Calling 中的动态交互。

    核心方法:
    - connect():          建立 SSH 连接
    - disconnect():       关闭连接，返回完整 Evidence
    - send_command():     执行命令并等待 shell prompt
    - send_line():        发送文本（不等待，用于交互式输入）
    - read_until():       等待指定输出模式（正则匹配）
    - read_available():   读取当前可用输出（非阻塞）
    """

    # Shell prompt 匹配模式
    SHELL_PROMPTS = [r"#\s*$", r"\$\s*$"]

    def __init__(
        self,
        host: str,
        port: int = 10022,
        user: str = "Administrator",
        password: str = "",
        connect_timeout: int = 15,
    ):
        if not HAS_PARAMIKO:
            raise RuntimeError("paramiko 未安装，请执行: pip install paramiko")

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.connect_timeout = connect_timeout

        self._client: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None
        self._connected: bool = False
        self._interaction_history: List[Dict[str, Any]] = []
        self._all_output: str = ""
        self._pending_output: str = ""  # send_command timeout 时的未消费输出
        self._opened_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        """检查会话是否仍然活跃"""
        if not self._connected or self._channel is None:
            return False
        if self._channel.closed:
            self._connected = False
            return False
        if getattr(self._channel, "eof_received", False):
            self._connected = False
            return False
        return True

    @property
    def interaction_count(self) -> int:
        """已记录的交互步骤数量"""
        return len(self._interaction_history)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        """
        建立 SSH 连接并打开 shell channel。

        Returns:
            dict with status, message, initial_output

        Raises:
            ConnectionError: 连接或认证失败
        """
        if self.is_alive:
            return {
                "status": "already_connected",
                "message": f"会话已存在 ({self.host}:{self.port})",
            }

        # 创建 SSH 客户端
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # BMC SSH 响应较慢，需要较长 banner 超时
        banner_timeout = max(self.connect_timeout * 2, 30)

        try:
            self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                timeout=self.connect_timeout,
                banner_timeout=banner_timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException as e:
            raise ConnectionError(
                f"SSH 认证失败 ({self.host}:{self.port}): {e}"
            )
        except paramiko.SSHException as e:
            raise ConnectionError(
                f"SSH 连接异常 ({self.host}:{self.port}): {e}"
            )
        except Exception as e:
            raise ConnectionError(
                f"SSH 连接失败 ({self.host}:{self.port}): {e}"
            )

        # 打开 shell channel
        self._channel = self._client.invoke_shell(
            term="xterm", width=200, height=50
        )
        self._channel.settimeout(2.0)

        # 等待初始 shell prompt
        initial_output, matched = self._recv_until(
            self.SHELL_PROMPTS, timeout=self.connect_timeout
        )

        self._connected = True
        self._all_output = initial_output
        self._opened_at = datetime.now()

        self._record_interaction(
            action="connect",
            input_data="",
            output=initial_output,
            matched=matched,
        )

        if not matched:
            return {
                "status": "connected_with_warning",
                "message": (
                    f"已连接到 {self.host}:{self.port}，"
                    f"但未检测到标准 shell prompt"
                ),
                "initial_output": _strip_ansi(initial_output)[-500:],
            }

        return {
            "status": "connected",
            "message": f"SSH 会话已建立 ({self.host}:{self.port})",
            "initial_output": _strip_ansi(initial_output)[-500:],
        }

    def disconnect(self) -> Dict[str, Any]:
        """
        关闭 SSH 会话，返回完整 Evidence。

        安全关闭 channel 和 client，无论当前状态如何。
        返回包含完整 interaction_history 的结果。

        Returns:
            dict with status, interaction_history, evidence
        """
        # 关闭 channel
        if self._channel and not self._channel.closed:
            try:
                self._channel.close()
            except Exception:
                pass

        # 关闭 client
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        self._connected = False

        result = {
            "status": "disconnected",
            "host": self.host,
            "port": self.port,
            "opened_at": (
                self._opened_at.isoformat() if self._opened_at else None
            ),
            "closed_at": datetime.now().isoformat(),
            "interaction_count": len(self._interaction_history),
            "interaction_history": self._get_masked_history(),
            "evidence": self._build_evidence(),
        }

        self._channel = None
        self._client = None

        return result

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        在会话中执行命令，等待 shell prompt 返回。

        适用于非交互式命令（如 ipmcget -d userlist）。
        发送命令后等待下一个 shell prompt，然后提取命令输出。

        Args:
            command: 要执行的命令
            timeout: 等待完成的超时秒数

        Returns:
            dict with status, command, output, raw_output, matched_prompt

        Raises:
            RuntimeError: 会话未连接或已断开
        """
        if not self.is_alive:
            raise RuntimeError(
                "SSH 会话未连接或已断开，请先调用 ssh_session_open"
            )

        self._channel.send(command + "\n")

        output, matched = self._recv_until(
            self.SHELL_PROMPTS, timeout=timeout
        )

        self._all_output += output

        if not matched:
            # 命令进入交互模式，输出保留给后续 read_until / expect
            self._pending_output += output

        clean = _strip_ansi(output)
        cmd_output = self._extract_command_output(clean, command)

        self._record_interaction(
            action="command",
            input_data=command,
            output=clean,
            matched=matched,
        )

        return {
            "status": "completed" if matched else "timeout",
            "command": command,
            "output": cmd_output,
            "raw_output": clean,
            "matched_prompt": matched,
        }

    # ------------------------------------------------------------------
    # Interactive methods
    # ------------------------------------------------------------------

    def send_line(
        self,
        text: str,
        is_password: bool = False,
        press_enter: bool = True,
    ) -> Dict[str, Any]:
        """
        向会话发送一行文本（不等待输出）。

        适用于交互式命令中输入密码等信息。
        发送后应紧跟 read_until() 来获取响应。

        Args:
            text: 要发送的文本
            is_password: 是否为密码（日志中脱敏显示为 ****）
            press_enter: 是否附加回车（默认 True）

        Returns:
            dict with status, sent_text, is_password

        Raises:
            RuntimeError: 会话未连接或已断开
        """
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        data = text + ("\n" if press_enter else "")
        self._channel.send(data)

        self._record_interaction(
            action="send",
            input_data=text,
            output="",
            matched=True,
            is_password=is_password,
        )

        # 短暂等待，让远程进程处理
        time.sleep(0.1)

        return {
            "status": "sent",
            "sent_text": "****" if is_password else text,
            "is_password": is_password,
        }

    def read_until(
        self,
        patterns: List[str],
        timeout: float = 10,
    ) -> Dict[str, Any]:
        """
        读取输出直到匹配任一模式或超时。

        用于交互式命令中等待提示符（如密码提示）。
        在 ANSI 清理后的文本上进行正则匹配。

        Args:
            patterns: 正则表达式模式列表，如 [r"[Pp]assword"]
            timeout: 超时秒数

        Returns:
            dict with status, output, matched, matched_pattern, timeout_seconds

        Raises:
            RuntimeError: 会话未连接或已断开
        """
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        output, matched = self._recv_until(patterns, timeout=timeout)

        self._all_output += output
        clean = _strip_ansi(output)

        # 确定哪个模式被匹配
        matched_pattern = None
        if matched:
            for p in patterns:
                try:
                    if re.search(p, clean, re.IGNORECASE | re.MULTILINE):
                        matched_pattern = p
                        break
                except re.error:
                    if p.lower() in clean.lower():
                        matched_pattern = p
                        break

        self._record_interaction(
            action="read_until",
            input_data=patterns,
            output=clean,
            matched=matched,
        )

        return {
            "status": "matched" if matched else "timeout",
            "output": clean,
            "matched": matched,
            "matched_pattern": matched_pattern,
            "timeout_seconds": timeout,
        }

    def read_available(self, timeout: float = 2.0) -> Dict[str, Any]:
        """
        读取当前可用的输出（非阻塞式，短超时）。

        如果有数据可读，立即返回；
        如果没有数据，最多等待 timeout 秒。

        Args:
            timeout: 最长等待秒数

        Returns:
            dict with status, output, has_data

        Raises:
            RuntimeError: 会话未连接或已断开
        """
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        accumulated = ""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )
                    accumulated += chunk
                except Exception:
                    break
            else:
                if accumulated:
                    break  # 已有数据且无更多可读
                time.sleep(0.05)

        if accumulated:
            self._all_output += accumulated

        clean = _strip_ansi(accumulated) if accumulated else ""

        if clean.strip():
            self._record_interaction(
                action="read", input_data="", output=clean, matched=True
            )

        return {
            "status": "ok",
            "output": clean,
            "has_data": bool(clean.strip()),
        }

    # ------------------------------------------------------------------
    # Evidence & History
    # ------------------------------------------------------------------

    def get_interaction_history(self) -> List[Dict[str, Any]]:
        """获取完整交互历史（密码已脱敏）"""
        return list(self._interaction_history)

    def build_evidence(self) -> Dict[str, Any]:
        """
        构建完整 Evidence 字典。

        包含:
        - evidence_type: "ssh_session_output"
        - content: 完整原始输出
        - metadata: 连接信息
        - interaction_history: 所有交互步骤
        - captured_at: 时间戳
        """
        return self._build_evidence()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recv_until(
        self,
        patterns: List[str],
        timeout: float = 10,
    ) -> Tuple[str, bool]:
        """
        Low-level: 从 Channel 接收数据直到匹配任一模式或超时。

        Returns:
            (accumulated_text, matched: bool)
        """
        deadline = time.monotonic() + timeout
        # 把上次 send_command timeout 未消费的输出拼接上来
        accumulated = self._pending_output
        self._pending_output = ""

        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )
                    accumulated += chunk
                except Exception:
                    break
            else:
                time.sleep(0.05)

            # 在 ANSI 清理后的文本上匹配模式
            clean = _strip_ansi(accumulated)
            for pattern in patterns:
                try:
                    if re.search(pattern, clean, re.IGNORECASE | re.MULTILINE):
                        return accumulated, True
                except re.error:
                    # 非法正则，退化为子串匹配
                    if pattern.lower() in clean.lower():
                        return accumulated, True

            # 检查 channel 是否已关闭
            if self._channel.closed:
                break
            if getattr(self._channel, "eof_received", False):
                break

        return accumulated, False

    @staticmethod
    def _extract_command_output(full_output: str, command: str) -> str:
        """从 shell 会话输出中提取命令执行结果（去除 echo 回显和 prompt）"""
        lines = full_output.split("\n")
        result_lines = []
        found_command = False

        for line in lines:
            stripped = line.strip()
            # 跳过命令 echo 行
            if not found_command and command.strip() in stripped:
                found_command = True
                continue
            # 跳过末尾 prompt 行
            if found_command and re.match(r"^~\s*[~$/#]", stripped):
                continue
            if found_command:
                result_lines.append(line)

        return "\n".join(result_lines).strip()

    def _record_interaction(
        self,
        action: str,
        input_data: Any,
        output: str,
        matched: bool,
        is_password: bool = False,
    ) -> None:
        """记录一次交互步骤到 interaction_history"""
        self._interaction_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "input": "****" if is_password else str(input_data),
                "output": output[-500:] if output else "",
                "matched": matched,
                "is_password": is_password,
            }
        )

    def _get_masked_history(self) -> List[Dict[str, Any]]:
        """获取脱敏后的交互历史（密码在记录时已脱敏）"""
        return list(self._interaction_history)

    def _build_evidence(self) -> Dict[str, Any]:
        """构建完整 Evidence 字典"""
        return {
            "evidence_type": "ssh_session_output",
            "content": _strip_ansi(self._all_output),
            "metadata": {
                "host": self.host,
                "port": self.port,
                "user": self.user,
                "opened_at": (
                    self._opened_at.isoformat() if self._opened_at else None
                ),
                "closed_at": datetime.now().isoformat(),
                "interaction_count": len(self._interaction_history),
            },
            "interaction_history": self._get_masked_history(),
            "captured_at": datetime.now().isoformat(),
        }
