# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - SSH Tool

使用 paramiko 作为后端，支持非交互式和交互式 SSH 命令执行。

设计:
- 非交互式: exec_command() 直接获取 stdout/stderr/exit_code
- 交互式: invoke_shell() + expect-like 提示检测，用于 ipmcset adduser 等交互命令
- 每次 execute() 创建新连接（避免交互式会话残留状态）
- 支持 SSH 端口配置（默认 10022，QEMU 环境）
- 返回结构化结果 + Evidence 数据
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko

    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


# ======================================================================
# 数据结构
# ======================================================================


@dataclass
class SSHResult:
    """SSH 命令执行结果"""

    success: bool
    command: str
    host: str
    port: int
    mode: str = "non_interactive"  # "non_interactive" or "interactive"
    exit_code: int = 0
    raw_stdout: str = ""
    raw_stderr: str = ""
    parsed_data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    interactions: List[Dict[str, str]] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ======================================================================
# ANSI 转义码清理
# ======================================================================

# 匹配 ANSI CSI 序列: ESC [ ... <letter>
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# 匹配其他控制序列 (OSC, title 等)
_ANSI_OTHER = re.compile(r"\x1b\][^\x07]*\x07|\x1b[\(\)][B0UK]")


def _strip_ansi(text: str) -> str:
    """去除终端 ANSI 转义码"""
    text = _ANSI_PATTERN.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return text


# ======================================================================
# SSHTool
# ======================================================================


