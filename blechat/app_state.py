"""UI 状态派生：把"badge / 连接数 / 目标 / 发送按钮可用 / busy"从**唯一真源**算出来。

以前这些值是在 `_on_state` / `_on_closed` / `_on_progress` / `_append_out` 里
增量改写的，互相覆盖后就会出现"按钮永久灰、输入框永久只读、badge 与实际不符"。
现在所有路径只调 `_sync_ui()`，由它调用这里的纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass

from .session import State

HANDSHAKE_STATES = frozenset(
    {State.CONNECTED, State.HELLO_SENT, State.CHALLENGE_SENT, State.AUTH_SENT}
)


@dataclass(frozen=True)
class UiState:
    badge_kind: str = "idle"
    badge_text: str = "IDLE"
    connections: int = 0
    targets: tuple[tuple[str, str], ...] = ()
    busy: bool = False

    @property
    def can_send(self) -> bool:
        return bool(self.targets) and not self.busy


def derive_ui_state(
    *,
    mode: str,
    server_running: bool,
    host,
    client_session,
    pending_count: int = 0,
    override_badge: tuple[str, str] | None = None,
    stopping: bool = False,
) -> UiState:
    """host/client 都从会话对象实时推导，不依赖之前画过什么。"""
    if stopping:
        return UiState()

    badge: tuple[str, str]
    targets: tuple[tuple[str, str], ...] = ()
    connections = 0

    if mode == "host":
        badge = ("ready", "READY") if server_running else ("idle", "IDLE")
        if host is not None:
            targets = tuple((pid, name or pid) for pid, name in host.peer_names())
            connections = host.connection_count
    else:
        sess = client_session
        if sess is None:
            badge = ("idle", "IDLE")
        elif sess.state is State.READY:
            badge = ("ready", "READY")
            targets = ((sess.transport.peer_id, sess.peer_name or sess.transport.peer_id),)
            connections = 1
        elif sess.state in HANDSHAKE_STATES:
            badge = ("handshake", "HANDSHAKE")
        elif sess.state is State.AUTH_FAIL:
            badge = ("error", "AUTH_FAIL")
        elif sess.state is State.CLOSED:
            badge = ("error", "CLOSED")
        else:
            badge = ("idle", "IDLE")

    if override_badge is not None:
        badge = override_badge

    return UiState(
        badge_kind=badge[0],
        badge_text=badge[1],
        connections=connections,
        targets=targets,
        busy=pending_count > 0,
    )
