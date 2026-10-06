from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from .theme import LIGHT
from .widgets.chat_view import ChatView
from .widgets.drop_area import DropArea
from .widgets.status_badge import StatusBadge
from .widgets.title_bar import TitleBar

RESIZE_MARGIN = 6
MODES = (("host", "Host"), ("join", "Join"))

_ui_log = logging.getLogger("blechat.ui")


def _paint_rounded_background(widget: QWidget, colors: dict[str, str]) -> None:
    """窗口的圆角背景（原 `MainWindow.paintEvent` 的绘制体）。

    单独提出来是为了让它能被 try/except 包住 —— 绘制里抛异常会把进程带走。
    """
    painter = QPainter(widget)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRect(widget.rect()).adjusted(4, 4, -4, -4)
        painter.setPen(QPen(QColor(colors["border"]), 1))
        painter.setBrush(QColor(colors["bg"]))
        painter.drawRoundedRect(rect, 12, 12)
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

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        self.title_bar = TitleBar("BLE Chat", self)
        self.title_bar.setMinimumHeight(32)
        self.title_bar.minimizeClicked.connect(self.showMinimized)
        self.title_bar.maximizeClicked.connect(self._toggle_maximized)
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
    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())
            if self.isMaximized():
                self.setContentsMargins(0, 0, 0, 0)
            else:
                self.setContentsMargins(8, 8, 8, 8)
        elif event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.windowActivated.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._close_to_tray and not self._allow_close:
            event.ignore()
            self.hide()
            self.closedToTray.emit()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------- resize
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        """自绘圆角背景。整块包起来：绘制里出异常不该带走进程。"""
        try:
            _paint_rounded_background(self, self._colors)
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
        if self.isMaximized():
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
