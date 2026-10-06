from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QTimer, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPushButton, QTableView,
                             QVBoxLayout)

from ..history import History, Message

COLUMNS = ("时间", "方向", "来源", "内容")


def copyable_text(message: Message) -> str:
    if message.kind == "file" and message.local_path:
        return message.local_path
    if message.kind == "image":
        return "[图片]"
    return message.text


class HistoryModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[tuple[str, object]] = []  # ("day", str) | ("msg", Message)

    def set_rows(self, rows: list[tuple[str, object]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        kind, payload = self.rows[index.row()]
        if kind == "day":
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
                return f"—— {payload} ——" if index.column() == 0 else ""
            if role == Qt.ItemDataRole.ForegroundRole:
                return QColor("#8A8A8A")
            return None
        message: Message = payload  # type: ignore[assignment]
        if role == Qt.ItemDataRole.UserRole + 1:
            return message
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return message.when
            if index.column() == 1:
                return "我" if message.direction == "out" else "对方"
            if index.column() == 2:
                return message.peer_name or (message.peer_id or "")[:8]
            return message.text
        if role == Qt.ItemDataRole.ToolTipRole:
            return message.text
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor("#5E5E5E")
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        kind, _ = self.rows[index.row()]
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base


class HistoryDialog(QDialog):
    """历史消息：按天分组 + 搜索 + 复制/删除/导出。"""

    def __init__(self, history: History, network_id: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Dialog")
        self.setWindowTitle("历史消息")
        self.resize(760, 520)
        self.setMinimumSize(560, 360)
        self.setModal(True)
        self._history = history
        self._network_id = network_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("历史消息")
        title.setObjectName("DialogTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("SearchEdit")
        self.search_edit.setPlaceholderText("搜索消息内容…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(240)
        head.addWidget(self.search_edit)
        layout.addLayout(head)

        self.table = QTableView(self)
        self.table.setObjectName("HistoryTable")
        self.model = HistoryModel(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._copy_row)
        layout.addWidget(self.table, 1)

        self.count_label = QLabel("", self)
        self.count_label.setObjectName("MutedLabel")
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.copy_button = QPushButton("复制选中", self)
        self.delete_button = QPushButton("删除选中", self)
        self.purge_button = QPushButton("清空全部", self)
        self.export_button = QPushButton("导出 JSON…", self)
        self.close_button = QPushButton("关闭", self)
        self.close_button.setObjectName("PrimaryButton")
        self.copy_button.clicked.connect(self._copy_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.purge_button.clicked.connect(self._purge_all)
        self.export_button.clicked.connect(self._export)
        self.close_button.clicked.connect(self.accept)
        for button in (self.copy_button, self.delete_button, self.purge_button, self.export_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.reload)
        self.search_edit.textChanged.connect(lambda _: self._debounce.start())

        self.reload()

    # ------------------------------------------------------------- data
    def _messages(self) -> list[Message]:
        query = self.search_edit.text().strip()
        if query:
            return self._history.search(query, self._network_id)
        return self._history.recent(self._network_id)

    def reload(self) -> None:
        messages = self._messages()
        rows: list[tuple[str, object]] = []
        last_day = ""
        for message in messages:
            day = time.strftime("%Y-%m-%d %A", time.localtime(message.created_at))
            if day != last_day:
                rows.append(("day", day))
                last_day = day
            rows.append(("msg", message))
        self.model.set_rows(rows)
        self.count_label.setText(f"共 {len(messages)} 条（最多显示 500 条）")

    def _selected_messages(self) -> list[Message]:
        out: list[Message] = []
        for index in self.table.selectionModel().selectedRows():
            kind, payload = self.model.rows[index.row()]
            if kind == "msg":
                out.append(payload)  # type: ignore[arg-type]
        return out

    # ------------------------------------------------------------- actions
    def _copy_row(self, index: QModelIndex) -> None:
        kind, payload = self.model.rows[index.row()]
        if kind != "msg":
            return
        message: Message = payload  # type: ignore[assignment]
        QApplication.clipboard().setText(copyable_text(message))
        self.count_label.setText("已复制到剪贴板")

    def _copy_selected(self) -> None:
        messages = self._selected_messages()
        if not messages:
            self.count_label.setText("未选中消息")
            return
        QApplication.clipboard().setText("\n".join(m.text for m in messages))
        self.count_label.setText(f"已复制 {len(messages)} 条")

    def _delete_selected(self) -> None:
        messages = self._selected_messages()
        if not messages:
            self.count_label.setText("未选中消息")
            return
        self._history.delete([m.id for m in messages])
        self.reload()

    def _purge_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "清空历史",
            "确定删除全部历史消息吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ids = [m.id for m in self._history.recent(self._network_id, limit=100000)]
        self._history.delete(ids)
        self.reload()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出历史", "history.json", "JSON 文件 (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        History.export_json(Path(path), self._messages())
        self.count_label.setText(f"已导出到 {path}")
