# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - SSH Session Manager

管理 SSH 长连接会话的生命周期：创建、获取、关闭、Evidence 构建。
ExecAgent 只通过 Manager 访问 Session，不直接管理连接细节。

增强功能:
- 会话健康检查（探测通道存活）
- 死会话自动清理
- 重连支持
- 完善的日志记录
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.tools.ssh_session import (
    SSHChannelClosedError,
    SSHConnectionError,
    SSHSession,
    SHELL_PROMPTS,
    strip_ansi,
)

logger = logging.getLogger("ssh_session_manager")


class SSHSessionManager:
    """
    SSH 会话生命周期管理器。

    职责:
    - 按 key 管理多个 SSH 长连接（当前只用 "default"）
    - 连接参数统一管理，Session 创建时注入
    - close() 时从 Session 读取原始数据构建 Evidence
    - close_all() 用于 finally 清理
    - get_or_none() 自动清理死会话
    - health_check() 探测会话存活
    """

    def __init__(
        self,
        host: str,
        port: int = 10022,
        user: str = "Administrator",
        password: str = "",
        connect_timeout: int = 15,
    ):
        self._params = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "connect_timeout": connect_timeout,
        }
        self._sessions: Dict[str, SSHSession] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def open(self, key: str = "default") -> Dict[str, Any]:
        """
        创建并连接一个新的 SSH Session。

        如果已存在活跃会话，直接返回；如果已断开，先清理再创建。

        Args:
            key: 会话标识

        Returns:
            连接结果 dict

        Raises:
            SSHConnectionError: 连接失败
        """
        existing = self._sessions.get(key)
        if existing and existing.is_alive:
            return {
                "status": "already_open",
                "message": (
                    f"会话已存在 ({key}: "
                    f"{self._params['host']}:{self._params['port']})"
                ),
            }

        # 清理已断开的旧会话
        if existing:
            self._cleanup_dead_session(key)

        session = SSHSession(**self._params)
        result = session.connect()
        self._sessions[key] = session

        logger.info(
            f"[SessionManager] opened '{key}' -> "
            f"{self._params['host']}:{self._params['port']} "
            f"({result.get('status')})"
        )
        return result

    def get(self, key: str = "default") -> Optional[SSHSession]:
        """
        获取活跃的 Session。

        自动清理已断开的死会话，返回 None。
        """
        session = self._sessions.get(key)
        if session is None:
            return None
        if session.is_alive:
            return session

        # 会话已死，记录原因并清理
        error = session.last_error
        logger.warning(
            f"[SessionManager] session '{key}' 已断开"
            f"{f': {error}' if error else ''}，正在清理"
        )
        self._cleanup_dead_session(key)
        return None

    def close(self, key: str = "default") -> Dict[str, Any]:
        """
        关闭 Session 并返回完整 Evidence。

        Evidence 从 Session 内部数据构建：
        - interaction_history（密码已在记录时脱敏）
        - all_output（ANSI 已清理）
        - 连接元数据

        Args:
            key: 会话标识

        Returns:
            包含 evidence 的关闭结果
        """
        session = self._sessions.pop(key, None)
        if not session:
            return {
                "status": "no_session",
                "message": f"没有名为 '{key}' 的会话",
            }

        # 先构建 evidence，再断开
        evidence = self._build_evidence(session)
        history = list(session._history)
        interaction_count = len(history)

        result = session.disconnect()
        result["evidence"] = evidence
        result["interaction_history"] = history
        result["interaction_count"] = interaction_count

        logger.info(
            f"[SessionManager] closed '{key}' "
            f"(interactions={interaction_count})"
        )
        return result

    def close_all(self) -> List[str]:
        """
        紧急清理：关闭所有 Session（不返回 Evidence）。

        Returns:
            已清理的 session key 列表
        """
        closed_keys = []
        for key in list(self._sessions.keys()):
            session = self._sessions.pop(key, None)
            if session:
                try:
                    session.disconnect()
                    closed_keys.append(key)
                except Exception as e:
                    logger.warning(
                        f"[SessionManager] close_all: 关闭 '{key}' 异常: {e}"
                    )
                    closed_keys.append(f"{key} (error)")
        logger.info(
            f"[SessionManager] close_all done: {closed_keys}"
        )
        return closed_keys

    def is_alive(self, key: str = "default") -> bool:
        """检查指定 Session 是否活跃。"""
        session = self._sessions.get(key)
        return session is not None and session.is_alive

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def health_check(self, key: str = "default") -> Dict[str, Any]:
        """
        对指定 Session 执行健康检查。

        检查内容:
        1. Session 对象是否存在
        2. 通道是否存活（is_alive）
        3. 尝试非阻塞读取确认通道可操作

        Returns:
            dict with healthy (bool), details (str)
        """
        session = self._sessions.get(key)
        if session is None:
            return {
                "healthy": False,
                "details": f"session '{key}' 不存在",
            }

        if not session.is_alive:
            error = session.last_error or "未知原因"
            return {
                "healthy": False,
                "details": f"session '{key}' 已断开: {error}",
            }

        # 尝试非阻塞读取，确认通道可用
        try:
            available = session.read_available(timeout=0.5)
            return {
                "healthy": True,
                "details": (
                    f"session '{key}' 健康"
                    f"{', 有残留数据' if available.get('has_data') else ''}"
                ),
                "pending_data": available.get("has_data", False),
            }
        except SSHChannelClosedError:
            self._cleanup_dead_session(key)
            return {
                "healthy": False,
                "details": f"session '{key}' 通道在健康检查时关闭",
            }
        except Exception as e:
            return {
                "healthy": False,
                "details": f"session '{key}' 健康检查异常: {e}",
            }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _cleanup_dead_session(self, key: str) -> None:
        """清理已死亡的 Session（安全释放资源）。"""
        session = self._sessions.pop(key, None)
        if session:
            try:
                session.disconnect()
            except Exception:
                pass
            logger.debug(f"[SessionManager] 清理死会话 '{key}'")

    # ------------------------------------------------------------------
    # Evidence building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_evidence(session: SSHSession) -> Dict[str, Any]:
        """
        从 Session 内部状态构建 Evidence dict。

        包含:
        - evidence_type: "ssh_session_output"
        - content: ANSI 清理后的完整输出
        - metadata: 连接信息 + 统计
        - interaction_history: 所有交互步骤（密码已脱敏）
        - captured_at: 时间戳
        """
        return {
            "evidence_type": "ssh_session_output",
            "content": session.all_output_clean,
            "metadata": {
                "host": session.host,
                "port": session.port,
                "user": session.user,
                "opened_at": (
                    session._opened_at.isoformat()
                    if session._opened_at
                    else None
                ),
                "closed_at": datetime.now().isoformat(),
                "interaction_count": session.interaction_count,
            },
            "interaction_history": session.history,
            "captured_at": datetime.now().isoformat(),
        }
