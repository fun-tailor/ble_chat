from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QWidget

COLORS = {
    "idle": "#909090",
    "handshake": "#F2C811",
    "ready": "#107C10",
    "error": "#C42B1C",
}


class StatusBadge(QWidget):
    """圆点 + 文本状态徽标：灰=IDLE / 黄=握手 / 绿=READY / 红=错误。"""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Badge")
        self._kind = "idle"
        self._text = "IDLE"
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_status(self, kind: str, text: str) -> None:
        kind = kind if kind in COLORS else "idle"
        changed = kind != self._kind or text != self._text
        self._kind, self._text = kind, text
        self.setToolTip(text)
        if changed:
            self.update()

    @property
    def kind(self) -> str:
        return self._kind

    def sizeHint(self):  # noqa: N802
        from PyQt6.QtCore import QSize

        fm = QFontMetrics(self.font())
        return QSize(14 + 6 + fm.horizontalAdvance(self._text) + 4, 22)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(COLORS[self._kind])
        dot = 9
        cy = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(1, cy - dot / 2, dot, dot))
        painter.setPen(QColor(self.palette().color(self.foregroundRole())))
        painter.drawText(
            QRectF(dot + 7, 0, self.width() - dot - 7, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


def badge_pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    return pen
