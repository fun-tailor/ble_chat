from __future__ import annotations

import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QToolButton, QVBoxLayout)

from .. import crypto
from ..ble.client import FoundDevice
from ..config import Network

ADDR_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class JoinDialog(QDialog):
    """选择要加入的 Host：扫描列表 / 按地址直连 + 密码或临时密钥。"""

    rescanRequested = pyqtSignal()

    def __init__(
        self,
        devices: list[FoundDevice],
        networks: list[Network] | None = None,
        *,
        hint: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Dialog")
        self.setWindowTitle("加入网络")
        self.resize(520, 560)
        self.setMinimumSize(440, 480)
        self.setModal(True)
        self._devices = list(devices)
        self._networks = list(networks or [])
        self._network: Network | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(8)

        title = QLabel("加入网络")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        head = QHBoxLayout()
        self.hint_label = QLabel(hint or "扫描附近的 Host；也可以直接输入地址。", self)
        self.hint_label.setObjectName("MutedLabel")
        self.hint_label.setWordWrap(True)
        head.addWidget(self.hint_label, 1)
        self.rescan_button = QPushButton("重新扫描", self)
        self.rescan_button.clicked.connect(self.rescanRequested)
        head.addWidget(self.rescan_button)
        layout.addLayout(head)

        self.list_widget = QListWidget(self)
        self.list_widget.setMinimumHeight(140)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.list_widget, 1)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(6)
        self.address_edit = QLineEdit(self)
        self.address_edit.setPlaceholderText("或直接输入地址（AA:BB:CC:DD:EE:FF），扫描不可用时用这个")
        self.address_edit.textChanged.connect(self._refresh_hint)
        manual_row.addWidget(self.address_edit, 1)
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("名称（可选）")
        self.name_edit.setMaximumWidth(130)
        manual_row.addWidget(self.name_edit)
        layout.addLayout(manual_row)

        self.eph_box = QCheckBox("用临时密钥加入（Host 生成的一次性密钥）", self)
        layout.addWidget(self.eph_box)

        self.eph_id_edit = QLineEdit(self)
        self.eph_id_edit.setPlaceholderText("短码（8 位 HEX，如 A7F3C9D1）")
        self.eph_id_edit.setMaxLength(8)
        self.eph_id_edit.setMaximumHeight(32)
        self.eph_id_edit.hide()
        layout.addWidget(self.eph_id_edit)

        self.eph_key_edit = QLineEdit(self)
        self.eph_key_edit.setPlaceholderText("临时密钥（Base32 分组，可直接粘贴）")
        self.eph_key_edit.setMaximumHeight(32)
        self.eph_key_edit.hide()
        layout.addWidget(self.eph_key_edit)

        self.eph_box.toggled.connect(self._toggle_auth)

        pw_row = QHBoxLayout()
        pw_row.setSpacing(6)
        self.password_edit = QLineEdit(self)
        self.password_edit.setObjectName("PasswordEdit")
        self.password_edit.setPlaceholderText("共享密码")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setMinimumHeight(32)
        pw_row.addWidget(self.password_edit, 1)
        self.eye_button = QToolButton(self)
        self.eye_button.setText("显示")
        self.eye_button.setCheckable(True)
        self.eye_button.setFixedHeight(32)
        self.eye_button.toggled.connect(self._toggle_echo)
        pw_row.addWidget(self.eye_button)
        layout.addLayout(pw_row)

        self.remember_box = QCheckBox("记住 30 分钟（仅内存，不落盘）", self)
        self.remember_box.setChecked(True)
        layout.addWidget(self.remember_box)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("连接")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("PrimaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._render_devices()
        if self._devices:
            self.list_widget.setCurrentRow(0)
        self._refresh_hint()

    # ------------------------------------------------------------- devices
    def set_devices(self, devices: list[FoundDevice], hint: str = "") -> None:
        self._devices = list(devices)
        self._render_devices()
        if self._devices:
            self.list_widget.setCurrentRow(0)
        if hint:
            self.hint_label.setText(hint)
        self._refresh_hint()

    def _render_devices(self) -> None:
        self.list_widget.clear()
        for device in self._devices:
            mark = "  [本服务]" if device.match_service else ""
            label = f"{device.name or '未知设备'}   {device.address}{mark}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, device)
            self.list_widget.addItem(item)
        if not self._devices:
            item = QListWidgetItem("（未发现设备 —— 请用下方地址直连）")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)

    def _toggle_auth(self, checked: bool) -> None:
        self.eph_id_edit.setVisible(checked)
        self.eph_key_edit.setVisible(checked)
        self.password_edit.setVisible(not checked)
        self.eye_button.setVisible(not checked)
        self.remember_box.setVisible(not checked)

    def _toggle_echo(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(mode)
        self.eye_button.setText("隐藏" if checked else "显示")

    # ------------------------------------------------------------- result
    @property
    def address(self) -> str:
        manual = self.address_edit.text().strip()
        if manual:
            return manual
        item = self.list_widget.currentItem()
        if item is not None:
            device = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(device, FoundDevice):
                return device.address
        return ""

    @property
    def name(self) -> str:
        manual_name = self.name_edit.text().strip()
        if manual_name:
            return manual_name
        item = self.list_widget.currentItem()
        if item is not None:
            device = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(device, FoundDevice):
                return device.name
        return ""

    @property
    def use_eph(self) -> bool:
        return self.eph_box.isChecked()

    @property
    def eph_id(self) -> str:
        return self.eph_id_edit.text().strip().upper()

    @property
    def eph_key(self) -> bytes:
        return crypto.parse_eph_key(self.eph_key_edit.text())

    @property
    def password(self) -> str:
        return self.password_edit.text()

    @property
    def remember(self) -> bool:
        return self.remember_box.isChecked()

    @property
    def network(self) -> Network | None:
        return self._network

    def _refresh_hint(self) -> None:
        address = self.address
        self._network = None
        if address and ADDR_RE.match(address):
            for net in self._networks:
                if net.host_address.upper() == address.upper():
                    self._network = net
                    break
        if self._network is not None:
            self.hint_label.setText(f"已知网络：{self._network.host_name or self._network.network_id}（本地校验密码）")
        elif address:
            self.hint_label.setText("新网络：密码将按 Host 下发的参数派生，握手成功后保存。")

    def _error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    # ------------------------------------------------------------- accept
    def accept(self) -> None:
        address = self.address
        if not address:
            self._error("请选择设备或输入地址")
            return
        if not ADDR_RE.match(address):
            self._error("地址格式应为 AA:BB:CC:DD:EE:FF")
            return
        if self.use_eph:
            if len(self.eph_id) != 8 or not all(c in "0123456789ABCDEF" for c in self.eph_id):
                self._error("短码应为 8 位 HEX（如 A7F3C9D1）")
                return
            try:
                crypto.parse_eph_key(self.eph_key_edit.text())
            except Exception as exc:
                self._error(str(exc))
                return
        elif len(self.password) < 4:
            self._error("密码至少 4 位")
            return
        self.error_label.setVisible(False)
        super().accept()
