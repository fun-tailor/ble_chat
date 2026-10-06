from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import atomic_write_json, app_root, read_json


@dataclass
class Identity:
    device_id: str
    name: str
    created_at: int
    alias: str = ""


def default_name() -> str:
    import os
    import socket

    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC"


def display_name(ident: Identity) -> str:
    """用户自定义昵称优先，否则回退系统设备名。"""
    return (ident.alias or "").strip() or ident.name


def load_identity(root: Path | None = None) -> Identity:
    root = root or app_root()
    path = root / "keys" / "identity.json"
    raw = read_json(path, {})
    device_id = raw.get("device_id")
    if not device_id:
        ident = Identity(device_id=str(uuid.uuid4()), name=default_name(), created_at=int(time.time()))
        save_identity(ident, root)
        return ident
    return Identity(
        device_id=str(device_id),
        name=str(raw.get("name") or default_name()),
        created_at=int(raw.get("created_at", 0)),
        alias=str(raw.get("alias") or ""),
    )


def save_identity(ident: Identity, root: Path | None = None) -> None:
    root = root or app_root()
    atomic_write_json(
        root / "keys" / "identity.json",
        {
            "device_id": ident.device_id,
            "name": ident.name,
            "created_at": ident.created_at,
            "alias": ident.alias,
        },
    )
