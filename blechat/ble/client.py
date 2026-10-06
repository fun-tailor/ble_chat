"""Client 侧传输：bleak (WinRT 后端)。

真机反复出现的失败几乎都出在 **Windows 这一侧的设备身份**上：

1. `BleakDeviceNotFoundError` / `TimeoutError` —— WinRT 只认「Windows 蓝牙设备表」
   里的地址，而设备表只有**扫描过**才刷新；
2. **对端地址本身会变** —— 系统为目标设备分配/轮换对外地址，真机日志里同一个
   Host 先后是 `6A:15:F4:A1:41:CB`、`48:0A:97:77:32:3F`、`57:69:77:64:10:35`。
   `networks.json` 里存的是上一次那个 ⇒ "按地址连接"恒失败 ⇒ **重启后几乎连不上**。
   唯一稳定的身份是**名字**，所以查不到地址时要按名字 + 服务 UUID 再扫一遍（`resolve_peer`）；
3. `OSError: [WinError -2147418113] 灾难性故障 (E_FAIL)` —— 上一次半连接的
   `BleakClient` 没被 dispose，WinRT 的 requester/GATT 会话还挂在栈上。

因此这里的规矩是：

* 每次连接前 `ensure_device()` 解析出**当前有效**的设备；解析不到就返回
  `"missing"`，让上层**跳过这一轮**（拿旧地址硬连只会白等一个 12s 超时）；
* 只用**一次** `BleakScanner.discover()` 做解析（不再叠加
  `find_device_by_address` 那层自己的 watcher）；
* 连上又失败的 `BleakClient` **一律 dispose**（`close()` 关的就是它）；
* 连续失败到一定次数做一次「端口级自愈」（丢设备对象 + 强制 GC）。

**睡眠/唤醒是另一类问题**（第八轮真机日志）：

```
08:22:07 gatt table incomplete (attempt 1/4, streak=1)   ← 距上一条日志只有 2s
08:22:12 gatt table incomplete (attempt 2/4, streak=2)   ← 丢句柄 + gc 也没用
08:25:59 connected ... in 4.84s; gatt table = 1800[…] 1801[…] 180a[…] a000[…]  ← 重启进程立刻好
```

判据是**耗时**：真连一次要 ~5s（扫描 + 建链 + 现场发现服务），而唤醒后的失败
只用 1~2s 就返回了 ⇒ WinRT 根本没上空中接口，走的是**缓存路径**（进程里那条
`GattSession` 还挂着 `maintain_connection=True`，Windows 认为链路还在）。
进程重启能修好 ⇒ 脏状态在栈这一侧，只有"明确放弃这条链路"才能清掉它：

* `mark_resume()` 标记刚睡醒；
* `connect()` 第一件事 `_post_resume_reset()`：等 radio 回到 ON →
  `release_gatt_session()`（`maintain_connection=False` + 关会话 + 关设备）→ 静置几秒；
* 连接**快得不像真连过**（< `PHANTOM_CONNECT_SECONDS`）且表里没有本服务 ⇒
  立即升级为 `hard_reset()`，不再傻等 `TABLE_RESET_AFTER` 次。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakError

from .base import Transport
from .radio import release_gatt_session, wait_radio_ready
from .uuid_defs import CHAR_CTRL, CHAR_RX, CHAR_TX, SERVICE_UUID

log = logging.getLogger("blechat.ble.client")

# GATT 表不完整时的重试节奏（**只**针对"连上了但特征表是旧的"这一种失败）
TABLE_RETRIES = 4
TABLE_BACKOFF = (1.0, 2.0, 3.0)
# 连续几轮"GATT 表不完整"就开始丢句柄（睡眠唤醒后 Windows 的 GATT 缓存是旧的，
# 普通重试多少次都拿不到特征，必须换一条新链路）
TABLE_RESET_AFTER = 2
TABLE_RESET_BACKOFF = 2.5
# 一次**真**连接（扫描 + 建链 + 现场发现服务）在这台机器上要 ~5s（真机日志
# `connected ... in 4.84s`）。唤醒之后如果 `connect()` 只用不到这个时间就回来了，
# 那它根本没上空中接口 —— WinRT 走的是进程里那条缓存链路。判据只用于**升级处理**，
# 不参与成败判定（表的完整性才是成败判据）。
PHANTOM_CONNECT_SECONDS = 2.5
# 命中"缓存链路"时的退避：比普通重试长一点，让 Windows 有时间把旧链路拆掉
PHANTOM_BACKOFF = 3.0
# 唤醒后第一件事：放弃旧链路 → 静置几秒再连。radio 回来、栈清干净都需要时间。
RESUME_SETTLE_SECONDS = 3.0

# 每次连接前重新解析设备对象的扫描超时。只是刷 Windows 设备表里已知地址的
# 属性，不需要真的等一次完整 discovery，所以比用户点"加入网络…"的 6s 短。
DEVICE_SCAN_TIMEOUT = 3.0
# 地址查不到时的兜底扫描预算。**地址会变**：Windows 会为目标设备轮换
# 隐私地址（`6A:15:F4:…` → `48:0A:97:…` → `57:69:77:…` 这种），
# 而 `networks.json` 里记的是上一次那个。这时只有靠**名字 / 服务 UUID**
# 才能重新找到它 —— 这一路要等对端广播，所以预算给足。
FALLBACK_SCAN_TIMEOUT = 8.0
# 连续失败多少次之后做一次端口级自愈
PORT_RESET_AFTER = 3
# 连上之后启动两条 notify 的时限（`start_notify` 本身不受 connect 的 timeout 约束）
NOTIFY_SETUP_TIMEOUT = 8.0
# 手上这个设备对象"还算新鲜"的时限：30s 内不重复扫描
DEVICE_FRESH_SECONDS = 30.0


def scan_candidates(
    found: dict, address: str, name: str = "", service_uuid: str = SERVICE_UUID
) -> tuple[list[tuple[str, str]], list[str]]:
    """从一次扫描结果里挑出候选设备，返回 `(候选列表, 用到过的策略名)`。

    候选按**确定性从高到低**排：
    1. `address+service` —— 地址还在、也确实是我们那个服务；
    2. `address` —— 地址对上（广播里没有服务 UUID 也认，地址本身就是强判据）；
    3. `name+service` —— **地址变了**，靠名字 + 服务找回；
    4. `name` —— 名字对上但广播里没有服务 UUID（被动扫描/缓存）；
    5. `service` —— 只剩服务 UUID（可能是同机的另一个实例，最弱）。

    只有一条规则都不命中时才返回空 —— 那就说明对端真的不在范围里。

    返回的第二个值是**胜出那一桶**用到的策略名（不是"所有出现过的策略"），
    这样日志里 `strategies=['address+service']` 就是最终生效的那条判据。
    """
    wanted_name = (name or "").strip()
    addr_l = (address or "").lower()
    buckets: dict[str, list[tuple[str, str]]] = {
        "address+service": [],
        "address": [],
        "name+service": [],
        "name": [],
        "service": [],
    }
    seen: set[str] = set()

    def keep(label: str, addr: str, local: str) -> None:
        if addr in seen:
            return  # 同一台设备只进一个桶，避免候选重复
        seen.add(addr)
        buckets[label].append((addr, local))

    for addr, (device, adv) in (found or {}).items():
        uuids = [str(u).lower() for u in (getattr(adv, "service_uuids", None) or ())]
        has_service = service_uuid in uuids
        local = (adv.local_name or device.name or "").strip()
        same_addr = bool(addr_l) and addr.lower() == addr_l
        same_name = bool(wanted_name) and local == wanted_name
        if same_addr:
            keep("address+service" if has_service else "address", addr, local)
        elif same_name:
            keep("name+service" if has_service else "name", addr, local)
        elif has_service:
            keep("service", addr, local)

    for label in ("address+service", "address", "name+service", "name", "service"):
        if buckets[label]:
            return buckets[label], [label]
    return [], []


async def scan_devices(timeout: float) -> dict:
    """一次全量扫描，返回 `{address: (BLEDevice, AdvertisementData)}`。

    扫描失败（定位权限被拒等）返回 `{}` —— 调用方据此决定是否退回按地址直连。
    """
    try:
        return await BleakScanner.discover(timeout=timeout, return_adv=True) or {}
    except Exception as exc:
        log.debug("discover(%.0fs) failed: %s", timeout, exc)
        return {}


async def resolve_peer(
    address: str,
    name: str = "",
    *,
    address_timeout: float = DEVICE_SCAN_TIMEOUT,
    fallback_timeout: float = FALLBACK_SCAN_TIMEOUT,
) -> tuple[BLEDevice | None, str]:
    """连接前把对端解析成一个**当前有效**的 `BLEDevice`。

    这是修「重启后很难连上」的核心：

    * WinRT 只认「Windows 蓝牙设备表」里的地址，而设备表只有**扫描过**才刷新；
    * 更麻烦的是**地址本身会变** —— 目标设备的对外地址由系统分配，
      重装/重启/隐私策略都可能让它换一个（真机日志：同一个 Host 先后是
      `6A:15:F4:A1:41:CB`、`48:0A:97:77:32:3F`）。`networks.json` 里存的是
      上一次那个，于是"按地址连接"恒失败，表现就是**重启后几乎连不上**。

    所以这里分两步：先按地址查（快，3s），查不到再按**名字 + 服务 UUID**
    扫描一遍（慢，8s，但能在地址变了之后重新找回对端）。

    返回 `(device, source)`；`device` 为 None 表示没找到（调用方**不该**再拿
    旧地址硬连 —— 那只会白等一个超时）。
    """
    device, source = await resolve_device_info(address, name, timeout=address_timeout)
    if device is not None:
        return device, source

    found = await scan_devices(fallback_timeout)
    candidates, strategies = scan_candidates(found, address, name)
    if not candidates:
        log.warning(
            "peer %s (%r) not found in a %.0fs scan (%s device(s) seen, strategies=%s)",
            address,
            name,
            fallback_timeout,
            len(found),
            strategies or ["none"],
        )
        return None, "missing"

    picked, local_name = candidates[0]
    if len(candidates) > 1:
        log.info(
            "peer %r matched %s candidates %s; taking %s",
            name,
            len(candidates),
            [c[0] for c in candidates],
            picked,
        )
    log.warning(
        "peer address changed: %s -> %s (name=%r strategies=%s)",
        address,
        picked,
        local_name or name,
        strategies,
    )
    return found[picked][0], strategies[0] if strategies else "scan"


async def resolve_device(address: str, name: str = "", timeout: float = DEVICE_SCAN_TIMEOUT):
    """只按地址解析一次（兼容旧签名）。返回 `BLEDevice | None`。"""
    device, _source = await resolve_device_info(address, name, timeout)
    return device


async def resolve_device_info(
    address: str, name: str = "", timeout: float = DEVICE_SCAN_TIMEOUT
) -> tuple[BLEDevice | None, str]:
    """按地址解析设备。返回 `(device|None, "scan"|"direct")`。

    只做**一次全量扫描**并按地址过滤，不调用
    `BleakScanner.find_device_by_address` —— 后者会自己 `async with BleakScanner(...)`
    起一个独立的 watcher，和调用方（以及 `BleakClient.connect`）的扫描**互相叠加**，
    真机上表现为"每次连接前先白等一个 3s 超时"。

    **找不到就返回 None**（以前返回 `direct_device(...)` 空壳）：拿一个没有
    `details` 的地址去连，WinRT 只会一直超时 —— 不如把"没找到"交给上层，
    让它去按名字兜底、或者干脆跳过这一轮，别浪费一个 12s 的连接超时。
    """
    found = await scan_devices(timeout)
    if not found:
        return None, "direct"
    device, source = pick_device(found, address, name)
    if device is None:
        return None, "direct"
    return device, source


def pick_device(found: dict, address: str, name: str = "") -> tuple[BLEDevice | None, str]:
    """从扫描结果里按地址（其次按名字）挑一个设备。"""
    addr_l = (address or "").lower()
    if addr_l:
        for addr, (device, adv) in found.items():
            if addr.lower() != addr_l:
                continue
            resolved = adv.local_name or device.name or name or address
            if not device.name:
                device.name = resolved
            # 广播里的服务 UUID 是**判据**：它还在 ⇒ 对端软件活着，问题在本机这条链路；
            # 它没了 ⇒ 对端软件没在广播本服务（那是 server 侧的问题）。排查唤醒后
            # 连不上时，这一行是整个日志里最值钱的信息。
            uuids = [str(u).lower() for u in (getattr(adv, "service_uuids", None) or ())]
            log.debug(
                "device resolved %s -> name=%r source=scan rssi=%s services=%s",
                address,
                resolved,
                getattr(adv, "rssi", None),
                uuids or ["<none>"],
            )
            return device, "scan"
    # 地址不在结果里：至少把名字对上（调用方可能还需要更长预算来找地址）
    if name:
        for addr, (device, adv) in found.items():
            if (adv.local_name or device.name or "").strip() == name.strip():
                log.debug("device resolved by name %r -> %s", name, addr)
                return device, "scan"
    return None, "direct"


class GattTableIncomplete(BleakError):
    """连上了，但对端没有本服务/特征 —— 通常是 Windows 手里的 GATT 缓存是旧表。

    Host 重启、或本机睡眠唤醒之后最常见：
    `connect()` 成功、`services_ok()` 也过（服务 UUID 还在），
    `start_notify(0000a002)` 却报 `Characteristic … was not found!`。
    """


async def release_link(address: str) -> str:
    """`release_gatt_session` 的兜底包装：**绝不抛异常**，返回一句描述。

    连接循环里这个动作属于"尽力而为的清理"，它失败不该影响重连。
    """
    try:
        return await release_gatt_session(address)
    except Exception as exc:  # pragma: no cover - 只可能是 winrt 层面的意外
        return f"failed: {exc}"


# --------------------------------------------------------------- 连接互斥
# 同一个对端**同一时刻只允许一条链路在建**。
#
# 为什么需要：换连接任务时（用户点"加入网络…"、watchdog 强制重启）旧任务只是
# `cancel()`，而 WinRT 调用不响应取消 —— `_drop_client_task` 等 10s 后就"放弃"
# 它，可它还在 `client.connect()` 里。两个 connect 撞在同一个对端上，两边都拿
# 不到完整服务表，用户看到的就是**"点了加入网络也没反应"**。
#
# 用"轮询 + 时间戳"而不是 `asyncio.Lock`：锁会把事件循环绑死（`asyncio.Lock`
# 一旦用过就拒绝在另一个 loop 里复用），而这里既要在 qasync 的循环里跑，
# 也要能在测试里被 `asyncio.run` 反复用。等到超时就放行 —— 宁可撞，也别卡死。
CONNECT_SLOT_WAIT = 20.0
# 占用超过这个时间视为持有者已经僵死，直接抢过来
CONNECT_SLOT_STALE = 90.0
_connect_slots: dict[str, tuple[object, float]] = {}


async def acquire_connect_slot(
    address: str,
    token: object | None = None,
    *,
    wait: float = CONNECT_SLOT_WAIT,
    stale: float = CONNECT_SLOT_STALE,
) -> object:
    """拿到 `address` 的连接名额（可重入：同一个 token 直接过）。"""
    token = object() if token is None else token
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        now = time.monotonic()
        holder = _connect_slots.get(address)
        if holder is None or holder[0] is token or now - holder[1] > stale:
            _connect_slots[address] = (token, now)
            return token
        if now >= deadline:
            log.warning(
                "connect slot for %s still held after %.0fs (another attempt is stuck);"
                " proceeding anyway",
                address,
                wait,
            )
            _connect_slots[address] = (token, now)
            return token
        await asyncio.sleep(0.2)


def release_connect_slot(address: str, token: object) -> None:
    """释放名额（只释放自己的，别人抢走了就别动）。"""
    holder = _connect_slots.get(address)
    if holder is not None and holder[0] is token:
        _connect_slots.pop(address, None)


@dataclass
class FoundDevice:
    address: str
    name: str
    service_uuids: list[str] = field(default_factory=list)

    @property
    def match_service(self) -> bool:
        return any(u.lower() == SERVICE_UUID for u in self.service_uuids)


async def scan(timeout: float = 6.0) -> list[FoundDevice]:
    """扫描附近广播了本服务的设备；也返回名字匹配的设备。"""
    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as exc:
        log.warning("scan failed: %s", exc)
        return []
    out: list[FoundDevice] = []
    for address, (_dev, adv) in found.items():
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        out.append(FoundDevice(address, adv.local_name or _dev.name or "", uuids))
    out.sort(key=lambda d: (not d.match_service, not bool(d.name), d.name))
    return out


def direct_device(address: str, name: str = "") -> BLEDevice:
    """按地址直连（绕过扫描）。`details=None` 表示"我们没有它的广播数据"，
    WinRT 后端会自己按地址 `BluetoothLEDevice.from_bluetooth_address_async`。"""
    return BLEDevice(address, name or address, None)


class BleakTransport(Transport):
    def __init__(
        self,
        device: BLEDevice,
        *,
        on_ctrl: Callable[[bytes], None] | None = None,
        on_data: Callable[[bytes], None] | None = None,
        on_closed: Callable[[str], None] | None = None,
    ) -> None:
        self.device = device
        self.peer_id = device.address
        self.peer_name = device.name or device.address
        self.on_ctrl = on_ctrl
        self.on_data = on_data
        self.on_closed = on_closed
        self.mtu = 23
        self._client: BleakClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._write_no_resp = True
        self._closed = False
        self._device_resolved_at = 0.0
        # 连接失败统计 / 端口自愈计数（诊断用，见 `state_snapshot()`）
        self.fail_streak = 0
        self.port_resets = 0
        self.table_fails = 0
        self.phantom_connects = 0
        self.resumes = 0
        self.last_device_source = "direct"
        self._resume_pending = False

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def ensure_device(
        self,
        *,
        name: str = "",
        force: bool = False,
        address_timeout: float = DEVICE_SCAN_TIMEOUT,
        fallback_timeout: float = FALLBACK_SCAN_TIMEOUT,
    ) -> str:
        """连之前调一次：解析出**当前有效**的设备对象，返回来源标记。

        来源标记会被上层写进日志（`device_source=`），也决定 `peer_id` 是否
        需要更新（地址变了 → 历史/网络的地址也得跟着变）。

        返回 `"missing"` 表示**没找到对端**：此时 `self.device` 保持原样，
        调用方应当**跳过连接**（拿旧地址硬连只会白等一个超时）。
        """
        if not force and self.last_device_source == "scan" and self._device_is_fresh():
            return self.last_device_source
        device, source = await resolve_peer(
            self.device.address,
            name or self.peer_name,
            address_timeout=address_timeout,
            fallback_timeout=fallback_timeout,
        )
        self.last_device_source = source
        if device is None:
            return source
        self.set_device(device)
        return source

    def _device_is_fresh(self) -> bool:
        """手上的设备对象是不是刚刚才解析出来的。"""
        return (time.monotonic() - self._device_resolved_at) < DEVICE_FRESH_SECONDS

    def set_device(self, device: BLEDevice) -> None:
        """换用一个新的设备对象。

        `peer_id` 是历史与 `network_id` 的分区键，**地址变了也必须跟着变**，
        否则新地址上收到的消息会写进另一个网络。地址变化由 `address_changed` 报出。
        """
        old = self.device.address
        self.device = device
        self._device_resolved_at = time.monotonic()
        if device.name:
            self.peer_name = device.name
        if device.address and device.address != old:
            log.warning("peer address updated: %s -> %s", old, device.address)
            self.peer_id = device.address

    @property
    def address_changed(self) -> bool:
        return bool(self.device.address) and self.device.address != self.peer_id

    def mark_resume(self, *, reason: str = "system resume") -> None:
        """系统刚睡醒：下一次 `connect()` 先做一次栈重置。

        只打个标记，真正动作在 `_post_resume_reset()` 里 —— 那里有 `await`，
        而本方法是给 Qt 定时器（同步上下文）调的。
        """
        self.resumes += 1
        self._resume_pending = True
        # 手上的设备对象是睡眠前解析的，广播数据、地址归属都可能已经变了：
        # 退回空壳并标成不新鲜，逼下一次 `ensure_device()` 重新扫描。
        self.set_device(direct_device(self.device.address, self.device.name or self.peer_name))
        self._device_resolved_at = 0.0
        self.last_device_source = ""
        log.warning(
            "transport marked for post-resume reset #%s (%s): device object dropped,"
            " next connect releases the winrt session first",
            self.resumes,
            reason,
        )

    async def _post_resume_reset(self) -> None:
        """唤醒后的第一件事：等 radio 回来 + 明确放弃上一轮那条链路。

        真机日志里唤醒后连续 4 次 `gatt table incomplete`、每次只用 1~2s，
        而重启进程立刻正常 —— 脏状态在栈里，不主动放弃就永远拿不到新表。
        """
        self._resume_pending = False
        address = self.device.address
        ready, state = await wait_radio_ready()
        detail = await release_link(address)
        log.warning(
            "post-resume reset: radio=%s (%s) winrt release -> %s; settle %.1fs",
            state,
            "ready" if ready else "NOT READY",
            detail,
            RESUME_SETTLE_SECONDS,
        )
        await asyncio.sleep(RESUME_SETTLE_SECONDS)

    async def hard_reset(self, *, cause: str) -> None:
        """丢句柄 + **明确放弃这条链路**。睡眠唤醒后唯一真正有效的动作。

        `_reset_port()` 只是把 Python 侧的引用丢掉；这里额外让 WinRT 把
        `GattSession.maintain_connection` 置回 False 再关会话/设备 ——
        否则 Windows 认为"还有人在维持这条链路"，新的连接继续走缓存路径。
        """
        detail = await release_link(self.device.address)
        self._reset_port(cause=cause)
        log.warning("hard reset (%s): winrt release -> %s", cause, detail)

    async def connect(self, timeout: float = 12.0) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        # 本次 connect 之前先把"屏蔽断开回调"的闸门复位。`close()` 会把它置真，
        # 而 `BleakTransport` 现在**跨重连被复用**（见 `app._client_loop`），
        # 忘了复位的话：上一条链路关闭 → 复用同一个 transport → 新链路真掉线时
        # `handle_disconnect` 直接 return，上层永远收不到"断开"通知。
        self._closed = False
        loop = self._loop
        assert loop is not None

        def dispatch(fn, *args):
            def run():
                fn(*args)

            if threading.get_ident() == self._loop_thread:
                run()
            else:
                loop.call_soon_threadsafe(run)

        def handle_notify(char, data):
            cb = self.on_data if str(char.uuid).lower() == CHAR_TX else self.on_ctrl
            if cb:
                dispatch(cb, bytes(data))

        def handle_disconnect(_client=None):
            if self._closed:
                return
            self._closed = True
            if self.on_closed:
                dispatch(self.on_closed, "disconnected")

        # 睡眠唤醒后的第一件事：radio 回来 + 放弃上一轮那条缓存链路。
        # 放在重试循环**外面**：这是"本轮连接之前的一次性清理"。
        if self._resume_pending:
            await self._post_resume_reset()

        last: Exception | None = None
        for attempt in range(1, TABLE_RETRIES + 1):
            if attempt > 1:
                # 表不完整 ⇒ 重新解析一次设备，逼 WinRT 走一次现场发现
                await self.ensure_device(force=True)
            started = time.monotonic()
            client = BleakClient(
                self.device,
                disconnected_callback=handle_disconnect,
                timeout=timeout,
                # 服务/特征表默认走 Windows 的 GATT **缓存**：Host 重启后缓存可能还是
                # 上一次的旧表，于是 `services_ok()` 通过、`start_notify` 却报
                # `BleakCharacteristicNotFoundError: 0000a002-... was not found!`。
                # UNCACHED 强制现场重新发现。
                winrt={"use_cached_services": False},
            )
            # 先登记再连：`connect()` 自己失败时也走了半条路（WinRT requester 已建），
            # 以前 `self._client` 还是 None ⇒ `close()` 什么都关不掉 ⇒ 这个
            # `BleakClient` 没人 dispose ⇒ 下一次连接撞 `灾难性故障(E_FAIL)`。
            self._client = client
            try:
                # 一次连接尝试的总时限由我们**自己**兜底。`BleakClient.connect`
                # 内部只在 `async with asyncio.timeout(...)` 里做事，而它之前还有
                # `BluetoothLEDevice.from_bluetooth_address_async` —— WinRT 在设备
                # 不在设备表里时会把它挂很久，真机日志里就是
                # `TimeoutError: (空文本) <- CancelledError` 这种"19.5s 白等"。
                # 套一层硬时限，超了就走重试/退避，别把这一轮拖死。
                await asyncio.wait_for(client.connect(timeout=timeout), timeout=timeout + 4.0)
                self.mtu = int(client.mtu_size or 23)
                if not self._has_service(client):
                    raise GattTableIncomplete("GATT 表里没有 BLE Chat 服务（缓存是旧的？）")
                await asyncio.wait_for(
                    client.start_notify(CHAR_TX, handle_notify), timeout=NOTIFY_SETUP_TIMEOUT
                )
                await asyncio.wait_for(
                    client.start_notify(CHAR_CTRL, handle_notify), timeout=NOTIFY_SETUP_TIMEOUT
                )
                self.fail_streak = 0
                self.table_fails = 0
                log.info(
                    "connected to %s mtu=%s attempts=%s source=%s in %.2fs",
                    self.peer_id,
                    self.mtu,
                    attempt,
                    self.last_device_source,
                    time.monotonic() - started,
                )
                return
            except (BleakCharacteristicNotFoundError, GattTableIncomplete) as exc:
                last = exc
                self.table_fails += 1
                elapsed = time.monotonic() - started
                # 三个信息缺一不可：**耗时**（判断是不是走了缓存路径）、
                # **表里到底有什么**（空表 / 只有系统服务 / 有本服务但没特征），
                # **广播里有没有本服务**（区分本机链路问题与对端软件问题）。
                table = self.service_summary()
                log.warning(
                    "gatt table incomplete (attempt %s/%s, streak=%s, elapsed=%.2fs,"
                    " table=%s): %s",
                    attempt,
                    TABLE_RETRIES,
                    self.table_fails,
                    elapsed,
                    table,
                    exc,
                )
                await self.close()
                if elapsed < PHANTOM_CONNECT_SECONDS:
                    # 真连一次要 ~5s。这么快就回来 ⇒ 没上空中接口，WinRT 用的是
                    # 进程里那条缓存链路；这时**必须**明确放弃它，光重试没用。
                    self.phantom_connects += 1
                    log.warning(
                        "connect returned in %.2fs with table=%s -> winrt 走了缓存链路，"
                        "丢弃会话与设备对象（phantom #%s）",
                        elapsed,
                        table,
                        self.phantom_connects,
                    )
                    await self.hard_reset(cause="phantom link")
                    delay = PHANTOM_BACKOFF
                elif self.table_fails >= TABLE_RESET_AFTER:
                    # **睡眠唤醒后的关键动作**：Windows 手里的 GATT 缓存只要还是旧的，
                    # 重试多少次都拿不到 `0000a000` 那三个特征（真机日志里连续 4 次
                    # 全是 `gatt table incomplete`，每次都只差 ~2s）。光换设备对象
                    # 逼不出重新发现，得把这条链路的句柄整段丢掉再排队等一会儿 ——
                    # 见 `hard_reset()` / `TABLE_RESET_BACKOFF`。
                    await self.hard_reset(cause="gatt table")
                    delay = TABLE_RESET_BACKOFF
                else:
                    delay = TABLE_BACKOFF[min(attempt - 1, len(TABLE_BACKOFF) - 1)]
                if attempt < TABLE_RETRIES:
                    log.debug("retrying gatt discovery in %.1fs", delay)
                    await asyncio.sleep(delay)
            except BaseException:
                # 一定要先把半连接的 client 关掉再抛：`close()` 关的就是它
                await self.close()
                await self._note_failure()
                raise
        assert last is not None
        await self._note_failure()
        raise last

    async def _note_failure(self) -> None:
        """记一次连接失败；连败到阈值就做端口级自愈。"""
        self.fail_streak += 1
        if self.fail_streak >= PORT_RESET_AFTER:
            await self.hard_reset(cause="connect failures")

    def _reset_port(self, *, cause: str = "failures") -> None:
        """端口级自愈：丢掉设备对象引用并强制回收。

        反复失败之后，WinRT 手里可能还挂着上几次的 requester / GATT 会话
        （每失败一次就多一份）。留着它们只会让后面每一次连接都更快地
        `灾难性故障`。这里把设备对象换掉 + `gc.collect()` 逼 COM 包装析构，
        等价于手动"把这一路的句柄清掉"。下一次 `ensure_device()` 会重新解析。

        `cause` 只影响日志：以前这里固定打 `fail_streak`，而"GATT 表不完整"
        这条路走的是 `table_fails`，于是真机日志里出现
        `port reset #1 ... after 0 consecutive failures` —— 看着像什么都没发生，
        排查时误导性极强。
        """
        address = self.device.address
        name = self.device.name or ""
        self.port_resets += 1
        if cause == "gatt table":
            detail = f"{self.table_fails} gatt table failure(s)"
        elif cause == "phantom link":
            detail = f"{self.phantom_connects} cached-link (phantom) connect(s)"
        else:
            detail = f"{self.fail_streak} consecutive connect failure(s)"
        self.table_fails = 0
        # 退回"没有广播数据"的空壳，并把它标成**不新鲜**：下一次 ensure_device
        # 一定会重新扫描，而不是拿这个空壳去连（那正是连不上的原因）。
        self.set_device(direct_device(address, name))
        self._device_resolved_at = 0.0
        self.last_device_source = ""  # 逼下一次 ensure_device 重新解析
        try:
            collected = gc.collect()
        except Exception:  # pragma: no cover - gc 不该抛
            collected = -1
        log.warning(
            "port reset #%s for %s after %s (gc=%s)",
            self.port_resets,
            address,
            detail,
            collected,
        )

    @staticmethod
    def _has_service(client: BleakClient) -> bool:
        try:
            services = client.services
        except Exception:
            return False
        return any(str(s.uuid).lower() == SERVICE_UUID for s in services)

    def service_summary(self, limit: int = 8) -> str:
        """把当前 GATT 表打出来（DEBUG 用，排查"缓存是旧表"）。

        **空表要显式写成 `<empty>`**：真机日志里的"表不完整"有两种完全不同的
        情况 —— 空表（链路根本没建立）与"只有系统服务 1800/1801/180a"
        （连上了但对端没发布本服务），靠一个空字符串分不出来。
        """
        client = self._client
        if client is None:
            return "<no client>"
        try:
            services = list(client.services)
        except Exception as exc:
            return f"<unavailable: {exc}>"
        if not services:
            return "<empty>"
        parts = []
        for svc in services[:limit]:
            chars = []
            try:
                chars = [str(c.uuid) for c in svc.characteristics]
            except Exception:
                pass
            parts.append(f"{svc.uuid}{chars}")
        if len(services) > limit:
            parts.append(f"…(+{len(services) - limit})")
        return " ".join(parts)

    def state_snapshot(self) -> str:
        """一行诊断串（连接状态 / 连续失败 / 端口自愈），供上层日志使用。"""
        return (
            f"connected={self.connected} mtu={self.mtu}"
            f" fails={self.fail_streak} port_resets={self.port_resets}"
            f" table_fails={self.table_fails}"
            f" phantom={self.phantom_connects} resumes={self.resumes}"
            f" source={self.last_device_source or '?'}"
        )

    async def send_ctrl(self, data: bytes) -> None:
        if not self._client:
            raise BleakError("not connected")
        await self._client.write_gatt_char(CHAR_CTRL, data, response=True)

    async def send_data(self, data: bytes) -> None:
        if not self._client:
            raise BleakError("not connected")
        if self._write_no_resp:
            try:
                await self._client.write_gatt_char(CHAR_RX, data, response=False)
                return
            except BleakError:
                log.warning("write without response failed, falling back to write with response")
                self._write_no_resp = False
        await self._client.write_gatt_char(CHAR_RX, data, response=True)

    async def pair(self) -> bool:
        if not self._client:
            return False
        try:
            result = await self._client.pair()
            return result is not False
        except Exception as exc:
            log.warning("pair failed: %s", exc)
            return False

    async def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is None:
            return
        started = time.monotonic()
        try:
            await client.disconnect()
        except Exception as exc:
            # 半连接（connect 抛异常）时 disconnect 常常也报错，这正是要
            # dispose 的场景 —— 打 DEBUG 就好，别吓人
            log.debug("disconnect during close failed: %s", exc)
        log.debug("client disposed in %.0fms", (time.monotonic() - started) * 1000)

    async def services_ok(self) -> bool:
        """确认对端真的提供本服务（直连地址可能指向别的设备）。"""
        if not self._client:
            return False
        try:
            services = self._client.services
        except Exception:
            return False
        if not services:
            return False
        return any(str(s.uuid).lower() == SERVICE_UUID for s in services)
