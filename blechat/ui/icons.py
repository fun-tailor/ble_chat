"""把 `assets/*.svg` 渲染成 **按主题换色** 的 `QIcon`。

为什么不直接 `QIcon("assets/send.svg")`：
`send.svg` 的填充色是写死的 `#262626`（深灰）。放在淡蓝按钮上就是一块深灰，
和"淡色主按钮"的设计对不上；而且 `QIcon` 从 SVG 生成 pixmap 时用的是**默认尺寸**
（`availableSizes()` 甚至是空的），高分屏上会糊。

这里做三件小事：
1. 读出 SVG 文本，把 `fill:#xxxxxx` 换成主题色；
2. 用 `QSvgRenderer` 按 `逻辑尺寸 × DPR` 渲染到 `QPixmap`（不会糊）；
3. 按 `(名字, 尺寸, DPR, 颜色)` 缓存 —— 主题切换/缩放只是查表。

拿不到 SVG（文件被删、QtSvg 缺失）时**不抛异常**：退化成 `QIcon(路径)` 用原色，
再不行返回空图标（按钮上还有 tooltip 和菜单，功能不受影响）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap

_icon_log = logging.getLogger("blechat.ui.icons")

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"

_FILL_RE = re.compile(r"fill:\s*#[0-9A-Fa-f]{3,8}")

_pixmap_cache: dict[tuple[str, int, int, str], QPixmap] = {}
_icon_cache: dict[tuple[str, int, int, str], QIcon] = {}


def svg_pixmap(name: str, color: str = "", size: int = 20, dpr: float = 1.0) -> QPixmap:
    """`assets/<name>.svg` → `QPixmap`（`color` 为空表示保留原色）。

    按 `size × dpr` 渲染并 `setDevicePixelRatio(dpr)` ⇒ 逻辑尺寸就是 `size`，
    高分屏上不糊。
    """
    dpr_key = max(1, round(dpr * 100))
    key = (name, size, dpr_key, color or "")
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    path = ASSETS_DIR / f"{name}.svg"
    pixmap = QPixmap()
    try:
        text = path.read_text(encoding="utf-8")
        if color:
            text = _FILL_RE.sub(f"fill:{color}", text)
        from PyQt6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
        if renderer.isValid():
            ratio = dpr_key / 100
            pixmap = QPixmap(round(size * ratio), round(size * ratio))
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                renderer.render(painter)
            finally:
                painter.end()
            pixmap.setDevicePixelRatio(ratio)
    except Exception as exc:  # noqa: BLE001
        _icon_log.debug("svg_pixmap(%s) failed: %s", name, exc)
        pixmap = QPixmap()

    _pixmap_cache[key] = pixmap
    return pixmap


def svg_icon(name: str, color: str = "", size: int = 20, dpr: float = 1.0) -> QIcon:
    """`assets/<name>.svg` → `QIcon`（给"交给 Qt 自己画"的地方用）。"""
    dpr_key = max(1, round(dpr * 100))
    key = (name, size, dpr_key, color or "")
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached

    pixmap = svg_pixmap(name, color, size, dpr)
    icon = QIcon()
    if not pixmap.isNull():
        icon.addPixmap(pixmap)
    else:
        path = ASSETS_DIR / f"{name}.svg"
        if path.exists():
            # 退化：SVG 原色（QtSvg 缺失时 QIcon 会去找 qsvg 图片插件）
            icon = QIcon(str(path))
    _icon_cache[key] = icon
    return icon
