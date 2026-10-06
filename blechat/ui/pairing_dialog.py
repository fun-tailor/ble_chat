from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QLineEdit, QToolButton, QVBoxLayout)

from .. import crypto

MEMORY_HINT = "记住 30 分钟（仅内存，不落盘）"


class PairingDialog(QDialog):
    """密码输入对话框。

    mode = "join"    校验已有网络的 verifier，返回密码
    mode = "create"  Host 首次设置新密码（需输入两次）
    """

    accepted_password = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        mode: str = "join",
        network_name: str = "",
        auth: dict | None = None,
        remember: bool = True,
        gate=None,
        on_result=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Dialog")
        self._mode = mode
        self._auth = auth
        self._gate = gate
        self._on_result = on_result
        self._password = ""
        self.setWindowTitle("设置密码" if mode == "create" else "输入网络密码")
        self.setMinimumWidth(380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel("设置共享密码" if mode == "create" else "加入网络")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        desc = QLabel(
            f"网络：{network_name or '未命名'}\n"
            + ("此密码用于所有加入该网络的设备，请与对端一致（至少 4 位）。"
               if mode == "create"
               else "请输入 Host 设置的共享密码，本地校验后才会发起握手。")
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.password_edit = QLineEdit(self)
        self.password_edit.setObjectName("PasswordEdit")
        self.password_edit.setPlaceholderText("共享密码")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setClearButtonEnabled(False)
        self.password_edit.setMinimumHeight(32)
        row.addWidget(self.password_edit, 1)

        self.eye_button = QToolButton(self)
        self.eye_button.setText("显示")
        self.eye_button.setCheckable(True)
        self.eye_button.setFixedHeight(32)
        self.eye_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye_button.toggled.connect(self._toggle_echo)
        row.addWidget(self.eye_button)
        layout.addLayout(row)

        self.confirm_edit: QLineEdit | None = None
        if mode == "create":
            self.confirm_edit = QLineEdit(self)
            self.confirm_edit.setObjectName("PasswordEdit")
            self.confirm_edit.setPlaceholderText("再次输入密码")
            self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.confirm_edit.setMinimumHeight(32)
            layout.addWidget(self.confirm_edit)

        self.remember_box = QCheckBox(MEMORY_HINT, self)
        self.remember_box.setChecked(remember)
        self.remember_box.setVisible(mode == "join")
        layout.addWidget(self.remember_box)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #C42B1C;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("PrimaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.password_edit.returnPressed.connect(self.accept)
        if self.confirm_edit is not None:
            self.confirm_edit.returnPressed.connect(self.accept)

    # ------------------------------------------------------------- helpers
    @property
    def password(self) -> str:
        return self._password

    @property
    def remember(self) -> bool:
        return self.remember_box.isChecked()

    def _toggle_echo(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(mode)
        self.eye_button.setText("隐藏" if checked else "显示")

    def _error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Escape,):
            self.reject()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------- accept
    def accept(self) -> None:
        if self._gate is not None:
            waiting = float(self._gate())
            if waiting > 0:
                self._error(f"尝试次数过多，请 {int(waiting) + 1} 秒后再试")
                return
        password = self.password_edit.text()
        if len(password) < 4:
            self._error("密码至少 4 位")
            return
        if self._mode == "create":
            if self.confirm_edit is None or self.confirm_edit.text() != password:
                self._error("两次输入的密码不一致")
                return
            ok = True
        else:
            ok = self._auth is None or crypto.verify_password(password, self._auth)
            if not ok:
                if self._on_result is not None:
                    self._on_result(False)
                self._error("密码错误")
                return
        if self._on_result is not None:
            self._on_result(True)
        self._password = password
        self.accepted_password.emit(password)
        super().accept()

    @staticmethod
    def ask(parent, **kwargs) -> tuple[str, bool] | None:
        """返回 (password, remember) 或 None（取消）。"""
        dialog = PairingDialog(parent, **kwargs)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.password, dialog.remember
        return None
