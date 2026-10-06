"""Host 侧传输：WinRT GattServiceProvider（广播 + GATT server）。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties as Props,
    GattLocalCharacteristic,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisementStatus,
    GattServiceProviderAdvertisingParameters,
)
from winrt.windows.storage.streams import DataReader, DataWriter

from ..protocol import DATA_HEADER
from .uuid_defs import (
    ADVERTISE_RETRY,
    ADVERTISE_WAIT,
    CHAR_CTRL,
    CHAR_TX,
    UUID_CTRL,
    UUID_RX,
    UUID_SERVICE,
    UUID_TX,
)

log = logging.getLogger("blechat.ble.server")

# watchdog 最短两次 revive 之间的间隔：反复 stop/start_advertising 会把在线对端踢掉
REVIVE_COOLDOWN = 30.0
# 会话多久没有任何入站数据就算"僵尸"。
# 两端各有 20s 一跳的 PING/PONG，所以**真活着**的对端不可能静默这么久；
# 而对端机器睡眠/崩溃时 WinRT 可能既不回调 `session_status_changed` 也不改
# `subscribed_clients`，`_sessions` 里就留着一条"看着还活着"的僵尸。
# 这条僵尸会让 `advertising_ok` 恒为真 ⇒ watchdog 永远不去恢复广播 ⇒
# 对端唤醒后一直连不上，而 host 这边看着一切正常。
SESSION_IDLE_TRUST = 60.0


# debug phone
def _dump_recv(channel: str, peer_id: str, data: bytes, *, limit: int = 256) -> None:
    """把手机写过来的字节打出来（hex + 可打印文本），过长的只打印前 limit 字节。"""
    head = data[:limit]
    tail = "" if len(data) <= limit else f" ...(+{len(data) - limit}B)"
    hexs = head.hex(" ")
    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    log.info(
        "recv %s from %s: %d bytes%s\n  HEX: %s\n  TXT: %s",
        channel, peer_id[-12:], len(data), tail, hexs, txt,
    )

class PeerGone(Exception):
    pass


# RO_E_CLOSED = 0x80000013 "The object has been closed."（WinRT 对象已被关闭）
RO_E_CLOSED = 0x80000013
RO_E_CLOSED_SIGNED = -2147483629


def is_closed_error(exc: BaseException) -> bool:
    """判断异常是否是 WinRT `RO_E_CLOSED`（对端/会话对象已销毁）。

    pywinrt 抛出的 OSError 里 HRESULT 落在 `winerror`，且**可能是有符号的**
    （真机实测 `OSError(22, '该对象已关闭。', None, -2147483629)`，`errno=22` 与
    HRESULT 无关，`str()` 只有本地化文本、没有 hex）。因此：
    1. `winerror` 先做 `& 0xFFFFFFFF` 归一化再比；
    2. `errno` 分支保留（两参数构造 `OSError(0x80000013, ...)` 会把 HRESULT 放这里）；
    3. 文本分支补十进制与本地化关键词 —— 中文系统上消息是「该对象已关闭。」。
    """
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror is not None and (winerror & 0xFFFFFFFF) == RO_E_CLOSED:
            return True
        if exc.errno in (RO_E_CLOSED, RO_E_CLOSED_SIGNED):
            return True
    text = str(exc)
    if "0x80000013" in text or "80000013" in text:
        return True
    if str(RO_E_CLOSED_SIGNED) in text:  # '[WinError -2147483629] ...'
        return True
    low = text.lower()
    return "has been closed" in low or "已关闭" in text


def is_link_dead(exc: BaseException) -> bool:
    """判断异常是否意味着**链路已经死了**（必须关会话触发重连）。

    早期只认 `RO_E_CLOSED`，导致 `GATT Protocol Error: Unlikely Error`
    这类"对端已不在"的错误只被打 WARNING、会话保持 READY → 永远重连不上。
    """
    if isinstance(exc, PeerGone):
        return True
    if is_closed_error(exc):
        return True
    text = str(exc).lower()
    if "gatt protocol error" in text:
        return True
    if "not connected" in text or "not connected to" in text:
        return True
    if "unexpected" in text and "error" in text:
        return True
    if "device" in text and "connect" in text and "fail" in text:
        return True
    if "unreachable" in text or "host is down" in text:
        return True
    # `GattServer._subscribed` 抛的 `PeerGone(peer … not subscribed to 0000a003)`。
    # 包一层 PeerGone 时已经 True，但同文案也可能从 WinRT 回调原样冒出来，
    # dev 的日志里就见过 —— 漏判会话会停在 READY，直到 20s PING 才发现。
    if "not subscribed" in text:
        return True
    return False


def to_buffer(data: bytes):
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()


def from_buffer(buf) -> bytes:
    r = DataReader.from_buffer(buf)
    out = bytearray(buf.length)
    r.read_bytes(out)
    return bytes(out)


def session_peer_id(session) -> str:
    """GattSession → 稳定 peer_id。

    `GattSession.device_id` 返回的是 `BluetoothDeviceId` **对象**，每次访问都会
    生成新的包装对象，`str()` 得到的 repr（`<BluetoothDeviceId object at …>`）
    每次都不同 —— 直接当 peer_id 会导致同一次连接的每次写都新建会话、且
    `subscribed_clients` 匹配永远失败（服务端发不出任何通知）。
    这里取其 `.id` 字符串（设备接口路径，按设备稳定）。
    """
    dev = getattr(session, "device_id", "")
    ident = getattr(dev, "id", None)
    if ident:
        return str(ident)
    return str(dev)


class GattServer:
    def __init__(self) -> None:
        self._provider: GattServiceProvider | None = None
        self._chars: dict[str, GattLocalCharacteristic] = {}
        self._sessions: dict[str, object] = {}
        self._last_activity: dict[str, float] = {}
        self._started_at = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._running = False
        self._last_revive: float = -1e9

        self.on_peer_ctrl: Callable[[str, bytes], None] | None = None
        self.on_peer_data: Callable[[str, bytes], None] | None = None
        self.on_peer_connected: Callable[[str], None] | None = None
        self.on_peer_disconnected: Callable[[str, str], None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def advertising_status(self) -> str:
        if not self._provider:
            return "NONE"
        try:
            return GattServiceProviderAdvertisementStatus(self._provider.advertisement_status).name
        except Exception:
            return "UNKNOWN"

    @property
    def advertising_ok(self) -> bool:
        """广播是否真的在跑（睡眠唤醒后 WinRT 会悄悄停掉）。

        注意：已经有对端连上来时，Windows 可能会把 `advertisement_status`
        变成 `ABORTED` —— 这是**正常**的。若仍按状态判断，watchdog 会每 5s
        `stop_advertising()` 一次把在线客户端踢掉（表现为对端 Unlikely Error
        / RO_E_CLOSED、server 端 UI 反复重建目标菜单）。

        但"有会话就算健康"也有反例（第八轮真机）：对端机器睡眠/崩溃后
        WinRT 可能留下一条**僵尸会话**，于是这里恒为真、watchdog 永远不去
        恢复广播 —— 对端唤醒后一直连不上，host 这边却一切正常。
        所以对端必须**最近说过话**（`SESSION_IDLE_TRUST` 内）才算数；
        太久没声音的会话当作僵尸，让 watchdog 去 revive。
        """
        if not self._running or self._provider is None:
            return False
        if self.advertising_status in (
            "STARTED",
            "STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA",
        ):
            return True
        live = [pid for pid in self._sessions if self.idle_seconds(pid) < SESSION_IDLE_TRUST]
        if live:
            return True
        if self._sessions:
            log.info(
                "advertising status=%s and every session idle > %.0fs -> needs revive (%s)",
                self.advertising_status,
                SESSION_IDLE_TRUST,
                {pid[:24]: round(self.idle_seconds(pid)) for pid in self._sessions},
            )
        return False

    def touch(self, peer_id: str) -> None:
        """记一次"这个对端刚说过话"（用于僵尸会话判定，见 `advertising_ok`）。"""
        if peer_id:
            self._last_activity[peer_id] = time.monotonic()

    def idle_seconds(self, peer_id: str) -> float:
        """这个对端静默了多久（没有记录的按"从 server 启动算起"）。"""
        return time.monotonic() - self._last_activity.get(peer_id, self._started_at)

    def stale_sessions(self, limit: float = SESSION_IDLE_TRUST) -> list[str]:
        return [pid for pid in self._sessions if self.idle_seconds(pid) >= limit]

    @property
    def peer_count(self) -> int:
        return len(self._sessions)

    @property
    def session_count(self) -> int:
        """当前记录的底层会话数（含尚未完成握手的），供诊断日志使用。"""
        return len(self._sessions)

    def has_peers(self) -> bool:
        return bool(self._sessions)

    async def revive(self, *, hard: bool = False) -> bool:
        """重新拉起广播。返回是否恢复成功。带 30s 冷却，避免反复踢对端。

        `hard=True`（本机睡眠唤醒后）走 `restart()`：把 provider 整段重建。
        光 `stop_advertising()` + `start_advertising()` 拉不回来 —— 唤醒后
        Windows 可能已经把服务的注册丢了，`advertisement_status` 会一直停在
        `ABORTED`，只有重建 provider 才能恢复。
        """
        if not self._running or self._provider is None:
            return False
        if self._sessions and not self.stale_sessions():
            # 真有对端在线：不要动广播，动了就是把人家踢下线
            return True
        if hard:
            return await self.restart(reason="hard revive")
        now = time.monotonic()
        if now - self._last_revive < REVIVE_COOLDOWN:
            return self.advertising_ok
        self._last_revive = now
        self.sweep_stale(reason="revive")
        try:
            self._provider.stop_advertising()
        except Exception:
            pass
        await asyncio.sleep(0.3)
        await self._advertise()
        return self.advertising_ok

    async def restart(self, *, reason: str) -> bool:
        """整段重建 GATT server（provider + 特征 + 广播）。

        用于两种"WinRT 对象已经不可信"的场合：本机睡眠唤醒、以及
        `advertisement_status` 卡在非 STARTED 又拉不回来。
        **只在没有活跃对端时调用**（调用方负责判断，见 `revive`）：
        重建意味着旧的特征对象全部作废，在线对端会立刻掉线。
        """
        log.warning("restarting gatt server (%s)", reason)
        self._running = False
        provider, self._provider = self._provider, None
        if provider is not None:
            try:
                provider.stop_advertising()
            except Exception:
                pass
        for peer_id in list(self._sessions):
            self.drop_peer(peer_id)
        self._sessions.clear()
        self._last_activity.clear()
        self._chars.clear()
        # 旧 provider 要能被回收（COM 引用释放 + WinRT 侧注销）才能建新的，
        # 立刻重建常常撞 "服务已被注册"。
        await asyncio.sleep(0.5)
        try:
            await self.start()
        except Exception as exc:
            log.error("gatt server restart failed (%s): %s", reason, exc)
            return False
        log.warning(
            "gatt server restarted (%s): advertising=%s", reason, self.advertising_status
        )
        return True

    def sweep_stale(self, *, reason: str) -> int:
        """清掉**没有活跃会话**的残留订阅与僵尸会话，返回清掉的条目数。

        为什么需要：对端软件直接下线/崩溃时，WinRT 可能既不回调
        `session_status_changed` 也不回调 `subscribed_clients_changed`，
        `_sessions`（以及 GATT server 的订阅表）里就会留着上一次的幽灵条目。
        之后新连接上来时，`subscribed_clients` 里混着幽灵 + 新条目，
        `_subscribed()` 可能挑中幽灵 ⇒ 通知发不出去 ⇒ dev 看到的
        「双断开后点加入网络，server 单方显示已连接、client 按钮灰」。

        只在**没有任何底层会话还活着**时动手（启动 / 恢复广播），
        因此不会误伤在线对端。

        注意：`GattLocalCharacteristic.subscribed_clients` 是 WinRT 内部维护的
        只读集合，**没有 API 可以主动移除订阅者** —— 这里能做的是把 Python 侧
        的记录清干净，并让 `_subscribed()` 优先挑活着的条目；真正的清理依赖
        WinRT 在链路断开后自己回收，以及两端的 `PING↔PONG` 探活兜底。
        """
        if any(self._session_alive(s) for s in self._sessions.values()):
            return 0
        removed = len(self._sessions)
        self._sessions.clear()
        self._last_activity.clear()
        for key in (CHAR_TX, CHAR_CTRL):
            char = self._chars.get(key)
            if char is None:
                continue
            try:
                subs = list(char.subscribed_clients or ())
            except Exception:
                continue
            if subs:
                removed += 1
                log.info(
                    "%s: %s still holds %s subscriber(s) with no live session",
                    reason,
                    key[:8],
                    len(subs),
                )
        if removed:
            log.info("%s: swept %s stale peer record(s)", reason, removed)
        return removed

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._started_at = time.monotonic()

        result = await GattServiceProvider.create_async(UUID_SERVICE)
        if result.error != 0:
            raise RuntimeError(f"GattServiceProvider 创建失败: {result.error}")
        provider = result.service_provider
        self._provider = provider
        service = provider.service

        for uuid_, props in (
            (UUID_RX, Props.WRITE | Props.WRITE_WITHOUT_RESPONSE),
            (UUID_TX, Props.INDICATE),
            (UUID_CTRL, Props.WRITE | Props.WRITE_WITHOUT_RESPONSE | Props.NOTIFY),
        ):
            params = GattLocalCharacteristicParameters()
            params.characteristic_properties = props
            params.read_protection_level = GattProtectionLevel.PLAIN
            params.write_protection_level = GattProtectionLevel.PLAIN
            cres = await service.create_characteristic_async(uuid_, params)
            if cres.error != 0:
                raise RuntimeError(f"特征 {uuid_} 创建失败: {cres.error}")
            self._chars[str(uuid_).lower()] = cres.characteristic

        self._chars[str(UUID_RX).lower()].add_write_requested(self._make_write_handler("rx"))
        self._chars[str(UUID_CTRL).lower()].add_write_requested(self._make_write_handler("ctrl"))
        for char in self._chars.values():
            char.add_subscribed_clients_changed(self._on_subscribed_changed)

        provider.add_advertisement_status_changed(self._on_adv_status)
        # 广播之前先把上一轮留下的幽灵会话/订阅清掉：本进程刚起来，不可能有
        # 在线对端，留着只会让第一条新连接撞上 `subscribed_clients` 里的死条目。
        self.sweep_stale(reason="server start")
        await self._advertise()
        self._running = True
        log.info("GATT server started, advertising=%s", self.advertising_status)

    async def _advertise(self) -> None:
        assert self._provider is not None
        adv = GattServiceProviderAdvertisingParameters()
        adv.is_connectable = True
        adv.is_discoverable = True
        for attempt in range(ADVERTISE_RETRY):
            try:
                self._provider.start_advertising_with_parameters(adv)
            except Exception as exc:
                log.warning("start_advertising attempt %s failed: %s", attempt, exc)
            for _ in range(int(ADVERTISE_WAIT / 0.05)):
                await asyncio.sleep(0.05)
                status = self._provider.advertisement_status
                if status == GattServiceProviderAdvertisementStatus.STARTED:
                    return
                if status == GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA:
                    return
            log.warning("advertising status=%s, retrying", self.advertising_status)
        log.error("advertising failed, status=%s", self.advertising_status)

    async def stop(self) -> None:
        self._running = False
        provider, self._provider = self._provider, None
        if provider:
            try:
                provider.stop_advertising()
            except Exception:
                pass
        for peer_id in list(self._sessions):
            self.drop_peer(peer_id)
        self._chars.clear()
        log.info("GATT server stopped")

    # ------------------------------------------------------------- events

    def _schedule(self, fn: Callable) -> None:
        loop = self._loop
        if loop is None:
            return
        if threading.get_ident() == self._loop_thread:
            fn()
        else:
            loop.call_soon_threadsafe(fn)

    def _make_write_handler(self, channel: str):
        def handler(sender, args) -> None:
            # deferral / session / request 必须在 WinRT 事件回调内同步取得：
            # 事件返回后再取会抛 E_ILLEGAL_METHOD_CALL (0x8000000E)。
            deferral = None
            try:
                deferral = args.get_deferral()
                session = args.session
                peer_id = session_peer_id(session)
                request_op = args.get_request_async()
            except Exception as exc:
                log.warning("write prepare failed (%s): %s", channel, exc)
                self._release_deferral(deferral)
                return

            async def handle() -> None:
                # 这次写请求是否已经被"应答"过。真机日志里的
                # `respond failed: [WinError -2147483618] The object has been committed.`
                # 就是**重复应答**：`request.value` 这个 getter 会隐式结束
                # deferral（WinRT 的 DataReader 语义），系统随即自动应答一次；
                # 之后我们再去 `respond()` 自然就撞 "already committed"。
                # 那不是错误，只是我们在多此一举 —— 记录状态、别再无脑重试。
                try:
                    request = await request_op
                    if request is None:
                        log.warning("write request empty (%s)", channel)
                        return
                    try:
                        request.respond()  # 先应答，避免解析失败时客户端一直等
                    except Exception as exc:
                        log.debug(
                            "respond skipped (%s, 系统应已自动应答): %s", channel, exc
                        )
                    data = from_buffer(request.value)
                    self._track_session(session, peer_id)
                    self.touch(peer_id)

                    # === 调试：打印手机写过来的原始数据 ===
                    _dump_recv(channel, peer_id, data)

                    cb = self.on_peer_data if channel == "rx" else self.on_peer_ctrl
                    if cb:
                        cb(peer_id, data)
                    # log.debug(
                    #     "recv %s from %s: %d bytes (responded=%s)",
                    #     channel,
                    #     peer_id,
                    #     len(data),
                    #     responded,
                    # )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("write handling failed (%s): %s", channel, exc)
                finally:
                    self._release_deferral(deferral)

            try:
                self._schedule(lambda: asyncio.ensure_future(handle()))
            except Exception as exc:
                log.warning("schedule write failed (%s): %s", channel, exc)
                self._release_deferral(deferral)

        return handler

    @staticmethod
    def _release_deferral(deferral) -> None:
        if deferral is None:
            return
        try:
            deferral.complete()
        except Exception as exc:
            log.debug("deferral complete failed: %s", exc)

    @staticmethod
    def _session_alive(session) -> bool:
        """底层会话是否还活着（对象已被关掉时读属性会抛 `RO_E_CLOSED`）。"""
        try:
            return int(session.session_status) != 0  # GattSessionStatus.CLOSED == 0
        except Exception:
            return False

    def _track_session(self, session, peer_id: str) -> None:
        self.touch(peer_id)
        existing = self._sessions.get(peer_id)
        if existing is session:
            return
        if existing is not None and self._session_alive(existing):
            # WinRT 每次读 `args.session` / `sub.session` 都可能返回**新的包装对象**，
            # 底层却是同一条会话。按对象身份判断会让**每一帧**都走到「换绑」分支，
            # 于是每帧 `log.info` 同步写盘 + 每帧 `add_session_status_changed`
            # （token 丢弃、从不反注册）。一次 4MB 传输 ≈ 8500 帧 ⇒ 8500 条日志 +
            # 8500 个泄漏回调，把 GUI 线程拖死 —— dev 反馈的「server 端 UI 卡死、
            # 无报错、对端 write response 超时」就是这么来的。
            # 底层会话还活着 ⇒ 只是换了包装，换引用即可，别动事件、别打日志。
            self._sessions[peer_id] = session
            return
        if existing is not None:
            # 真的是换了一条 GattSession（对端重连）：换绑并重新挂钩。
            log.info("peer %s rebound to new gatt session", peer_id)
            self._sessions.pop(peer_id, None)
        self._sessions[peer_id] = session

        def on_status(s, a) -> None:
            def fire() -> None:
                # 事件参数是 GattSessionStatusChangedEventArgs，只有 `status`/`error`
                # 两个属性（`session_status` 是 GattSession 的属性，事件上没有）。
                # 且 CLOSED=0 / ACTIVE=1，所以「关闭」是 == 0。
                try:
                    closed = int(a.status) == 0
                except Exception:
                    closed = True
                if not closed:
                    if self.on_peer_connected:
                        self.on_peer_connected(peer_id)
                    return
                # 身份判断不能比对象（包装每次都新），改成「现在存的这条是否还活着」：
                # 还活着 ⇒ 对端早已重连、这条是旧会话迟到的事件，忽略。
                cur = self._sessions.get(peer_id)
                if cur is not None and self._session_alive(cur):
                    return
                self._sessions.pop(peer_id, None)
                log.info("peer %s disconnected", peer_id)
                if self.on_peer_disconnected:
                    self.on_peer_disconnected(peer_id, "link lost")

            self._schedule(fire)

        try:
            session.add_session_status_changed(on_status)
        except Exception as exc:
            log.debug("session status hook failed: %s", exc)
        if self.on_peer_connected:
            self.on_peer_connected(peer_id)

    def _on_subscribed_changed(self, sender, args) -> None:
        def fire() -> None:
            uuid_str = str(sender.uuid).lower()
            log.debug("subscribed %s -> %d", uuid_str, len(sender.subscribed_clients))
            # 订阅集合按 **TX + CTRL 两条 notify 通道取并集**（握手走 CTRL、数据走 TX）。
            # 以前这里只"加"不"减"：对端软件下线/重启后 `_sessions` 里留下一条僵尸会话，
            # keepalive PING 要等 20s 才发现 `not subscribed` —— 这 20s 里双方状态不一致，
            # 界面上就是 dev 报的"掉线了却还报一堆 `UNKNOWN：peer … not subscribed …`"。
            sessions: dict[str, object] = {}
            duplicates: set[str] = set()
            for key in (CHAR_TX, CHAR_CTRL):
                char = self._chars.get(key)
                if char is None:
                    continue
                try:
                    subs = list(char.subscribed_clients or ())
                except Exception as exc:
                    log.debug("subscribed_clients unavailable on %s: %s", key[:8], exc)
                    continue
                for sub in subs:
                    try:
                        peer = session_peer_id(sub.session)
                    except Exception as exc:
                        log.debug("skip subscription: %s", exc)
                        continue
                    if peer in sessions:
                        # 同一个 peer 出现两条订阅：WinRT 的订阅表在"对端下线又
                        # 重新连上"之后确实会留下幽灵条目。这是排查"单方已连接"
                        # 的第一手证据，所以留一条 DEBUG。
                        duplicates.add(peer)
                    else:
                        sessions[peer] = sub.session
            if duplicates:
                log.debug(
                    "duplicate subscriptions for %s on %s+%s",
                    sorted(duplicates),
                    CHAR_TX[:8],
                    CHAR_CTRL[:8],
                )
            for peer_id, session in sessions.items():
                self._track_session(session, peer_id)
            if uuid_str not in (CHAR_TX, CHAR_CTRL):
                return  # RX 是写通道，没有 notify 订阅，不参与掉线判定
            for peer_id in list(self._sessions):
                if peer_id in sessions:
                    continue
                if not self._session_alive(self._sessions[peer_id]):
                    self._sessions.pop(peer_id, None)  # 底层已关，`on_status` 可能报过
                    continue
                self._sessions.pop(peer_id, None)
                log.info("peer %s unsubscribed from all notify channels", peer_id)
                if self.on_peer_disconnected:
                    self.on_peer_disconnected(peer_id, "unsubscribed")

        self._schedule(fire)

    def _on_adv_status(self, sender, args) -> None:
        def fire() -> None:
            try:
                status = GattServiceProviderAdvertisementStatus(args.status).name
            except Exception:
                status = str(args.status)
            log.info("advertising status -> %s", status)

        self._schedule(fire)

    # ------------------------------------------------------------- outbound

    def _subscribed(self, char_uuid: str, peer_id: str):
        """找出该对端在这条通道上的订阅，**优先挑底层会话还活着的**。

        对端软件直接下线时，`subscribed_clients` 里常常还留着幽灵条目，
        同时新连接又登记了一条 —— 老代码取"第一个匹配"，可能正好挑中幽灵，
        于是 `notify_value_for_subscribed_client_async` 静默失败/抛 PeerGone，
        上层看到的就是「单方已连接」。
        """
        char = self._chars.get(char_uuid)
        if not char:
            raise PeerGone("no characteristic")
        fallback = None
        for sub in char.subscribed_clients:
            if session_peer_id(sub.session) != peer_id:
                continue
            if self._session_alive(sub.session):
                return char, sub
            if fallback is None:
                fallback = sub
        if fallback is not None:
            log.debug("subscribed(%s): only a dead session for %s", char_uuid[:8], peer_id)
            return char, fallback
        raise PeerGone(f"peer {peer_id} not subscribed to {char_uuid}")

    async def send_ctrl(self, peer_id: str, data: bytes) -> None:
        char, sub = self._subscribed(CHAR_CTRL, peer_id)
        self._check_notify_size(sub, data, peer_id, "ctrl")
        try:
            await char.notify_value_for_subscribed_client_async(to_buffer(data), sub)
        except OSError as exc:
            if is_link_dead(exc):
                raise PeerGone(f"peer {peer_id} session closed") from exc
            raise

    async def send_data(self, peer_id: str, data: bytes) -> None:
        char, sub = self._subscribed(CHAR_TX, peer_id)
        self._check_notify_size(sub, data, peer_id, "data")
        try:
            await char.notify_value_for_subscribed_client_async(to_buffer(data), sub)
        except OSError as exc:
            if is_link_dead(exc):
                raise PeerGone(f"peer {peer_id} session closed") from exc
            raise

    @staticmethod
    def _check_notify_size(sub, data: bytes, peer_id: str, channel: str) -> None:
        try:
            limit = int(sub.max_notification_size)
        except Exception:
            return
        if limit and len(data) > limit:
            log.warning(
                "%s frame %d bytes exceeds max_notification_size %d (peer=%s)",
                channel,
                len(data),
                limit,
                peer_id,
            )

    def peer_mtu(self, peer_id: str) -> int:
        for char in self._chars.values():
            for sub in char.subscribed_clients:
                if session_peer_id(sub.session) == peer_id:
                    try:
                        return max(23, int(sub.session.max_pdu_size))
                    except Exception:
                        return 23
        return 23

    def peer_chunk(self, peer_id: str) -> int:
        mtu = self.peer_mtu(peer_id)
        for char in self._chars.values():
            for sub in char.subscribed_clients:
                if session_peer_id(sub.session) == peer_id:
                    try:
                        size = int(sub.max_notification_size)
                    except Exception:
                        size = 0
                    if size <= 0:
                        size = max(23, mtu - 3)
                    return max(8, min(size, mtu) - 3 - DATA_HEADER)
        return 8

    def list_peers(self) -> list[str]:
        return list(self._sessions)

    def drop_peer(self, peer_id: str) -> None:
        session = self._sessions.pop(peer_id, None)
        if session is None:
            return
        try:
            session.close()
        except Exception as exc:
            log.debug("close session %s failed: %s", peer_id, exc)


def _fmt_addr(value: int) -> str:
    if not value:
        return ""
    return ":".join(f"{(value >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))


async def adapter_address() -> str:
    """本机蓝牙 MAC（用于直连/展示）。"""
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter

        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            return ""
        return _fmt_addr(int(adapter.bluetooth_address))
    except Exception as exc:
        log.debug("adapter address failed: %s", exc)
        return ""


__all__ = [
    "GattServer",
    "PeerGone",
    "is_closed_error",
    "is_link_dead",
    "to_buffer",
    "from_buffer",
    "adapter_address",
    "session_peer_id",
]
