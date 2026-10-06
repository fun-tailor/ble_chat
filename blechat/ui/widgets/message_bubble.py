from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap

THUMB_MAX_W = 240
THUMB_MAX_H = 200
RADIUS = 12
PAD_X = 10
PAD_Y = 7
HEADER_H = 16
GAP = 8
MAX_DISPLAY_CHARS = 500  # 气泡内最多展示这么多字符，超出截断（双击仍可复制全文）

_thumb_cache: dict[str, QPixmap] = {}


@dataclass
class MessageItem:
    key: str
    msg_id: int
    direction: str  # 'in' | 'out'
    peer_id: str
    peer_name: str
    kind: str  # 'text' | 'image' | 'file'
    content: bytes
    created_at: int
    status: str = ""  # '' | 'sending' | 'failed'
    progress: tuple[int, int] | None = None
    thumb: QPixmap | None = field(default=None, repr=False)
    file_name: str = ""
    file_size: int = 0
    mime_type: str = ""
    local_path: str = ""

    @property
    def is_out(self) -> bool:
        return self.direction == "out"

    @property
    def text(self) -> str:
        """完整文本（复制、历史用）。"""
        if self.kind == "text":
            return self.content.decode("utf-8", "replace")
        if self.kind == "file":
            from ...history import format_size

            name = self.file_name or "文件"
            return f"📄 {name}（{format_size(self.file_size)}）"
        return "[图片]"

    @property
    def display_text(self) -> str:
        """气泡里展示的文本：限长 + 省略号。"""
        text = self.text
        if len(text) <= MAX_DISPLAY_CHARS:
            return text
        return text[:MAX_DISPLAY_CHARS] + "…"


def time_label(ts: int) -> str:
    lt = time.localtime(ts)
    now = time.localtime()
    if (lt.tm_year, lt.tm_mon, lt.tm_mday) == (now.tm_year, now.tm_mon, now.tm_mday):
        return time.strftime("%H:%M", lt)
    return time.strftime("%m-%d %H:%M", lt)


def header_label(item: MessageItem) -> str:
    who = "我" if item.is_out else (item.peer_name or "对方")
    return f"{who} {time_label(item.created_at)}"


def load_thumb(content: bytes) -> QPixmap | None:
    digest = hashlib.sha1(content[:4096] + str(len(content)).encode()).hexdigest()
    cached = _thumb_cache.get(digest)
    if cached is not None:
        return cached
    pix = QPixmap()
    if not pix.loadFromData(content):
        return None
    scaled = pix
    if pix.width() > THUMB_MAX_W or pix.height() > THUMB_MAX_H:
        scaled = pix.scaled(
            THUMB_MAX_W,
            THUMB_MAX_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if len(_thumb_cache) < 64:
        _thumb_cache[digest] = scaled
    return scaled


def ensure_thumb(item: MessageItem) -> QPixmap | None:
    if item.thumb is None and item.kind == "image":
        item.thumb = load_thumb(item.content)
    return item.thumb


def status_text(item: MessageItem) -> str:
    if item.progress:
        done, total = item.progress
        if total:
            return f"发送中 {done}/{total}"
    if item.status == "sending":
        return "发送中…"
    if item.status == "failed":
        return "发送失败"
    return ""
