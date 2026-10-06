from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from .theme import LIGHT
from .widgets.chat_view import ChatView
from .widgets.drop_area import DropArea
from .widgets.status_badge import StatusBadge
from .widgets.title_bar import TitleBar

# 窗口四周留白：自绘的圆角边框画在这 8px 里的第 4px 上，子控件从第 8px 开始。
WINDOW_MARGIN = 8

# 边缘可拖动改尺寸的感应宽度（逻辑像素）。取 8 而不是 4：自绘边框在 4px 处，
# 8px 的感应带把它包在中间，用户"看着边框拖"就一定命中。
RESIZE_MARGIN = 8

MODES = (("host", "Host"), ("join", "Join"))

_ui_log = logging.getLogger("blechat.ui")

# ---------------------------------------------------------------- Win32 常量
#
# 无边框窗口在 Windows 上是 `WS_POPUP`（Qt 会去掉 `WS_CAPTION|WS_THICKFRAME`），
# 于是"拖边框改尺寸"在**系统层面**根本不存在。要把它拿回来，需要三件套：
#   1. `WS_THICKFRAME` —— 让窗口变成"可调整大小"的（否则 DefWindowProc 收到
#      `SC_SIZE` 也不会进入调整循环，`startSystemResize()` 同样返回失败）；
#   2. `WM_NCCALCSIZE` 返回"客户区 = 整窗" —— 抵消 THICKFRAME 带来的非客户区边框，
#      否则整个界面会凭空向内缩一圈；
#   3. `WM_NCHITTEST` 在边缘回 `HTLEFT/HTTOP…` —— 光标形状和拖动都由系统接管。
#
# 第 3 条还顺带解决了本窗口最根本的一个坑：窗口是 `WA_TranslucentBackground`
# （`WS_EX_LAYERED`），**完全透明的像素是穿透的**（点击会落到桌面/别的窗口上）。
# 自绘边框之外那 4px 正好是全透明 —— 所以纯靠 Qt 鼠标事件的改尺寸方案
# 在那一圈永远收不到事件，"鼠标移到边界没有反应"就是这个原因。
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSG(ctypes.Structure):
    """Win32 `MSG`。

    字段类型必须能装下 64 位：`wParam` 是 `WPARAM`（UINT_PTR）、`lParam` 是
    `LPARAM`（LONG_PTR）。以前用 `c_uint` 读 `lParam` 会把屏幕坐标截断成低 32 位，
    `WM_NCHITTEST` 的判断就全错了。
    """

    _fields_ = [
        ("hWnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", _POINT),
    ]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


def _user32():
    return ctypes.windll.user32


def _native_ok() -> bool:
    """能不能碰 Win32 窗口句柄。

    `pytest` 里跑的是 `QT_QPA_PLATFORM=offscreen`：那时 `winId()` 是个假句柄，
    拿它去调 `GetWindowLongPtrW` 轻则无效、重则误伤真窗口。所以只在
    "Windows 平台 + windows 插件" 时才走原生那条路。
    """
    if sys.platform != "win32":
        return False
    try:
        return QApplication.platformName() == "windows"
    except Exception:  # noqa: BLE001
        return False


def _is_windows_msg(eventType) -> bool:
    """`nativeEvent` 的第一个参数是不是 Windows 消息类型。

    ⚠️ PyQt6 传进来的是 `QByteArray`：它既不等于 `b"windows_generic_MSG"`，
    也不等于同名的 `str`。必须显式转一次才算得对。
    """
    try:
        return bytes(eventType) == b"windows_generic_MSG"
    except Exception:  # noqa: BLE001
        return eventType == "windows_generic_MSG"


