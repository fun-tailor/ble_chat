"""会话状态机：握手、分片、ACK/重传、消息回调。Host 与 Client 共用。"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from . import crypto, protocol
from .ble.base import Transport
from .ble.server import PeerGone, is_link_dead
from .errors import BleChatError, Err
from .protocol import CtrlType

ACK_TIMEOUT = 0.5
# 连续多少个 ACK 窗口没有任何进展就判"重传超限"。
# 3 太紧：BLE 的丢包是成串的（走远一点、身体挡住就是连丢几帧），而一帧要走通
# 需要**数据帧 + 它的 ACK** 两次都活下来（45% 丢包下单窗口成功率只有 ~30%），
# 于是 `(1-0.3)^3 ≈ 34%` 的概率在某一帧上误判 —— 真机上的表现就是"偶尔一条消息
# 发不出去、会话被 BYE 掉"。给到 5 个窗口（2.5s 无进展）能吃掉绝大多数突发丢包，
# 真死了的链路仍然由 keepalive / `SEND_TIMEOUT` 兜底。
MAX_RETRIES = 5
SEND_WINDOW = 4
MAX_AUTH_FAILURES = 3
AUTH_LOCK_SECONDS = 30
PROGRESS_EVERY = 8
HANDSHAKE_TIMEOUT = 15.0  # 无控制帧进展超时（用户输密码期间暂停）
KEEPALIVE_INTERVAL = 20.0  # READY 后的 PING 间隔（保活链路）
# 对方连续多少帧 PING 没回 PONG 就判链路已死。
# 为什么要有这个：只判「我方发得出去」是不够的 —— 对端软件已经下线、
# Windows 却还留着那条 LE 链路时，`notify_value_..._async` 会**静默成功**
# （帧写进了本地缓冲），于是双方都停在 READY：一边以为在连、另一边按钮灰。
#
# 节奏：每 20s 一帧 PING。宽限 `KEEPALIVE_GRACE_MISSES` 轮（给慢/忙的对端留
# 反应时间），之后连续 `KEEPALIVE_MAX_MISSES` 轮收不到 PONG 才判死 ——
# 也就是大约 80~140s 才动手，足够保守。
# 见 PROTOCOL.md §2.1 `PONG(0x0C)`。
KEEPALIVE_GRACE_MISSES = 2
KEEPALIVE_MAX_MISSES = 3
# 单次底层写超时。WinRT 在链路被服务器踢掉时可能**永远不返回**，
# 必须自己掐死，否则 PING/_pump 挂住 → 会话永远停在 READY → 重连不上。
SEND_TIMEOUT = 8.0
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILE = "file"

log = logging.getLogger("blechat.session")

PskProvider = Callable[[bytes, bytes, int, bool], Awaitable[bytes | None]]
# (nonce_c, challenge_salt, challenge_iters, use_eph) -> psk | None(取消)
EphLookup = Callable[[str], tuple[bytes, float] | None]
PeersProvider = Callable[[], list[tuple[str, str]]]


def link_down_reason(exc: BaseException) -> str:
    """把底层 WinRT/bleak 那串原文翻成人话，用于 `连接断开：<原因>` 提示。

    dev 反馈：掉线时界面上刷的是
    `UNKNOWN：peer BluetoothLE#… not subscribed to 0000a003-…` 这种原始串，
    "应该简单提示掉线即可"。详细原文仍然进日志。
    """
    if isinstance(exc, TimeoutError):
        return "链路超时"
    if is_link_dead(exc):
        return "对方已离线"
    text = str(exc).strip()
    return text or "链路已断开"


class State(Enum):
    IDLE = "idle"
    CONNECTED = "connected"
    HELLO_SENT = "hello_sent"
    CHALLENGE_SENT = "challenge_sent"
    AUTH_SENT = "auth_sent"
    AUTH_FAIL = "auth_fail"
    READY = "ready"
    CLOSED = "closed"


@dataclass(slots=True)
class _SendJob:
    msg_id: int
    frames: list[bytes]
    total: int
    cursor: int = 0
    acked: int = 0
    retries: int = 0
    last_ack: float = field(default_factory=time.monotonic)


class PeerSession:
    """一条 BLE 链路上的协议会话（Host 或 Client 角色）。"""

    def __init__(
        self,
        transport: Transport,
        *,
        network_id: str,
        is_host: bool,
        local_device_id: str = "",
        local_name: str = "",
        auth: dict | None = None,
        psk: bytes | None = None,
        psk_provider: PskProvider | None = None,
        eph_lookup: EphLookup | None = None,
        eph_consume: Callable[[str], None] | None = None,
        peers_provider: PeersProvider | None = None,
        auto_pair: bool = False,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
    ) -> None:
        self.transport = transport
        self.network_id = network_id
        self.is_host = is_host
        self.local_device_id = local_device_id
        self.local_name = local_name
        self.auth = auth or {}
        self._psk_static = psk
        self._psk_provider = psk_provider
        self._eph_lookup = eph_lookup
        self._eph_consume = eph_consume
        self._peers_provider = peers_provider
        self.auto_pair = auto_pair
        self._hs_timeout = handshake_timeout
        self._hs_deadline = 0.0
        self._waiting_user = False
        self._keepalive_started = False
        self._retired = False

        self.state = State.IDLE
        self.session_id: int | None = None
        self.session_key: bytes | None = None
        self.peer_device_id = ""
        self.peer_name = transport.peer_name
        self.last_error: Err | None = None
        self.auth_failures = 0
        self.paired = False

        self._psk: bytes | None = None
        self._nonce_c: bytes = b""
        self._nonce_s: bytes = b""
        self._use_eph = False
        self._eph_id = ""
        self._reassembler: protocol.Reassembler | None = None
        self._jobs: dict[int, _SendJob] = {}
        self._acked_seen: dict[int, int] = {}
        # ACK 合并发送：`{msg_id: (contig, total, with_progress)}` + 常驻发送任务
        self._ack_pending: dict[int, tuple[int, int, bool]] = {}
        self._ack_wake = asyncio.Event()
        self._ack_worker_task: asyncio.Task | None = None
        self._msg_counter = secrets.randbits(16)
        self._session_counter = secrets.randbits(16)
        self._bg: set[asyncio.Task] = set()
        self._closed_reason = ""
        self._closed_fired = False
        # 保活（PING↔PONG）状态，见 `_keepalive()`
        self._pong_deadline = 0.0
        self._pong_misses = 0
        self._ping_timeouts = 0
        self.last_pong_at = 0.0

        self.on_state: Callable[[PeerSession], None] | None = None
        self.on_message: Callable[[PeerSession, int, str, bytes], None] | None = None
        self.on_progress: Callable[[PeerSession, int, int, int], None] | None = None
        self.on_peers: Callable[[PeerSession, list[tuple[str, str]]], None] | None = None
        self.on_error: Callable[[PeerSession, Err, str], None] | None = None
        self.on_closed: Callable[[PeerSession, str], None] | None = None
        self.on_resync: Callable[[PeerSession], None] | None = None

        transport.on_ctrl = self.feed_ctrl
        transport.on_data = self.feed_data
        transport.on_closed = self._transport_closed

    # ------------------------------------------------------------- helpers

    @property
    def ready(self) -> bool:
        return self.state is State.READY

    @property
    def label(self) -> str:
        return f"{self.peer_name or self.transport.peer_id} [{self.state.value}]"

    def _spawn(self, coro: Awaitable) -> None:
        self._spawn_task(coro)

    def _spawn_task(self, coro: Awaitable) -> asyncio.Task:
        """起一个后台任务并登记（`close()` 会统一取消）。返回任务本身，
        便于调用方记住它做去重（见 `_post_ack` / `_ack_worker`）。"""
        task = asyncio.ensure_future(coro)
        self._bg.add(task)

        def done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if not t.cancelled() and t.exception():
                log.warning("task error: %s", t.exception())

        task.add_done_callback(done)
        return task

    def _set_state(self, state: State) -> None:
        if self.state is state:
            return
        self.state = state
        log.debug("%s -> %s", self.transport.peer_id, state.value)
        if state is State.READY and not self._keepalive_started:
            self._keepalive_started = True
            # 刚 READY：给对端一个保活宽限期（第一帧 PING 还没发出去）
            self._pong_deadline = time.monotonic() + KEEPALIVE_INTERVAL + SEND_TIMEOUT
            self._spawn(self._keepalive())
            # 我刚连上 → 通知对端**无条件**刷新一次（见 `RESYNC`）。
            # 只靠各自的 `on_state` 兜不住"单方已连接"：一侧重启/睡眠唤醒后，
            # 另一侧可能停在旧状态（READY 却按钮灰、CLOSED 却没重连），
            # 要等下一次 20s PING 才被发现。
            self._send_ctrl(CtrlType.RESYNC)
        if self.on_state:
            self.on_state(self)

    async def _keepalive(self) -> None:
        """READY 后周期性 PING，并**要求对端回 PONG**。

        PING 本身有两个作用：防止 Windows 对空闲 LE 链路做电源管理断开，
        以及探测链路是否还活着。但只判「发得出去」会漏掉最麻烦的一种坏法 ——
        对端软件已经下线，`notify_value_for_subscribed_client_async` 依然**成功**
        （帧写进本地缓冲就算完），于是两边都停在 READY，界面上就是
        「一侧显示已连接、另一侧发送按钮灰」。所以收到对端 PING 要回一帧 PONG，
        自己发出的 PING 超过 `KEEPALIVE_MAX_MISSES` 次没等到 PONG 就判链路已死。

        旧版本按 PROTOCOL.md §7 静默忽略未知类型 `PONG`，不会因为收到它出问题；
        代价是它们不回 PONG，因此与新版本混用时 keepalive **不会**主动判死
        （只靠发送失败判定），行为与之前一致。
        """
        while self.state is State.READY:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self.state is not State.READY:
                return
            # 先判"上一帧 PING 有没有被回应"，再发下一帧。
            # 发送本身失败（对端没订阅/写超时）由 `_send_ctrl_now` 直接判链路已死。
            if not await self._keepalive_round():
                return
            await self._send_ctrl_now(CtrlType.PING)

    async def _keepalive_round(self) -> bool:
        """一轮保活：检查 PONG 是否按时回来（PING 由调用方在上一轮发出）。

        单独拆出来是为了能直接测（不用真等 20s）。
        """
        if self.state is not State.READY:
            return False
        if time.monotonic() < self._pong_deadline:
            return True
        if self._pong_misses < KEEPALIVE_GRACE_MISSES:
            # 宽限期内：对端可能只是慢，再等一轮
            self._pong_misses += 1
            log.debug(
                "PONG late from %s (grace %s/%s)",
                self.transport.peer_id,
                self._pong_misses,
                KEEPALIVE_GRACE_MISSES,
            )
            self._pong_deadline = time.monotonic() + KEEPALIVE_INTERVAL
            return True
        self._pong_misses += 1
        log.warning(
            "no PONG from %s (miss %s/%s)",
            self.transport.peer_id,
            self._pong_misses - KEEPALIVE_GRACE_MISSES,
            KEEPALIVE_MAX_MISSES,
        )
        if self._pong_misses - KEEPALIVE_GRACE_MISSES >= KEEPALIVE_MAX_MISSES:
            await self._link_dead(
                PeerGone(
                    f"no PONG from peer after {self._pong_misses} keepalives"
                    " (half-open link)"
                )
            )
            return False
        # 下一轮再等一个周期
        self._pong_deadline = time.monotonic() + KEEPALIVE_INTERVAL
        return True

    def _emit_error(self, code: Err, message: str) -> None:
        self.last_error = code
        log.warning("%s error %s: %s", self.transport.peer_id, code.name, message)
        if self.on_error:
            self.on_error(self, code, message)

    async def _link_dead(self, exc: BaseException) -> None:
        """链路已死 → 关掉会话触发上层重连。

        以前这类错误只被 `_send_ctrl_now`/`_pump` 打一行 WARNING，
        会话状态仍停在 READY → 上层 watchdog 认为一切正常 → 永远不重连。

        这里**不再 `_emit_error(Err.UNKNOWN, ...)`**：掉线不是"错误"。
        之前界面上会刷出 `UNKNOWN：peer BluetoothLE#… not subscribed to 0000a003…`
        这种吓人的原始串（dev 反馈：应该简单提示掉线即可）。
        真正的提示走 `on_closed` → `连接断开：<人话>`。
        """
        if self.state is State.CLOSED:
            return
        log.info("link dead: %s", exc)
        await self.close(link_down_reason(exc))

    def _send_ctrl(self, ctrl_type: int, payload: bytes = b"") -> None:
        self._spawn(self._send_ctrl_bg(ctrl_type, payload))

    async def _send_ctrl_bg(self, ctrl_type: int, payload: bytes) -> None:
        """后台控制帧（ACK/PROGRESS/BYE/HELLO/AUTH/PEERS/RESYNC）也必须套超时。

        对端不消费通知时 `notify_value_for_subscribed_client_async` 可能**永远
        不返回**，不掐死就会在 `_bg` 里堆成僵尸任务、越积越多。
        PROTOCOL 写的是"底层写一律套 8s"，以前这条 fire-and-forget 路径漏了。
        """
        try:
            await asyncio.wait_for(
                self.transport.send_ctrl(protocol.encode_ctrl(ctrl_type, payload)),
                timeout=SEND_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("send ctrl %s failed: %s", ctrl_type, exc)
            # 链路已死就不能只打一行日志：否则会话停在 READY、
            # 上层以为一切正常，直到下一次 20s PING 才发现 —— 期间就是"单方已连接"
            if is_link_dead(exc) and self.state is State.READY:
                await self._link_dead(exc)

    def _send_bye(self, code: Err) -> None:
        self._send_ctrl(CtrlType.BYE, bytes([int(code)]))

    async def _send_ctrl_now(self, ctrl_type: int, payload: bytes = b"") -> bool:
        try:
            await asyncio.wait_for(
                self.transport.send_ctrl(protocol.encode_ctrl(ctrl_type, payload)),
                timeout=SEND_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            self._ping_timeouts += 1
            log.warning("send ctrl %s timed out after %ss", ctrl_type, SEND_TIMEOUT)
            if self.state is State.READY:
                await self._link_dead(TimeoutError(f"ctrl {ctrl_type} send timeout"))
            return False
        except Exception as exc:
            log.warning("send ctrl %s failed: %s", ctrl_type, exc)
            if is_link_dead(exc) and self.state is State.READY:
                await self._link_dead(exc)
            return False

    async def _bye_now(self, code: Err) -> None:
        await self._send_ctrl_now(CtrlType.BYE, bytes([int(code)]))

    def _alloc_msg_id(self) -> int:
        self._msg_counter = (self._msg_counter + 1) & 0xFFFF
        return (self._session_counter << 16) | self._msg_counter

    # ------------------------------------------------------------- receive

    def feed_ctrl(self, raw: bytes) -> None:
        try:
            ctrl_type, payload = protocol.decode_ctrl(raw)
        except BleChatError as exc:
            self._emit_error(exc.code, str(exc))
            return
        if self.state not in (State.READY, State.CLOSED):
            self._hs_deadline = time.monotonic() + self._hs_timeout
        try:
            if ctrl_type == CtrlType.BYE:
                code = Err(payload[0]) if payload else Err.UNKNOWN
                self._handle_bye(code)
            elif ctrl_type == CtrlType.ACK:
                self._on_ack(*protocol.decode_ack(payload))
            elif ctrl_type == CtrlType.PROGRESS:
                self._on_ack(*protocol.decode_progress(payload)[:2])
            elif ctrl_type == CtrlType.RESYNC:
                self._handle_resync()
            elif ctrl_type == CtrlType.PING:
                self._handle_ping()
            elif ctrl_type == CtrlType.PONG:
                self._handle_pong()
            elif self.is_host:
                self._host_ctrl(ctrl_type, payload)
            else:
                self._client_ctrl(ctrl_type, payload)
        except BleChatError as exc:
            self._emit_error(exc.code, str(exc))
        except Exception as exc:
            log.exception("ctrl handler failed")
            self._emit_error(Err.UNKNOWN, str(exc))

    def feed_data(self, raw: bytes) -> None:
        if self.state is not State.READY or self._reassembler is None or self.session_key is None:
            self._emit_error(Err.NOT_READY, "未完成握手，数据帧被丢弃")
            self._send_bye(Err.NOT_READY)
            return
        try:
            chunk = protocol.DataChunk.decode(raw)
            result = self._reassembler.feed(raw)
        except BleChatError as exc:
            self._emit_error(exc.code, str(exc))
            if exc.code is Err.CRC_MISMATCH:
                self._send_bye(Err.CRC_MISMATCH)
            return

        if result is not None:
            msg_id, flags, payload = result
            contig = chunk.total
            kind = protocol.kind_of_flags(flags)
            if self.on_message:
                self.on_message(self, msg_id, kind, payload)
        else:
            msg_id = chunk.msg_id
            contig = self._reassembler.progress(msg_id)

        if contig > self._acked_seen.get(msg_id, -1):
            self._acked_seen[msg_id] = contig
            # ACK 走**合并发送**（见 `_post_ack`），不是每帧 spawn 一个任务。
            # 真机上对方一次发 17 片就是 17 个 ACK，每个 ACK 在 server 侧是
            # 一次 `notify_value_for_subscribed_client_async`；全 spawn 出去会
            # 让事件循环里排满 WinRT 异步写，GUI/watchdog/PONG 全被挤到后面
            # （dev 报的"server 端 UI 卡住"就是这样起来的）。
            want_progress = contig % PROGRESS_EVERY == 0 or contig == chunk.total
            self._post_ack(msg_id, contig, chunk.total, with_progress=want_progress)
        if len(self._acked_seen) > 64:
            for key in list(self._acked_seen)[:32]:
                del self._acked_seen[key]

    def _post_ack(self, msg_id: int, contig: int, total: int, *, with_progress: bool) -> None:
        """登记"待发 ACK"，由 `_ack_worker` 合并发送。

        合并规则：同一 `msg_id` 只保留**最新**进度（`contig` 单调递增）；
        已经在飞的那一帧照发。这样一个 17 片的突发最多产生少量控制帧，
        而不是 17 个 spawn 出去的发送任务。
        """
        pending = self._ack_pending.get(msg_id)
        if pending is None or contig > pending[0]:
            self._ack_pending[msg_id] = (contig, total, with_progress)
        elif with_progress:
            self._ack_pending[msg_id] = (pending[0], pending[1], True)
        self._ack_wake.set()
        if self._ack_worker_task is None and self.state is State.READY:
            self._ack_worker_task = self._spawn_task(self._ack_worker())

    async def _ack_worker(self) -> None:
        """常驻：攒下的 ACK / PROGRESS 逐条发出去；没活就挂在 Event 上。

        常驻（而不是"发完就退出、有新活再 spawn"）是为了避免两个任务同时
        在 `finally` 里抢着重启自己 —— 那会漏掉刚登记的 ACK。
        用 `Event` 而不是轮询：空闲时**完全不占事件循环**。
        """
        try:
            while self.state is State.READY:
                if not self._ack_pending:
                    self._ack_wake.clear()
                    await self._ack_wake.wait()
                    continue
                msg_id, (contig, total, with_progress) = self._ack_pending.popitem()
                frames = [(CtrlType.ACK, protocol.encode_ack(msg_id, contig))]
                if with_progress:
                    frames.append(
                        (CtrlType.PROGRESS, protocol.encode_progress(msg_id, contig, total))
                    )
                for ctrl_type, payload in frames:
                    try:
                        await asyncio.wait_for(
                            self.transport.send_ctrl(protocol.encode_ctrl(ctrl_type, payload)),
                            timeout=SEND_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug("ack send failed: %s", exc)
                        if is_link_dead(exc) and self.state is State.READY:
                            await self._link_dead(exc)
                        return
        finally:
            self._ack_worker_task = None

    # ------------------------------------------------------------- client role

    def start_handshake(self) -> None:
        """Client：发出 HELLO。"""
        self._set_state(State.CONNECTED)
        self._nonce_c = os.urandom(16)
        hello = protocol.Hello(
            device_id=self.local_device_id or "00000000-0000-0000-0000-000000000000",
            name=self.local_name,
            nonce_c=self._nonce_c,
            use_eph=self._use_eph,
            proto_ver=protocol.PROTO_VER,
            eph_id=self._eph_id,
        )
        self._send_ctrl(CtrlType.HELLO, hello.encode())
        log.info(
            "HELLO sent to %s (use_eph=%s, proto=%s)",
            self.transport.peer_id,
            hello.use_eph,
            hello.proto_ver,
        )
        self._set_state(State.HELLO_SENT)
        self._hs_deadline = time.monotonic() + self._hs_timeout
        self._spawn(self._handshake_watchdog())

    async def _handshake_watchdog(self) -> None:
        """握手无进展则超时关闭，交由上层退避重连（用户输密码期间不计时）。"""
        while True:
            await asyncio.sleep(0.5)
            if self.state in (State.READY, State.CLOSED):
                return
            if self._waiting_user:
                self._hs_deadline = time.monotonic() + self._hs_timeout
            elif time.monotonic() > self._hs_deadline:
                self._emit_error(
                    Err.HANDSHAKE_TIMEOUT,
                    f"握手超时（{self._hs_timeout:.0f}s 无进展，当前 {self.state.value}）",
                )
                await self._bye_now(Err.HANDSHAKE_TIMEOUT)
                await self.close("handshake timeout")
                return

    def use_ephemeral(self, eph_id: str) -> None:
        self._use_eph = True
        self._eph_id = eph_id

    def _client_ctrl(self, ctrl_type: int, payload: bytes) -> None:
        if ctrl_type == CtrlType.CHALLENGE:
            self._spawn(self._handle_challenge(protocol.Challenge.decode(payload)))
        elif ctrl_type == CtrlType.AUTH_OK:
            self._handle_auth_ok(protocol.AuthOk.decode(payload))
        elif ctrl_type == CtrlType.AUTH_FAIL:
            self._handle_auth_fail(payload)
        elif ctrl_type == CtrlType.PEERS:
            peers = protocol.decode_peers(payload)
            if self.on_peers:
                self.on_peers(self, peers)
        elif ctrl_type == CtrlType.HELLO:
            self._emit_error(Err.UNKNOWN, "客户端收到 HELLO")

    async def _handle_challenge(self, ch: protocol.Challenge) -> None:
        if self.state is not State.HELLO_SENT:
            self._emit_error(Err.NOT_READY, "收到 CHALLENGE 但状态不对")
            return
        self._nonce_s = ch.nonce_s
        self._waiting_user = True
        try:
            if self._psk_provider is not None:
                psk = await self._psk_provider(self._nonce_c, ch.salt, ch.iters, ch.use_eph)
            else:
                psk = self._psk_static
        finally:
            self._waiting_user = False
        if psk is None:
            self._emit_error(Err.AUTH_FAILED, "已取消密码输入")
            await self.close("cancelled")
            return
        self._psk = psk
        self._send_ctrl(CtrlType.AUTH, crypto.hmac_auth(psk, self._nonce_c, ch.nonce_s))
        log.info("CHALLENGE received, AUTH sent to %s (eph=%s)", self.transport.peer_id, ch.use_eph)
        self._set_state(State.AUTH_SENT)

    def _handle_auth_ok(self, ok: protocol.AuthOk) -> None:
        if self._psk is None:
            self._emit_error(Err.AUTH_FAILED, "未派生 PSK")
            return
        self.session_id = ok.session_id
        self.session_key = crypto.hkdf_session_key(self._psk, ok.hkdf_salt)
        self._psk = None  # 握手完成即丢弃
        self._reassembler = protocol.Reassembler(self.session_key)
        # Host 在 AUTH_OK 里带自己的昵称：client 之前只拿到 BLE 广播名
        # （Windows 设备名/代号），所以界面上一直显示代号。
        if ok.name:
            self.peer_name = ok.name
            self.transport.peer_name = ok.name
        self._set_state(State.READY)
        log.info("handshake READY with %s (session=%s)", self.transport.peer_id, ok.session_id)
        if self.auto_pair:
            self._spawn(self._do_pair())

    async def _do_pair(self) -> None:
        ok = await self.transport.pair()
        self.paired = ok
        log.info("system pairing: %s", ok)
        if not ok:
            self._emit_error(
                Err.UNKNOWN,
                "系统配对失败（Windows 通常会顺带断开链路；可保持 config.json 的 system_pairing=false）",
            )

    def _handle_auth_fail(self, payload: bytes) -> None:
        code = Err(payload[0]) if payload else Err.AUTH_FAILED
        self.auth_failures += 1
        self._set_state(State.AUTH_FAIL)
        self._emit_error(code, f"认证失败（第 {self.auth_failures} 次）")

    # ------------------------------------------------------------- host role

    def _host_ctrl(self, ctrl_type: int, payload: bytes) -> None:
        if ctrl_type == CtrlType.HELLO:
            self._spawn(self._handle_hello(protocol.Hello.decode(payload)))
        elif ctrl_type == CtrlType.AUTH:
            self._spawn(self._handle_auth(payload))
        elif ctrl_type == CtrlType.AUTH_OK:
            self._emit_error(Err.UNKNOWN, "Host 收到 AUTH_OK")

    async def _handle_hello(self, hello: protocol.Hello) -> None:
        if hello.proto_ver != protocol.PROTO_VER:
            self._emit_error(Err.PROTO_VER_MISMATCH, f"协议版本 {hello.proto_ver}")
            await self._bye_now(Err.PROTO_VER_MISMATCH)
            await self.close("proto ver")
            return
        self.peer_device_id = hello.device_id
        self.peer_name = hello.name or self.transport.peer_name
        self.transport.peer_name = self.peer_name
        self._nonce_c = hello.nonce_c
        self._use_eph = hello.use_eph
        self._eph_id = hello.eph_id
        self._set_state(State.CONNECTED)

        salt = b"\x00" * 16
        iters = 0
        try:
            import base64

            salt = base64.b64decode(self.auth.get("salt", ""))
            iters = int(self.auth.get("iters", 0))
        except Exception:
            pass

        if hello.use_eph:
            entry = self._eph_lookup(hello.eph_id) if self._eph_lookup else None
            if entry is None:
                self._emit_error(Err.EPH_NOT_FOUND, f"临时密钥 {hello.eph_id} 不存在")
                await self._send_ctrl_now(CtrlType.AUTH_FAIL, bytes([int(Err.EPH_NOT_FOUND)]))
                await self._bye_now(Err.EPH_NOT_FOUND)
                await self.close("eph not found")
                return
            key, expires_at = entry
            if expires_at <= time.time():
                self._emit_error(Err.EPH_EXPIRED, f"临时密钥 {hello.eph_id} 已过期")
                await self._send_ctrl_now(CtrlType.AUTH_FAIL, bytes([int(Err.EPH_EXPIRED)]))
                await self._bye_now(Err.EPH_EXPIRED)
                await self.close("eph expired")
                return
            self._psk = key
        else:
            self._psk = self._psk_static
            if self._psk is None:
                self._emit_error(Err.AUTH_FAILED, "Host 未提供密码")
                await self._bye_now(Err.AUTH_FAILED)
                await self.close("no psk")
                return

        self._nonce_s = os.urandom(16)
        ok = await self._send_ctrl_now(
            CtrlType.CHALLENGE,
            protocol.Challenge(self._nonce_s, salt, iters, hello.use_eph).encode(),
        )
        if not ok:
            self._emit_error(Err.UNKNOWN, "CHALLENGE 发送失败（对端未订阅 CTRL 通知？）")
            return
        log.info(
            "HELLO ok -> CHALLENGE to %s (peer=%s, proto=%s, eph=%s)",
            self.transport.peer_id,
            self.peer_name,
            hello.proto_ver,
            hello.use_eph,
        )
        self._set_state(State.CHALLENGE_SENT)

    async def _handle_auth(self, payload: bytes) -> None:
        if self.state not in (State.CHALLENGE_SENT, State.AUTH_FAIL) or not self._psk:
            self._emit_error(Err.NOT_READY, "未进入认证阶段")
            await self._bye_now(Err.NOT_READY)
            return
        if not crypto.verify_hmac(self._psk, self._nonce_c, self._nonce_s, payload):
            self.auth_failures += 1
            await self._send_ctrl_now(CtrlType.AUTH_FAIL, bytes([int(Err.AUTH_FAILED)]))
            self._set_state(State.AUTH_FAIL)
            self._emit_error(Err.AUTH_FAILED, f"HMAC 校验失败（第 {self.auth_failures} 次）")
            if self.auth_failures >= MAX_AUTH_FAILURES:
                await self._bye_now(Err.AUTH_FAILED)
                await self.close("too many auth failures")
            return

        self.session_id = (self._session_counter << 16) | secrets.randbits(16)
        self.session_key = crypto.hkdf_session_key(self._psk, self._nonce_s)
        ok = await self._send_ctrl_now(
            CtrlType.AUTH_OK,
            protocol.AuthOk(self.session_id, self._nonce_s, self.local_name).encode(),
        )
        if not ok:
            self._emit_error(Err.UNKNOWN, "AUTH_OK 发送失败（对端已断开？）")
        else:
            log.info("AUTH ok -> READY with %s", self.transport.peer_id)
        self._psk = None
        self._reassembler = protocol.Reassembler(self.session_key)
        self._set_state(State.READY)
        if self._use_eph and self._eph_id and self._eph_consume:
            try:
                self._eph_consume(self._eph_id)
            except Exception as exc:
                log.warning("eph consume failed: %s", exc)
        self._broadcast_peers()

    def _broadcast_peers(self) -> None:
        if not self._peers_provider:
            return
        try:
            peers = self._peers_provider()
        except Exception:
            return
        self._send_ctrl(CtrlType.PEERS, protocol.encode_peers(peers))

    # ------------------------------------------------------------- common

    def _handle_bye(self, code: Err) -> None:
        self._emit_error(code, f"对端 BYE: {code.name}")
        self._closed_reason = code.name
        self._set_state(State.CLOSED)
        self._fire_closed(code.name)

    def _fire_closed(self, reason: str) -> None:
        if self._closed_fired:
            return
        self._closed_fired = True
        if self.on_closed:
            self.on_closed(self, reason)

    def _transport_closed(self, reason: str) -> None:
        if self.state is State.CLOSED:
            self._fire_closed(reason)
            return
        self._closed_reason = reason
        self._set_state(State.CLOSED)
        self._fire_closed(reason)

    def _handle_resync(self) -> None:
        """对端刚进入 READY，要求我方**无条件**刷新一次状态/UI。

        不回包 —— 双方各自在自己的 READY 时各发一次就覆盖了双向，
        回包会让两端 ping-pong 成环（见 PROTOCOL.md §7）。
        """
        log.info(
            "resync requested by %s (state=%s)", self.transport.peer_id, self.state.value
        )
        if self.is_host:
            # 顺带把最新在线名单推给对端：对端刚从睡眠/重启回来，列表可能是旧的
            self._broadcast_peers()
        if self.on_resync:
            self.on_resync(self)

    def _handle_ping(self) -> None:
        """对端保活探针 → 回一帧 PONG。

        `PING` 以前是"发了不要求回"的，导致对端只能确认「我方发得出去」，
        无法确认「我方还收得到」—— 半开链路（对端软件已下线、Windows 还留着
        LE 链路）因此能一直停在 READY。回包让发送方 20s 内就能判死。
        """
        log.debug("ping from %s -> pong", self.transport.peer_id)
        self._send_ctrl(CtrlType.PONG)

    def _handle_pong(self) -> None:
        self._pong_misses = 0
        self.last_pong_at = time.time()
        self._pong_deadline = time.monotonic() + KEEPALIVE_INTERVAL + SEND_TIMEOUT
        log.debug("pong from %s at %.1f", self.transport.peer_id, self.last_pong_at)

    def _on_ack(self, msg_id: int, next_seq: int) -> None:
        job = self._jobs.get(msg_id)
        if job is None:
            return
        if next_seq > job.acked:
            job.acked = min(int(next_seq), job.total)
            job.retries = 0
            job.last_ack = time.monotonic()
            job.cursor = max(job.cursor, job.acked)
            if self.on_progress:
                self.on_progress(self, msg_id, job.acked, job.total)

    # ------------------------------------------------------------- sending

    def send_text(self, text: str) -> int:
        return self._send_payload(text.encode("utf-8"), image=False)

    def send_image(self, data: bytes) -> int:
        return self._send_payload(bytes(data), image=True)

    def send_file(self, data: bytes, meta: protocol.FileMeta) -> int:
        if len(data) > protocol.MAX_FILE:
            raise BleChatError(
                Err.MSG_TOO_LARGE, f"文件超过 2MB（{len(data) // 1024} KB）"
            )
        return self._send_payload(meta.encode(bytes(data)), file=True)

    def _send_payload(self, payload: bytes, *, image: bool = False, file: bool = False) -> int:
        if self.state is not State.READY or self.session_key is None:
            raise BleChatError(Err.NOT_READY, "尚未就绪")
        if file and len(payload) > protocol.MAX_FILE + 4100:
            raise BleChatError(Err.MSG_TOO_LARGE, f"文件超过 2MB（{len(payload) // 1024} KB）")
        msg_id = self._alloc_msg_id()
        frames = protocol.pack_message(
            msg_id,
            payload,
            self.session_key,
            image=image,
            file=file,
            allow_compress=True,
            max_chunk=self.transport.max_chunk,
        )
        job = _SendJob(msg_id=msg_id, frames=frames, total=len(frames))
        self._jobs[msg_id] = job
        self._spawn(self._pump(job))
        if self.on_progress:
            self.on_progress(self, msg_id, 0, job.total)
        return msg_id

    async def _pump(self, job: _SendJob) -> None:
        try:
            while job.acked < job.total:
                if job.acked >= 0 and time.monotonic() - job.last_ack > ACK_TIMEOUT:
                    job.retries += 1
                    if job.retries > MAX_RETRIES:
                        raise BleChatError(Err.TOO_MANY_RETRIES, f"msg {job.msg_id} 重传超限")
                    log.warning(
                        "resend msg=%s from %s (retry %s)", job.msg_id, job.acked, job.retries
                    )
                    job.cursor = job.acked
                    job.last_ack = time.monotonic()
                while job.cursor < job.total and job.cursor - job.acked < SEND_WINDOW:
                    await asyncio.wait_for(
                        self.transport.send_data(job.frames[job.cursor]),
                        timeout=SEND_TIMEOUT,
                    )
                    job.cursor += 1
                    # 让出一拍：给人机会把对端的 ACK 处理掉再判超时。
                    # 连续 `await` 在 FakeTransport 这类"同步回调"的传输层上
                    # 不会真正切走事件循环，于是一整个窗口发完才第一次看到
                    # ACK（`resend … from 0 (retry 1)` 这种假重传就是这么来的）。
                    await asyncio.sleep(0)
                await asyncio.sleep(0.02)
        except asyncio.TimeoutError as exc:
            log.warning("send_data timed out for msg=%s", job.msg_id)
            await self._link_dead(exc)
        except BleChatError as exc:
            self._emit_error(exc.code, str(exc))
            self._send_bye(exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("send failed")
            if is_link_dead(exc):
                await self._link_dead(exc)
            else:
                # 未知错误也不能让 pending 卡在半圈：发一次错误，上层清掉进度环
                self._emit_error(Err.UNKNOWN, str(exc))
        finally:
            self._jobs.pop(job.msg_id, None)
            if job.acked >= job.total and self.on_progress:
                self.on_progress(self, job.msg_id, job.total, job.total)

    # ------------------------------------------------------------- lifecycle

    def retire(self, reason: str) -> None:
        """本地作废会话但**不碰传输层**。

        用于对端在同一个 peer_id 上重新发起握手（对端重启/重连）时，
        把上一条僵尸会话清掉，避免新 GATT 会话被旧对象的关闭逻辑误伤。
        """
        if self.state is State.CLOSED:
            return
        self._retired = True
        self._set_state(State.CLOSED)
        self._fire_closed(reason)
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        self._jobs.clear()

    async def close(self, reason: str = "closed") -> None:
        self._closed_reason = reason
        self._set_state(State.CLOSED)
        self._fire_closed(reason)
        current = asyncio.current_task()
        for task in list(self._bg):
            if task is not current:
                task.cancel()
        self._bg.clear()
        self._jobs.clear()
        if getattr(self, "_retired", False):
            # 已被顶替：传输层属于新会话，绝不能关
            return
        try:
            await self.transport.close()
        except Exception:
            pass


class HostService:
    """Host 角色：在 GATT server 上管理多个 Client 会话。"""

    def __init__(
        self,
        server,
        *,
        network_id: str,
        auth: dict,
        psk: bytes | None,
        local_device_id: str = "",
        local_name: str = "",
        eph_lookup: EphLookup | None = None,
        eph_consume: Callable[[str], None] | None = None,
        on_session: Callable[[PeerSession], None] | None = None,
    ) -> None:
        from .ble.base import ServerTransport

        self.server = server
        self.network_id = network_id
        self.auth = auth
        self.psk = psk
        self.local_device_id = local_device_id
        self.local_name = local_name
        self.eph_lookup = eph_lookup
        self.eph_consume = eph_consume
        self.on_session = on_session
        self.sessions: dict[str, PeerSession] = {}
        self._transport_cls = ServerTransport

        server.on_peer_ctrl = self._on_ctrl
        server.on_peer_data = self._on_data
        server.on_peer_disconnected = self._on_disconnected

    # ------------------------------------------------------------- plumbing

    def _ensure(self, peer_id: str, peer_name: str = "") -> PeerSession:
        session = self.sessions.get(peer_id)
        if session is not None and session.state is State.CLOSED:
            # 已关闭却还没从表里摘掉：作废重建，别把新帧喂给僵尸会话
            self.sessions.pop(peer_id, None)
            session = None
        if session is None:
            transport = self._transport_cls(self.server, peer_id, peer_name)
            session = PeerSession(
                transport,
                network_id=self.network_id,
                is_host=True,
                local_device_id=self.local_device_id,
                local_name=self.local_name,
                auth=self.auth,
                psk=self.psk,
                eph_lookup=self.eph_lookup,
                eph_consume=self.eph_consume,
                peers_provider=self._peers,
            )
            self.sessions[peer_id] = session
            log.info("new peer session %s", peer_id)
            if self.on_session:
                self.on_session(session)
            app_state = session.on_state
            app_closed = session.on_closed

            def _state(s: PeerSession) -> None:
                if app_state:
                    app_state(s)
                if s.state is State.READY:
                    self._broadcast_peers()

            def _closed(s: PeerSession, reason: str) -> None:
                if app_closed:
                    app_closed(s, reason)
                self._session_closed(s, reason)

            session.on_state = _state
            session.on_closed = _closed
        return session

    def _on_ctrl(self, peer_id: str, data: bytes) -> None:
        # 对端重新握手 = 新链路：旧会话（可能仍挂在 READY 上）就地作废
        if len(data) >= 2 and data[0] == protocol.CTRL_MAGIC and data[1] == CtrlType.HELLO:
            existing = self.sessions.get(peer_id)
            if existing is not None and existing.state not in (State.IDLE, State.CONNECTED):
                log.info(
                    "session replaced for %s (old=%s)", peer_id, existing.state.value
                )
                self.sessions.pop(peer_id, None)
                existing.retire("session replaced")
        self._ensure(peer_id).feed_ctrl(data)

    def _on_data(self, peer_id: str, data: bytes) -> None:
        self._ensure(peer_id).feed_data(data)

    def _on_disconnected(self, peer_id: str, reason: str) -> None:
        session = self.sessions.pop(peer_id, None)
        if session is not None:
            session._transport_closed(reason)

    def _session_closed(self, session: PeerSession, reason: str) -> None:
        peer_id = session.transport.peer_id
        if self.sessions.get(peer_id) is session:
            self.sessions.pop(peer_id, None)
        log.info("peer %s closed: %s", peer_id, reason)
        self._broadcast_peers()

    def _peers(self) -> list[tuple[str, str]]:
        return [
            (s.peer_device_id, s.peer_name)
            for s in self.sessions.values()
            if s.state is State.READY and s.peer_device_id
        ]

    def _broadcast_peers(self) -> None:
        payload = protocol.encode_peers(self._peers())
        for session in list(self.sessions.values()):
            if session.state is State.READY:
                session._send_ctrl(CtrlType.PEERS, payload)

    # ------------------------------------------------------------- api

    @property
    def ready_sessions(self) -> list[PeerSession]:
        return [s for s in self.sessions.values() if s.state is State.READY]

    @property
    def connection_count(self) -> int:
        return len(self.ready_sessions)

    def send_text(self, peer_ids: list[str] | None, text: str) -> list[tuple[str, int]]:
        return self._send(peer_ids, lambda s: s.send_text(text))

    def send_image(self, peer_ids: list[str] | None, data: bytes) -> list[tuple[str, int]]:
        return self._send(peer_ids, lambda s: s.send_image(data))

    def send_file(
        self, peer_ids: list[str] | None, data: bytes, meta: protocol.FileMeta
    ) -> list[tuple[str, int]]:
        return self._send(peer_ids, lambda s: s.send_file(data, meta))

    def _send(self, peer_ids: list[str] | None, fn) -> list[tuple[str, int]]:
        wanted = set(peer_ids) if peer_ids else None
        targets = (
            [s for s in self.sessions.values() if wanted and s.transport.peer_id in wanted]
            if wanted is not None
            else self.ready_sessions
        )
        out: list[tuple[str, int]] = []
        for session in targets:
            if session.state is not State.READY:
                continue
            try:
                out.append((session.transport.peer_id, fn(session)))
            except BleChatError as exc:
                log.warning("send to %s failed: %s", session.transport.peer_id, exc)
        return out

    def peer_names(self) -> list[tuple[str, str]]:
        return [(s.transport.peer_id, s.peer_name) for s in self.ready_sessions]

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            try:
                await session.close("server shutdown")
            except Exception:
                pass
        self.sessions.clear()
