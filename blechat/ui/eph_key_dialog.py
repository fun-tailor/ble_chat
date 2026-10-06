from __future__ import annotations

import time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout)

from .. import crypto


class EphKeyDialog(QDialog):
    """生成临时密钥后的一次性展示：短码 + Base32 key + 倒计时。"""

    def __init__(
        self,
        parent=None,
        *,
        eph_id: str = "",
        key: bytes = b"",
        expires_at: float = 0.0,
        network_name: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Dialog")
        self.setWindowTitle("临时密钥（只显示一次）")
        self.setMinimumWidth(460)
        self.setModal(True)
        self._expires_at = expires_at

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel("临时密钥已生成")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        desc = QLabel(
            f"网络：{network_name or '未命名'}\n"
            "把下面的短码与密钥抄给对端，对方在加入时选择“临时密钥”并粘贴即可。"
            "密钥只显示这一次，关闭后无法再次查看。"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        code_row = QHBoxLayout()
        code_label = QLabel("短码")
        code_label.setObjectName("MutedLabel")
        code_row.addWidget(code_label)
        self.code_edit = QLineEdit(eph_id, self)
        self.code_edit.setObjectName("CodeEdit")
        self.code_edit.setReadOnly(True)
        self.code_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_edit.setMinimumHeight(40)
        self.code_edit.setStyleSheet("font-family: Consolas; font-size: 26px; letter-spacing: 6px;")
        code_row.addWidget(self.code_edit, 1)
        copy_code = QPushButton("复制", self)
        copy_code.setFixedWidth(72)
        copy_code.clicked.connect(lambda: self._copy(self.code_edit.text(), "短码"))
        code_row.addWidget(copy_code)
        layout.addLayout(code_row)

        key_row = QHBoxLayout()
        key_label = QLabel("密钥")
        key_label.setObjectName("MutedLabel")
        key_row.addWidget(key_label)
        self.key_edit = QLineEdit(crypto.format_eph_key(key) if key else "", self)
        self.key_edit.setObjectName("CodeEdit")
        self.key_edit.setReadOnly(True)
        self.key_edit.setMinimumHeight(34)
        self.key_edit.setStyleSheet("font-family: Consolas; font-size: 14px;")
        key_row.addWidget(self.key_edit, 1)
        copy_key = QPushButton("复制", self)
        copy_key.setFixedWidth(72)
        copy_key.clicked.connect(lambda: self._copy(self.key_edit.text(), "密钥"))
        key_row.addWidget(copy_key)
        layout.addLayout(key_row)

        self.countdown = QLabel("", self)
        self.countdown.setObjectName("MutedLabel")
        self.countdown.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.countdown)

        warn = QLabel("临时密钥 1 小时后过期，且只能使用一次；成功认证后立即作废。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #C42B1C;")
        layout.addWidget(warn)

        close = QPushButton("我已抄好，关闭", self)
        close.setObjectName("PrimaryButton")
        close.setMinimumHeight(34)
        close.clicked.connect(self.accept)
        layout.addWidget(close)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    def _tick(self) -> None:
        left = int(self._expires_at - time.time())
        if left <= 0:
            self.countdown.setText("已过期")
            self._timer.stop()
            return
        minutes, seconds = divmod(left, 60)
        hours, minutes = divmod(minutes, 60)
        self.countdown.setText(f"剩余 {hours:02d}:{minutes:02d}:{seconds:02d}")

    def _copy(self, text: str, what: str) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self.countdown.setText(f"{what}已复制到剪贴板")

    @staticmethod
    def show_key(parent, **kwargs) -> None:
        EphKeyDialog(parent, **kwargs).exec()
