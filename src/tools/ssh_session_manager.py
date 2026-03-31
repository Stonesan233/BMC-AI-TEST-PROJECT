# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - SSH Session Manager

管理 SSH 长连接会话的生命周期：创建、获取、关闭、Evidence 构建。
ExecAgent 只通过 Manager 访问 Session，不直接管理连接细节。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.tools.ssh_session import SSHSession, strip_ansi

logger = logging.getLogger("ssh_session_manager")


class SSHSessionManager:
    """
    SSH 会话生命周期管理器。

    职责:
    - 按 key 管理多个 SSH 长连接（当前只用 "default"）
    - 连接参数统一管理，Session 创建时注入
    - close() 时从 Session 读取原始数据构建 Evidence
    - close_all() 用于 finally 清理
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
        """创建并连接一个新的 SSH Session。"""
        existing = self._sessions.get(key)
        if existing and existing.is_alive:
            return {
                "status": "already_open",
                "message": f"会话已存在 ({key}: {self._params['host']}:{self._params['port']})",
            }

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
        """获取活跃的 Session，如果不存在或已断开返回 None。"""
        session = self._sessions.get(key)
        if session and session.is_alive:
            return session
        return None

    def close(self, key: str = "default") -> Dict[str, Any]:
        """
        关闭 Session 并返回完整 Evidence。

        Evidence 从 Session 内部数据构建：
        - interaction_history（密码已在记录时脱敏）
        - all_output（ANSI 已清理）
        - 连接元数据
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

    def close_all(self) -> None:
        """紧急清理：关闭所有 Session（不返回 Evidence）。"""
        for key in list(self._sessions.keys()):
            session = self._sessions.pop(key, None)
            if session:
                try:
                    session.disconnect()
                except Exception:
                    pass
        logger.info("[SessionManager] close_all done")

    def is_alive(self, key: str = "default") -> bool:
        """检查指定 Session 是否活跃。"""
        session = self._sessions.get(key)
        return session is not None and session.is_alive

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
                    session._opened_at.isoformat() if session._opened_at else None
                ),
                "closed_at": datetime.now().isoformat(),
                "interaction_count": session.interaction_count,
            },
            "interaction_history": session.history,
            "captured_at": datetime.now().isoformat(),
        }
