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
        self.max_button.setText("❐" if maximized else "▢")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is None:
            return
        if event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_offset
            window = self.window()
            if window.isMaximized():
                window.showNormal()
                self.set_maximized(False)
            window.move(target)
            event.accept()
        else:
            self._drag_offset = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximizeClicked.emit()
            event.accept()
