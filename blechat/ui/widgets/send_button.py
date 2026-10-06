"""发送按钮：淡色药丸 + 纸飞机图标 + **自己画的**下拉小箭头。

两个"不交给 Qt"的理由：

1. **箭头**：`MenuButtonPopup` 模式下 Qt 会在右侧画默认的 `PE_IndicatorArrowDown`
   —— 一个实心三角。放在淡蓝药丸上比纸飞机还抢眼，而且 QSS 管不住它的尺寸
   （实测 `image: none` 能去掉它，`width/height` 无效）。这里改画一条 1.6px 折线。
2. **图标**：Qt 会把图标在**整个按钮**里居中 —— 而按钮右边还有一格"选发给谁"，
   于是视觉重心被箭头拽偏（dev 的原话："看着偏向箭头"）。
   所以图标也自己画：**在左侧（不含下拉格）那块矩形里居中**。
   左侧那块的右边界直接问样式（`::menu-button { width: 20px }` 是它算出来的），
   免得 QSS 改了宽度这里跟着错。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QStyle, QStyleOptionToolButton, QToolButton

# 右侧"选发给谁"那一格宽度的兜底值（拿不到样式时才用；正常由 QSS 决定）
MENU_CELL_W = 20

# 折线的半宽/半高（逻辑像素）
ARROW_HALF_W = 4.0
ARROW_HALF_H = 2.0

# 禁用时图标的透明度
DISABLED_OPACITY = 0.4

# 图标在“左格居中”之后，再额外向右挪动的逻辑像素。
ICON_SHIFT_X = 3.0

class SendButton(QToolButton):
    """纸飞机在左半区居中、右侧自带下拉箭头的发送按钮。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SendButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 图标/文字都由本类自己决定（见 set_icon_pixmap），所以这里永远是 TextOnly：
        # 有图标时 text 为空（我们自己画），没图标时 text = "发送"（Qt 画，作为降级）。
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setAccessibleName("发送")
        self._icon = QPixmap()
        self._arrow_color = "#0F4A85"

    # ------------------------------------------------------------- api
    def set_arrow_color(self, color: str) -> None:
        if color and color != self._arrow_color:
            self._arrow_color = color
            self.update()

    def set_icon_pixmap(self, pixmap: QPixmap) -> None:
        """设置纸飞机；传空 `QPixmap` 表示"没有素材"，此时退回纯文字。"""
        self._icon = QPixmap(pixmap) if not pixmap.isNull() else QPixmap()
        self.setText("" if not self._icon.isNull() else "发送")
        self.update()

    @property
    def icon_pixmap(self) -> QPixmap:
        return self._icon

    # ------------------------------------------------------------- 几何
    def icon_cell(self) -> QRect:
        """图标那一格 = 整个按钮**减去**右下拉格。"""
        cell = self._menu_cell()
        return QRect(0, 0, max(0, cell.left()), self.height())

    def _menu_cell(self) -> QRect:
        """右侧下拉格的矩形：先问样式，拿不到才退回"最右 MENU_CELL_W 像素"。"""
        try:
            opt = QStyleOptionToolButton()
            self.initStyleOption(opt)
            rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_ToolButton,
                opt,
                QStyle.SubControl.SC_ToolButtonMenu,
                self,
            )
            if rect.isValid() and rect.width() > 0 and rect.width() < self.width():
                #计算 PyQt6.QtCore.QRect(46, 0, 21, 36)
                return rect
        except Exception:  # noqa: BLE001
            pass
        width = min(MENU_CELL_W, max(0, self.width()))
        return QRect(self.width() - width, 0, width, self.height())

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)  # 背景/边框/（降级时的）文字交给 QSS
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_icon(painter)
            self._paint_arrow(painter)
        finally:
            painter.end()

    def _paint_icon(self, painter: QPainter) -> None:
        if self._icon.isNull():
            return
        ratio = self._icon.devicePixelRatio() or 1.0
        width = self._icon.width() / ratio
        height = self._icon.height() / ratio
        cell = self.icon_cell()
        if cell.width() <= 0:
            return
        painter.save()
        try:
            if not self.isEnabled():
                painter.setOpacity(DISABLED_OPACITY)
            x = cell.x() + (cell.width() - width) / 2.0 + ICON_SHIFT_X
            y = cell.y() + (cell.height() - height) / 2.0
            painter.drawPixmap(QPointF(x, y), self._icon)
        finally:
            painter.restore()

    def _paint_arrow(self, painter: QPainter) -> None:
        if self.menu() is None:
            return
        if self.popupMode() != QToolButton.ToolButtonPopupMode.MenuButtonPopup:
            return
        color = QColor(self._arrow_color)
        if not self.isEnabled():
            color.setAlpha(int(255 * DISABLED_OPACITY) + 40)
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center = self._menu_cell().center()
        cx, cy = float(center.x()), float(center.y())
        painter.drawPolyline(
            QPolygonF([
                QPointF(cx - ARROW_HALF_W, cy - ARROW_HALF_H),
                QPointF(cx, cy + ARROW_HALF_H),
                QPointF(cx + ARROW_HALF_W, cy - ARROW_HALF_H),
            ])
        )