def _paint_rounded_background(
    widget: QWidget, colors: dict[str, str], margined: bool = True
) -> None:
    """窗口的背景（原 `MainWindow.paintEvent` 的绘制体）。

    单独提出来是为了让它能被 try/except 包住 —— 绘制里抛异常会把进程带走。

    `margined=False`（最大化）时铺满整窗、直角、不描边：最大化后窗口四边贴着
    屏幕，再留 8px 白边 + 圆角就会露出底下的桌面，很难看。
    """
    painter = QPainter(widget)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(colors["bg"]))
        if margined:
            rect = QRect(widget.rect()).adjusted(4, 4, -4, -4)
            painter.setPen(QPen(QColor(colors["border"]), 1))
            painter.drawRoundedRect(rect, 12, 12)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(widget.rect())
    finally:
        painter.end()


class MainWindow(QWidget):
    """无边框主窗口：标题栏 / 状态卡片 / 聊天区 / 输入区 / 提示栏。"""

    modeChangeRequested = pyqtSignal(str)
    joinRequested = pyqtSignal()
    ephRequested = pyqtSignal()
    historyRequested = pyqtSignal()
    sendRequested = pyqtSignal()
    clipboardRequested = pyqtSignal()
    exitRequested = pyqtSignal()
    closedToTray = pyqtSignal()
    settingsRequested = pyqtSignal()
    radioResetRequested = pyqtSignal()
    windowActivated = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("MainWindow")
        self.setWindowTitle("BLE Chat")
        self.setMinimumSize(720, 520)
        self.resize(900, 640)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._colors = LIGHT
        self._close_to_tray = True
        self._allow_close = False
        self._resizing = False
        self._resize_edge = 0
        self._drag_origin = QPoint()
        self._start_geometry = self.geometry()
        # 最大化用**自己管**的状态，不交给 Windows（原因见 `set_maximized`）
        self._maximized = False
        self._normal_geometry: QRect | None = None
        self._native_ready = False

        root = QVBoxLayout(self)
        root.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
        root.setSpacing(0)

        self.title_bar = TitleBar("BLE Chat", self)
        self.title_bar.setMinimumHeight(32)
        self.title_bar.minimizeClicked.connect(self.showMinimized)
        self.title_bar.maximizeClicked.connect(self.toggle_maximized)
        self.title_bar.closeClicked.connect(self.close)
        root.addWidget(self.title_bar)

        content = QWidget(self)
        content.setObjectName("MainWindow")
        content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root.addWidget(content, 1)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addWidget(self._build_status_card())

        self.chat = ChatView(content)
        self.chat.setMinimumHeight(160)
        layout.addWidget(self.chat, 1)

        self.drop = DropArea(content)
        self.drop.setMinimumHeight(150)
        layout.addWidget(self.drop)

        self.footer = QLabel("", content)
        self.footer.setObjectName("StatusLabel")
        self.footer.setFixedHeight(20)
        layout.addWidget(self.footer)

        self.drop.sendRequested.connect(self.sendRequested)
        self.drop.clipboardRequested.connect(self.clipboardRequested)
        self.drop.hint.connect(self.show_hint)
        self.chat.copied.connect(self.show_hint)

    # ------------------------------------------------------------- status card
    def _build_status_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(12, 8, 12, 8)
        box.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self.mode_combo = QComboBox(card)
        self.mode_combo.setFixedWidth(96)
        for _, label in MODES:
            self.mode_combo.addItem(label)
        self.mode_combo.setToolTip("选择运行模式（切换前会确认）")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_index)
        row1.addWidget(self.mode_combo)

        self.net_label = QLabel("未连接", card)
        self.net_label.setObjectName("NetLabel")
        row1.addWidget(self.net_label)

        self.addr_label = QLabel("—", card)
        self.addr_label.setObjectName("AddrLabel")
        row1.addWidget(self.addr_label)

        self.peer_strip = QFrame(card)
        self.peer_strip.setObjectName("PeerStrip")
        self.peer_strip_layout = QHBoxLayout(self.peer_strip)
        self.peer_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.peer_strip_layout.setSpacing(6)
        self.peer_strip.hide()
        row1.addWidget(self.peer_strip, 1)

        row1.addStretch(1)

        self.badge = StatusBadge(card)
        self.badge.set_status("idle", "IDLE")
        row1.addWidget(self.badge)

        self.conn_label = QLabel("0 conn", card)
        self.conn_label.setObjectName("MutedLabel")
        row1.addWidget(self.conn_label)
        box.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.switch_button = QPushButton("切换模式", card)
        self.switch_button.setObjectName("GhostButton")
        self.switch_button.setToolTip("打开模式下拉")
        self.switch_button.clicked.connect(self.mode_combo.showPopup)
        self.join_button = QPushButton("加入网络…", card)
        self.join_button.setObjectName("GhostButton")
        self.join_button.clicked.connect(self.joinRequested)
        self.eph_button = QPushButton("生成临时密钥", card)
        self.eph_button.setObjectName("GhostButton")
        self.eph_button.clicked.connect(self.ephRequested)
        self.history_button = QPushButton("历史", card)
        self.history_button.setObjectName("GhostButton")
        self.history_button.clicked.connect(self.historyRequested)
        self.settings_button = QPushButton("设置", card)
        self.settings_button.setObjectName("GhostButton")
        self.settings_button.clicked.connect(self.settingsRequested)
        # 睡眠唤醒后 Windows 的蓝牙栈常常不自己回来（连上去 1~2s 就"成功"，
        # 表却是旧的）。那时唯一可靠的动作是把蓝牙关掉再打开 —— 程序没有这个
        # 权限（`Radio.set_state_async` 返回 DENIED_BY_USER），所以给一个显式
        # 入口：点它会尝试自动重置，不行就打开系统蓝牙设置让用户手动关一次。
        # self.radio_button = QPushButton("重置蓝牙", card)
        # self.radio_button.setObjectName("GhostButton")
        # self.radio_button.setToolTip("睡眠唤醒后连不上时用：关闭再打开蓝牙适配器（Ctrl+R）")
        # self.radio_button.clicked.connect(self.radioResetRequested)
        for button in (
            self.switch_button,
            self.join_button,
            self.eph_button,
            self.history_button,
            self.settings_button,
            # self.radio_button,
        ):
            button.setFixedHeight(26)
            row2.addWidget(button)
        row2.addStretch(1)
        box.addLayout(row2)

        card.setMinimumHeight(64)
        self._status_card = card
        return card

    # ------------------------------------------------------------- api
    def set_mode(self, mode: str) -> None:
        index = 0 if mode == "host" else 1
        if self.mode_combo.currentIndex() != index:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(index)
            self.mode_combo.blockSignals(False)
        self.join_button.setVisible(mode == "join")
        self.eph_button.setVisible(mode == "host")

    @property
    def mode(self) -> str:
        return "host" if self.mode_combo.currentIndex() == 0 else "join"

    def set_network(self, name: str, address: str = "") -> None:
        self.net_label.setText(name or "未连接")
        self.addr_label.setText(address or "—")

    def set_badge(self, kind: str, text: str) -> None:
        self.badge.set_status(kind, text)

    def set_connections(self, count: int) -> None:
        if getattr(self, "_conn_count", None) == count:
            return
        self._conn_count = count
        self.conn_label.setText(f"{count} conn")

    def set_peers(self, peers: list[str]) -> None:
        if peers == getattr(self, "_peers", None):
            return  # 内容没变就别重建 chips（state 变化时会被高频调用）
        self._peers = list(peers)
        while self.peer_strip_layout.count():
            item = self.peer_strip_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for name in peers:
            chip = QLabel(name, self.peer_strip)
            chip.setObjectName("PeerChip")
            self.peer_strip_layout.addWidget(chip)
        self.peer_strip.setVisible(bool(peers))

    def set_targets(self, targets: list[tuple[str, str]]) -> None:
        self.drop.set_targets(targets)

    def set_busy(self, busy: bool) -> None:
        self.drop.set_busy(busy)

    def set_progress(self, done: int, total: int) -> None:
        self.drop.set_progress(done, total)

    def set_emoji_enabled(self, enabled: bool) -> None:
        self.drop.set_emoji_enabled(enabled)

    def show_hint(self, text: str) -> None:
        self.footer.setText(text)

    def set_close_to_tray(self, enabled: bool) -> None:
        self._close_to_tray = enabled

    def force_close(self) -> None:
        self._allow_close = True
        self.close()

    def apply_colors(self, colors: dict[str, str]) -> None:
        self._colors = colors
        self.chat.set_theme(colors)
        self.drop.progress.set_colors(colors["accent"], colors["track"])
        # 发送按钮的纸飞机是**渲染出来**的位图，颜色跟着主题走（见 icons.svg_icon）
        self.drop.set_colors(colors)
        self.update()

    # ------------------------------------------------------------- mode switch
    def _on_mode_index(self, index: int) -> None:
        from PyQt6.QtWidgets import QMessageBox

        mode = "host" if index == 0 else "join"
        answer = QMessageBox.question(
            self,
            "切换模式",
            "切换到 Host 模式将停止当前连接并开始广播，切换到 Join 模式将断开当前会话。\n确定切换吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.modeChangeRequested.emit(mode)
        else:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(1 if mode == "host" else 0)
            self.mode_combo.blockSignals(False)

    # ------------------------------------------------------------- window
    #
    # 最大化为什么要自己管（不用 `showMaximized()`）：
    # 真机上出现过「点 ⟶ 窗口最大化了，但按钮图标还是 ▢；再点一下才变成 ❐，
    # 而且内层内容又向外扩一点；第三次点才还原」—— 也就是**一次点击被拆成了两次状态变化**。
    # 根因是"窗口真的变大了"和"Qt/Windows 的状态标记变成最大化"这两件事不同步：
    # `showMaximized()` 之后那一次 `changeEvent` 里 `isMaximized()` 还是 False，
    # 于是图标/边距没跟着变，第二次点击才补上（顺带又改了一次几何）。
    #
    # 自己算几何就没有这个时间差：状态、图标、边距、几何在**同一次调用**里改完，
    # 而且"还原"用的是自己记住的普通尺寸，不需要再去问系统。
    @property
    def is_maximized(self) -> bool:
        return self._maximized

    def toggle_maximized(self) -> None:
        self.set_maximized(not self._maximized)

    def set_maximized(self, on: bool) -> None:
        """最大化 / 还原。幂等：同一个状态调两次不会产生第二次几何变化。"""
        on = bool(on)
        if on == self._maximized:
            return
        if on:
            if not self.isMinimized():
                self._normal_geometry = QRect(self.geometry())
            screen = self.screen() or QApplication.primaryScreen()
            target = screen.availableGeometry() if screen is not None else self.geometry()
            self._maximized = True
            self._apply_maximized_look()
            self.setGeometry(target)
        else:
            self._maximized = False
            self._apply_maximized_look()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)

    def _apply_maximized_look(self) -> None:
        """标题栏图标 + 四周留白一起切。"""
        self.title_bar.set_maximized(self._maximized)
        margin = 0 if self._maximized else WINDOW_MARGIN
        layout = self.layout()
        if layout is not None:
            # 只调 `self.setContentsMargins` 不一定生效（布局在 __init__ 里显式设过
            # 自己的边距），两边都设才稳。
            layout.setContentsMargins(margin, margin, margin, margin)
        self.setContentsMargins(margin, margin, margin, margin)
        self.update()

    def normal_geometry(self) -> QRect:
        """保存到 config / 从最大化还原用的"普通尺寸"。

        `_save_geometry` 必须用它：最大化时退出程序，如果把最大化后的几何存进
        config，下次启动会直接是个贴着屏幕的巨窗。
        """
        if self._maximized and self._normal_geometry is not None:
            return QRect(self._normal_geometry)
        return self.geometry()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            # 只要**外部**（系统菜单 / Win+↑ / 托盘）改了状态就跟着对齐；
            # 我们自己调 set_maximized 时 Qt 的状态没变，所以不会走进这里。
            qt_max = self.isMaximized()
            if qt_max != self._maximized:
                if qt_max:
                    normal = self.normalGeometry()
                    if not normal.isEmpty():
                        self._normal_geometry = QRect(normal)
                self._maximized = qt_max
                self._apply_maximized_look()
        elif event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.windowActivated.emit()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._native_ready:
            self._native_ready = True
            self._enable_native_resize()

    # ------------------------------------------------------------- 原生改尺寸
    def _enable_native_resize(self) -> None:
        """给无边框窗口补上 `WS_THICKFRAME`（原因见文件头的 Win32 常量段）。"""
        if not _native_ok():
            return
        try:
            hwnd = int(self.winId())
            user32 = _user32()
            get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get.restype = ctypes.c_ssize_t
            get.argtypes = [ctypes.c_void_p, ctypes.c_int]
            put.restype = ctypes.c_ssize_t
            put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            style = get(hwnd, GWL_STYLE)
            if not style & WS_THICKFRAME:
                put(hwnd, GWL_STYLE, style | WS_THICKFRAME)
                # 让新样式立刻生效（否则要等到下次改尺寸才重算非客户区）
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
                )
                _ui_log.debug("native resize enabled (WS_THICKFRAME)")
        except Exception:  # noqa: BLE001
            _ui_log.exception("enable native resize failed")

    def nativeEvent(self, eventType, message):  # noqa: N802
        """接管 `WM_NCCALCSIZE` / `WM_NCHITTEST`。

        两个坑都是实测踩出来的：

        1. **不能调 `super().nativeEvent(eventType, message)`** —— 在 PyQt6 6.9 上
           把那个 `message`（`sip.voidptr`）原样传回 C++ 会让进程立刻以
           `0xC000041D`（callback 里逃出异常）退出，而且**连 traceback 都看不到**。
           `QWidget.nativeEvent` 的默认实现本来就是"什么都不处理"，所以返回
           `(False, 0)` 与调 super 等价。
        2. `eventType` 是 `QByteArray`（不是 `bytes` 也不是 `str`），
           直接和 `b"..."` 比较**永远为假** —— 那就是"明明写了拦截却完全没生效"。
        """
        if _native_ok() and _is_windows_msg(eventType):
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_NCCALCSIZE:
                    self._strip_frame(msg)
                    return True, 0
                if msg.message == WM_NCHITTEST:
                    hit = self._hit_test(int(msg.lParam))
                    if hit is not None:
                        return True, hit
            except Exception:  # noqa: BLE001
                # Win32 结构体解析出问题时宁可退回普通客户区，也不能让异常穿过事件循环
                _ui_log.exception("nativeEvent failed")
        return False, 0

    @staticmethod
    def _strip_frame(msg: _MSG) -> None:
        """让客户区 = 整窗（抵消 `WS_THICKFRAME` 带来的边框）。

        ⚠️ `wParam != 0` 时**什么都不改**：那个 `rgrc[0]` 进来时已经是"这次要应用
        的窗口矩形"（屏幕坐标），返回 0 就等于"客户端区就用它" —— 这正是
        "把非客户区抹掉"的标准写法。

        以前这里把 `rgrc[0]` 覆盖成 `GetWindowRect()` 的结果，而那一刻窗口矩形
        **还没更新**（它是"旧几何"）⇒ Qt 发完 `SetWindowPos` 读回来发现对不上，
        于是打出 `QWindowsWindow::setGeometry: Unable to set geometry …`
        （最大化时会看到这一行，虽然最终结果是对的）。

        `wParam == 0`（尺寸没变、只问一次客户区）时 `lParam` 是 `RECT*`，
        这时才需要自己补成窗口矩形。
        """
        if msg.wParam:
            return
        user32 = _user32()
        rect = wintypes.RECT()
        user32.GetWindowRect(ctypes.c_void_p(msg.hWnd), ctypes.byref(rect))
        target = ctypes.cast(msg.lParam, ctypes.POINTER(wintypes.RECT)).contents
        target.left, target.top = rect.left, rect.top
        target.right, target.bottom = rect.right, rect.bottom

    def _hit_test(self, lparam: int) -> int | None:
        """`WM_NCHITTEST` 的答复：边缘给 HT*，其余交给 Qt（None = 不接管）。"""
        if self._maximized:
            return None  # 最大化时不允许拖边改尺寸
        if not _native_ok():
            return None
        try:
            # lParam 低 16 位 = x、高 16 位 = y（**物理像素**，屏幕坐标，有符号）
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            _user32().GetWindowRect(ctypes.c_void_p(int(self.winId())), ctypes.byref(rect))
            band = max(4, round(RESIZE_MARGIN * self.devicePixelRatioF()))
            left = x - rect.left
            top = y - rect.top
            right = rect.right - x
            bottom = rect.bottom - y
        except Exception:  # noqa: BLE001
            return None
        on_left, on_right = left < band, right < band
        on_top, on_bottom = top < band, bottom < band
        if on_top and on_left:
            return HTTOPLEFT
        if on_top and on_right:
            return HTTOPRIGHT
        if on_bottom and on_left:
            return HTBOTTOMLEFT
        if on_bottom and on_right:
            return HTBOTTOMRIGHT
        if on_left:
            return HTLEFT
        if on_right:
            return HTRIGHT
        if on_top:
            return HTTOP
        if on_bottom:
            return HTBOTTOM
        return None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._close_to_tray and not self._allow_close:
            event.ignore()
            self.hide()
            self.closedToTray.emit()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------- resize
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        """自绘背景。整块包起来：绘制里出异常不该带走进程。"""
        try:
            _paint_rounded_background(self, self._colors, margined=not self._maximized)
        except Exception:  # noqa: BLE001
            _ui_log.exception("paint failed")

    @staticmethod
    def _edge_bits() -> tuple[int, int, int, int]:
        """`(left, right, top, bottom)` 的**纯 int** 位。

        这个 PyQt6 构建里 `Qt.Edge` 是 `enum.Flag`：
        * 和 `int` 做 `|=` 会抛 `TypeError`（原来的闪退）；
        * `int(Qt.Edge.TopEdge)` 也**不行**（`int()` 不接受 `Edge`）。
        拿纯 int 只有一条路：读 `.value`。
        """
        return (
            Qt.Edge.LeftEdge.value,
            Qt.Edge.RightEdge.value,
            Qt.Edge.TopEdge.value,
            Qt.Edge.BottomEdge.value,
        )

    def _edge_at(self, pos) -> int:
        """鼠标落在哪条/哪两条边上（返回 `Qt.Edge` 旗标的 **int**）。

        ⚠️ 全程用 `.value` 拿纯 int：`Qt.Edge` 直接和 `int` 运算会抛
        `TypeError: unsupported operand type(s) for |=: 'int' and 'Edge'`
        —— 在 `mouseMoveEvent` 里抛出就是**未捕获异常直接闪退**
        （dev 报的"按住上边缘向上拖动 → 软件强退"）。
        """
        if self._maximized:
            return 0
        left, right, top, bottom = self._edge_bits()
        rect = self.rect()
        margin = RESIZE_MARGIN
        edge = 0
        if pos.x() <= margin:
            edge |= left
        elif pos.x() >= rect.width() - margin:
            edge |= right
        if pos.y() <= margin:
            edge |= top
        elif pos.y() >= rect.height() - margin:
            edge |= bottom
        return edge

    def _try_system_resize(self, edges: int) -> bool:
        """把改尺寸交给系统（`QWindow.startSystemResize`）。

        比自己算几何好：系统的调整循环跟手、按 Esc 能取消、也不会在拖动中
        掉进 Qt 的事件重入。返回 False 时调用方再退回自己拖。
        """
        try:
            handle = self.windowHandle()
            if handle is None:
                return False
            return bool(handle.startSystemResize(Qt.Edge(edges)))
        except Exception:  # noqa: BLE001
            _ui_log.exception("startSystemResize failed")
            return False

    @classmethod
    def _cursor_for(cls, edge: int):
        left, right, top, bottom = cls._edge_bits()
        mapping = {
            left: Qt.CursorShape.SizeHorCursor,
            right: Qt.CursorShape.SizeHorCursor,
            top: Qt.CursorShape.SizeVerCursor,
            bottom: Qt.CursorShape.SizeVerCursor,
        }
        if edge == (left | top):
            return Qt.CursorShape.SizeFDiagCursor
        if edge == (right | bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if edge == (right | top):
            return Qt.CursorShape.SizeBDiagCursor
        if edge == (left | bottom):
            return Qt.CursorShape.SizeBDiagCursor
        return mapping.get(edge, Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------- safety net
    #
    # 为什么要在**每个**鼠标处理函数里兜：PyQt6 是直接调用这些虚函数的，
    # 覆写 `event()` 看不到里面的异常 —— 异常会一路冒到 Qt 的
    # "Exceptions caught in Qt event loop"（而且那行只在有 QApplication 且
    # stderr 可见时才看得到；`pythonw` 启动就是**静默闪退**，正是 dev 报的现象）。
    @staticmethod
    def _guarded(fn):
        """给鼠标处理函数包一层：异常吞掉 + 写日志，绝不让进程被带走。"""

        def wrapper(self, event, *args, **kwargs):
            try:
                return fn(self, event, *args, **kwargs)
            except Exception:  # noqa: BLE001
                _ui_log.exception("unhandled exception in %s", fn.__name__)
                try:
                    event.ignore()
                except Exception:
                    pass
                return None

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn
        return wrapper

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        edge = self._edge_at(event.position())
        if edge and event.button() == Qt.MouseButton.LeftButton:
            # 先让系统接管（原生调整循环）；在它不生效的环境里再自己拖。
            if self._try_system_resize(edge):
                event.accept()
                return
            self._resizing = True
            self._resize_edge = edge
            self._drag_origin = event.globalPosition().toPoint()
            self._start_geometry = self.geometry()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        left, right, top, bottom = self._edge_bits()
        if self._resizing:
            delta = event.globalPosition().toPoint() - self._drag_origin
            geo = QRect(self._start_geometry)
            edge = self._resize_edge
            if edge & left:
                geo.setLeft(geo.left() + delta.x())
            if edge & right:
                geo.setRight(geo.right() + delta.x())
            if edge & top:
                geo.setTop(geo.top() + delta.y())
            if edge & bottom:
                geo.setBottom(geo.bottom() + delta.y())
            geo.setSize(QSize(max(geo.width(), self.minimumWidth()), max(geo.height(), self.minimumHeight())))
            self.setGeometry(geo)
            event.accept()
            return
        edge = self._edge_at(event.position())
        self.setCursor(self._cursor_for(edge))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._resizing = False
        self._resize_edge = 0
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._edge_at(event.position()) == 0:
            super().mouseDoubleClickEvent(event)


# ---- 关键：给鼠标事件装上"不会被异常带走"的兜底 -----------------------------
#
# PyQt6 直接调用这些虚函数，覆写 `event()` 看不到里面的异常 —— 异常会冒到 Qt 的
# "Exceptions caught in Qt event loop"，而 `pythonw` 下那行只进 stderr（不可见），
# 用户看到的就是**静默闪退**（dev：按住上边缘拖动 → 软件强退）。
MainWindow.mousePressEvent = MainWindow._guarded(MainWindow.mousePressEvent)
MainWindow.mouseMoveEvent = MainWindow._guarded(MainWindow.mouseMoveEvent)
MainWindow.mouseReleaseEvent = MainWindow._guarded(MainWindow.mouseReleaseEvent)
MainWindow.mouseDoubleClickEvent = MainWindow._guarded(MainWindow.mouseDoubleClickEvent)
