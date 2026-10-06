from __future__ import annotations

from PyQt6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt, QPropertyAnimation,
                           pyqtProperty)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

START = 225 * 16  # QPainter.drawArc 使用 1/16 度
SPAN = 270 * 16


class CircularProgress(QWidget):
    """270° 圆弧进度环，QPropertyAnimation 驱动 value(0..1)。"""

    def __init__(self, parent: QWidget | None = None, size: int = 24) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._value = 0.0
        self._foreground = "#0078D4"
        self._track = "#E0E0E0"
        self._animation = QPropertyAnimation(self, b"value", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hide()

    # ------------------------------------------------------------- property
    def get_value(self) -> float:
        return self._value

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(1.0, float(value)))
        self.update()

    value = pyqtProperty(float, fget=get_value, fset=set_value)

    def set_colors(self, foreground: str, track: str) -> None:
        self._foreground = foreground
        self._track = track
        self.update()

    # ------------------------------------------------------------- api
    def show_progress(self, done: int, total: int) -> None:
        target = 0.0 if not total else done / total
        self.show()
        self._animation.stop()
        self._animation.setStartValue(self._value)
        self._animation.setEndValue(target)
        self._animation.start()

    def reset(self) -> None:
        self._animation.stop()
        self._value = 0.0
        self.update()
        self.hide()

    def finish(self) -> None:
        self.show_progress(1, 1)

    # ------------------------------------------------------------- paint
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(24, 24)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2.5, 2.5, self.width() - 5, self.height() - 5)

        track = QPen(QColor(self._track), 3)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, START, SPAN)

        if self._value > 0:
            pen = QPen(QColor(self._foreground), 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, START, int(SPAN * self._value))
        painter.end()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()


def center_point(rect: QRectF) -> QPointF:
    return QPointF(rect.center())
