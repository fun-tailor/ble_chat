from __future__ import annotations

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QTextCursor
from PyQt6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QTextEdit,
                             QToolButton, QVBoxLayout, QWidgetAction)

from ...protocol import MAX_FILE, MAX_PAYLOAD
from ..emoji_panel import EmojiPanel
from .circular_progress import CircularProgress
from .send_button import SendButton

IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

# 发送按钮上纸飞机的逻辑尺寸（图标按 DPR 渲染，见 ui/icons.py）
SEND_ICON_PX = 18


def _oversize_limit(path: str) -> int | None:
    """读之前先看大小；超限返回该类别的上限，否则 None。

    以前是**整读完**才发现超限（`add_file` 里判断），在 GUI 线程同步读一个
    大文件会让 Qt 事件循环停摆 —— 界面不 repaint（看起来就是"卡死"）、
    qasync 的跨线程信号不投递、GATT 写请求的 `respond()` 跟着推迟，
    对端 8s 超时，而全程**没有任何报错**。
    """
    limit = MAX_PAYLOAD if path.lower().endswith(IMAGE_SUFFIX) else MAX_FILE
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    return limit if size > limit else None

class DropArea(QFrame):
    """输入区：多行文本 + 待发文件队列 + 剪贴板 + 表情 + 选文件 + 发送。"""

    sendRequested = pyqtSignal()
    clipboardRequested = pyqtSignal()
    hint = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self._files: list[tuple[str, bytes, str, str]] = []  # (name, data, mime, src_path)
        self._targets: list[tuple[str, str]] = []
        self._selected: list[str] | None = None
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.thumbs = QLabel("", self)
        self.thumbs.setObjectName("ThumbsLabel")
        self.thumbs.setWordWrap(True)
        self.thumbs.setMinimumHeight(0)
        self.thumbs.setMaximumHeight(76)
        self.thumbs.hide()
        layout.addWidget(self.thumbs)

        self.text_area = QTextEdit(self)
        self.text_area.setObjectName("InputArea")
        self.text_area.setAcceptRichText(False)
        self.text_area.setPlaceholderText("输入消息，Ctrl+Enter 发送，可拖入图片或文件")
        self.text_area.setMinimumHeight(72)
        self.text_area.setMaximumHeight(120)
        self.text_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.text_area, 1)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.clipboard_button = self._tool_button("📋 剪贴板", "直接把剪贴板文本发出去")
        self.clipboard_button.clicked.connect(self.clipboardRequested)
        row.addWidget(self.clipboard_button)

        self.emoji_button = self._tool_button("😊 表情", "插入表情")
        emoji_menu = QMenu(self.emoji_button)
        self.emoji_panel = EmojiPanel(emoji_menu)
        self.emoji_panel.chosen.connect(self._on_emoji)
        action = QWidgetAction(emoji_menu)
        action.setDefaultWidget(self.emoji_panel)
        emoji_menu.addAction(action)
        self.emoji_button.setMenu(emoji_menu)
        self.emoji_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.addWidget(self.emoji_button)

        self.file_button = self._tool_button("📎 文件", "选择要发送的文件（≤ 2MB）")
        self.file_button.clicked.connect(self._pick_files)
        row.addWidget(self.file_button)

        self.drop_hint = QLabel("拖入图片/文件", self)
        self.drop_hint.setObjectName("MutedLabel")
        row.addWidget(self.drop_hint)
        row.addStretch(1)

        self.progress = CircularProgress(self, 24)
        row.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignCenter)

        self.send_button = SendButton(self)
        self.send_button.setToolTip("发送（Ctrl+Enter）；点右侧箭头选发给谁")
        self.send_button.clicked.connect(self.sendRequested)
        self.send_button.setEnabled(False)
        self._menu = QMenu(self)
        self.send_button.setMenu(self._menu)
        self._icon_color = ""
        self._icon_dpr = 0.0
        row.addWidget(self.send_button)

        layout.addLayout(row)

    # ------------------------------------------------------------- theme
    def set_colors(self, colors: dict[str, str]) -> None:
        """主题变化时重渲染发送按钮的纸飞机 + 下拉箭头（都是画出来的，得跟着换色）。"""
        self._apply_send_icon(colors.get("on_accent_soft", ""))

    def _apply_send_icon(self, color: str) -> None:
        from ..icons import svg_pixmap

        dpr = self.devicePixelRatioF()
        self._icon_color = color
        self._icon_dpr = dpr
        pixmap = svg_pixmap("send", color, SEND_ICON_PX, dpr)
        # 传空 pixmap 时按钮自己退回纯文字 —— `assets/send.svg` 缺失
        # （注意 `.gitignore` 里 `/assets` 是被忽略的）或 QtSvg 缺失都能兜住，
        # 绝不会变成一块空白。
        self.send_button.set_icon_pixmap(pixmap)
        self.send_button.set_arrow_color(color)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 拖到另一块缩放不同的屏幕上时重渲染一次，否则图标会糊
        if abs(self.devicePixelRatioF() - self._icon_dpr) > 0.01:
            self._apply_send_icon(self._icon_color)

    def set_emoji_enabled(self, enabled: bool) -> None:
        """表情按钮开关（默认关闭，设置里可开）。"""
        self.emoji_button.setVisible(enabled)

    @staticmethod
    def _tool_button(text: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # ------------------------------------------------------------- targets
    def set_targets(self, targets: list[tuple[str, str]]) -> None:
        """[(peer_id, name)] —— 空表示未连接。

        目标没变就不要重建 QMenu：发送时每个 ACK 都会走到这里，
        反复 `QMenu.clear()` + `addAction` 会把 server 端 UI 拖死。
        """
        same = targets == self._targets
        prev_selected = list(self._selected) if self._selected is not None else None
        self._targets = list(targets)
        if self._selected is not None:
            ids = {t[0] for t in self._targets}
            if not set(self._selected) & ids:
                self._selected = None
        new_selected = list(self._selected) if self._selected is not None else None
        if not same or prev_selected != new_selected:
            self._rebuild_menu()
        self.send_button.setEnabled(bool(self._targets) and not self._busy)

    def _rebuild_menu(self) -> None:
        self._menu.clear()
        if not self._targets:
            return
        if len(self._targets) > 1:
            all_action = self._menu.addAction("发给全部")
            all_action.setCheckable(True)
            all_action.setChecked(self._selected is None)
            all_action.triggered.connect(lambda: self._choose(None))
            self._menu.addSeparator()
        for peer_id, name in self._targets:
            action = self._menu.addAction(name or peer_id)
            action.setCheckable(True)
            action.setChecked(self._selected == [peer_id])
            action.triggered.connect(lambda checked=False, pid=peer_id: self._choose([pid]))

    def _choose(self, peer_ids: list[str] | None) -> None:
        self._selected = peer_ids
        self._rebuild_menu()

    @property
    def selected_targets(self) -> list[str] | None:
        return self._selected

    # ------------------------------------------------------------- input
    def text(self) -> str:
        return self.text_area.toPlainText().strip("\n")

    def clear(self) -> None:
        """清空输入框。

        先收掉选区再清：**输入框里有选中的文字**时直接 `clear()`，Qt 内部那次
        `QTextCursor::setPosition` 会短暂越界，偶尔打出一行
        `QTextCursor::setPosition: Position 'N' out of range` 并卡一下
        （dev 报的"输入框有文字且被选中时偶发卡顿"）。先把光标挪到末尾是零成本的。
        """
        cursor = self.text_area.textCursor()
        if cursor.hasSelection():
            cursor.clearSelection()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_area.setTextCursor(cursor)
        self.text_area.clear()

    def _on_emoji(self, emoji: str) -> None:
        cursor = self.text_area.textCursor()
        cursor.insertText(emoji)
        self.text_area.setTextCursor(cursor)
        self.text_area.setFocus()

    # ------------------------------------------------------------- files
    def take_files(self) -> list[tuple[str, bytes, str, str]]:
        files, self._files = self._files, []
        self._render_files()
        return files

    def add_image(self, data: bytes) -> bool:
        return self.add_file("剪贴板图片.png", bytes(data), "image/png")

    def add_file(self, name: str, data: bytes, mime: str = "", path: str = "") -> bool:
        name = (name or "文件").split("/")[-1].split("\\")[-1] or "文件"
        is_image = name.lower().endswith(IMAGE_SUFFIX)
        limit = MAX_PAYLOAD if is_image else MAX_FILE
        if len(data) > limit:
            self.hint.emit(f"{name} 超过 {limit // (1024 * 1024)}MB（{len(data) // 1024} KB）")
            return False
        self._files.append((name, bytes(data), mime, path))
        self._render_files()
        return True

    def _render_files(self) -> None:
        if not self._files:
            self.thumbs.hide()
            self.thumbs.setText("")
            return
        from ...history import format_size

        parts = []
        for i, (name, data, _, _) in enumerate(self._files, 1):
            parts.append(f"📎 {i}. {name}（{format_size(len(data))}）")
        self.thumbs.setText("   ".join(parts) + "   点 × 或重新选择可清空")
        self.thumbs.show()

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        for path in paths or []:
            limit = _oversize_limit(path)
            if limit is not None:
                name = path.rsplit("\\", 1)[-1]
                self.hint.emit(f"{name} 超过 {limit // (1024 * 1024)}MB，未加入")
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                self.hint.emit(f"读取失败：{exc}")
                continue
            if self.add_file(path.rsplit("\\", 1)[-1], data, "", path):
                self.hint.emit("已加入待发队列，点“发送”发出")

    # ------------------------------------------------------------- busy
    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return  # 幂等：_sync_ui 会反复调用，不能每次都把进度条打回 0/1
        self._busy = busy
        self.clipboard_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)
        self.emoji_button.setEnabled(not busy)
        self.text_area.setReadOnly(busy)
        if busy:
            self.send_button.setEnabled(False)
            self.progress.show_progress(0, 1)
        else:
            self.progress.reset()
            self.send_button.setEnabled(bool(self._targets))

    def set_progress(self, done: int, total: int) -> None:
        self.progress.show_progress(done, total)

    def finish_progress(self) -> None:
        self.progress.finish()

    # ------------------------------------------------------------- events
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasImage() or (mime.hasUrls() and any(url.isLocalFile() for url in mime.urls())):
            event.acceptProposedAction()
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        mime = event.mimeData()
        added = False
        if mime.hasImage():
            image = mime.imageData()
            if image is not None and not image.isNull():
                from PyQt6.QtCore import QBuffer, QIODevice
                from PyQt6.QtGui import QImage

                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                qimage = QImage(image)
                qimage.save(buffer, "PNG")
                added |= self.add_file("拖入图片.png", bytes(buffer.data()), "image/png")
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if not path or not url.isLocalFile():
                    continue
                limit = _oversize_limit(path)
                if limit is not None:
                    name = path.rsplit("\\", 1)[-1]
                    self.hint.emit(f"{name} 超过 {limit // (1024 * 1024)}MB，未加入")
                    continue
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    self.hint.emit(f"读取失败：{exc}")
                    continue
                added |= self.add_file(path.rsplit("\\", 1)[-1], data, "", path)
        if added:
            event.acceptProposedAction()
            self.hint.emit("已加入待发队列，点“发送”发出")
        else:
            event.ignore()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if self.send_button.isEnabled():
                self.sendRequested.emit()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._files:
            self.take_files()
            self.hint.emit("已清空待发文件")
        super().mouseDoubleClickEvent(event)
