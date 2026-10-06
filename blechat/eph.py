from __future__ import annotations

import time

from .config import atomic_write_json, app_root, read_json


class EphStore:
    """临时密钥：真实 key 只在内存，落盘只有 hash + 过期时间。"""

    def __init__(self, root=None) -> None:
        self.root = root or app_root()
        self.path = self.root / "keys" / "eph_keys.json"
        raw = read_json(self.path, {})
        self.entries: dict[str, dict] = {e["eph_id"]: dict(e) for e in raw.get("keys", []) if "eph_id" in e}
        self.live: dict[str, tuple[bytes, float]] = {}

    def save(self) -> None:
        atomic_write_json(self.path, {"keys": list(self.entries.values())})

    def create(self, key: bytes, eph_id: str, ttl: int = 3600) -> float:
        from .crypto import eph_key_hash

        expires_at = time.time() + ttl
        self.entries[eph_id] = {
            "eph_id": eph_id,
            "eph_key_hash": eph_key_hash(key),
            "expires_at": int(expires_at),
        }
        self.live[eph_id] = (key, expires_at)
        self.purge_expired(save=False)
        self.save()
        return expires_at

    def lookup(self, eph_id: str):
        """Host 握手用：返回 (key, expires_at)；内存没有或过期 → None。"""
        entry = self.live.get(eph_id)
        if entry is None:
            return None
        key, expires_at = entry
        if expires_at <= time.time():
            self.live.pop(eph_id, None)
            return None
        return key, expires_at

    def consume(self, eph_id: str) -> None:
        self.live.pop(eph_id, None)
        if eph_id in self.entries:
            self.entries.pop(eph_id, None)
            self.save()

    def purge_expired(self, save: bool = True) -> int:
        now = time.time()
        removed = 0
        for eph_id in [k for k, e in self.entries.items() if float(e.get("expires_at", 0)) <= now]:
            self.entries.pop(eph_id, None)
            removed += 1
        for eph_id in [k for k, (_, exp) in self.live.items() if exp <= now]:
            self.live.pop(eph_id, None)
            removed += 1
        if removed and save:
            self.save()
        return removed

    def count(self) -> int:
        return len(self.entries)
