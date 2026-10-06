from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import credentials as credentials_store
from . import crypto
from . import protocol
from .app_state import HANDSHAKE_STATES, derive_ui_state
from .ble.client import (
    DEVICE_SCAN_TIMEOUT,
    FALLBACK_SCAN_TIMEOUT,
    BleakTransport,
    FoundDevice,
    acquire_connect_slot,
    direct_device,
    release_connect_slot,
    scan,
)
from .ble.server import GattServer, adapter_address
from .ble.uuid_defs import SERVICE_UUID
from .ble.radio import cycle_radio, open_bluetooth_settings, radio_state, wait_radio_ready
from .config import Config, Network, NetworksStore, addr_keys, app_root
from .eph import EphStore
from .errors import Err, error_text
from .history import History
from .identity import display_name, load_identity, save_identity
from .logging_setup import purge_old_logs, resolve_level, setup_logging
from .power import StallDetector
from .session import HostService, PeerSession, State
from .ui.eph_key_dialog import EphKeyDialog
from .ui.history_dialog import HistoryDialog
from .ui.join_dialog import JoinDialog
from .ui.main_window import MainWindow
from .ui.pairing_dialog import PairingDialog
from .ui.settings_dialog import SettingsDialog
from .ui.theme import DARK, LIGHT, apply_theme
from .ui.widgets.chat_view import make_item

BACKOFF = [3, 6, 12, 24, 30]
MAX_ATTEMPTS = 40
PASSWORD_TTL = 1800  # 30 分钟内存缓存
EPH_TTL = 3600
TEXT = "text"
IMAGE = "image"
FILE = "file"
IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
RECENT_MESSAGES = 10
WATCHDOG_MS = 5000
# client 任务"活着但长期没 READY"多久算挂死（transport.connect 挂死时
# `_client_task` 永远不会 done，watchdog 什么都不做 → 只能手动点加入网络）
CLIENT_HUNG_SECONDS = 90
# "单次连接尝试"的宽限：扫描(3+8) + 连接(12+4) + notify(8) + 握手(15) ≈ 50s，
# 留一倍余量。**在这个窗口内 watchdog / 窗口激活都不许掐连接** ——
# 掐掉正在进行的 connect 正是"重启后很难连上"的直接原因。
CONNECT_GRACE_SECONDS = 110
# 唤醒之后多久还没连上，就认为"蓝牙栈没自己回来"（触发提示/自动重置）。
# 唤醒后第一轮的成本是：等 radio(≤20s) + 扫描(3+8s) + 连接(12+4s) + 握手(15s)，
# 两轮加起来 ~120s，所以 150s 之后还连不上就一定是栈的问题而不是运气问题。
RESUME_STALL_SECONDS = 150
# 唤醒后多久之内算"还在处理这次唤醒"（`_resume_at` 的有效窗口）
RESUME_WINDOW_SECONDS = 300

log = logging.getLogger("blechat.app")


class PeerNotFound(Exception):
    """扫描里没有这个对端（它没开 / 不在范围 / 地址+名字都对不上）。

    单独一个类型是为了跟"连上了但握手失败"区分开：这种失败**不需要**浪费
    一个 12s 的连接超时去确认，直接进退避重试即可。
    """


