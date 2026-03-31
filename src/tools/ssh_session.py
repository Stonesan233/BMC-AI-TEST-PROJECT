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
- drain()          排空通道中的残留数据

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
# BMC 常见 Prompt 模式库
# ======================================================================

# 标准 shell prompt（BMC Linux shell）
SHELL_PROMPTS = [
    r"#\s*$",           # root prompt
    r"\$\s*$",          # user prompt
    r">\s*$",           # 某些 BMC CLI prompt
]

# 密码输入提示（大小写 + 空格变体）
PASSWORD_PROMPTS = [
    r"[Pp]ass(word|wd)\s*:\s*$",
    r"password\s*[：:]\s*$",
]

# 确认/选择提示
CONFIRM_PROMPTS = [
    r"\(y/n\)\s*:\s*$",
    r"\(yes/no\)\s*:\s*$",
    r"\[y/N\]\s*:\s*$",
    r"\[Y/n\]\s*:\s*$",
    r"confirm\s*:\s*$",
]

# 错误提示
ERROR_INDICATORS = [
    r"[Ee]rror",
    r"[Ff]ailed",
    r"denied",
    r"invalid",
    r"not (found|support|allowed|exist)",
    r"unable to",
]

# 所有交互式 prompt 合集（用于通用交互检测）
ALL_INTERACTIVE_PROMPTS = PASSWORD_PROMPTS + CONFIRM_PROMPTS


# ======================================================================
# 自定义异常
# ======================================================================


class SSHSessionError(Exception):
    """SSH 会话基础异常"""
    pass


class SSHConnectionError(SSHSessionError):
    """连接/认证失败"""
    pass


class SSHChannelClosedError(SSHSessionError):
    """通道已关闭"""
    pass


class SSHTimeoutError(SSHSessionError):
    """操作超时"""
    pass


# ======================================================================
# SSHSession
# ======================================================================