class SSHTool:
    """
    BMC SSH 命令工具

    使用 paramiko 作为后端，支持两种模式:
    - 非交互式: exec_command() 用于简单的命令执行
    - 交互式: invoke_shell() + expect-like 提示检测，用于 ipmcset adduser 等
    """

    # 默认 shell prompt 匹配模式
    DEFAULT_PROMPT_PATTERNS = [
        r"#\s*$",       # root prompt
        r"\$\s*$",      # user prompt
        r":\s*$",       # 冒号提示 (password 等)
    ]

    def __init__(
        self,
        host: str,
        port: int = 10022,
        user: str = "Administrator",
        password: str = "",
        connect_timeout: int = 15,
    ):
        if not HAS_PARAMIKO:
            raise RuntimeError(
                "paramiko 未安装，请执行: pip install paramiko"
            )

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.connect_timeout = connect_timeout

    def close(self):
        """关闭资源（无持久连接，空操作）。"""
        pass

    def create_session(self) -> "SSHSession":
        """
        创建一个 SSH 长连接会话。

        返回的 SSHSession 使用与当前 SSHTool 相同的连接参数，
        支持 Agent 多轮 Tool Calling 中的动态交互。

        Returns:
            SSHSession 实例（尚未连接，需调用 session.connect()）
        """
        from src.tools.ssh_session import SSHSession

        return SSHSession(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            connect_timeout=self.connect_timeout,
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        interactions: Optional[List[Dict[str, str]]] = None,
    ) -> SSHResult:
        """
        执行 SSH 命令（异步）。

        Args:
            command: 要执行的命令
            timeout: 总超时秒数
            interactions: 交互步骤列表，每个元素 {"expect": "...", "send": "..."}
                          非空时进入交互模式

        Returns:
            SSHResult 结构化结果
        """
        started_at = datetime.now()

        try:
            if interactions:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._execute_interactive_sync,
                        command,
                        timeout,
                        interactions,
                    ),
                    timeout=timeout + 10,  # 留余量给 asyncio 层
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._execute_non_interactive_sync,
                        command,
                        timeout,
                    ),
                    timeout=timeout + 10,
                )
        except asyncio.TimeoutError:
            completed_at = datetime.now()
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                exit_code=-1,
                error=f"SSH 命令超时 ({timeout}s): {command}",
                started_at=started_at,
                completed_at=completed_at,
            )
        except Exception as e:
            completed_at = datetime.now()
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                exit_code=-1,
                error=f"SSH 连接/执行异常: {e}",
                started_at=started_at,
                completed_at=completed_at,
            )

        result.started_at = started_at
        result.completed_at = datetime.now()
        return result

    # ------------------------------------------------------------------
    # 非交互式执行
    # ------------------------------------------------------------------

    def _execute_non_interactive_sync(
        self, command: str, timeout: int
    ) -> SSHResult:
        """
        同步非交互式执行（在工作线程中运行）。

        使用 invoke_shell() 而非 exec_command()，
        因为 BMC dropbear SSH 不支持 SSH exec 请求。
        """
        try:
            client = self._create_client()
            try:
                channel = client.invoke_shell(
                    term="xterm", width=200, height=50
                )
                channel.settimeout(2.0)

                # 1. 等待初始 shell prompt
                initial_output, matched = self._recv_until(
                    channel, [r"#\s*$", r"\$\s*$"],
                    timeout=self.connect_timeout,
                )
                if not matched:
                    return SSHResult(
                        success=False,
                        command=command,
                        host=self.host,
                        port=self.port,
                        error=(
                            f"等待初始 shell prompt 超时，"
                            f"收到: {initial_output[-200:]}"
                        ),
                    )

                # 2. 发送命令
                channel.send(command + "\n")

                # 3. 等待命令执行完成（下一个 shell prompt）
                cmd_output, matched = self._recv_until(
                    channel, [r"#\s*$", r"\$\s*$"],
                    timeout=timeout,
                )

                # 4. 收集剩余输出
                time.sleep(0.2)
                if channel.recv_ready():
                    cmd_output += channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )

                channel.close()

                # 5. 提取命令输出（去掉 echo 的命令本身和 prompt）
                full_output = initial_output + cmd_output
                clean = _strip_ansi(full_output)

                # 从完整输出中提取命令结果部分
                raw_stdout = self._extract_command_output(clean, command)

                parsed = {"exit_code": 0}
                if raw_stdout.strip():
                    parsed["stdout_lines"] = raw_stdout.strip().split("\n")

                evidence = self._build_evidence(
                    command, raw_stdout, "", mode="shell"
                )

                return SSHResult(
                    success=True,
                    command=command,
                    host=self.host,
                    port=self.port,
                    mode="shell",
                    exit_code=0,
                    raw_stdout=raw_stdout,
                    parsed_data=parsed,
                    evidence=evidence,
                )
            finally:
                client.close()

        except paramiko.AuthenticationException as e:
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                error=f"SSH 认证失败: {e}",
            )
        except paramiko.SSHException as e:
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                error=f"SSH 连接异常: {e}",
            )
        except Exception as e:
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                error=f"SSH 执行异常: {e}",
            )

    @staticmethod
    def _extract_command_output(full_output: str, command: str) -> str:
        """
        从 shell 会话完整输出中提取命令执行结果。

        去掉：echo 回显的命令行、末尾的 prompt。
        保留：命令执行后的实际输出。
        """
        lines = full_output.split("\n")
        result_lines = []
        found_command = False

        for line in lines:
            stripped = line.strip()
            # 跳过命令 echo 行
            if not found_command and command.strip() in stripped:
                found_command = True
                continue
            # 跳过末尾 prompt 行（如 "~ ~ $" 或 "~ # "）
            if found_command and re.match(r"^~\s*[~$/#]", stripped):
                continue
            if found_command:
                result_lines.append(line)

        output = "\n".join(result_lines).strip()
        return output

    # ------------------------------------------------------------------
    # 交互式执行
    # ------------------------------------------------------------------

    def _execute_interactive_sync(
        self,
        command: str,
        timeout: int,
        interactions: List[Dict[str, str]],
    ) -> SSHResult:
        """同步交互式执行（在工作线程中运行）"""
        masked_interactions = []

        try:
            client = self._create_client()
            try:
                channel = client.invoke_shell(
                    term="xterm", width=200, height=50
                )
                channel.settimeout(2.0)

                # 1. 等待初始 shell prompt
                initial_output, matched = self._recv_until(
                    channel, [r"#\s*$", r"\$\s*$"], timeout=self.connect_timeout
                )
                if not matched:
                    return SSHResult(
                        success=False,
                        command=command,
                        host=self.host,
                        port=self.port,
                        mode="interactive",
                        error=f"等待初始 shell prompt 超时，收到: {initial_output[-200:]}",
                    )

                # 2. 发送主命令
                channel.send(command + "\n")
                all_output = initial_output

                # 3. 逐个处理交互步骤
                per_step_timeout = timeout // max(len(interactions), 1)
                per_step_timeout = max(per_step_timeout, 5)  # 至少 5 秒

                for i, step in enumerate(interactions):
                    expect_pattern = step.get("expect", "")
                    send_value = step.get("send", "")
                    is_password = step.get("is_password", False)

                    # 等待 expect 模式
                    if expect_pattern:
                        chunk, matched = self._recv_until(
                            channel,
                            [expect_pattern],
                            timeout=per_step_timeout,
                        )
                        all_output += chunk

                        if not matched:
                            # 交互失败，收集剩余输出
                            error_msg = (
                                f"交互步骤 {i + 1} 等待 '{expect_pattern}' 超时，"
                                f"实际收到: {chunk[-300:]}"
                            )
                            masked_interactions.append(
                                self._mask_step(
                                    expect_pattern, send_value, is_password, matched=False
                                )
                            )
                            return SSHResult(
                                success=False,
                                command=command,
                                host=self.host,
                                port=self.port,
                                mode="interactive",
                                raw_stdout=_strip_ansi(all_output),
                                error=error_msg,
                                interactions=masked_interactions,
                            )
                    else:
                        # 无 expect，短暂等待输出
                        time.sleep(0.3)
                        if channel.recv_ready():
                            chunk = channel.recv(4096).decode(
                                "utf-8", errors="replace"
                            )
                            all_output += chunk

                    # 发送响应
                    channel.send(send_value + "\n")
                    time.sleep(0.2)  # 让远程处理

                    # 记录交互（脱敏）
                    masked_interactions.append(
                        self._mask_step(
                            expect_pattern, send_value, is_password, matched=True
                        )
                    )

                # 4. 等待最终 shell prompt
                final_chunk, _ = self._recv_until(
                    channel, [r"#\s*$", r"\$\s*$"], timeout=per_step_timeout
                )
                all_output += final_chunk

                # 5. 收集剩余输出
                time.sleep(0.3)
                if channel.recv_ready():
                    all_output += channel.recv(4096).decode(
                        "utf-8", errors="replace"
                    )

                channel.close()

                clean_output = _strip_ansi(all_output)
                evidence = self._build_evidence(
                    command, clean_output, "", mode="interactive",
                    interactions_count=len(interactions),
                )

                return SSHResult(
                    success=True,
                    command=command,
                    host=self.host,
                    port=self.port,
                    mode="interactive",
                    exit_code=0,
                    raw_stdout=clean_output,
                    parsed_data={"interaction_count": len(interactions)},
                    evidence=evidence,
                    interactions=masked_interactions,
                )

            finally:
                client.close()

        except paramiko.AuthenticationException as e:
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                mode="interactive",
                error=f"SSH 认证失败: {e}",
                interactions=masked_interactions,
            )
        except Exception as e:
            return SSHResult(
                success=False,
                command=command,
                host=self.host,
                port=self.port,
                mode="interactive",
                error=f"SSH 交互执行异常: {e}",
                interactions=masked_interactions,
            )

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _create_client(self) -> "paramiko.SSHClient":
        """创建并连接 SSHClient（适配 BMC dropbear 慢响应）"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # BMC SSH 服务器响应较慢，需要更长的 banner 超时
        # 默认 banner_timeout 只有 15s，BMC 可能需要更久
        banner_timeout = max(self.connect_timeout * 2, 30)

        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=self.connect_timeout,
            banner_timeout=banner_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    # ------------------------------------------------------------------
    # Expect 辅助
    # ------------------------------------------------------------------

    def _recv_until(
        self,
        channel: "paramiko.Channel",
        patterns: List[str],
        timeout: float = 10,
    ) -> Tuple[str, bool]:
        """
        从 Channel 接收数据直到匹配任一模式或超时。

        Args:
            channel: SSH Channel
            patterns: 正则表达式模式列表
            timeout: 秒

        Returns:
            (accumulated_text, matched: bool)
        """
        deadline = time.monotonic() + timeout
        accumulated = ""

        while time.monotonic() < deadline:
            # 尝试读取
            if channel.recv_ready():
                try:
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    accumulated += chunk
                except Exception:
                    break
            else:
                time.sleep(0.05)

            # 检查模式匹配（在清理 ANSI 后的文本上匹配）
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
            if channel.closed or channel.eof_received:
                # 最后一次检查
                break

        return accumulated, False

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_step(
        expect: str, send: str, is_password: bool, matched: bool
    ) -> Dict[str, str]:
        """记录单步交互，密码脱敏"""
        return {
            "expect": expect,
            "send": "****" if is_password else send,
            "matched": matched,
        }

    def _build_evidence(
        self,
        command: str,
        raw_stdout: str,
        raw_stderr: str,
        mode: str = "non_interactive",
        interactions_count: int = 0,
    ) -> Dict[str, Any]:
        """构建 Evidence 字典 (evidence_type="ssh_output")"""
        metadata = {
            "command": command,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "mode": mode,
        }
        if interactions_count:
            metadata["interactions_count"] = interactions_count

        content = raw_stdout
        if raw_stderr:
            content += f"\n--- STDERR ---\n{raw_stderr}"

        return {
            "evidence_type": "ssh_output",
            "content": content,
            "metadata": metadata,
            "captured_at": datetime.now().isoformat(),
        }

    @staticmethod
    def to_json(result: SSHResult) -> str:
        """将 SSHResult 序列化为 JSON 字符串"""
        data = {
            "success": result.success,
            "command": result.command,
            "host": result.host,
            "port": result.port,
            "mode": result.mode,
            "exit_code": result.exit_code,
            "raw_stdout": result.raw_stdout,
            "raw_stderr": result.raw_stderr,
        }
        if result.error:
            data["error"] = result.error
        if result.evidence:
            data["evidence"] = result.evidence
        if result.interactions:
            data["interactions"] = result.interactions
        if result.parsed_data:
            data["parsed_data"] = result.parsed_data
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