def describe_exception(exc: BaseException) -> str:
    """把异常打成一行**带 HRESULT** 的全文，专供 DEBUG 日志。

    真机报障时最有用的三个信息是「异常类名 / 文本 / 数字错误码」，而
    `str(OSError)` 在中文系统上只有本地化文本（`该对象已关闭。`），
    看不出是 `RO_E_CLOSED(0x80000013)` 还是 `E_FAIL(0x80004005)`。
    这里把 `errno` / `winerror` 补成十六进制。
    """
    parts = [type(exc).__name__, ": ", str(exc) or "(空文本)"]
    codes = []
    for attr in ("errno", "winerror"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            codes.append(f"{attr}={value}({value & 0xFFFFFFFF:#010x})")
    if codes:
        parts.append(" [" + " ".join(codes) + "]")
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        parts.append(f" <- {type(cause).__name__}: {cause}")
    return "".join(parts)


def _friendly_join_error(exc: BaseException) -> str:
    """把连接失败翻成用户能照着做的提示。

    以前直接把底层原文糊到提示栏：`[WinError -2147418113] 灾难性故障` ——
    用户完全不知道下一步该干什么（其实只要"重新点一次加入网络"就行，
    因为那会重新扫一次、刷新 Windows 设备表）。
    """
    if isinstance(exc, PeerNotFound):
        return f"{exc}。请确认对方已打开软件并处于 Host 模式。"
    if isinstance(exc, TimeoutError):
        return "连接超时（对方可能没开、不在范围，或设备表过期）"
    text = str(exc)
    low = text.lower()
    if "灾难性故障" in text or "-2147418113" in text or "0x80004005" in low:
        # 必须点出"设备表过期"这个原因，用户才知道自己在重新扫描
        return f"连接失败：{text}；设备表过期，正重新扫描后重试"
    return text or type(exc).__name__


def _looks_like_link_loss(message: str) -> bool:
    """底层那串原文是不是"链路没了"（而不是真的协议/业务错误）。"""
    low = message.lower()
    return any(
        key in low
        for key in (
            "not subscribed",
            "has been closed",
            "not connected",
            "gatt protocol error",
            "link dead",
            "keepalive ping failed",
            "send timeout",
        )
    ) or "已关闭" in message


class BleChatApp(QObject):
    """装配 UI、BLE、会话与持久化。"""

    def __init__(self, qapp: QApplication) -> None:
        super().__init__()
        self.qapp = qapp
        self.root = app_root()
        self.config = Config.load(self.root)
        # 日志级别必须在**任何东西写日志之前**定下来（config 里的 log_level
        # 或环境变量 BLECHAT_LOG_LEVEL=DEBUG）
        setup_logging(self.root, level=resolve_level(self.config.log_level),log_file=self.config.log_file)
        self.networks = NetworksStore(self.root)
        identity = load_identity(self.root)
        if self.networks.device_id != identity.device_id:
            self.networks.set_identity(identity.device_id, identity.name)
            self.networks.save()
        self.identity = identity
        self.device_id = identity.device_id
        self.device_name = display_name(identity)
        self.history = History(days=self.config.history_days)
        self.eph = EphStore(self.root)
        self.files_dir = self.root / "history_files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._remap_legacy_network_ids()

        self._mode = self.config.mode
        self._server: GattServer | None = None
        self._host: HostService | None = None
        self._client_session: PeerSession | None = None
        self._client_task: asyncio.Task | None = None
        self._client_started_at = 0.0
        # 当前这一轮"连接尝试"的开始时刻（扫描/connect 都算）。watchdog 与
        # 窗口激活据此判断"是否正在连" —— 见 `CONNECT_GRACE_SECONDS`。
        self._connect_started_at = 0.0
        self._network: Network | None = None
        self._host_address = ""
        self._stopping = False
        self._closing = False
        self._ready_seen = False
        self._join_password: str | None = None
        self._passwords: dict[str, tuple[str, float]] = {}
        self._auth_failures: dict[str, int] = {}
        self._auth_locked_until: dict[str, float] = {}
        self._first_join: tuple[str, bytes, int, bytes] | None = None
        self._pending: dict[tuple[str, int], list[str]] = {}
        self._progress_ui_at: dict[tuple[str, int], float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._tray: QSystemTrayIcon | None = None
        self._tray_notified = False
        self._purge_day = -1
        self._purge_hour = -1
        self._badge_override: tuple[str, str] | None = None
        self._connected_once = False
        self._reviving = False
        self._restarting = False
        # 睡眠/挂起检测：心跳之间墙钟跳了 45s+ 就是刚睡醒（见 `power.StallDetector`）
        self._stall = StallDetector()
        self._last_stall_gap = 0.0
        self._resume_at = 0.0
        self._resume_event = asyncio.Event()
        self._client_transport = None
        self._radio_reset_tried = False

        dark = apply_theme(qapp, None if self.config.theme == "auto" else self.config.theme == "dark")
        self.window = MainWindow()
        self.window.apply_colors(DARK if dark else LIGHT)
        self.window.set_emoji_enabled(self.config.enable_emoji)
        self._restore_geometry()
        self._wire_window()
        self._setup_tray()
        self._setup_timers()

        self.window.show()
        self.window.set_mode(self._mode)
        self.window.set_network("", "")
        self._load_recent_history()
        self._sync_ui()
        self.window.show_hint("就绪。Host 模式会广播网络；Join 模式点“加入网络…”连接对方。")
        self._spawn(self._start_mode(self._mode, initial=True))

    # ------------------------------------------------------------- plumbing
    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("task failed: %s", t.exception())

        task.add_done_callback(done)

    async def _modal(self, factory):
        """在「没有任务在册」的时机执行模态框，返回其结果。

        在协程里直接 `dialog.exec()` 时，**当前任务**会一直挂在
        `asyncio.tasks._current_tasks[loop]` 上不放；而 qasync 的 Qt 事件循环在
        嵌套的模态循环里仍然会驱动 asyncio —— 于是**别的任务**（例如
        `_spawn_client` 里 `connect()` 的 FutureLike）一 step 就抛
        `RuntimeError: Cannot enter into task ... while another task is being executed`
        ，那一步直接作废、协程永远恢复不了 ⇒ 「加入网络」永远 `TimeoutError`、
        扫描/重连全部哑掉，且这条异常走的是 root logger，以前完全看不见。

        这里把对话框丢进 `loop.call_soon` 的**裸回调**里执行（此时不在任何
        任务里），当前任务则挂在 Future 上等待 —— 两个条件同时满足才安全。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def run() -> None:
            if fut.done():
                return
            try:
                result = factory()
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
            else:
                fut.set_result(result)

        loop.call_soon(run)
        return await fut

    def _wire_window(self) -> None:
        w = self.window
        w.modeChangeRequested.connect(self._on_mode_change)
        w.joinRequested.connect(lambda: self._spawn(self._open_join()))
        w.ephRequested.connect(self._on_eph_clicked)
        w.historyRequested.connect(self._open_history)
        w.sendRequested.connect(self._on_send)
        w.clipboardRequested.connect(self._on_clipboard)
        w.exitRequested.connect(self._quit)
        w.closedToTray.connect(self.on_window_hidden_to_tray)
        w.settingsRequested.connect(self._open_settings)
        w.radioResetRequested.connect(lambda: self._spawn(self._reset_radio()))
        w.windowActivated.connect(self._on_window_activated)
        w.chat.deleteRequested.connect(self._delete_item)

    def _setup_tray(self) -> None:
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0078D4"))
        painter.drawRoundedRect(0, 0, 32, 32, 8, 8)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "B")
        painter.end()

        menu = QMenu()
        act_show = menu.addAction("显示主窗口")
        act_mode = menu.addAction("切换模式")
        menu.addSeparator()
        act_radio = menu.addAction("重置蓝牙适配器")
        act_radio.setShortcut("Ctrl+R")
        act_quit = menu.addAction("退出")

        tray = QSystemTrayIcon(QIcon(pixmap), self.qapp)
        tray.setToolTip("BLE Chat")
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        act_show.triggered.connect(self._show_window)
        act_mode.triggered.connect(self._switch_mode_from_tray)
        act_radio.triggered.connect(lambda: self._spawn(self._reset_radio()))
        act_quit.triggered.connect(self._quit)
        # 托盘菜单里的快捷键只在菜单打开时有效；挂一份到窗口上（Ctrl+R 随时可用），
        # 唤醒后卡住时这是"一键重置蓝牙"的键盘入口。
        short = QAction("重置蓝牙适配器", self.window)
        short.setShortcut("Ctrl+R")
        short.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        short.triggered.connect(self._open_radio_reset)
        self.window.addAction(short)
        self._radio_shortcut = short
        tray.show()
        self._tray = tray

    def _setup_timers(self) -> None:
        self._tick_count = 0
        self._timer = QTimer(self)
        # 15s 一跳：分钟级维护自己数跳数（以前直接 60s），
        # 顺带每 4 分钟打一行完整状态快照（见 `_diagnostic_snapshot`）
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self._on_minute_tick)
        self._timer.start()

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start()

    # ------------------------------------------------------------- helpers
    def _now(self) -> int:
        return int(time.time())

    def _refresh_targets(self) -> None:
        self._sync_ui()

    def _sync_ui(self) -> None:
        """badge / 连接数 / 目标 / busy 的**唯一**写入点（见 app_state.py）。"""
        self._prune_pending()
        state = derive_ui_state(
            mode=self._mode,
            server_running=self._server is not None and self._server.running,
            host=self._host,
            client_session=self._client_session,
            pending_count=len(self._pending),
            override_badge=self._badge_override,
            stopping=self._stopping,
        )
        self.window.set_badge(state.badge_kind, state.badge_text)
        self.window.set_connections(state.connections)
        self.window.set_targets(list(state.targets))
        self.window.set_busy(state.busy)

    def _ready_peer_ids(self) -> set[str]:
        if self._mode == "host":
            if self._host is None:
                return set()
            return {pid for pid, _name in self._host.peer_names()}
        if self._client_session is not None and self._client_session.ready:
            return {self._client_session.transport.peer_id}
        return set()

    def _prune_pending(self) -> None:
        """没有 READY 会话的 pending 一律判失败，避免进度环/发送按钮永久卡住。

        链路异常（对端重启、睡眠唤醒、`GATT Protocol Error`）时 `_on_closed`
        有时根本不会被触发，pending 就永远留在 `busy=True`：
        现象是发送按钮变灰、进度环停在半圈。这里做兜底。
        """
        if not self._pending:
            return
        ready = self._ready_peer_ids()
        stale = [key for key in self._pending if key[0] not in ready]
        if not stale:
            return
        model = self.window.chat.chat_model
        for key in stale:
            for bubble_key in self._pending.pop(key, []):
                row = model.row_of(bubble_key)
                item = model.item_at(row) if row >= 0 else None
                if item is not None:
                    item.status = "failed"
                    item.progress = None
                    model.touch(bubble_key)
            self._progress_ui_at.pop(key, None)
        log.debug("pruned %s stale pending entries", len(stale))
        # 这里以前**只**在"全清空"时调 `finish_progress()`，它会 `show_progress(1,1)`
        # —— 进度环停在全满、一直 visible（dev 报的"发送按钮旁边的圈卡住不走"）。
        # 正确做法是把 busy 状态交给 `_sync_ui()` 重算：`_pending` 空了就该
        # `set_busy(False)`（→ `progress.reset()` 隐藏），没空就保持。
        # `_prune_pending` 平时就是 `_sync_ui()` 调进来的，这里加守卫是为了
        # 让它可以被单独调用（测试 / 未来的其它入口）。
        sync = getattr(self, "_sync_ui", None)
        if callable(sync):
            sync()

    def _set_badge_override(self, badge: tuple[str, str] | None) -> None:
        self._badge_override = badge
        self._sync_ui()

    # ------------------------------------------------------------- history
    def _remap_legacy_network_ids(self) -> None:
        """老库里 network_id 是 `ad-hoc:{地址}`，统一并入真实 network_id。

        地址可能写成 `8C:C6:…` / `8c-c6:…` / WinRT 设备路径等多种形式，
        这里按 `addr_keys()` 归一化取交集，否则永远搬不动。
        """
        present = {old: True for old in self.history.ad_hoc_ids()}
        if not present:
            return
        for net in self.networks.networks:
            if not net.host_address or not net.network_id:
                continue
            target = addr_keys(net.host_address)
            if not target:
                continue
            orphan = [
                old
                for old in present
                if addr_keys(old.split(":", 1)[-1]) & target
            ]
            for old in orphan:
                try:
                    moved = self.history.remap_network(old, net.network_id)
                except Exception as exc:
                    log.warning("remap history failed: %s", exc)
                    return
                if moved:
                    present.pop(old, None)
                    log.info("history remapped %s rows: %s -> %s", moved, old, net.network_id)

    def _remap_adhoc(self, address: str, network_id: str) -> None:
        """把库里所有 `ad-hoc:{该地址}` 变体并入 network_id（分隔符/大小写/设备路径都认）。"""
        if not address or not network_id:
            return
        target = addr_keys(address)
        if not target:
            return
        for old in self.history.ad_hoc_ids():
            if not (addr_keys(old.split(":", 1)[-1]) & target):
                continue
            try:
                moved = self.history.remap_network(old, network_id)
            except Exception as exc:
                log.warning("history remap on connect failed: %s", exc)
                return
            if moved:
                log.info("history remapped %s rows: %s -> %s", moved, old, network_id)

    def _pick_history_network_id(self, first: str | None = None) -> str | None:
        """聊天区 / 历史窗口该按哪个 `network_id` 取行。

        `last_network_id` **会换号**（换网络、config 读失败重铸、按 MAC 直连首次加入…），
        按它查空**不代表历史没了** —— 行都还在别的 id 下面。以前这里查不到就直接
        return，界面整屏空白，dev 看到的就是「client 历史被清空（timing 不定）」。

        优先级（第一个"真的有行"的胜出）：
        1. `first` —— 调用方明确指定的（正在连的那个网络、历史窗口当前网络）；
        2. **库里最近写入过的 id** —— 只按 `last_network_id` 查是不够的：
           它换号之后旧行还在自己名下，会把真正最新的那批盖住（dev 报的
           「重启后历史看着被清空，其实是显示了上一批」）；
        3. `last_network_id`；
        4. 全都没行 ⇒ None（真的空库）。
        """
        candidates = list(
            dict.fromkeys(
                [first, self.history.most_recent_network_id(), self.config.last_network_id]
            )
        )
        tried: list[str] = []
        picked: str | None = None
        for cid in candidates:
            if not cid:
                continue
            tried.append(cid)
            if self.history.recent(cid, limit=1):
                picked = cid
                break
        # 这一段是排查「历史被清空」的关键证据：候选 id 各有多少行、最后用了谁。
        # 平时是 DEBUG（不进 app.log），需要时把日志级别开到 DEBUG 即可。
        if log.isEnabledFor(logging.DEBUG):
            counts = self.history.count_by_network()
            log.debug(
                "history pick: first=%r last_network_id=%r tried=%s picked=%r",
                first,
                self.config.last_network_id,
                tried,
                picked,
            )
            log.debug(
                "history rows by network_id: %s (total=%s)",
                counts or "{}",
                sum(counts.values()),
            )
        elif picked is not None and picked != self.config.last_network_id:
            log.info(
                "history pick fell back to %s (last_network_id=%r)",
                picked,
                self.config.last_network_id,
            )
        return picked

    def _load_recent_history(self) -> None:
        """启动时把上次会话最近 10 条消息画进聊天区（只读，不再重发）。"""
        network_id = self._pick_history_network_id()
        if network_id is None:
            return
        if network_id != self.config.last_network_id:
            log.info(
                "history view id fallback: %s -> %s",
                self.config.last_network_id,
                network_id,
            )
        rows = list(reversed(self.history.recent(network_id, limit=RECENT_MESSAGES)))
        if not rows:
            return
        # client 侧 host 的昵称：老行里存的是 BLE 广播名（代号），
        # 用 networks.json 里已保存的 alias 覆盖显示。
        alias = ""
        if self._mode == "join":
            net = self.networks.get(network_id)
            alias = ((net.peer_alias or net.host_name).strip() if net else "")
        for msg in rows:
            self.window.chat.append(
                make_item(
                    msg_id=msg.msg_id,
                    direction=msg.direction,
                    peer_id=msg.peer_id or "",
                    peer_name=alias or msg.peer_name or "对方",
                    kind=msg.kind,
                    content=msg.content,
                    created_at=msg.created_at,
                    file_name=msg.file_name or "",
                    file_size=msg.file_size or 0,
                    mime_type=msg.mime_type or "",
                    local_path=msg.local_path or "",
                ),
                scroll=False,
            )
        self.window.chat.scroll_to_bottom()
        log.info("loaded %s recent messages for %s", len(rows), network_id)

    # ------------------------------------------------------------- watchdog
    def _watchdog_tick(self) -> None:
        """每 5s：睡眠/唤醒或系统抢占蓝牙后把断掉的链路拉回来。"""
        if self._closing or self._stopping:
            return
        self._check_resume_stall()
        if self._mode == "host":
            if self._server is None or not self._server.running:
                return
            if not self._server.advertising_ok:
                self._spawn(self._revive_host())
            return
        if self._mode != "join":
            return
        if not self._connected_once:
            return
        task = self._client_task
        if task is None or task.done():
            self._spawn(self._restart_client())
            return
        # 任务"活着"但根本连不上（`transport.connect` 挂死/会话停在握手前）
        # → 任务永远不会 done，以前这条路径什么都不做，只能手动点"加入网络"。
        if self._client_hung():
            log.warning(
                "client task hung for %ss, restarting",
                CONNECT_GRACE_SECONDS if self._connect_started_at else CLIENT_HUNG_SECONDS,
            )
            self.window.show_hint("连接长时间无进展，正在重新连接…")
            self._spawn(self._restart_client(force=True))

    def _client_hung(self) -> bool:
        if self._client_task is None or self._client_task.done():
            return False
        session = self._client_session
        if session is not None and session.state is not State.READY:
            # 连接进行中（扫描 / connect / 握手）。给足宽限：一次尝试的正常成本是
            # 3s 地址扫描 + 最多 8s 兜底扫描 + 12s 连接 + 8s notify + 15s 握手。
            # 以前这里按"90s 没有 READY 就算挂死"，正常重连也会被误判，
            # 于是 watchdog/窗口激活会把正在进行的 `connect()` 取消重来
            # （日志里的 `TimeoutError <- CancelledError`），**越掐越连不上**。
            if self._connect_started_at:
                return time.monotonic() - self._connect_started_at > CONNECT_GRACE_SECONDS
            return time.monotonic() - self._client_started_at > CLIENT_HUNG_SECONDS
        if session is None:
            return time.monotonic() - self._client_started_at > CLIENT_HUNG_SECONDS
        if session.ready:
            return False
        if getattr(session, "_waiting_user", False):
            return False  # 正在等用户输密码，别掐
        if self._badge_override is not None and self._badge_override[1] == "RETRY_STOP":
            return False
        return time.monotonic() - self._client_started_at > CLIENT_HUNG_SECONDS

    def _on_window_activated(self) -> None:
        """窗口重新获得焦点 = 用户回来了，立即检查链路而不是等 5s。

        ⚠️ 只在**确认没在连接**时才动手。以前这里是
        `if task is None or task.done() or self._client_hung(): restart(force=...)`
        —— `_client_hung()` 在"连接中"是真，于是每切一次窗口就把正在进行的
        `connect()` 取消重来。用户点一下别的窗口再点回来，连接就永远走不完
        （真机日志：`join attempt failed ... TimeoutError <- CancelledError` 反复出现）。
        """
        if self._closing or self._stopping:
            return
        if self._mode == "host" and self._server is not None and self._server.running:
            if not self._server.advertising_ok:
                self._spawn(self._revive_host())
        elif self._mode == "join" and self._connected_once:
            task = self._client_task
            if task is None or task.done():
                self._spawn(self._restart_client())
            elif self._client_hung():
                log.warning("client task hung, force restarting")
                self._spawn(self._restart_client(force=True))

    async def _revive_host(self, *, hard: bool = False) -> None:
        if self._reviving or self._server is None:
            return
        self._reviving = True
        try:
            ok = await self._server.revive(hard=hard)
            log.info("host revive: %s (hard=%s adv=%s)", ok, hard, self._server.advertising_status)
            if ok:
                self.window.show_hint("已恢复广播。")
            elif hard:
                self.window.show_hint("广播恢复失败，可在“设置”里查看日志，或点“重置蓝牙”。")
        except Exception as exc:
            log.warning("host revive failed: %s", exc)
        finally:
            self._reviving = False

    async def _restart_client(self, *, force: bool = False) -> None:
        if self._restarting or self._stopping or self._closing:
            return
        if self._mode != "join":
            return
        net = self.networks.get(self.config.last_network_id)
        if net is None or not net.host_address:
            return
        running = self._client_task is not None and not self._client_task.done()
        if running and not force:
            return
        if not self._has_credential(net) and not self._join_password:
            return
        self._restarting = True
        try:
            if running:
                await self._drop_client_task(self._client_task)
            self._badge_override = ("handshake", "RETRY")
            self._sync_ui()
            self.window.show_hint(f"检测到连接已断开，正在重连 {net.host_name or net.host_address}…")
            self._connect(
                net.host_address,
                net.host_name or net.host_address,
                net,
                self._join_password,
                "",
                None,
            )
        finally:
            self._restarting = False

    async def _drop_client_task(self, task: asyncio.Task | None) -> None:
        """取消并等旧 client 任务真正结束，避免两个 loop 互相写 `self._client_session`。"""
        if task is None or task.done():
            self._client_task = None
            return
        self._client_task = None
        task.cancel()
        try:
            # 卡在不响应取消的 WinRT 调用里时不能死等，否则 `_restarting`
            # 永远为 True，后续重连全被挡住。等久一点（15s）是因为"旧任务还在
            # connect 里"时新任务会去抢同一个连接名额（见 `acquire_connect_slot`），
            # 留出它自己走完超时的余地。
            await asyncio.wait({task}, timeout=15.0)
            if not task.done():
                log.warning("old client task did not stop within 15s, abandoning it")
        except Exception as exc:
            log.debug("client task cancel: %s", exc)

    def _restore_geometry(self) -> None:
        geom = self.config.window
        self.window.resize(geom.w, geom.h)
        if geom.x is not None and geom.y is not None:
            self.window.move(geom.x, geom.y)

    def _save_geometry(self) -> None:
        geo = self.window.geometry()
        self.config.window.w = geo.width()
        self.config.window.h = geo.height()
        self.config.window.x = geo.x()
        self.config.window.y = geo.y()
        self.config.save()

    # ------------------------------------------------------------- mode
    def _on_mode_change(self, mode: str) -> None:
        self._spawn(self._switch_mode(mode))

    async def _switch_mode(self, mode: str) -> None:
        if mode == self._mode and self._mode_running:
            return
        await self._stop_all()
        self._mode = mode
        self.config.mode = mode
        self.config.save()
        self.window.set_mode(mode)
        await self._start_mode(mode)

    @property
    def _mode_running(self) -> bool:
        if self._mode == "host":
            return self._server is not None
        return self._client_task is not None and not self._client_task.done()

    async def _start_mode(self, mode: str, *, initial: bool = False) -> None:
        if mode == "host":
            await self._start_host(initial=initial)
            return
        self._set_badge_override(None)
        self.window.set_network("", "")
        self.window.show_hint("Join 模式：点“加入网络…”扫描或输入 Host 地址。")
        if initial:
            self._spawn(self._auto_join())

    async def _auto_join(self) -> None:
        """启动时如果上次的网络和密码都在，直接恢复连接（不用每次重新输密码）。"""
        if self._mode != "join":
            return
        net = self.networks.get(self.config.last_network_id)
        if net is None or not net.host_address:
            return
        if not self._has_credential(net):
            return
        await asyncio.sleep(1.0)
        if self._stopping or self._closing or self._mode != "join":
            return
        if self._client_task is not None and not self._client_task.done():
            return
        name = net.peer_alias or net.host_name or net.host_address
        self.window.show_hint(f"正在恢复与 {name} 的连接…")
        self._connect(net.host_address, name, net, None, "", None)

    async def _stop_all(self) -> None:
        self._stopping = True
        task, self._client_task = self._client_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.debug("client task cancel: %s", exc)
        session, self._client_session = self._client_session, None
        if session is not None:
            try:
                await session.close("switch mode")
            except Exception:
                pass
        if self._host is not None:
            try:
                await self._host.stop()
            except Exception:
                pass
            self._host = None
        if self._server is not None:
            try:
                await self._server.stop()
            except Exception:
                pass
            self._server = None
        self._pending.clear()
        self._progress_ui_at.clear()
        self._network = None
        self.window.set_peers([])
        self.window.set_network("", "")
        self._badge_override = None
        self._connected_once = False
        self._stopping = False
        self._sync_ui()

    # ------------------------------------------------------------- host
    async def _start_host(self, *, initial: bool = False) -> None:
        self._set_badge_override(("handshake", "STARTING"))
        if initial:
            self.window.show_hint("Host 模式：请设置/输入本网络的共享密码…")
        credentials = await self._host_credentials()
        if credentials is None:
            self._set_badge_override(None)
            self.window.show_hint("已取消密码输入，Host 未启动。")
            return
        net, psk = credentials

        server = GattServer()
        # 先接好 HostService 回调再开广播，避免广播期间最早的写请求被丢弃
        host = HostService(
            server,
            network_id=net.network_id,
            auth=net.auth,
            psk=psk,
            local_device_id=self.device_id,
            local_name=self.device_name,
            eph_lookup=self.eph.lookup,
            eph_consume=self.eph.consume,
            on_session=self._on_host_session,
        )
        self._server = server
        self._host = host
        self._network = net
        try:
            await server.start()
        except Exception as exc:
            log.error("gatt server start failed: %s", exc)
            self._server = None
            self._host = None
            self._network = None
            self._set_badge_override(("error", "ERROR"))
            self.window.show_hint(f"广播启动失败：{exc}")
            return

        address = ""
        try:
            address = await adapter_address()
        except Exception:
            address = ""
        if address and net.host_address != address:
            net.host_address = address
            self.networks.upsert(net)
            self.networks.save()

        self._host_address = address or net.host_address
        self._remember_network_id(net.network_id)
        self.window.set_network(net.host_name or self.device_name, self._host_address)
        self._set_badge_override(None)
        self.window.show_hint(
            f"Host 已广播，等待对方加入…（服务 {SERVICE_UUID[:13]}…，连接数 0）"
        )

    def _note_peer_address(self, network: Network | None, address: str) -> None:
        """对端地址变了（系统轮换了它的隐私地址）：更新网络记录并落盘。

        这是「重启 client 难以连接」的根因之一 —— `networks.json` 里存的是
        上一次的地址，而地址已经换了。学到新地址后立刻写回，下次重启就能按
        新地址快查命中，不用每次等一轮 8s 的兜底扫描。
        """
        if not address:
            return
        if network is None:
            log.info("learned peer address %s (no network record to update)", address)
            return
        old = network.host_address
        if old == address:
            return
        network.host_address = address
        self.networks.upsert(network)
        self.networks.save()
        log.warning(
            "peer address learned: %s -> %s (network=%s)",
            old,
            address,
            network.network_id,
        )

    def _remember_network_id(self, network_id: str) -> None:
        """落盘当前 `network_id`，并把"换号"记进日志。

        换号会让聊天区/历史按新 id 查旧行 ⇒ 看起来像被清空（其实行还在）。
        dev 报「client 历史清空、timing 不定、可能重新创建」时，
        `last_network_id changed: A -> B` 这行就是第一手证据。
        """
        old = self.config.last_network_id
        if old == network_id:
            return
        log.info("last_network_id changed: %s -> %s", old, network_id)
        self.config.last_network_id = network_id
        self.config.save()

    def _find_own_network(self) -> Network | None:
        """`last_network_id` 找不到（config 读失败/被清/老版本）时，找回本机原网络。

        只认本服务、且带 `auth` 的记录，取最近加入的那个。
        """
        candidates = [n for n in self.networks.networks if n.service_uuid == SERVICE_UUID and n.auth]
        if not candidates:
            return None
        candidates.sort(key=lambda n: n.last_joined_at or 0, reverse=True)
        return candidates[0]

    async def _host_credentials(self) -> tuple[Network, bytes] | None:
        net = self.networks.get(self.config.last_network_id)
        if net is None:
            net = self._find_own_network()
            if self.networks.networks:
                # 这是 network_id 换号的主要入口之一：config 里的 id 在 networks.json
                # 里不存在 ⇒ 必须找回原网络，**绝不能**走到下面 `uuid.uuid4()` 铸新 id
                # （铸新 id 会让历史按新 id 查空，看着就是「server 重启后历史被清空」）。
                log.warning(
                    "host credentials: last_network_id=%r not found in networks.json"
                    " (%s entries), fallback=%r",
                    self.config.last_network_id,
                    len(self.networks.networks),
                    net.network_id if net else None,
                )
            else:
                # 第一次运行：本来就没有网络记录，铸新 id 是正确的
                log.info("host credentials: first run (networks.json empty)")
        if net is not None and self.config.last_network_id != net.network_id:
            self._remember_network_id(net.network_id)
        if net is None or not net.auth:
            log.info("host credentials: creating a brand-new network (no reusable record)")
            result = await self._modal(
                lambda: PairingDialog.ask(self.window, mode="create", network_name=self.device_name)
            )
            if result is None:
                return None
            password, remember = result
            net = Network(
                network_id=str(uuid.uuid4()),
                host_address="",
                host_name=self.device_name,
                service_uuid=SERVICE_UUID,
                last_joined_at=self._now(),
                auth=crypto.make_auth_params(password),
            )
            self.networks.upsert(net)
            self.networks.save()
            # 立刻落盘：以前要等 `server.start()` 成功才写（见 `_start_host` 末尾），
            # 启动一失败 `last_network_id` 就还是 None，下次启动又铸新 id。
            self._remember_network_id(net.network_id)
            if remember:
                self._passwords[net.network_id] = (password, time.time())
            psk = crypto.psk_from_password(password, net.auth)
            self._remember_psk(net.network_id, psk)
            return net, psk

        stored = self._stored_psk(net)
        if stored is not None:
            return net, stored

        password = self._cached_password(net.network_id)
        remember = True
        if password is None:
            if self._auth_gate(net.network_id) > 0:
                self.window.show_hint("密码错误次数过多，请 30 秒后再启动 Host。")
                return None
            result = await self._modal(
                lambda: PairingDialog.ask(
                    self.window,
                    mode="join",
                    auth=net.auth,
                    network_name=net.host_name or self.device_name,
                    gate=lambda: self._auth_gate(net.network_id),
                    on_result=lambda ok: (
                        self._auth_succeeded(net.network_id) if ok else self._auth_failed(net.network_id)
                    ),
                )
            )
            if result is None:
                return None
            password, remember = result
            if remember:
                self._passwords[net.network_id] = (password, time.time())
        psk = crypto.psk_from_password(password, net.auth)
        if remember:
            self._remember_psk(net.network_id, psk)
        return net, psk

    # ------------------------------------------------------------- persisted PSK
    def _stored_psk(self, net: Network | None) -> bytes | None:
        """读磁盘上 DPAPI 加密的 PSK；校验 verifier，不对就丢弃。"""
        if net is None or not net.auth or not self.config.persist_credentials:
            return None
        psk = credentials_store.get_psk(net.network_id, self.root)
        if psk is None:
            return None
        verifier = net.auth.get("verifier")
        if verifier:
            try:
                expected = base64.b64decode(verifier)
            except Exception:
                return None
            if crypto.psk_verifier(psk) != expected:
                log.warning("stored PSK mismatch, dropping %s", net.network_id)
                credentials_store.forget(net.network_id, self.root)
                return None
        log.info("using persisted PSK for %s", net.network_id)
        return psk

    def _remember_psk(self, network_id: str, psk: bytes) -> None:
        if not self.config.persist_credentials:
            return
        if credentials_store.save_psk(network_id, psk, self.root):
            log.info("persisted PSK for %s", network_id)

    def _has_credential(self, net: Network | None) -> bool:
        if net is None:
            return False
        return self._stored_psk(net) is not None or self._cached_password(net.network_id) is not None

    def _cached_password(self, network_id: str) -> str | None:
        entry = self._passwords.get(network_id)
        if entry is None:
            return None
        password, ts = entry
        if time.time() - ts > PASSWORD_TTL:
            self._passwords.pop(network_id, None)
            return None
        return password

    # ------------------------------------------------------------- password lockout
    def _auth_gate(self, network_id: str) -> float:
        """返回还需等待的秒数（0 = 未锁定）。"""
        return max(0.0, self._auth_locked_until.get(network_id, 0.0) - time.time())

    def _auth_failed(self, network_id: str) -> None:
        count = self._auth_failures.get(network_id, 0) + 1
        if count >= 3:
            self._auth_failures[network_id] = 0
            self._auth_locked_until[network_id] = time.time() + 30
            self.window.show_hint("密码连续错误 3 次，已锁定 30 秒。")
        else:
            self._auth_failures[network_id] = count
            self.window.show_hint(f"密码错误（{count}/3）。")

    def _auth_succeeded(self, network_id: str) -> None:
        self._auth_failures.pop(network_id, None)
        self._auth_locked_until.pop(network_id, None)

    def _on_host_session(self, session: PeerSession) -> None:
        self._wire_session(session)

    # ------------------------------------------------------------- join
    async def _open_join(self) -> None:
        if self._mode != "join":
            self.window.show_hint("请先切换到 Join 模式。")
            return
        self._set_badge_override(("handshake", "SCANNING"))
        self.window.show_hint("正在扫描附近设备…（Windows 需要允许定位/蓝牙权限）")
        devices = await scan(6.0)
        log.info(
            "join dialog scan: %s device(s) %s",
            len(devices),
            [d.address for d in devices[:6]],
        )
        hint = (
            f"扫描到 {len(devices)} 台设备。"
            if devices
            else "扫描结果为空：请检查 Windows 定位权限，或直接输入 Host 地址（上方输入框）。"
        )
        self.window.show_hint(hint)
        await self._modal(lambda: self._open_join_dialog(devices, hint))

    def _open_join_dialog(self, devices: list[FoundDevice], hint: str) -> None:
        dialog = JoinDialog(devices, self.networks.networks, hint=hint, parent=self.window)
        dialog.rescanRequested.connect(lambda: self._spawn(self._rescan(dialog)))
        if dialog.exec() != JoinDialog.DialogCode.Accepted:
            self._set_badge_override(None)
            self.window.show_hint("已取消加入。")
            return
        address = dialog.address
        name = dialog.name or address
        network = dialog.network or self.networks.find_by_host(address, SERVICE_UUID)
        if dialog.use_eph:
            password, eph_id, eph_key = None, dialog.eph_id, dialog.eph_key
        else:
            password, eph_id, eph_key = dialog.password, "", None
            if dialog.remember and network is not None:
                self._passwords[network.network_id] = (password, time.time())
        self._connect(address, name, network, password, eph_id, eph_key)

    async def _rescan(self, dialog: JoinDialog) -> None:
        self.window.show_hint("正在重新扫描…")
        devices = await scan(6.0)
        hint = f"扫描到 {len(devices)} 台设备。" if devices else "扫描结果为空，可用地址直连。"
        dialog.set_devices(devices, hint)

    def _connect(
        self,
        address: str,
        name: str,
        network: Network | None,
        password: str | None,
        eph_id: str,
        eph_key: bytes | None,
    ) -> None:
        if network is not None:
            self._remember_network_id(network.network_id)
            self._network = network
            if network.host_address:
                self._remap_adhoc(network.host_address, network.network_id)
            if name and network.peer_alias != name:
                network.peer_alias = name
                self.networks.upsert(network)
                self.networks.save()
        else:
            self._network = None
        self._ready_seen = False
        self._join_password = password
        self.window.set_network(name, address)
        self._set_badge_override(("handshake", "CONNECTING"))
        self.window.show_hint(f"正在连接 {name}（{address}）…")
        log.debug(
            "connect target name=%r address=%r network_id=%r auth=%s",
            name,
            address,
            network.network_id if network else None,
            bool(network.auth) if network else False,
        )
        # 旧任务可能还挂在 `transport.connect` 上：不 cancel 就会泄漏成
        # "Task was destroyed but it is pending"，还会互相抢 `_client_session`
        old, self._client_task = self._client_task, None
        if old is not None and not old.done():
            self._spawn(self._drop_client_task(old))
        self._connected_once = True
        self._client_started_at = time.monotonic()
        self._client_task = self._spawn_client(
            address, name, network, password, eph_id, eph_key
        )

    def _spawn_client(
        self,
        address: str,
        name: str,
        network: Network | None,
        password: str | None,
        eph_id: str,
        eph_key: bytes | None,
    ) -> asyncio.Task:
        async def runner() -> None:
            await self._client_loop(address, name, network, password, eph_id, eph_key)

        return asyncio.ensure_future(runner())

    async def _client_loop(
        self,
        address: str,
        name: str,
        network: Network | None,
        password: str | None,
        eph_id: str,
        eph_key: bytes | None,
    ) -> None:
        attempt = 0
        me = asyncio.current_task()
        # 一次连接任务只建一个 transport：反复失败时由它自己做端口级自愈
        # （见 `BleakTransport._reset_port`）。以前每次重试都 new 一个，
        # 上一条半连接的对象没人 dispose ⇒ 越连越坏。
        transport = BleakTransport(direct_device(address, name))
        # 记在 self 上：睡眠检测（`_on_stall_resume`，跑在 Qt 定时器里）要能
        # 直接把它标脏 —— 那条路径是同步的，没法 await。
        self._client_transport = transport
        # 名字是**唯一稳定的身份**：对外的 BLE 地址会变（系统轮换隐私地址），
        # 名字不会。按名字兜底扫描是"重启后连不上"的解法（见 `resolve_peer`）。
        peer_name = name or (network.host_name if network else "") or address
        while not self._stopping and not self._closing:
            session: PeerSession | None = None
            self._client_started_at = time.monotonic()
            self._connect_started_at = time.monotonic()
            started = time.monotonic()
            try:
                # 睡眠唤醒后**先等 radio 回来再扫描**：唤醒瞬间 radio 还是 OFF，
                # 这时候扫出来的结果要么是空的、要么是上一代的缓存，两种情况都会
                # 白烧掉一轮 11s 的扫描预算。
                if self._resume_at and time.monotonic() - self._resume_at < RESUME_WINDOW_SECONDS:
                    ready, state = await wait_radio_ready()
                    log.warning(
                        "post-resume: radio=%s (%s) before resolve",
                        state,
                        "ready" if ready else "NOT READY",
                    )
                # 每次连接前先把对端解析成**当前有效**的设备：
                # 1) 按地址快查（3s）；2) 地址变了就按名字 + 服务 UUID 扫（8s）。
                # 解析不出来就跳过这一轮 —— 拿旧地址硬连只会白等一个 12s 超时，
                # 而且会把 WinRT 那一路句柄越搞越脏。
                source = await transport.ensure_device(
                    name=peer_name,
                    force=True,
                    address_timeout=DEVICE_SCAN_TIMEOUT,
                    fallback_timeout=FALLBACK_SCAN_TIMEOUT,
                )
                log.debug(
                    "connect attempt=%s addr=%s name=%r device_source=%s"
                    " device=%r fails=%s port_resets=%s",
                    attempt + 1,
                    address,
                    peer_name,
                    source,
                    getattr(transport.device, "name", ""),
                    transport.fail_streak,
                    transport.port_resets,
                )
                if source == "missing":
                    # 扫描里没有对端：**不要**拿旧地址硬连（那只会白等一个 12s 超时，
                    # 还把这边的 WinRT 句柄搞脏）。直接进退避重试。
                    self.window.show_hint(f"未发现 {peer_name}，正在重新扫描…")
                    raise PeerNotFound(f"{peer_name}（{address}）不在范围内")
                # 地址变了：历史/network 的键要跟着走，否则新地址上收到的消息
                # 会写进另一个 network_id（界面看着就是"历史被清空"）。
                learned = transport.device.address
                if learned and learned != address:
                    self._note_peer_address(network, learned)
                    address = learned
                session = PeerSession(
                    transport,
                    network_id=network.network_id if network else f"ad-hoc:{address.upper()}",
                    is_host=False,
                    local_device_id=self.device_id,
                    local_name=self.device_name,
                    auth=network.auth if network else {},
                    psk_provider=self._make_psk_provider(network, password, eph_id, eph_key),
                    auto_pair=self.config.system_pairing,
                )
                log.debug(
                    "session network_id=%s peer_id=%s peer_name=%r",
                    session.network_id,
                    session.transport.peer_id,
                    session.peer_name,
                )
                self._wire_session(session)
                self._client_session = session
                self._sync_ui()
                # 同一对端只允许一条链路在建：旧任务被"放弃"之后可能还卡在
                # WinRT 里，两个 connect 撞在一起 = 两边都拿不到服务表
                # （用户看到的就是"点了加入网络也没反应"）。
                slot_addr = transport.device.address
                token = await acquire_connect_slot(slot_addr)
                try:
                    await transport.connect(timeout=12.0)
                finally:
                    release_connect_slot(slot_addr, token)
                if not await transport.services_ok():
                    log.warning(
                        "service %s missing on %s; gatt table = %s",
                        SERVICE_UUID[:8],
                        address,
                        transport.service_summary(),
                    )
                    self.window.show_hint("对方设备没有 BLE Chat 服务（地址可能不对），稍后重试。")
                    await session.close("no service")
                    session = None
                    raise RuntimeError("service not found")
                log.debug(
                    "link up in %.2fs mtu=%s source=%s; gatt table = %s",
                    time.monotonic() - started,
                    transport.mtu,
                    transport.last_device_source,
                    transport.service_summary(),
                )
                session.start_handshake()
                while session.state is not State.CLOSED and not self._stopping:
                    await asyncio.sleep(0.2)
                log.debug(
                    "session ended after %.1fs state=%s last_error=%s",
                    time.monotonic() - started,
                    session.state.value,
                    session.last_error.name if session.last_error else None,
                )
                if self._ready_seen:
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 全文（含 HRESULT）只进日志；提示栏给人话
                log.warning(
                    "join attempt failed after %.1fs: %s",
                    time.monotonic() - started,
                    describe_exception(exc),
                )
                if not self._stopping and self._client_task is me:
                    self._badge_override = ("error", "ERROR")
                    self.window.show_hint(f"连接失败：{_friendly_join_error(exc)}")
            finally:
                if session is not None and session.state is not State.CLOSED:
                    try:
                        await session.close("client loop")
                    except Exception:
                        pass
                # 只清理"还是我"的会话：`_connect` 已经换新任务时，
                # 这里的 None 会把新会话的 UI 状态抹掉
                if self._client_session is session:
                    self._client_session = None
                self._sync_ui()

            if self._stopping or self._closing:
                return
            if self._client_task is not me:
                return  # 已被新的连接任务接管，别再写状态
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                self._badge_override = ("error", "RETRY_STOP")
                self._sync_ui()
                self.window.show_hint("多次重连失败，将自动重试（也可点“加入网络…”确认地址）。")
                return
            delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            if not self._ready_seen:
                self._badge_override = ("handshake", "RETRY")
                self._sync_ui()
            self.window.show_hint(f"{delay}s 后自动重连（第 {attempt} 次）…")
            log.debug(
                "reconnect in %ss (attempt=%s ready_seen=%s fails=%s port_resets=%s)",
                delay,
                attempt,
                self._ready_seen,
                transport.fail_streak,
                transport.port_resets,
            )
            # 退避期间不算"挂死"，否则 watchdog 会把正常的指数退避掐掉；
            # 同时清掉"正在连接"标记（现在是纯等待）。
            self._client_started_at = time.monotonic()
            self._connect_started_at = 0.0
            if await self._sleep_or_resume(delay):
                # 睡醒了：退避作废，立刻重来一轮（transport 已被标脏，
                # 下一次 connect 会先做栈重置）
                log.warning("resume during backoff: reconnecting immediately")
                attempt = 0

    async def _sleep_or_resume(self, delay: float) -> bool:
        """退避等待；期间系统唤醒会**立刻**返回 True。

        没有这个的话，唤醒后最长还要等满 30s 退避才重新尝试，而用户看到的
        就是"唤醒后一直连不上"。
        """
        if self._resume_event.is_set():
            # 唤醒发生在上一轮连接/扫描里（事件已经置上）：这一轮退避直接跳过。
            # 注意**不能**无条件先 clear —— 那会把刚发生的唤醒信号吃掉，
            # 于是还要把退避等完（真机 3~30s）。
            self._resume_event.clear()
            return True
        try:
            await asyncio.wait_for(self._resume_event.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        self._resume_event.clear()
        return True

    def _make_psk_provider(self, network: Network | None, password: str | None, eph_id: str, eph_key: bytes | None):
        auth = network.auth if network is not None else None
        network_name = (network.host_name if network is not None else None) or "Host"
        network_id = network.network_id if network is not None else ""
        stored = self._stored_psk(network)

        async def provider(nonce_c: bytes, salt: bytes, iters: int, use_eph: bool) -> bytes | None:
            if use_eph:
                if eph_key is None:
                    self.window.show_hint("Host 要求临时密钥，请用临时密钥重新加入。")
                    return None
                return eph_key
            if stored is not None:
                return stored
            if network_id and self._auth_gate(network_id) > 0:
                self.window.show_hint("密码错误次数过多，请 30 秒后重试。")
                return None
            current = self._join_password
            for _ in range(3):
                if not current:
                    result = await self._modal(
                        lambda: PairingDialog.ask(
                            self.window,
                            mode="join",
                            auth=auth,
                            network_name=network_name,
                            gate=(lambda: self._auth_gate(network_id)) if network_id else None,
                            on_result=(
                                (
                                    lambda ok: (
                                        self._auth_succeeded(network_id)
                                        if ok
                                        else self._auth_failed(network_id)
                                    )
                                )
                                if network_id
                                else None
                            ),
                        )
                    )
                    if result is None:
                        return None
                    current, remember = result
                    self._join_password = current
                    if remember and network is not None:
                        self._passwords[network.network_id] = (current, time.time())
                if auth is None:
                    psk = crypto.derive_psk(current, salt, iters)
                    self._first_join = (current, bytes(salt), int(iters), psk)
                    return psk
                if crypto.verify_password(current, auth):
                    if network_id:
                        self._auth_succeeded(network_id)
                    psk = crypto.psk_from_password(current, auth)
                    if remember:
                        self._remember_psk(network_id, psk)
                    return psk
                if network_id:
                    self._auth_failed(network_id)
                self.window.show_hint("密码错误，请重试。")
                current = ""
                self._join_password = None
            return None

        return provider

    # ------------------------------------------------------------- session callbacks
    def _wire_session(self, session: PeerSession) -> None:
        session.on_state = self._on_state
        session.on_message = self._on_message
        session.on_progress = self._on_progress
        session.on_error = self._on_error
        session.on_closed = self._on_closed
        session.on_peers = self._on_peers
        session.on_resync = self._on_resync

    def _on_resync(self, session: PeerSession) -> None:
        """对端刚进入 READY，要求我方无条件刷新一次状态（修"单方已连接"）。

        只靠各自的 `on_state` 兜不住：一侧重启/睡眠唤醒后，另一侧可能停在旧状态
        （READY 却按钮灰 / CLOSED 却没重连），要等下一次 20s PING 才被发现。
        这里不弹提示、只做幂等刷新，Host 侧的在线名单由 session 层一并重发。
        """
        log.info(
            "resync from %s (state=%s)",
            session.transport.peer_id,
            session.state.value,
        )
        if session.state is State.READY and not session.is_host:
            self._ready_seen = True
            self._connected_once = True
            self._client_started_at = time.monotonic()
            self._badge_override = None
        self._sync_ui()

    def _on_state(self, session: PeerSession) -> None:
        state = session.state
        if state is State.READY:
            if not session.is_host:
                self._ready_seen = True
                self._connected_once = True
                self._client_started_at = time.monotonic()
                self._badge_override = None
                self.window.show_hint(f"已连接 {session.peer_name}")
                self._finalize_first_join(session)
                self._adopt_host_alias(session)
            else:
                self.window.show_hint(f"{session.peer_name} 已加入")
        elif state in HANDSHAKE_STATES and not session.is_host:
            if self._badge_override is None:
                self._badge_override = ("handshake", "HANDSHAKE")
        elif state is State.AUTH_FAIL and not session.is_host:
            self._badge_override = ("error", "AUTH_FAIL")
            self.window.show_hint("认证失败：请检查密码，重连时会重新输入。")
            self._join_password = None
            self._spawn(session.close("auth failed"))
        self._sync_ui()
        self._refresh_peers(session)

    def _refresh_peers(self, session: PeerSession) -> None:
        if session.is_host and self._host is not None:
            names = [name or pid for pid, name in self._host.peer_names()]
            self.window.set_peers(names)

    def _on_peers(self, session: PeerSession, peers: list[tuple[str, str]]) -> None:
        self.window.set_peers([name or pid for pid, name in peers])

    def _on_message(self, session: PeerSession, msg_id: int, kind: str, payload: bytes) -> None:
        created = self._now()
        content = payload
        file_name = file_size = mime_type = local_path = ""
        if kind == FILE:
            try:
                meta, blob = protocol.FileMeta.decode(payload)
            except Exception as exc:
                log.warning("bad file payload from %s: %s", session.transport.peer_id, exc)
                self.window.show_hint("收到无法解析的文件消息。")
                return
            content = blob
            file_name = meta.filename
            file_size = int(meta.size or len(blob))
            mime_type = meta.mime
            local_path = self._store_received_file(msg_id, file_name, blob)

        item = make_item(
            msg_id=msg_id,
            direction="in",
            peer_id=session.transport.peer_id,
            peer_name=session.peer_name or "对方",
            kind=kind,
            content=content,
            created_at=created,
            file_name=file_name,
            file_size=file_size,
            mime_type=mime_type,
            local_path=local_path,
        )
        self.window.chat.append(item, force=True)
        self.history.add(
            msg_id=msg_id,
            network_id=session.network_id,
            direction="in",
            kind=kind,
            content=content,
            peer_id=session.transport.peer_id,
            peer_name=session.peer_name,
            created_at=created,
            file_name=file_name or None,
            file_size=file_size or None,
            mime_type=mime_type or None,
            local_path=local_path or None,
        )
        label = {IMAGE: "图片", FILE: f"文件 {file_name}"}.get(kind, "消息")
        log.info(
            "recv msg_id=%s kind=%s from=%s bytes=%s",
            msg_id,
            kind,
            session.transport.peer_id,
            len(content),
        )
        self.window.show_hint(f"收到 {session.peer_name} 的{label}")

    def _store_received_file(self, msg_id: int, name: str, blob: bytes) -> str:
        import re
        import shutil

        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "").strip() or "file"
        path = self.files_dir / f"{msg_id}-{safe}"
        try:
            path.write_bytes(blob)
        except OSError as exc:
            log.warning("store received file failed: %s", exc)
            return ""
        if self.config.auto_save_files and self.config.receive_dir:
            try:
                dest_dir = Path(self.config.receive_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / safe
                if dest.exists():
                    dest = dest_dir / f"{msg_id}-{safe}"
                shutil.copy2(path, dest)
            except OSError as exc:
                log.warning("auto save failed: %s", exc)
        return str(path)

    def _on_progress(self, session: PeerSession, msg_id: int, acked: int, total: int) -> None:
        key = (session.transport.peer_id, msg_id)
        now = time.monotonic()
        finished = bool(total) and acked >= total
        # 每个 ACK 都 touch 气泡 + `_sync_ui()`（重建目标菜单）会把 server 端
        # UI 拖死：一次 4MB 传输 ≈ 8500 个 ACK。限流到 10Hz。
        last = self._progress_ui_at.get(key, 0.0)
        paint = finished or (now - last) >= 0.1
        for bubble_key in self._pending.get(key, []):
            row = self.window.chat.chat_model.row_of(bubble_key)
            if row < 0:
                continue
            item = self.window.chat.chat_model.item_at(row)
            if item is None:
                continue
            item.progress = (acked, total)
            if finished:
                item.progress = None
                item.status = ""
            if paint:
                self.window.chat.chat_model.touch(bubble_key)
        if not paint:
            return
        self._progress_ui_at[key] = now
        busy_before = bool(self._pending)
        if finished:
            self._pending.pop(key, None)
            self._progress_ui_at.pop(key, None)
        # 收尾统一交给 `_sync_ui()`：`_pending` 从"有"变"没有"的那一次，
        # 它会算出 busy=False 并 `progress.reset()`（隐藏进度环）。
        # 以前这里额外调 `finish_progress()`（= `show_progress(1,1)`）想做个
        # "满格再消失"的效果，但它只是把环**显示**出来，
        # 紧接着 `set_busy(False)` 又把它 reset —— 时序一乱环就卡在屏幕上。
        if busy_before != bool(self._pending):
            self._sync_ui()

    def _on_error(self, session: PeerSession, code: Err, message: str) -> None:
        log.warning("session error %s: %s", code.name, message)
        if code is Err.UNKNOWN and _looks_like_link_loss(message):
            # 掉线不是"错误"：以前这里会把
            # `UNKNOWN：peer BluetoothLE#… not subscribed to 0000a003-…` 直接糊到界面上，
            # dev 反馈"应该简单提示掉线即可"
            self.window.show_hint("对方已离线，正在重连…")
        else:
            self.window.show_hint(f"{error_text(int(code))}：{message}")
        if code in (Err.TOO_MANY_RETRIES, Err.CRC_MISMATCH, Err.MSG_TOO_LARGE):
            self._fail_pending(session)
        elif code in (Err.NOT_READY, Err.UNKNOWN) and not session.ready:
            # 发送过程中链路挂了：pending 不清的话按钮永远灰、进度环卡半圈
            self._fail_pending(session)
        if code in (Err.AUTH_FAILED, Err.EPH_EXPIRED, Err.EPH_NOT_FOUND) and not session.is_host:
            self._set_badge_override(("error", code.name))

    def _on_closed(self, session: PeerSession, reason: str) -> None:
        log.info("session closed: %s (%s)", session.transport.peer_id, reason)
        self._fail_pending(session)
        self._pending = {
            key: value for key, value in self._pending.items() if key[0] != session.transport.peer_id
        }
        self._progress_ui_at = {
            key: ts
            for key, ts in self._progress_ui_at.items()
            if key[0] != session.transport.peer_id
        }
        if not session.is_host and not self._stopping and not self._closing:
            self._badge_override = ("error", "CLOSED")
            self.window.show_hint(f"连接断开：{reason}")
        self._sync_ui()
        self._refresh_peers(session)

    def _fail_pending(self, session: PeerSession) -> None:
        model = self.window.chat.chat_model
        peer_id = session.transport.peer_id
        for key in [k for k in self._pending if k[0] == peer_id]:
            self._progress_ui_at.pop(key, None)
            for bubble_key in self._pending.pop(key, []):
                row = model.row_of(bubble_key)
                item = model.item_at(row) if row >= 0 else None
                if item is not None:
                    item.status = "failed"
                    item.progress = None
                    model.touch(bubble_key)
        # 不要再调 `finish_progress()`：它是 `show_progress(1,1)`，
        # 会把进度环**留在屏幕上**（全满、一直 visible），dev 报的正是这个。
        # busy 状态统一由 `_sync_ui()` 重算 —— `_pending` 空了就 `set_busy(False)`
        # → `progress.reset()` 隐藏。
        self._sync_ui()

    def _finalize_first_join(self, session: PeerSession) -> None:
        if self._first_join is None:
            return
        password, salt, iters, psk = self._first_join
        self._first_join = None
        address = session.transport.peer_id
        existing = self.networks.find_by_host(address, SERVICE_UUID)
        auth = {
            "salt": base64.b64encode(salt).decode("ascii"),
            "iters": iters,
            "verifier": base64.b64encode(crypto.psk_verifier(psk)).decode("ascii"),
        }
        net = existing or Network(
            network_id=str(uuid.uuid4()),
            host_address=address.upper(),
            host_name=session.peer_name or address,
            service_uuid=SERVICE_UUID,
        )
        net.host_address = address.upper()
        net.host_name = session.peer_name or net.host_name
        net.peer_alias = session.peer_name or ""
        net.auth = auth
        net.last_joined_at = self._now()
        self.networks.upsert(net)
        self.networks.save()
        self._network = net
        self._remember_network_id(net.network_id)
        self._passwords[net.network_id] = (password, time.time())
        self._remember_psk(net.network_id, psk)
        self._adopt_network_id(session, net)
        self.window.show_hint(f"已保存网络 {net.host_name}（下次可本地校验密码）")

    def _adopt_network_id(self, session: PeerSession, net: Network) -> None:
        """把会话/历史里临时的 `ad-hoc:{地址}` 归一到真实 network_id。"""
        if session.network_id == net.network_id:
            return
        old = session.network_id
        session.network_id = net.network_id
        try:
            moved = self.history.remap_network(old, net.network_id)
        except Exception as exc:
            log.warning("remap failed: %s", exc)
            return
        if moved:
            log.info("session history remapped %s rows: %s -> %s", moved, old, net.network_id)

    def _adopt_host_alias(self, session: PeerSession) -> None:
        """把 Host 经 AUTH_OK 下发的昵称落到界面与 networks.json。

        client 侧原先只有 BLE 广播名（Windows 设备名/代号）可用 ——
        协议里没有反向携带 Host 昵称的字段，所以状态栏/气泡头一直是代号。
        """
        alias = (session.peer_name or "").strip()
        if not alias:
            return
        net = self._network
        if net is not None:
            changed = False
            if net.host_name != alias:
                net.host_name = alias
                changed = True
            if net.peer_alias != alias:
                net.peer_alias = alias
                changed = True
            if changed:
                self.networks.upsert(net)
                self.networks.save()
        self.window.set_network(alias, session.transport.peer_id or "")

    # ------------------------------------------------------------- send
    def _ready_targets(self) -> list[str] | None:
        if self._mode == "host":
            if self._host is None or not self._host.ready_sessions:
                return []
            return self.window.drop.selected_targets
        if self._client_session is not None and self._client_session.ready:
            return [self._client_session.transport.peer_id]
        return []

    def _on_send(self) -> None:
        text = self.window.drop.text()
        files = self.window.drop.take_files()
        if not text and not files:
            self.window.show_hint("请输入消息或拖入文件。")
            return
        self.window.drop.clear()
        self._dispatch(text.strip() if text else "", files)

    def _on_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if not text or not text.strip():
            self.window.show_hint("剪贴板没有文本。")
            return
        self._dispatch(text.strip(), [])

    def _dispatch(self, text: str, files: list[tuple[str, bytes, str, str]]) -> None:
        """jobs = [(kind, name, data, mime, path)]"""
        jobs: list[tuple[str, str, bytes, str, str]] = []
        if text:
            jobs.append((TEXT, "", text.encode("utf-8"), "text/plain", ""))
        for name, data, mime, path in files:
            kind = IMAGE if name.lower().endswith(IMAGE_SUFFIX) else FILE
            jobs.append((kind, name, data, mime, path))

        if self._mode == "host" and self._host is not None:
            targets = self._ready_targets()
            if targets == []:
                self.window.show_hint("还没有对端连接，消息未发送。")
                return
            names = dict(self._host.peer_names())
            network_id = self._network.network_id if self._network else ""
            sent = 0
            for kind, name, data, mime, path in jobs:
                try:
                    results = self._host_send_one(self._host, targets, kind, name, data, mime)
                except Exception as exc:
                    self.window.show_hint(f"发送失败：{exc}")
                    continue
                for peer_id, msg_id in results:
                    self._append_out(
                        peer_id,
                        names.get(peer_id, peer_id),
                        kind,
                        data,
                        msg_id,
                        network_id,
                        name=name,
                        mime=mime,
                        path=path,
                    )
                    sent += 1
            if not sent:
                self.window.show_hint("发送失败：没有就绪的连接。")
            return

        session = self._client_session
        if session is None or not session.ready:
            self.window.show_hint("尚未连接，消息未发送。")
            return
        for kind, name, data, mime, path in jobs:
            try:
                results = self._client_send_one(session, kind, name, data, mime)
            except Exception as exc:
                self.window.show_hint(f"发送失败：{exc}")
                continue
            for _peer_id, msg_id in results:
                self._append_out(
                    session.transport.peer_id,
                    session.peer_name,
                    kind,
                    data,
                    msg_id,
                    session.network_id,
                    name=name,
                    mime=mime,
                    path=path,
                )

    @staticmethod
    def _make_meta(name: str, data: bytes, mime: str) -> protocol.FileMeta:
        return protocol.FileMeta(
            filename=name or "file", mime=mime or "application/octet-stream", size=len(data)
        )

    @classmethod
    def _host_send_one(cls, host, targets, kind: str, name: str, data: bytes, mime: str):
        if kind == TEXT:
            return host.send_text(targets, data.decode("utf-8"))
        if kind == IMAGE:
            return host.send_image(targets, data)
        return host.send_file(targets, data, cls._make_meta(name, data, mime))

    @classmethod
    def _client_send_one(cls, session, kind: str, name: str, data: bytes, mime: str):
        if kind == TEXT:
            return [(session.transport.peer_id, session.send_text(data.decode("utf-8")))]
        if kind == IMAGE:
            return [(session.transport.peer_id, session.send_image(data))]
        return [(session.transport.peer_id, session.send_file(data, cls._make_meta(name, data, mime)))]

    def _append_out(
        self,
        peer_id: str,
        peer_name: str,
        kind: str,
        data: bytes,
        msg_id: int,
        network_id: str,
        *,
        name: str = "",
        mime: str = "",
        path: str = "",
    ) -> None:
        created = self._now()
        is_file = kind == FILE
        item = make_item(
            msg_id=msg_id,
            direction="out",
            peer_id=peer_id,
            peer_name=peer_name or "对方",
            kind=kind,
            content=b"" if is_file else data,
            created_at=created,
            status="sending",
            progress=(0, 1),
            file_name=name,
            file_size=len(data) if is_file else 0,
            mime_type=mime,
            local_path=path,
        )
        self.window.chat.append(item)
        self.history.add(
            msg_id=msg_id,
            network_id=network_id or f"ad-hoc:{peer_id.upper()}",
            direction="out",
            kind=kind,
            content=b"" if is_file else data,
            peer_id=peer_id,
            peer_name=peer_name,
            created_at=created,
            file_name=name or None,
            file_size=len(data) if is_file else None,
            mime_type=mime or None,
            local_path=path or None,
        )
        self._pending.setdefault((peer_id, msg_id), []).append(item.key)
        self._sync_ui()
        self.window.set_progress(0, 1)

    def _delete_item(self, item) -> None:
        self.window.chat.remove(item.key)
        if self._network is not None:
            self.history.delete_by_msg_id(self._network.network_id, item.msg_id)
        self.window.show_hint("已删除该消息（本地）")

    # ------------------------------------------------------------- dialogs
    def _on_eph_clicked(self) -> None:
        if self._mode != "host" or self._host is None:
            QMessageBox.information(self.window, "生成临时密钥", "请先切换到 Host 模式并开始广播。")
            return
        eph_id, key = crypto.new_ephemeral()
        expires_at = self.eph.create(key, eph_id, ttl=EPH_TTL)
        name = self._network.host_name if self._network is not None else self.device_name
        EphKeyDialog.show_key(
            self.window, eph_id=eph_id, key=key, expires_at=expires_at, network_name=name
        )

    def _open_history(self) -> None:
        # 聊天区能看到的，历史窗口也必须能看到：`last_network_id` 换号 / 还没连上时，
        # 按 `_network`、`last_network_id` 查会是空的 —— dev 报的
        # 「聊天区还有历史消息，历史却是空的」就是这么来的。
        first = self._network.network_id if self._network else None
        HistoryDialog(self.history, self._pick_history_network_id(first), self.window).exec()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self.identity.alias, self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.apply()
        alias = dialog.new_alias
        alias_changed = alias != self.identity.alias
        if alias_changed:
            self.identity.alias = alias
            save_identity(self.identity, self.root)
            self.device_name = display_name(self.identity)
            self.networks.set_identity(self.identity.device_id, self.identity.name)
            self.networks.save()
            self._apply_local_name()
        self.config.save()
        self._apply_log_level()
        self.window.set_emoji_enabled(self.config.enable_emoji)
        if result.theme_changed:
            dark = apply_theme(
                self.qapp, None if self.config.theme == "auto" else self.config.theme == "dark"
            )
            self.window.apply_colors(DARK if dark else LIGHT)
        if alias_changed:
            if self._mode == "host" and self._host is not None:
                self.window.show_hint(
                    f"设置已保存。本机别名已改为「{self.device_name}」，状态栏即时生效。"
                )
            else:
                self.window.show_hint(
                    "设置已保存。别名将在下次连接时生效（切换模式或重新“加入网络…”）。"
                )
        else:
            self.window.show_hint("设置已保存。")

    def _apply_log_level(self) -> None:
        """把 config 里的 `log_level` 立刻生效（不用重启）。"""
        level = resolve_level(self.config.log_level)
        logging.getLogger("blechat").setLevel(level)
        logging.getLogger().setLevel(level)
        for handler in logging.getLogger().handlers:
            handler.setLevel(level)
        log.info("log level -> %s", logging.getLevelName(level))

    def _apply_local_name(self) -> None:
        """把新别名同步到正在运行的 Host / 会话 / 网络记录 / 状态栏。

        以前只改了 `self.device_name`，host 侧 `HostService.local_name`、
        `Network.host_name` 和状态栏都还是旧值 → "server 端改昵称没效果"。
        """
        if self._host is not None:
            self._host.local_name = self.device_name
            for session in self._host.sessions.values():
                session.local_name = self.device_name
        if self._mode != "host" or self._network is None:
            return
        self._network.host_name = self.device_name
        self.networks.upsert(self._network)
        self.networks.save()
        self.window.set_network(self.device_name, self._host_address)

    # ------------------------------------------------------------- tray / lifecycle
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    def _show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _switch_mode_from_tray(self) -> None:
        self._show_window()
        self.window.mode_combo.showPopup()

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._save_geometry()
        self.window.force_close()
        self._spawn(self._shutdown())

    async def _shutdown(self) -> None:
        try:
            await self._stop_all()
        except Exception as exc:
            log.warning("shutdown stop failed: %s", exc)
        self.history.close()
        if self._tray is not None:
            self._tray.hide()
        self.qapp.quit()

    def on_window_hidden_to_tray(self) -> None:
        self._save_geometry()
        if not self._tray_notified and self._tray is not None:
            self._tray_notified = True
            # self._tray.showMessage(
                # "BLE Chat", "已最小化到托盘，双击托盘图标恢复窗口。", QSystemTrayIcon.MessageIcon.Information, 4000
            # )

    # ------------------------------------------------------------- housekeeping
    def _on_minute_tick(self) -> None:
        """每 15s 一跳；每 4 跳跑一次分钟级维护，每 4 分钟打一次诊断快照。

        顺带充当**睡眠检测**：心跳之间墙钟跳了 45s+ ⇒ 这台机器刚才被挂起过。
        用墙钟而不是 monotonic/QTimer，是因为 Windows 睡眠时计时基准的行为
        不一致，而墙钟一定会跨过睡眠。
        """
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        gap = self._stall.tick()
        if gap:
            self._on_stall_resume(gap)
        if self._tick_count % 4 == 1:
            self._housekeeping()
        if self._tick_count % 16 == 1:
            self._diagnostic_snapshot()

    def _on_stall_resume(self, gap: float) -> None:
        """系统刚从睡眠/挂起回来：把蓝牙栈标记为脏，并立刻重启链路。

        为什么必须**主动**做：睡眠期间进程里的 Python 对象原样保留，但底下的
        蓝牙栈（radio、GATT 会话、设备对象）已经全变了。唤醒后不清理的话，
        真机表现是 `connect()` 只用 1~2s 就"连上"，GATT 表却是旧的/空的，
        连续 4 次都这样；而**重启进程立刻正常** —— 脏状态在栈里。
        """
        self._last_stall_gap = gap
        self._resume_at = time.monotonic()
        self._radio_reset_tried = False
        log.warning(
            "system resume detected (wall-clock gap %.0fs); mode=%s -> resetting ble stack",
            gap,
            self._mode,
        )
        if self._mode == "host":
            self.window.show_hint("系统已唤醒，正在重建广播…")
            self._spawn(self._revive_host(hard=True))
            return
        if self._mode != "join":
            return
        transport = self._client_transport
        task = self._client_task
        if (
            transport is not None
            and task is not None
            and not task.done()
            and hasattr(transport, "mark_resume")
        ):
            transport.mark_resume(reason=f"system resume (gap {gap:.0f}s)")
        # 睡着之前 READY 的那条链路**一定**已经死了（链路监督超时 ~20~30s，
        # 而我们的检测阈值是 45s）。主动关掉它，让连接循环立刻重连 ——
        # 不关的话要等 keepalive 3 次未应答（60~125s）才发现，用户看到的就是
        # "唤醒后一直连不上"。
        session = self._client_session
        if session is not None and session.state is not State.CLOSED:
            log.warning(
                "resume with session in state=%s: closing it to force a clean reconnect",
                session.state.value,
            )
            self._spawn(self._close_after_resume(session))
        else:
            self.window.show_hint("系统已唤醒，正在重置蓝牙链路并重连…")
        # 退避中的循环立刻醒过来（否则最长还要等 30s 才重新尝试）
        self._resume_event.set()

    async def _close_after_resume(self, session: PeerSession) -> None:
        """唤醒后主动关掉睡着前那条链路，然后给出提示。

        提示放在 `close()` **之后**：`close()` 会触发 `_on_closed`，那里会刷一条
        "连接断开：…"，先提示就会被它盖掉。
        """
        try:
            await session.close("系统唤醒")
        except Exception as exc:  # pragma: no cover - close 自己吞异常
            log.debug("close after resume failed: %s", exc)
        self.window.show_hint("系统已唤醒，正在重置蓝牙链路并重连…")

    def _check_resume_stall(self) -> None:
        """唤醒后拖太久（`RESUME_STALL_SECONDS`）还没连上 ⇒ 栈没自己回来。

        这时才动用最后手段（自动重置蓝牙 radio，见 `_reset_radio`）—— 或者，
        在没权限/没开自动重置时，明确告诉用户按哪个按钮。
        """
        if not self._resume_at:
            return
        waited = time.monotonic() - self._resume_at
        if waited > RESUME_WINDOW_SECONDS:
            self._resume_at = 0.0
            return
        if waited < RESUME_STALL_SECONDS:
            return
        if self._mode == "host":
            server = self._server
            if server is not None and server.advertising_ok:
                self._resume_at = 0.0
                return
        else:
            session = self._client_session
            if session is not None and session.ready:
                self._resume_at = 0.0
                return
        if self._radio_reset_tried:
            return
        self._radio_reset_tried = True
        log.warning("still not connected %.0fs after resume -> resetting bluetooth radio", waited)
        self._spawn(self._reset_radio(auto=True))

    def _open_radio_reset(self) -> None:
        """Ctrl+R / 托盘菜单：“重置蓝牙适配器”。先问一句再动手。"""
        self._spawn(self._confirm_radio_reset())

    async def _confirm_radio_reset(self) -> None:
        answer = await self._modal(
            lambda: QMessageBox.question(
                self.window,
                "重置蓝牙适配器",
                "将尝试关闭再打开蓝牙适配器（约 5 秒，期间蓝牙设备会短暂断开）。\n\n"
                "本程序通常没有这个权限，那时会打开系统蓝牙设置页，"
                "请手动把蓝牙关掉再打开。\n\n继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        )
        if answer == QMessageBox.StandardButton.Yes:
            await self._reset_radio()

    async def _reset_radio(self, *, auto: bool = False) -> None:
        """关掉再打开蓝牙适配器 —— 唤醒后连不上的**最后手段**。

        本机实测未打包的 Win32 程序调 `Radio.set_state_async` 会返回
        `DENIED_BY_USER`，所以这条路经常走不通；走不通时就把 Windows 的
        蓝牙设置页打开，让用户自己关一下开关（等价于一次彻底重置）。
        """
        state = await radio_state()
        self.window.show_hint(f"正在重置蓝牙适配器…（当前 {state}）")
        ok, detail = await cycle_radio()
        log.warning("radio reset (auto=%s): ok=%s %s", auto, ok, detail)
        if ok:
            self.window.show_hint("蓝牙适配器已重置，正在重连…")
            if self._mode == "host":
                await self._revive_host(hard=True)
            else:
                await self._restart_client(force=True)
            return
        if auto:
            self.window.show_hint(
                "唤醒后蓝牙栈没恢复，需要手动重置：请按 Ctrl+R（或点“重置蓝牙”）。"
            )
            return
        opened = open_bluetooth_settings()
        self.window.show_hint(
            "本程序没有修改蓝牙开关的权限，已打开系统蓝牙设置：请把蓝牙关掉再打开。"
            if opened
            else "本程序没有修改蓝牙开关的权限：请到 Windows 设置里把蓝牙关掉再打开。"
        )


    def _diagnostic_snapshot(self) -> None:
        """定期把「当前链路/会话/历史」状态打进日志（INFO）。

        真机问题（掉线、单方已连接、历史看着被清空）都是**时序**问题：
        事后只看到零散 WARNING，拼不出"当时到底什么状态"。这里每 4 分钟留一行
        完整现场，出问题的时间点前后直接对照即可。
        """
        try:
            parts = [f"mode={self._mode}", f"reachable={self._ready_peer_ids()}"]
            if self._mode == "host":
                if self._server is not None:
                    parts.append(f"adv={self._server.advertising_status}")
                    parts.append(f"sessions={self._server.session_count}")
                if self._host is not None:
                    parts.append(f"ready={len(self._host.ready_sessions)}")
            else:
                session = self._client_session
                parts.append(f"session={session.state.value if session else None}")
                task = self._client_task
                parts.append(
                    "client_task="
                    + ("none" if task is None else ("done" if task.done() else "running"))
                )
                parts.append(f"ready_seen={self._ready_seen}")
                parts.append(f"override={self._badge_override}")
                transport = getattr(session, "transport", None)
                if transport is not None and hasattr(transport, "state_snapshot"):
                    parts.append(transport.state_snapshot())
            parts.append(f"pending={len(self._pending)}")
            if self._resume_at:
                parts.append(f"resume_wait={time.monotonic() - self._resume_at:.0f}s")
            if self._last_stall_gap:
                parts.append(f"last_stall={self._last_stall_gap:.0f}s")
            parts.append(f"last_network_id={self.config.last_network_id}")
            if self._network is not None:
                parts.append(f"peer_addr={self._network.host_address}")
            parts.append(f"history={self.history.count_by_network()}")
            log.info("state: %s", " ".join(parts))
        except Exception as exc:  # 诊断代码绝不能影响主流程
            log.debug("snapshot failed: %s", exc)

    def _housekeeping(self) -> None:
        local = time.localtime()
        if local.tm_hour == 3 and local.tm_min == 0 and self._purge_day != local.tm_yday:
            removed = self.history.purge()
            self._purge_day = local.tm_yday
            if removed:
                log.info("history purge: %s rows", removed)
            removed = purge_old_logs(self.root / "logs")
            if removed:
                log.info("log purge: %s files", removed)
        if local.tm_min == 0 and self._purge_hour != local.tm_hour:
            removed = self.eph.purge_expired()
            self._purge_hour = local.tm_hour
            if removed:
                log.info("eph purge: %s entries", removed)

    # ------------------------------------------------------------- misc
    @staticmethod
    def hint_color() -> str:
        return LIGHT["muted"]