class SSHSession:
    """
    SSH 长连接会话（paramiko invoke_shell 后端）。

    只负责底层 SSH 操作和交互记录。
    Evidence 构建由外部 SSHSessionManager 负责。

    稳定性设计:
    - 渐进式轮询（50ms -> 100ms -> 200ms）避免空转或漏数据
    - 通道健康检查（recv_ready / closed / EOF 三重检测）
    - 合理的默认超时分级（connect/command/read 各不同）
    - 异常后自动标记断开，防止后续操作在死通道上执行
    """

    # 默认超时分级（秒）
    DEFAULT_CONNECT_TIMEOUT = 15
    DEFAULT_COMMAND_TIMEOUT = 30
    DEFAULT_READ_TIMEOUT = 10
    DEFAULT_DRAIN_TIMEOUT = 2.0

    def __init__(
        self,
        host: str,
        port: int = 10022,
        user: str = "Administrator",
        password: str = "",
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
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
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        if not self._connected or self._channel is None:
            return False
        try:
            if self._channel.closed:
                self._connected = False
                return False
        except Exception:
            self._connected = False
            return False
        return True

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

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

        Returns:
            dict with status, message, initial_output

        Raises:
            SSHConnectionError: 连接或认证失败
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
            self._cleanup_resources()
            raise SSHConnectionError(
                f"SSH 认证失败 ({self.host}:{self.port}): {e}"
            )
        except paramiko.SSHException as e:
            self._cleanup_resources()
            raise SSHConnectionError(
                f"SSH 协议异常 ({self.host}:{self.port}): {e}"
            )
        except OSError as e:
            self._cleanup_resources()
            raise SSHConnectionError(
                f"SSH 网络错误 ({self.host}:{self.port}): {e}"
            )
        except Exception as e:
            self._cleanup_resources()
            raise SSHConnectionError(
                f"SSH 连接失败 ({self.host}:{self.port}): {e}"
            )

        try:
            self._channel = self._client.invoke_shell(
                term="xterm", width=200, height=50
            )
            self._channel.settimeout(2.0)
        except Exception as e:
            self._cleanup_resources()
            raise SSHConnectionError(
                f"打开 shell channel 失败 ({self.host}:{self.port}): {e}"
            )

        try:
            initial_output, matched = self._recv_until(
                SHELL_PROMPTS, timeout=self.connect_timeout
            )
        except SSHChannelClosedError:
            self._cleanup_resources()
            raise SSHConnectionError(
                f"Shell channel 在初始化时关闭 ({self.host}:{self.port})"
            )

        self._connected = True
        self._all_output = initial_output
        self._opened_at = datetime.now()
        self._last_error = None

        self._record("connect", "", initial_output, matched)

        clean_initial = strip_ansi(initial_output)[-500:]

        if not matched:
            return {
                "status": "connected_with_warning",
                "message": (
                    f"已连接到 {self.host}:{self.port}，"
                    f"但未检测到标准 shell prompt"
                ),
                "initial_output": clean_initial,
            }

        return {
            "status": "connected",
            "message": f"SSH 会话已建立 ({self.host}:{self.port})",
            "initial_output": clean_initial,
        }

    def disconnect(self) -> Dict[str, Any]:
        """
        关闭 SSH 会话。

        Returns:
            dict with status, host, port, interaction_count, opened_at, closed_at
        """
        closed_at = datetime.now().isoformat()
        interaction_count = len(self._history)

        result = {
            "status": "disconnected",
            "host": self.host,
            "port": self.port,
            "opened_at": (
                self._opened_at.isoformat() if self._opened_at else None
            ),
            "closed_at": closed_at,
            "interaction_count": interaction_count,
        }

        self._cleanup_resources()
        return result

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: int = 0) -> Dict[str, Any]:
        """
        执行命令并等待 shell prompt。

        Args:
            command: 要执行的命令
            timeout: 超时秒数，0 表示使用默认值 (30s)

        Returns:
            dict with status, command, output, raw_output, matched_prompt

        如果命令进入交互模式（timeout），输出保留到 _pending 供后续 read_until 消费。
        """
        self._check_alive()

        if timeout <= 0:
            timeout = self.DEFAULT_COMMAND_TIMEOUT

        try:
            self._channel.send(command + "\n")
        except OSError as e:
            self._mark_disconnected(f"发送命令时通道异常: {e}")
            raise SSHChannelClosedError(
                f"发送命令失败，通道已关闭: {e}"
            )

        output, matched = self._recv_until(
            SHELL_PROMPTS, timeout=timeout
        )

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
        delay: float = 0.15,
    ) -> Dict[str, Any]:
        """
        发送一行文本（不等待完整输出）。用于交互式输入。

        Args:
            text: 要发送的文本
            is_password: 是否为密码（日志脱敏）
            press_enter: 发送后是否附加回车
            delay: 发送后等待秒数（让远端有时间处理）

        Returns:
            dict with status, sent_text
        """
        self._check_alive()

        if not text:
            return {
                "status": "sent_empty",
                "sent_text": "",
                "is_password": is_password,
            }

        data = text + ("\n" if press_enter else "")

        try:
            self._channel.send(data)
        except OSError as e:
            self._mark_disconnected(f"发送文本时通道异常: {e}")
            raise SSHChannelClosedError(
                f"发送失败，通道已关闭: {e}"
            )

        self._record("send", text, "", True, is_password=is_password)

        # 等待远端处理，避免快速连发导致输入混乱
        time.sleep(delay)

        return {
            "status": "sent",
            "sent_text": "****" if is_password else text,
            "is_password": is_password,
        }

    def read_until(
        self,
        patterns: List[str],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        """
        读取输出直到匹配任一正则模式或超时。

        Args:
            patterns: 正则表达式列表，匹配任一即停止
            timeout: 超时秒数，0 表示使用默认值 (10s)

        Returns:
            dict with status, output, matched, matched_pattern
        """
        self._check_alive()

        if timeout <= 0:
            timeout = self.DEFAULT_READ_TIMEOUT

        if not patterns:
            return {
                "status": "error",
                "output": "",
                "matched": False,
                "matched_pattern": None,
                "error": "patterns 不能为空",
            }

        output, matched = self._recv_until(patterns, timeout=timeout)

        self._all_output += output
        clean = strip_ansi(output)

        matched_pattern = None
        if matched:
            matched_pattern = self._find_matched_pattern(clean, patterns)

        self._record("read_until", patterns, clean, matched)

        return {
            "status": "matched" if matched else "timeout",
            "output": clean,
            "matched": matched,
            "matched_pattern": matched_pattern,
            "timeout_seconds": timeout,
        }

    def read_available(self, timeout: float = 2.0) -> Dict[str, Any]:
        """
        读取当前可用输出（短超时，非阻塞式）。

        Args:
            timeout: 最大等待秒数

        Returns:
            dict with status, output, has_data
        """
        self._check_alive()

        accumulated = self._drain_channel(timeout)

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

    def drain(self, timeout: float = 0) -> str:
        """
        排空通道中的残留数据。

        用于在操作前清理上次命令的残留输出，避免干扰后续交互。

        Args:
            timeout: 最大等待秒数，0 使用默认值 (2s)

        Returns:
            排空的数据（ANSI 已清理）
        """
        if timeout <= 0:
            timeout = self.DEFAULT_DRAIN_TIMEOUT

        if not self.is_alive:
            return ""

        raw = self._drain_channel(timeout)
        if raw:
            self._all_output += raw

        return strip_ansi(raw)

    # ------------------------------------------------------------------
    # Internal: 通道读取核心
    # ------------------------------------------------------------------

    def _recv_until(
        self,
        patterns: List[str],
        timeout: float = 10,
    ) -> Tuple[str, bool]:
        """
        底层: 从 Channel 接收数据直到匹配任一模式或超时。

        渐进式轮询策略:
        - 前 2s: 50ms 间隔（快速捕获初始响应）
        - 2-5s: 100ms 间隔（等待处理中响应）
        - 5s+: 200ms 间隔（慢速等待长操作）

        同时进行通道健康检查:
        - recv_ready(): 有数据可读
        - channel.closed: 通道已关闭
        - channel.eof_received: 收到 EOF
        """
        deadline = time.monotonic() + timeout
        accumulated = self._pending
        self._pending = ""

        # 预编译正则（带容错）
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE | re.MULTILINE))
            except re.error:
                compiled.append(None)

        last_recv_time = time.monotonic()

        while True:
            now = time.monotonic()
            if now >= deadline:
                break

            # --- 通道健康检查 ---
            if self._is_channel_dead():
                if accumulated:
                    # 通道关闭前有数据，做最后一次匹配尝试
                    clean = strip_ansi(accumulated)
                    if self._match_any(clean, compiled, patterns):
                        return accumulated, True
                return accumulated, False

            # --- 读取数据 ---
            received = False
            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )
                    if chunk:
                        accumulated += chunk
                        received = True
                        last_recv_time = now
                except Exception:
                    # recv 异常通常意味着通道已坏
                    break

            # --- 模式匹配检测 ---
            clean = strip_ansi(accumulated)
            if self._match_any(clean, compiled, patterns):
                return accumulated, True

            # --- 渐进式轮询间隔 ---
            elapsed = deadline - now
            if received:
                # 刚收到数据，快速轮询
                time.sleep(0.05)
            elif elapsed > 5:
                time.sleep(0.1)
            else:
                time.sleep(0.2)

        return accumulated, False

    def _drain_channel(self, timeout: float) -> str:
        """
        排空通道中的可用数据（不进行模式匹配）。

        用于 read_available 和 drain，只收集数据不等待特定模式。
        """
        deadline = time.monotonic() + timeout
        accumulated = ""
        idle_rounds = 0

        while time.monotonic() < deadline:
            if self._is_channel_dead():
                break

            if self._channel.recv_ready():
                try:
                    chunk = self._channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )
                    if chunk:
                        accumulated += chunk
                        idle_rounds = 0
                        continue
                except Exception:
                    break
            else:
                idle_rounds += 1
                # 连续 3 轮无数据且有已累积内容，认为输出结束
                if accumulated and idle_rounds >= 3:
                    break
                time.sleep(0.05)

        return accumulated

    # ------------------------------------------------------------------
    # Internal: 辅助方法
    # ------------------------------------------------------------------

    def _check_alive(self) -> None:
        """检查会话存活状态，已断开则抛出异常。"""
        if not self.is_alive:
            msg = self._last_error or "未知原因"
            raise SSHChannelClosedError(
                f"SSH 会话未连接或已断开: {msg}"
            )

    def _mark_disconnected(self, reason: str) -> None:
        """标记会话为断开状态并记录原因。"""
        self._connected = False
        self._last_error = reason

    def _is_channel_dead(self) -> bool:
        """检测通道是否已失效。"""
        if self._channel is None:
            return True
        try:
            if self._channel.closed:
                return True
            if getattr(self._channel, "eof_received", False):
                return True
        except Exception:
            return True
        return False

    def _cleanup_resources(self) -> None:
        """安全释放所有资源（channel + client）。"""
        if self._channel is not None:
            try:
                if not self._channel.closed:
                    self._channel.close()
            except Exception:
                pass
            self._channel = None

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        self._connected = False

    @staticmethod
    def _match_any(
        text: str,
        compiled: List[Optional[re.Pattern]],
        raw_patterns: List[str],
    ) -> bool:
        """
        用预编译正则列表匹配文本，回退到字符串包含检测。
        """
        for i, regex in enumerate(compiled):
            if regex is not None:
                if regex.search(text):
                    return True
            else:
                # 正则编译失败的 pattern，用字符串包含检测
                if raw_patterns[i].lower() in text.lower():
                    return True
        return False

    @staticmethod
    def _find_matched_pattern(
        text: str, patterns: List[str]
    ) -> Optional[str]:
        """找出第一个匹配的 pattern 原文。"""
        for p in patterns:
            try:
                if re.search(p, text, re.IGNORECASE | re.MULTILINE):
                    return p
            except re.error:
                if p.lower() in text.lower():
                    return p
        return None

    @staticmethod
    def _extract_command_output(full_output: str, command: str) -> str:
        """从 shell 会话输出中提取命令结果（去除 echo 回显和 prompt）"""
        lines = full_output.split("\n")
        result_lines = []
        found_command = False
        cmd_stripped = command.strip()

        for line in lines:
            stripped = line.strip()
            if not found_command:
                if cmd_stripped and cmd_stripped in stripped:
                    found_command = True
                continue
            # 跳过 prompt 行（如 ~ # / ~$ 等）
            if re.match(r"^~\s*[~$/#]", stripped):
                continue
            # 空行 + 后续无内容时跳过
            if not stripped and not result_lines:
                continue
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
