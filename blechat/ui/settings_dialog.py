"""设置对话框：主题 / 别名 / 凭证 / 表情 / 文件接收 / 历史 / 系统配对 / 诊断日志。"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox,
                             QVBoxLayout)

from ..config import Config


@dataclass(frozen=True)
class SettingsResult:
    """`apply()` 的结果。以前只返回"主题是否变化"的 bool，多一个维度就得再改
    调用方 —— 换成结构体后加字段不用动签名。"""

    theme_changed: bool = False
    log_level: str = "INFO"


class SettingsDialog(QDialog):
    def __init__(self, config: Config, alias: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._config = config

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self.alias_edit = QLineEdit(alias, self)
        self.alias_edit.setPlaceholderText("对方看到的名字，留空用系统设备名")
        self.alias_edit.setMaxLength(32)
        form.addRow("本机别名", self.alias_edit)

        self.theme_combo = QComboBox(self)
        self.theme_combo.addItem("跟随系统", "auto")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        index = max(0, [self.theme_combo.itemData(i) for i in range(self.theme_combo.count())].index(
            config.theme if config.theme in ("auto", "light", "dark") else "auto"
        ))
        self.theme_combo.setCurrentIndex(index)
        form.addRow("主题", self.theme_combo)

        self.history_spin = QSpinBox(self)
        self.history_spin.setRange(1, 90)
        self.history_spin.setValue(config.history_days)
        self.history_spin.setSuffix(" 天")
        form.addRow("历史保留", self.history_spin)

        self.persist_check = QCheckBox("记住网络密码（Windows DPAPI 加密保存）", self)
        self.persist_check.setChecked(config.persist_credentials)
        form.addRow("", self.persist_check)

        self.emoji_check = QCheckBox("显示表情按钮", self)
        self.emoji_check.setChecked(config.enable_emoji)
        form.addRow("", self.emoji_check)

        self.log_check = QCheckBox("写入 log 文件", self)
        self.log_check.setChecked(config.log_file)
        form.addRow("", self.log_check) 

        self.pairing_check = QCheckBox("使用 Windows 系统配对（多数情况保持关闭）", self)
        self.pairing_check.setChecked(config.system_pairing)
        form.addRow("", self.pairing_check)

        self.autosave_check = QCheckBox("收到文件后自动保存到目录", self)
        self.autosave_check.setChecked(config.auto_save_files)
        form.addRow("", self.autosave_check)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(config.receive_dir, self)
        self.dir_edit.setPlaceholderText("留空 = 每次询问")
        browse = QPushButton("浏览…", self)
        browse.setObjectName("GhostButton")
        browse.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        form.addRow("接收目录", dir_row)

        self.log_combo = QComboBox(self)
        self.log_combo.addItem("普通（INFO）", "INFO")
        self.log_combo.addItem("详细（DEBUG，排查连接/历史问题用）", "DEBUG")
        level = (config.log_level or "INFO").upper()
        self.log_combo.setCurrentIndex(0 if level != "DEBUG" else 1)
        self.log_combo.setToolTip(
            "选“详细”后会在 logs/app.log 里记录每一次连接尝试、端口自愈、\n"
            "历史按哪个 network_id 取行等信息 — 报障时请打包这个文件。"
        )
        form.addRow("诊断日志", self.log_combo)

        root.addLayout(form)

        self.clear_button = QPushButton("清除已保存的网络密码…", self)
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.clicked.connect(self._forget_credentials)
        root.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择接收目录", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _forget_credentials(self) -> None:
        from PyQt6.QtWidgets import QMessageBox

        from .. import credentials

        answer = QMessageBox.question(
            self,
            "清除密码",
            "将删除本机保存的全部网络密码，下次启动 Host/加入时需要重新输入。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # 必须显式带上本机 root：`forget_all()` 的默认参数是 `app_root()`，
        # 打包/源码两种运行方式虽然一致，但显式传参才不会在测试或换 root 时清错文件。
        credentials.forget_all(self._config.path.parent)
        QMessageBox.information(self, "清除密码", "已清除。")

    # ------------------------------------------------------------- results
    @property
    def new_alias(self) -> str:
        return self.alias_edit.text().strip()

    def apply(self) -> SettingsResult:
        """写回 config，返回本次变更摘要。"""
        theme = self.theme_combo.currentData()
        changed_theme = theme != self._config.theme
        self._config.theme = theme
        self._config.history_days = int(self.history_spin.value())
        self._config.persist_credentials = self.persist_check.isChecked()
        self._config.enable_emoji = self.emoji_check.isChecked()
        self._config.system_pairing = self.pairing_check.isChecked()
        self._config.auto_save_files = self.autosave_check.isChecked()
        self._config.receive_dir = self.dir_edit.text().strip()
        self._config.log_file = self.log_check.isChecked() #日志
        level = str(self.log_combo.currentData() or "INFO").upper()
        self._config.log_level = level
        return SettingsResult(theme_changed=changed_theme, log_level=level)
