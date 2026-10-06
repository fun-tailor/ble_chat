"""本机凭证：用 Windows DPAPI 加密 PSK 后落盘（keys/credentials.json）。

SPEC 要求"不存密码本身"——这里存的是**加密后的 PSK**（两端握手真正需要的量），
密文只有本机 + 本 Windows 用户可解，磁盘上不是明文。
任何一步失败都返回 None，由上层降级为弹密码框，绝不阻塞启动。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import time
from ctypes import wintypes
from pathlib import Path

from .config import atomic_write_json, app_root, read_json

log = logging.getLogger("blechat.credentials")

DESCRIPTION = b"blechat-psk-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _to_blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _protect(data: bytes) -> bytes | None:
    """CryptProtectData → 密文。失败返回 None。"""
    try:
        crypt32 = ctypes.windll.crypt32
    except (AttributeError, OSError):
        return None
    src = _to_blob(data)
    out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(src),
        DESCRIPTION,
        None,
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out),
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(out.pbData)
        except Exception:
            pass


def _unprotect(data: bytes) -> bytes | None:
    """CryptUnprotectData → 明文。失败返回 None。"""
    try:
        crypt32 = ctypes.windll.crypt32
    except (AttributeError, OSError):
        return None
    src = _to_blob(data)
    out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(src),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(out),
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(out.pbData)
        except Exception:
            pass


def _path(root: Path | None = None) -> Path:
    return (root or app_root()) / "keys" / "credentials.json"


def save_psk(network_id: str, psk: bytes, root: Path | None = None) -> bool:
    if not network_id or not psk:
        return False
    blob = _protect(psk)
    if blob is None:
        log.warning("DPAPI protect failed; PSK not persisted")
        return False
    raw = read_json(_path(root), {})
    raw[network_id] = {"psk": base64.b64encode(blob).decode("ascii"), "saved_at": int(time.time())}
    try:
        atomic_write_json(_path(root), raw)
    except OSError as exc:
        log.warning("credential write failed: %s", exc)
        return False
    return True


def get_psk(network_id: str, root: Path | None = None) -> bytes | None:
    if not network_id:
        return None
    raw = read_json(_path(root), {})
    entry = raw.get(network_id)
    if not isinstance(entry, dict):
        return None
    try:
        blob = base64.b64decode(entry["psk"])
    except Exception:
        return None
    return _unprotect(blob)


def forget(network_id: str, root: Path | None = None) -> None:
    path = _path(root)
    raw = read_json(path, {})
    if network_id in raw:
        raw.pop(network_id, None)
        try:
            atomic_write_json(path, raw)
        except OSError as exc:
            log.warning("credential delete failed: %s", exc)


def has(network_id: str, root: Path | None = None) -> bool:
    return get_psk(network_id, root) is not None


def forget_all(root: Path | None = None) -> None:
    path = _path(root)
    try:
        atomic_write_json(path, {})
    except OSError as exc:
        log.warning("credential clear failed: %s", exc)
