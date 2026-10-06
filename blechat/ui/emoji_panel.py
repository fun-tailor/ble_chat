"""表情面板：点选插入到输入框光标处。默认关闭，需在设置里打开。"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QGridLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout

# 分组：常用 / 符号 / 手势
# 注意：这里必须使用 .split() 而不是 list()，否则多字符 Emoji（如 ❤️）会被拆散导致排版错乱
EMOJI_GROUPS: list[tuple[str, list[str]]] = [
    (
        "常用",
        (
            "😀 😃 😄 😁 😆 😅 😂 🙂 😉 😊 😍 😘 😜 🤪 🤔 🤨 😐 😴 🥳 😎 🤓 😭 😱 🤗 🤝 "
            "👍 👎 👏 🙏 💪"
        ).split(),
    ),
    (
        "符号",
        (
            "❤️ 💙 💚 💛 🧡 💜 🖤 ✨ ⭐ 🌟 💡 🎉 🎊 🔥 💯 ✅ ❌ ⚠️ ➡️ ⬅️ ⬆️ ⬇️ ♻️ 🔒 🔓 ⏰ "
            "📌 📍 🏳️ 🏴"
        ).split(),
    ),
    (
        "手势",
        (
            "👌 ✌️ 🤞 ☝️ 👈 👉 👆 👇 🤫 🙄 😶 🤐 🥱 🤒 🤕 🥶 🥵 🥴 😵 🤠 👻 🤖 🦾"
        ).split(),
    ),
]

COLS = 8


class EmojiPanel(QFrame):
    """表情面板组件，点击表情后通过 chosen 信号发射所选表情字符串。"""
    chosen = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("EmojiPanel")
        
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QFrame()
        host = QVBoxLayout(body)
        host.setContentsMargins(0, 0, 0, 0)
        host.setSpacing(6)

        for title, emojis in EMOJI_GROUPS:
            caption = QLabel(title, body)
            caption.setObjectName("MutedLabel")
            host.addWidget(caption)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)  # 稍微增加间距，让视觉更舒适
            
            for i, emoji in enumerate(emojis):
                button = QToolButton(body)
                button.setText(emoji)
                button.setFixedSize(32, 32)  # 略微调大按钮，给复杂 Emoji 留出空间
                button.setAutoRaise(True)
                button.setToolTip(f"插入 {emoji}")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                # 使用默认参数 e=emoji 固定当前循环变量，避免闭包问题
                button.clicked.connect(lambda _=False, e=emoji: self.chosen.emit(e))
                grid.addWidget(button, i // COLS, i % COLS)
                
            host.addLayout(grid)

        host.addStretch(1)
        scroll.setWidget(body)
        scroll.setMinimumWidth(320)   # 调大最小宽度，确保 8 列排布宽松
        scroll.setMinimumHeight(220)
        outer.addWidget(scroll)


# ================= 测试运行代码 =================
if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)

    # 模拟主窗口
    window = QFrame()
    window.setWindowTitle("Emoji Panel Test")
    window.resize(400, 500)
    
    layout = QVBoxLayout(window)
    layout.setContentsMargins(10, 10, 10, 10)
    
    panel = EmojiPanel(window)
    layout.addWidget(panel)
    
    # 打印点击的表情
    panel.chosen.connect(lambda e: print(f"选中表情: {e}"))

    window.show()
    sys.exit(app.exec())