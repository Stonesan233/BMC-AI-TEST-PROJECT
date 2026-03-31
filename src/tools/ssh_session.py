# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - SSH Session（长连接 + 流式交互）

使用 paramiko invoke_shell() 保持持久连接，
支持 Agent 多轮 Tool Calling 中的动态交互。

核心方法:
- connect()        建立 SSH 连接
- disconnect()     关闭连接
- send_command()   执行命令并等待 shell prompt
- send_line()      发送文本（不等待，用于交互式输入）
- read_until()     等待指定输出模式（正则匹配）
- read_available() 读取当前可用输出（非阻塞）

Evidence 构建由 SSHSessionManager 负责，Session 只暴露原始数据。
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
# ANSI 转义码清理
# ======================================================================

_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07|\x1b[\(\)][B0UK]")


def strip_ansi(text: str) -> str:
    """去除终端 ANSI 转义码"""
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    return text


# ======================================================================
# SSHSession
# ======================================================================


class SSHSession:
    """
    SSH 长连接会话（paramiko invoke_shell 后端）。

    只负责底层 SSH 操作和交互记录。
    Evidence 构建由外部 SSHSessionManager 负责。
    """

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
        self._history: List[Dict[str, Any]] = []
        self._all_output: str = ""
        self._pending: str = ""  # send_command timeout 时保留给后续 read_until
        self._opened_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
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
        return len(self._history)

    @property
    def history(self) -> List[Dict[str, Any]]:
        """脱敏后的交互历史（密码在记录时已替换为 ****）"""
        return list(self._history)

    @property
    def all_output_clean(self) -> str:
        """ANSI 清理后的完整输出"""
        return strip_ansi(self._all_output)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        """
        建立 SSH 连接并打开 shell channel。

        Raises:
            ConnectionError: 连接或认证失败
        """
        if self.is_alive:
            return {
                "status": "already_connected",
                "message": f"会话已存在 ({self.host}:{self.port})",
            }

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

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
            raise ConnectionError(f"SSH 认证失败 ({self.host}:{self.port}): {e}")
        except paramiko.SSHException as e:
            raise ConnectionError(f"SSH 连接异常 ({self.host}:{self.port}): {e}")
        except Exception as e:
            raise ConnectionError(f"SSH 连接失败 ({self.host}:{self.port}): {e}")

        self._channel = self._client.invoke_shell(term="xterm", width=200, height=50)
        self._channel.settimeout(2.0)

        initial_output, matched = self._recv_until(
            self.SHELL_PROMPTS, timeout=self.connect_timeout
        )

        self._connected = True
        self._all_output = initial_output
        self._opened_at = datetime.now()

        self._record("connect", "", initial_output, matched)

        if not matched:
            return {
                "status": "connected_with_warning",
                "message": f"已连接到 {self.host}:{self.port}，但未检测到标准 shell prompt",
                "initial_output": strip_ansi(initial_output)[-500:],
            }

        return {
            "status": "connected",
            "message": f"SSH 会话已建立 ({self.host}:{self.port})",
            "initial_output": strip_ansi(initial_output)[-500:],
        }

    def disconnect(self) -> Dict[str, Any]:
        """
        关闭 SSH 会话。

        Returns:
            dict with status, host, port, interaction_count, opened_at, closed_at
        """
        if self._channel and not self._channel.closed:
            try:
                self._channel.close()
            except Exception:
                pass

        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        self._connected = False
        closed_at = datetime.now().isoformat()

        result = {
            "status": "disconnected",
            "host": self.host,
            "port": self.port,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            "closed_at": closed_at,
            "interaction_count": len(self._history),
        }

        self._channel = None
        self._client = None

        return result

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        执行命令并等待 shell prompt。

        如果命令进入交互模式（timeout），输出保留到 _pending 供后续 read_until 消费。
        """
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        self._channel.send(command + "\n")

        output, matched = self._recv_until(self.SHELL_PROMPTS, timeout=timeout)

        self._all_output += output

        if not matched:
            self._pending += output

        clean = strip_ansi(output)
        cmd_output = self._extract_command_output(clean, command)

        self._record("command", command, clean, matched)

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
        """发送一行文本（不等待输出）。用于交互式输入。"""
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        data = text + ("\n" if press_enter else "")
        self._channel.send(data)

        self._record("send", text, "", True, is_password=is_password)

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
        """读取输出直到匹配任一正则模式或超时。"""
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        output, matched = self._recv_until(patterns, timeout=timeout)

        self._all_output += output
        clean = strip_ansi(output)

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

        self._record("read_until", patterns, clean, matched)

        return {
            "status": "matched" if matched else "timeout",
            "output": clean,
            "matched": matched,
            "matched_pattern": matched_pattern,
            "timeout_seconds": timeout,
        }

    def read_available(self, timeout: float = 2.0) -> Dict[str, Any]:
        """读取当前可用输出（短超时，非阻塞式）。"""
        if not self.is_alive:
            raise RuntimeError("SSH 会话未连接或已断开")

        accumulated = ""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                    accumulated += chunk
                except Exception:
                    break
            else:
                if accumulated:
                    break
                time.sleep(0.05)

        if accumulated:
            self._all_output += accumulated

        clean = strip_ansi(accumulated) if accumulated else ""

        if clean.strip():
            self._record("read", "", clean, True)

        return {
            "status": "ok",
            "output": clean,
            "has_data": bool(clean.strip()),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recv_until(
        self,
        patterns: List[str],
        timeout: float = 10,
    ) -> Tuple[str, bool]:
        """底层: 从 Channel 接收数据直到匹配任一模式或超时。"""
        deadline = time.monotonic() + timeout
        accumulated = self._pending
        self._pending = ""

        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                    accumulated += chunk
                except Exception:
                    break
            else:
                time.sleep(0.05)

            clean = strip_ansi(accumulated)
            for pattern in patterns:
                try:
                    if re.search(pattern, clean, re.IGNORECASE | re.MULTILINE):
                        return accumulated, True
                except re.error:
                    if pattern.lower() in clean.lower():
                        return accumulated, True

            if self._channel.closed:
                break
            if getattr(self._channel, "eof_received", False):
                break

        return accumulated, False

    @staticmethod
    def _extract_command_output(full_output: str, command: str) -> str:
        """从 shell 会话输出中提取命令结果（去除 echo 回显和 prompt）"""
        lines = full_output.split("\n")
        result_lines = []
        found_command = False

        for line in lines:
            stripped = line.strip()
            if not found_command and command.strip() in stripped:
                found_command = True
                continue
            if found_command and re.match(r"^~\s*[~$/#]", stripped):
                continue
            if found_command:
                result_lines.append(line)

        return "\n".join(result_lines).strip()

    def _record(
        self,
        action: str,
        input_data: Any,
        output: str,
        matched: bool,
        is_password: bool = False,
    ) -> None:
        """记录一次交互步骤"""
        self._history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "input": "****" if is_password else str(input_data),
            "output": output[-500:] if output else "",
            "matched": matched,
            "is_password": is_password,
        })
