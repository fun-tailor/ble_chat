"""聊天气泡区：`QScrollArea` + 每条消息一个 widget。

为什么不用 QListView + 自定义 delegate：
    `sizeHint()` 返回 `viewport().width()`，而 QListView 在 `setWrapping(True)`
    下是流式布局，resize 时宽度缓存与实际不符 → 两条消息被排进同一行（重叠）。
    消息条数有上限（MAX_VIEW），不需要虚拟化，用普通 widget 最省心。

对外 API 与旧版保持一致：`append/remove/clear/chat_model/set_theme`、
`chat_model.{add,row_of,item_at,touch,remove,clear,items}`、信号 `deleteRequested/copied`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QContextMenuEvent, QDesktopServices, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QMenu, QMessageBox, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..theme import LIGHT
from .message_bubble import (MessageItem, ensure_thumb, header_label, status_text)

MAX_VIEW = 100
MAX_BUBBLE_RATIO = 0.7
MIN_BUBBLE_PX = 200

_view_log = logging.getLogger("blechat.ui.chat")


class ChatModel(QObject):
    """消息容器。只负责存取与发信号，不再继承 QAbstractListModel。"""

    added = pyqtSignal(object)
    changed = pyqtSignal(str)
    removed = pyqtSignal(str)
    reset = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[MessageItem] = []

    def rowCount(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------- mutation
    def add(self, item: MessageItem) -> int:
        self._items.append(item)
        self.added.emit(item)
        return len(self._items) - 1

    def row_of(self, key: str) -> int:
        for i, it in enumerate(self._items):
            if it.key == key:
                return i
        return -1

    def item_at(self, row: int) -> MessageItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def touch(self, key: str) -> None:
        if self.row_of(key) >= 0:
            self.changed.emit(key)

    def remove(self, key: str) -> bool:
        row = self.row_of(key)
        if row < 0:
            return False
        del self._items[row]
        self.removed.emit(key)
        return True

    def clear(self) -> None:
        self._items.clear()
        self.reset.emit()

    @property
    def items(self) -> list[MessageItem]:
        return list(self._items)


class BubbleWidget(QWidget):
    """单条消息：头部（昵称·时间）+ 气泡/缩略图/文件卡片 + 状态行。"""

    def __init__(self, item: MessageItem, view: "ChatView", parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self._view = view
        self._max_w = 420

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QWidget(self)
        self._card.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        card = QVBoxLayout(self._card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(3)

        # 头部（昵称 · 时间）跟气泡**同侧**贴边：自己的消息在右边，所以
        # `我 22:10` 也要靠右 —— 以前它固定左对齐，在靠右的气泡上方显得错位。
        # 用一个 HBox 顶住（而不是给 QLabel 设对齐）：`_card` 是
        # `QSizePolicy.Maximum`，只有这一行真的占满卡片宽度，右对齐才看得见。
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        self._header = QLabel(self._card)
        self._header.setObjectName("BubbleHeader")
        self._header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        if item.is_out:
            header_row.addStretch(1)
            header_row.addWidget(self._header)
        else:
            header_row.addWidget(self._header)
            header_row.addStretch(1)
        card.addLayout(header_row)

        self._body = QFrame(self._card)
        self._body.setObjectName("BubbleOut" if item.is_out else "BubbleIn")
        self._body.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(12, 8, 12, 8)
        body.setSpacing(4)

        self._text = QLabel(self._body)
        self._text.setObjectName("BubbleText")
        self._text.setWordWrap(True)
        self._text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._text.setMinimumWidth(0)
        body.addWidget(self._text)

        self._image = QLabel(self._body)
        self._image.setObjectName("BubbleImage")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self._image)

        self._file = QFrame(self._body)
        self._file.setObjectName("FileCard")
        file_row = QHBoxLayout(self._file)
        file_row.setContentsMargins(10, 8, 10, 8)
        file_row.setSpacing(8)
        self._file_icon = QLabel(self._file)
        self._file_icon.setObjectName("FileIcon")
        self._file_name = QLabel(self._file)
        self._file_name.setObjectName("FileName")
        self._file_name.setWordWrap(True)
        self._file_size = QLabel(self._file)
        self._file_size.setObjectName("MutedLabel")
        file_row.addWidget(self._file_icon)
        file_row.addWidget(self._file_name, 1)
        file_row.addWidget(self._file_size)
        body.addWidget(self._file)

        card.addWidget(self._body)

        self._status = QLabel(self._card)
        self._status.setObjectName("BubbleStatus")
        self._status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        card.addWidget(self._status)

        if item.is_out:
            outer.addStretch(1)
            outer.addWidget(self._card, 0, Qt.AlignmentFlag.AlignRight)
        else:
            outer.addWidget(self._card, 0, Qt.AlignmentFlag.AlignLeft)
            outer.addStretch(1)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh()

    # ------------------------------------------------------------- layout
    def fit(self, max_w: int) -> None:
        if max_w == self._max_w:
            return
        self._max_w = max_w
        self._card.setMaximumWidth(max_w)
        self._body.setMaximumWidth(max_w)
        self._header.setMaximumWidth(max_w)
        self._status.setMaximumWidth(max_w)
        self._text.setMaximumWidth(max(40, max_w - 26))

    def refresh(self) -> None:
        item = self.item
        self._header.setText(header_label(item))

        is_image = item.kind == "image"
        is_file = item.kind == "file"
        self._text.setVisible(not is_image and not is_file)
        self._image.setVisible(is_image)
        self._file.setVisible(is_file)

        if is_image:
            pix = ensure_thumb(item)
            if pix is not None:
                self._image.setPixmap(pix)
            else:
                self._image.setText("[图片]")
        elif is_file:
            self._file_icon.setText("📄")
            self._file_name.setText(item.file_name or "文件")
            from ...history import format_size

            self._file_size.setText(format_size(item.file_size))
            self._file.setToolTip(item.local_path or item.file_name or "")
        else:
            self._text.setText(item.display_text)

        text = status_text(item)
        self._status.setVisible(bool(text))
        self._status.setText(text)

    # ------------------------------------------------------------- interaction
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._view.copy_item(self.item)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.item.kind == "file":
            self._view.open_file(self.item)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._view.copy_item(self.item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        self._view.menu_for(self.item, event.globalPos())


class ChatView(QScrollArea):
    """聊天气泡列表：单击/双击复制、右键菜单、图片大图、文件打开。"""

    deleteRequested = pyqtSignal(object)
    copied = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatView")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)

        self._container = QWidget()
        self._container.setObjectName("ChatArea")
        self._layout = QVBoxLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(16, 10, 16, 10)
        self._layout.setSpacing(12)
        self.setWidget(self._container)

        self._model = ChatModel(self)
        self._model.added.connect(self._on_added)
        self._model.changed.connect(self._on_changed)
        self._model.removed.connect(self._on_removed)
        self._model.reset.connect(self._on_reset)
        self._bubbles: dict[str, BubbleWidget] = {}
        self._colors = dict(LIGHT)
        self._scroll_gen = 0

    # ------------------------------------------------------------- api
    @property
    def chat_model(self) -> ChatModel:
        return self._model

    @property
    def max_bubble(self) -> int:
        return max(MIN_BUBBLE_PX, int(self.viewport().width() * MAX_BUBBLE_RATIO))

    def append(self, item: MessageItem, scroll: bool = True, force: bool = False) -> None:
        """加一条。`force=True` = 无视"用户是否上翻"，强制贴底（收到新消息用）。"""
        stick = self._near_bottom()
        self._model.add(item)
        self._trim()
        if scroll and (force or stick):
            self.scroll_to_bottom()

    def remove(self, key: str) -> None:
        self._model.remove(key)

    def clear(self) -> None:
        self._model.clear()

    def scroll_to_bottom(self) -> None:
        """滚到底。

        刚 `addWidget` 完 layout 还没跑，`bar.maximum()` 是**旧值**，
        只滚一次会停在半路 —— 这就是"收到消息不自动往下滚"的原因。
        先同步 `activate()` 让布局立刻生效，再补两轮延迟滚动兜底。
        """
        self._layout.activate()
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        stick = bar.value()
        self._scroll_gen += 1
        gen = self._scroll_gen
        QTimer.singleShot(0, lambda: self._scroll_later(gen, stick))
        QTimer.singleShot(120, lambda: self._scroll_later(gen, stick))

    def _scroll_later(self, gen: int, stick: int) -> None:
        if gen != self._scroll_gen:
            return
        try:
            bar = self.verticalScrollBar()
        except RuntimeError:
            return  # widget 已销毁，延迟回调才跑
        if bar.value() < stick - 8:
            return  # 用户在这一小段时间里往上翻了，别拽回来
        self._layout.activate()
        bar.setValue(bar.maximum())

    def set_theme(self, colors: dict[str, str]) -> None:
        self._colors = dict(colors)
        for widget in self._bubbles.values():
            widget._body.setObjectName("BubbleOut" if widget.item.is_out else "BubbleIn")
            widget._body.style().unpolish(widget._body)
            widget._body.style().polish(widget._body)
        self._container.style().unpolish(self._container)
        self._container.style().polish(self._container)
        self.viewport().update()

    # ------------------------------------------------------------- model wiring
    def _on_added(self, item: MessageItem) -> None:
        bubble = BubbleWidget(item, self)
        bubble.fit(self.max_bubble)
        self._bubbles[item.key] = bubble
        self._layout.addWidget(bubble)

    def _on_changed(self, key: str) -> None:
        bubble = self._bubbles.get(key)
        if bubble is not None:
            bubble.refresh()

    def _on_removed(self, key: str) -> None:
        bubble = self._bubbles.pop(key, None)
        if bubble is not None:
            self._layout.removeWidget(bubble)
            bubble.setParent(None)
            bubble.deleteLater()

    def _on_reset(self) -> None:
        for key in list(self._bubbles):
            self._on_removed(key)

    def _trim(self) -> None:
        items = self._model.items
        while len(items) > MAX_VIEW:
            self._model.remove(items[0].key)
            items = self._model.items

    def _near_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 48

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.max_bubble
        for bubble in self._bubbles.values():
            bubble.fit(width)

    # ------------------------------------------------------------- actions
    def copy_item(self, item: MessageItem) -> None:
        if item.kind == "text":
            QApplication.clipboard().setText(item.text)
            self.copied.emit("已复制到剪贴板")
        elif item.kind == "file":
            QApplication.clipboard().setText(item.local_path or item.file_name)
            self.copied.emit("文件路径已复制")
        else:
            from PyQt6.QtGui import QImage

            image = QImage(item.content)
            if not image.isNull():
                QApplication.clipboard().setImage(image)
                self.copied.emit("图片已复制")

    def open_file(self, item: MessageItem) -> None:
        if item.local_path:
            QDesktopServices.openUrl(QUrl_from_local(item.local_path))
            self.copied.emit(f"已打开 {item.local_path}")
            return
        self.copy_item(item)

    def reveal_file(self, item: MessageItem) -> None:
        """打开文件所在目录，并**尽量在资源管理器里选中它**。

        磁盘上的文件名带 `{msg_id}-` 前缀（防重名互相覆盖），但用户看到的是原始
        文件名 —— 右键"打开文件所在目录"时不该把一个带编号的名字怼到眼前。
        所以这里：
        1. 优先用资源管理器 `/select,` 直接**高亮**那个文件（目录里显示的仍是
           原文件名）；
        2. 高亮失败就退化成打开目录；
        3. 文件已经被清理/移动时，把**原文件名**复制到剪贴板并打开目录，
           而不是什么都不做。
        """
        if not item.local_path:
            self.copy_item(item)
            return
        path = Path(item.local_path)
        folder = str(path.parent)
        name = item.file_name or path.name
        if path.exists() and self._select_in_explorer(path):
            self.copied.emit(f"已在资源管理器中定位：{name}")
            return
        if not path.exists():
            QApplication.clipboard().setText(name)
            self.copied.emit(f"文件已不在（{name}），文件名已复制")
        else:
            self.copied.emit(f"已打开目录 {folder}")
        QDesktopServices.openUrl(QUrl_from_local(folder))

    @staticmethod
    def _select_in_explorer(path: Path) -> bool:
        """用 `explorer /select,"<path>"` 在目录里高亮该文件；成功返回 True。

        用列表参数（不经 shell）→ 路径里的空格/中文都不需要自己加引号，
        `list2cmdline` 会处理好。子进程的输出必须**丢掉**，否则 explorer 会因为
        标准句柄被重定向而只打开"此电脑"。
        """
        import subprocess
        import sys

        if sys.platform != "win32":
            return False
        try:
            subprocess.Popen(
                ["explorer", "/select,",path],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            _view_log.debug("explorer /select failed: %s", exc)
            return False
        return True

    def menu_for(self, item: MessageItem, pos) -> None:
        menu = QMenu(self)
        act_copy = menu.addAction("复制")
        act_view = menu.addAction("查看原文")
        act_save = menu.addAction("保存图片") if item.kind == "image" else None
        act_reveal = (
            menu.addAction("打开文件所在目录")
            if (item.kind == "file" and item.local_path)
            else None
        )
        menu.addSeparator()
        act_del = menu.addAction("删除")
        chosen = menu.exec(pos)
        if chosen is act_copy:
            self.copy_item(item)
        elif chosen is act_view:
            self.show_source(item)
        elif act_save is not None and chosen is act_save:
            self.save_image(item)
        elif act_reveal is not None and chosen is act_reveal:
            self.reveal_file(item)
        elif chosen is act_del:
            self.deleteRequested.emit(item)

    def show_source(self, item: MessageItem) -> None:
        if item.kind == "text":
            box = QMessageBox(self)
            box.setWindowTitle("消息原文")
            box.setText(item.text)
            box.exec()
            return
        if item.kind == "file":
            from ...history import format_size

            box = QMessageBox(self)
            box.setWindowTitle("文件信息")
            box.setText(
                f"{item.file_name}\n大小：{format_size(item.file_size)}\n"
                f"类型：{item.mime_type or '-'}\n路径：{item.local_path or '-'}"
            )
            box.exec()
            return
        pix = ensure_thumb(item)
        if pix is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("图片预览")
        layout = QVBoxLayout(dialog)
        label = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(item.content)
        label.setPixmap(pixmap)
        label.setScaledContents(False)
        layout.addWidget(label)
        dialog.resize(min(900, pixmap.width() + 40), min(700, pixmap.height() + 60))
        dialog.exec()

    def save_image(self, item: MessageItem) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存图片", "image.png", "PNG 图片 (*.png)")
        if not path:
            return
        from PyQt6.QtGui import QImage

        image = QImage(item.content)
        if not path.lower().endswith(".png"):
            path += ".png"
        image.save(path)
        self.copied.emit(f"已保存到 {path}")


def QUrl_from_local(path: str):
    from PyQt6.QtCore import QUrl

    return QUrl.fromLocalFile(path)


def make_item(**kwargs) -> MessageItem:
    return MessageItem(key=uuid4().hex[:12], **kwargs)
