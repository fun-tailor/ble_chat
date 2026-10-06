from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class TitleBar(QWidget):
    """自绘标题栏：左标题，右 — □ ×；支持拖拽移动与双击最大化。"""

    minimizeClicked = pyqtSignal()
    maximizeClicked = pyqtSignal()
    closeClicked = pyqtSignal()

    def __init__(self, title: str = "BLE Chat", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(32)
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(0)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("AppTitle")
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.min_button = self._caption_button("—", "最小化")
        self.max_button = self._caption_button("▢", "最大化")
        self.close_button = self._caption_button("×", "关闭")
        self.close_button.setObjectName("CaptionButton")
        self.close_button.setProperty("close", True)
        self.close_button.setStyleSheet("QPushButton#CloseButton:hover{background:#C42B1C;color:#FFFFFF;}")

        layout.addWidget(self.min_button)
        layout.addWidget(self.max_button)
        layout.addWidget(self.close_button)

        self.min_button.clicked.connect(self.minimizeClicked)
        self.max_button.clicked.connect(self.maximizeClicked)
        self.close_button.clicked.connect(self.closeClicked)

    def _caption_button(self, text: str, tooltip: str) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setObjectName("CaptionButton")
        btn.setAccessibleName(tooltip)
        btn.setToolTip(tooltip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_maximized(self, maximized: bool) -> None:
        """切换"最大化/还原"图标。

        图标文案只跟着这一个入口变 —— 以前它由 `MainWindow.changeEvent` 里的
        `isMaximized()` 驱动，而那个值在第一次点击时还没更新，于是出现
        「窗口已经最大化了，图标还是 ▢，再点一次才变 ❐」。
        """
        maximized = bool(maximized)
        self.max_button.setText("❐" if maximized else "▢")
        self.max_button.setToolTip("向下还原" if maximized else "最大化")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is None:
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            self._drag_offset = None
            return
        window = self.window()
        if getattr(window, "is_maximized", False):
            # 从最大化状态"拖出来"：先还原，再按**抓取点占标题栏宽度的比例**换算
            # 新的抓取偏移，这样光标底下的东西不会突然跳到窗口最左边。
            ratio = event.position().x() / max(1, self.width())
            window.set_maximized(False)
            y = self._drag_offset.y()
            self._drag_offset = QPoint(max(0, min(window.width() - 80, round(window.width() * ratio))), y)
        target = event.globalPosition().toPoint() - self._drag_offset
        window.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximizeClicked.emit()
            event.accept()
