from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import app_root

log = logging.getLogger("blechat.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id       INTEGER NOT NULL,
  network_id   TEXT    NOT NULL,
  direction    TEXT    NOT NULL,
  peer_id      TEXT,
  peer_name    TEXT,
  kind         TEXT    NOT NULL,
  content      BLOB    NOT NULL,
  created_at   INTEGER NOT NULL,
  file_name    TEXT,
  file_size    INTEGER,
  mime_type    TEXT,
  local_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_msg_network ON messages(network_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_id ON messages(network_id, msg_id);
"""

# 老库（无文件字段）升级用；CREATE TABLE IF NOT EXISTS 不会补列，必须显式 ALTER。
MIGRATIONS = (
    "ALTER TABLE messages ADD COLUMN file_name TEXT",
    "ALTER TABLE messages ADD COLUMN file_size INTEGER",
    "ALTER TABLE messages ADD COLUMN mime_type TEXT",
    "ALTER TABLE messages ADD COLUMN local_path TEXT",
)


@dataclass(slots=True)
class Message:
    id: int
    msg_id: int
    network_id: str
    direction: str
    peer_id: str | None
    peer_name: str | None
    kind: str
    content: bytes
    created_at: int
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    local_path: str | None = None

    @property
    def text(self) -> str:
        if self.kind == "text":
            return self.content.decode("utf-8", "replace")
        if self.kind == "file":
            name = self.file_name or "文件"
            size = f"（{format_size(self.file_size or 0)}）" if self.file_size else ""
            return f"📄 {name}{size}"
        return "[图片]"

    @property
    def when(self) -> str:
        lt = time.localtime(self.created_at)
        now = time.localtime()
        if (lt.tm_year, lt.tm_mon, lt.tm_mday) == (now.tm_year, now.tm_mon, now.tm_mday):
            return time.strftime("%H:%M", lt)
        return time.strftime("%m-%d %H:%M", lt)


def format_size(n: int) -> str:
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class History:
    def __init__(self, path: Path | None = None, days: int = 3) -> None:
        self.path = path or (app_root() / "history.db")
        self.days = max(1, days)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        try:
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        except sqlite3.Error:
            return
        for stmt in MIGRATIONS:
            column = stmt.split("ADD COLUMN ")[1].split(" ")[0]
            if column in have:
                continue
            try:
                self._conn.execute(stmt)
            except sqlite3.Error:
                pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def add(
        self,
        *,
        msg_id: int,
        network_id: str,
        direction: str,
        kind: str,
        content: bytes,
        peer_id: str | None = None,
        peer_name: str | None = None,
        created_at: int | None = None,
        file_name: str | None = None,
        file_size: int | None = None,
        mime_type: str | None = None,
        local_path: str | None = None,
    ) -> None:
        ts = int(created_at or time.time())
        values = (
            network_id,
            direction,
            peer_id,
            peer_name,
            kind,
            content,
            ts,
            file_name,
            file_size,
            mime_type,
            local_path,
        )
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO messages"
                " (msg_id, network_id, direction, peer_id, peer_name, kind, content, created_at,"
                " file_name, file_size, mime_type, local_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, *values),
            )
            if cur.rowcount == 0:
                # (network_id, msg_id) 唯一索引挡下了这次插入。
                # 只有"同网络 + 同方向 + 同内容 + 同秒"才算真·重复；
                # 否则是另一个会话复用了 msg_id —— 静默丢就是用户报的"丢消息"，
                # 改用主键负值兜底（真实 msg_id 恒 >= 0）。
                dup = self._conn.execute(
                    "SELECT 1 FROM messages"
                    " WHERE network_id=? AND direction=? AND kind=? AND content=?"
                    " AND created_at=? AND COALESCE(peer_name, '')=?"
                    " AND COALESCE(file_name, '')=? LIMIT 1",
                    (
                        network_id,
                        direction,
                        kind,
                        content,
                        ts,
                        peer_name or "",
                        file_name or "",
                    ),
                ).fetchone()
                if dup:
                    pass  # 真·重复入库（同一条消息被重放），去重
                else:
                    row = self._conn.execute(
                        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM messages"
                    ).fetchone()
                    fallback = -int(row["n"]) - 1
                    log.warning(
                        "msg_id collision in %s (%s) -> stored as %s",
                        network_id,
                        msg_id,
                        fallback,
                    )
                    self._conn.execute(
                        "INSERT INTO messages"
                        " (msg_id, network_id, direction, peer_id, peer_name, kind, content,"
                        " created_at, file_name, file_size, mime_type, local_path)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fallback, *values),
                    )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("history add failed: %s", exc)

    def set_local_path(self, network_id: str, msg_id: int, local_path: str) -> None:
        """接收方把文件落到磁盘后回填路径。"""
        try:
            self._conn.execute(
                "UPDATE messages SET local_path=? WHERE network_id=? AND msg_id=?",
                (local_path, network_id, int(msg_id)),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def remap_network(self, old_id: str, new_id: str) -> int:
        """把孤儿 network_id（如 ad-hoc:XX:XX…）并入真实 network_id。

        **不能用 `UPDATE OR IGNORE`**：`(network_id, msg_id)` 唯一索引一旦撞号，
        旧行会被静默跳过、永远留在 ad-hoc 下 → 用户看到"历史被清空/丢消息"。
        这里先给撞号的行改一个不冲突的 msg_id（主键负值），再搬。
        """
        if not old_id or not new_id or old_id == new_id:
            return 0
        try:
            old_rows = self._conn.execute(
                "SELECT id, msg_id FROM messages WHERE network_id=?", (old_id,)
            ).fetchall()
            if not old_rows:
                return 0
            occupied = {
                int(r["msg_id"])
                for r in self._conn.execute(
                    "SELECT msg_id FROM messages WHERE network_id=?", (new_id,)
                )
            }
            moved = 0
            renumbered = 0
            for row in old_rows:
                row_id = int(row["id"])
                msg_id = int(row["msg_id"])
                if msg_id in occupied:
                    msg_id = -row_id
                    self._conn.execute(
                        "UPDATE messages SET msg_id=? WHERE id=?", (msg_id, row_id)
                    )
                    renumbered += 1
                occupied.add(msg_id)
                self._conn.execute(
                    "UPDATE messages SET network_id=? WHERE id=?", (new_id, row_id)
                )
                moved += 1
            self._conn.commit()
            if renumbered:
                log.info(
                    "remap renumbered %s colliding rows %s -> %s", renumbered, old_id, new_id
                )
            return moved
        except sqlite3.Error as exc:
            log.warning("remap %s -> %s failed: %s", old_id, new_id, exc)
            return 0

    @staticmethod
    def _norm_addr(value: str) -> str:
        """MAC 归一：去掉分隔符、统一大写，兼容 `8C:C6…` / `8c-c6…`。"""
        return re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()

    def ad_hoc_ids(self) -> list[str]:
        try:
            cur = self._conn.execute(
                "SELECT DISTINCT network_id FROM messages WHERE network_id LIKE 'ad-hoc:%'"
            )
        except sqlite3.Error:
            return []
        return [str(r["network_id"]) for r in cur.fetchall()]

    def networks_with_rows(self) -> list[str]:
        try:
            cur = self._conn.execute(
                "SELECT DISTINCT network_id FROM messages ORDER BY network_id"
            )
        except sqlite3.Error:
            return []
        return [str(r["network_id"]) for r in cur.fetchall()]

    def most_recent_network_id(self) -> str | None:
        """库里最近写入过的 network_id。

        `network_id` 换号（启动失败重铸 uuid4、按 MAC 直连首次加入…）**不等于**
        历史被删 —— 行都还在，只是按新 id 查不到，界面看起来就是「被清空」。
        """
        try:
            cur = self._conn.execute(
                "SELECT network_id FROM messages ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        except sqlite3.Error:
            return None
        row = cur.fetchone()
        return str(row["network_id"]) if row else None

    def count_by_network(self) -> dict[str, int]:
        """每个 `network_id` 各有多少行 —— 排查「历史被清空」的第一手证据。

        界面空白时，把这张表打出来就能立刻分清两种情况：
        * 表非空 ⇒ 行还在，只是**按错了 id 查**（network_id 换号）；
        * 表为空 ⇒ 真的没有行（首次运行 / 被清理 / 写库失败）。
        """
        try:
            cur = self._conn.execute(
                "SELECT network_id, COUNT(*) AS n FROM messages"
                " GROUP BY network_id ORDER BY n DESC"
            )
        except sqlite3.Error:
            return {}
        return {str(r["network_id"]): int(r["n"]) for r in cur.fetchall()}

    def _rows(self, sql: str, params: tuple) -> list[Message]:
        try:
            cur = self._conn.execute(sql, params)
        except sqlite3.Error:
            return []
        return [Message(**dict(r)) for r in cur.fetchall()]

    def recent(self, network_id: str | None = None, limit: int = 500) -> list[Message]:
        if network_id:
            return self._rows(
                "SELECT * FROM messages WHERE network_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (network_id, limit),
            )
        return self._rows("SELECT * FROM messages ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))

    def search(self, text: str, network_id: str | None = None, limit: int = 500) -> list[Message]:
        pattern = f"%{text}%"
        if network_id:
            return self._rows(
                "SELECT * FROM messages WHERE network_id=? AND (content LIKE ? OR file_name LIKE ?)"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (network_id, pattern, pattern, limit),
            )
        return self._rows(
            "SELECT * FROM messages WHERE content LIKE ? OR file_name LIKE ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (pattern, pattern, limit),
        )

    def delete(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        try:
            cur = self._conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", [int(i) for i in ids])
            self._conn.commit()
            return cur.rowcount
        except sqlite3.Error:
            return 0

    def delete_by_msg_id(self, network_id: str, msg_id: int) -> int:
        try:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE network_id=? AND msg_id=?", (network_id, int(msg_id))
            )
            self._conn.commit()
            return cur.rowcount
        except sqlite3.Error:
            return 0

    def purge(self, days: int | None = None) -> int:
        days = self.days if days is None else days
        cutoff = int(time.time()) - max(1, days) * 86400
        try:
            cur = self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount
        except sqlite3.Error:
            return 0

    @staticmethod
    def export_json(path: Path, rows: list[Message]) -> None:
        data = [
            {
                "id": m.id,
                "msg_id": m.msg_id,
                "network_id": m.network_id,
                "direction": m.direction,
                "peer_name": m.peer_name,
                "kind": m.kind,
                "content": m.content.decode("utf-8", "replace") if m.kind == "text" else None,
                "created_at": m.created_at,
                "file_name": m.file_name,
                "file_size": m.file_size,
                "mime_type": m.mime_type,
                "local_path": m.local_path,
            }
            for m in rows
        ]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
