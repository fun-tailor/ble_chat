from __future__ import annotations

import json
import struct
import time
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from uuid import UUID

from . import compress, crypto
from .errors import BleChatError, Err

CTRL_MAGIC = 0xB1
DATA_MAGIC = 0xB2
DATA_HEADER = 16
AAD_LEN = 9
PROTO_VER = 1

FLAG_COMPRESSED = 0x01
FLAG_IMAGE = 0x02
FLAG_LAST = 0x04
FLAG_FILE = 0x08
FLAG_MASK_BASE = FLAG_COMPRESSED | FLAG_IMAGE | FLAG_FILE

MAX_PAYLOAD = 5 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024  # BLE 实测吞吐 10~50KB/s，2MB 约 1~3 分钟
MAX_NAME_LEN = 64

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILE = "file"

HDR_STRUCT = struct.Struct("<BIHHBHI")  # magic, msg_id, seq, total, flags, aad_len, aad_crc32


class CtrlType(IntEnum):
    HELLO = 0x01
    CHALLENGE = 0x02
    AUTH = 0x03
    AUTH_OK = 0x04
    AUTH_FAIL = 0x05
    ACK = 0x06
    BYE = 0x07
    PEERS = 0x08
    PROGRESS = 0x09
    PING = 0x0A
    # 双方状态同步：任一端进入 READY 就发一帧，对端**无条件**刷新一次状态/UI。
    # 用来兜住"单方已连接"（一侧重启/睡眠唤醒后另一侧停在旧状态）。
    # 旧版本按 §7 静默忽略未知类型，因此可以安全追加。
    RESYNC = 0x0B
    # 保活应答：收到 `PING` 回一帧空 `PONG`。
    # 没有它时发送方只能确认"我发得出去"，无法确认"我还收得到" —— 对端软件
    # 已下线但 Windows 还留着 LE 链路时（半开链路），通知写会**静默成功**，
    # 两边能一直停在 READY（界面上"一侧已连接、另一侧按钮灰"）。
    # 旧版本同样静默忽略，代价是它们不回 PONG（见 session.KEEPALIVE_MAX_MISSES）。
    PONG = 0x0C


def chunk_size(mtu: int) -> int:
    return max(8, mtu - 3 - DATA_HEADER)


def _utf8_trunc(data: bytes, limit: int) -> bytes:
    return data[:limit].decode("utf-8", "ignore").encode("utf-8")


# ---------------------------------------------------------------- control frames


def encode_ctrl(ctrl_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise BleChatError(Err.MSG_TOO_LARGE, "control payload too large")
    return bytes([CTRL_MAGIC, int(ctrl_type)]) + struct.pack("<H", len(payload)) + payload


def decode_ctrl(frame: bytes) -> tuple[int, bytes]:
    if len(frame) < 4 or frame[0] != CTRL_MAGIC:
        raise BleChatError(Err.UNKNOWN, "bad control frame")
    ctrl_type = frame[1]
    (length,) = struct.unpack_from("<H", frame, 2)
    if len(frame) < 4 + length:
        raise BleChatError(Err.UNKNOWN, "truncated control frame")
    return ctrl_type, frame[4 : 4 + length]


@dataclass(slots=True)
class Hello:
    device_id: str
    name: str
    nonce_c: bytes
    use_eph: bool = False
    proto_ver: int = PROTO_VER
    eph_id: str = ""

    def encode(self) -> bytes:
        out = UUID(self.device_id).bytes
        name = _utf8_trunc(self.name.encode("utf-8"), MAX_NAME_LEN)
        out += bytes([len(name)]) + name
        out += self.nonce_c + bytes([1 if self.use_eph else 0, self.proto_ver])
        if self.use_eph:
            out += self.eph_id.encode("ascii")[:8].ljust(8, b"\x00")
        return out

    @classmethod
    def decode(cls, payload: bytes) -> Hello:
        if len(payload) < 22:
            raise BleChatError(Err.UNKNOWN, "hello too short")
        device_id = str(UUID(bytes=payload[:16]))
        nlen = payload[16]
        if len(payload) < 17 + nlen + 16 + 2:
            raise BleChatError(Err.UNKNOWN, "hello truncated")
        name = payload[17 : 17 + nlen].decode("utf-8", "replace")
        off = 17 + nlen
        nonce_c = payload[off : off + 16]
        off += 16
        use_eph = payload[off] == 1
        proto_ver = payload[off + 1]
        eph_id = ""
        if use_eph:
            if len(payload) < off + 2 + 8:
                raise BleChatError(Err.UNKNOWN, "hello eph truncated")
            eph_id = payload[off + 2 : off + 10].decode("ascii", "replace")
        return cls(device_id, name, nonce_c, use_eph, proto_ver, eph_id)


@dataclass(slots=True)
class Challenge:
    nonce_s: bytes
    salt: bytes
    iters: int
    use_eph: bool = False

    def encode(self) -> bytes:
        return self.nonce_s + self.salt + struct.pack("<IB", self.iters, 1 if self.use_eph else 0)

    @classmethod
    def decode(cls, payload: bytes) -> Challenge:
        if len(payload) != 16 + 16 + 4 + 1:
            raise BleChatError(Err.UNKNOWN, "challenge bad length")
        nonce_s = payload[:16]
        salt = payload[16:32]
        (iters,) = struct.unpack_from("<I", payload, 32)
        return cls(nonce_s, salt, iters, payload[36] == 1)


@dataclass(slots=True)
class AuthOk:
    session_id: int
    hkdf_salt: bytes
    # Host 的昵称。HELLO 是 C→H，协议里原本**没有任何方向**能带上 Host 的
    # `alias`，client 只能显示 BLE 广播名（Windows 设备名/代号）——
    # 这就是「client 端对 host 的显示还是代号而非昵称」。
    # 追加在 20 字节固定段之后（`decode` 认 `>= 20`，旧端只发 20 字节也兼容）。
    name: str = ""

    def encode(self) -> bytes:
        name = _utf8_trunc(self.name.encode("utf-8"), MAX_NAME_LEN)
        return (
            struct.pack("<I", self.session_id)
            + self.hkdf_salt
            + bytes([len(name)])
            + name
        )

    @classmethod
    def decode(cls, payload: bytes) -> AuthOk:
        if len(payload) < 20:
            raise BleChatError(Err.UNKNOWN, "auth_ok bad length")
        (sid,) = struct.unpack_from("<I", payload, 0)
        name = ""
        if len(payload) > 20:
            nlen = payload[20]
            name = payload[21 : 21 + nlen].decode("utf-8", "replace")
        return cls(sid, payload[4:20], name)


def encode_ack(msg_id: int, next_seq: int) -> bytes:
    return struct.pack("<IH", msg_id, next_seq)


def decode_ack(payload: bytes) -> tuple[int, int]:
    if len(payload) != 6:
        raise BleChatError(Err.UNKNOWN, "ack bad length")
    return struct.unpack("<IH", payload)


def encode_progress(msg_id: int, acked: int, total: int) -> bytes:
    return struct.pack("<IHH", msg_id, acked, total)


def decode_progress(payload: bytes) -> tuple[int, int, int]:
    if len(payload) != 8:
        raise BleChatError(Err.UNKNOWN, "progress bad length")
    return struct.unpack("<IHH", payload)


def encode_peers(peers: list[tuple[str, str]]) -> bytes:
    out = bytes([min(len(peers), 255)])
    for device_id, name in peers:
        raw = UUID(device_id).bytes
        name_b = _utf8_trunc(name.encode("utf-8"), MAX_NAME_LEN)
        out += raw + bytes([len(name_b)]) + name_b
    return out


def decode_peers(payload: bytes) -> list[tuple[str, str]]:
    if not payload:
        return []
    count = payload[0]
    out: list[tuple[str, str]] = []
    off = 1
    for _ in range(count):
        if len(payload) < off + 17:
            break
        device_id = str(UUID(bytes=payload[off : off + 16]))
        nlen = payload[off + 16]
        name = payload[off + 17 : off + 17 + nlen].decode("utf-8", "replace")
        out.append((device_id, name))
        off += 17 + nlen
    return out


# ---------------------------------------------------------------- data frames


@dataclass(slots=True)
class DataChunk:
    msg_id: int
    seq: int
    total: int
    flags: int
    chunk: bytes

    def encode(self) -> bytes:
        aad = struct.pack("<IHHB", self.msg_id, self.seq, self.total, self.flags)
        crc = zlib.crc32(aad) & 0xFFFFFFFF
        return (
            HDR_STRUCT.pack(
                DATA_MAGIC,
                self.msg_id,
                self.seq,
                self.total,
                self.flags,
                AAD_LEN,
                crc,
            )
            + self.chunk
        )

    @classmethod
    def decode(cls, frame: bytes) -> DataChunk:
        if len(frame) < DATA_HEADER:
            raise BleChatError(Err.UNKNOWN, "data frame too short")
        magic, msg_id, seq, total, flags, aad_len, crc = HDR_STRUCT.unpack_from(frame, 0)
        if magic != DATA_MAGIC:
            raise BleChatError(Err.UNKNOWN, "bad data magic")
        if aad_len != AAD_LEN:
            raise BleChatError(Err.UNKNOWN, "unsupported aad_len")
        aad = struct.pack("<IHHB", msg_id, seq, total, flags)
        if zlib.crc32(aad) & 0xFFFFFFFF != crc:
            raise BleChatError(Err.CRC_MISMATCH, f"crc mismatch seq={seq}")
        return cls(msg_id, seq, total, flags, frame[DATA_HEADER:])


def kind_of_flags(flags: int) -> str:
    if flags & FLAG_FILE:
        return KIND_FILE
    if flags & FLAG_IMAGE:
        return KIND_IMAGE
    return KIND_TEXT


@dataclass(slots=True)
class FileMeta:
    """文件消息 payload = u32(meta_len) ‖ JSON(UTF-8) ‖ 文件字节。"""

    filename: str
    mime: str = "application/octet-stream"
    size: int = 0

    def encode(self, blob: bytes) -> bytes:
        name = _utf8_trunc(self.filename.encode("utf-8"), 200) or b"file"
        meta = json.dumps(
            {"filename": name.decode("utf-8", "ignore"), "mime": self.mime, "size": int(self.size)},
            ensure_ascii=False,
        ).encode("utf-8")
        return struct.pack("<I", len(meta)) + meta + blob

    @classmethod
    def decode(cls, payload: bytes) -> tuple[FileMeta, bytes]:
        if len(payload) < 4:
            raise BleChatError(Err.UNKNOWN, "file payload too short")
        (meta_len,) = struct.unpack_from("<I", payload, 0)
        if meta_len > 4096 or len(payload) < 4 + meta_len:
            raise BleChatError(Err.UNKNOWN, "file meta truncated")
        try:
            raw = json.loads(payload[4 : 4 + meta_len].decode("utf-8"))
            meta = cls(
                filename=str(raw.get("filename") or "file"),
                mime=str(raw.get("mime") or "application/octet-stream"),
                size=int(raw.get("size") or 0),
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise BleChatError(Err.UNKNOWN, f"file meta invalid: {exc}") from exc
        return meta, payload[4 + meta_len :]


def pack_message(
    msg_id: int,
    plaintext: bytes,
    session_key: bytes,
    *,
    image: bool,
    allow_compress: bool,
    max_chunk: int,
    file: bool = False,
) -> list[bytes]:
    """Encrypt whole message then split into framed chunks."""
    if len(plaintext) > MAX_PAYLOAD:
        raise BleChatError(Err.MSG_TOO_LARGE, f"{len(plaintext)} > {MAX_PAYLOAD}")
    binary = image or file
    payload, did_compress = compress.maybe_compress(plaintext, allow_compress and not binary)
    flags_base = (
        (FLAG_COMPRESSED if did_compress else 0)
        | (FLAG_IMAGE if image else 0)
        | (FLAG_FILE if file else 0)
    )
    blob_len = crypto.NONCE_LEN + len(payload) + crypto.TAG_LEN
    total = -(-blob_len // max_chunk)
    if total > 0xFFFF:
        raise BleChatError(Err.MSG_TOO_LARGE, "too many chunks")
    flags0 = flags_base | (FLAG_LAST if total == 1 else 0)
    aad = struct.pack("<IHHB", msg_id, 0, total, flags0)
    blob = crypto.encrypt(session_key, payload, aad)
    out: list[bytes] = []
    for seq in range(total):
        piece = blob[seq * max_chunk : (seq + 1) * max_chunk]
        flags = flags_base | (FLAG_LAST if seq == total - 1 else 0)
        out.append(DataChunk(msg_id, seq, total, flags, piece).encode())
    return out


@dataclass(slots=True)
class _Pending:
    total: int
    flags0: int | None = None
    parts: dict[int, bytes] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)


class Reassembler:
    def __init__(self, session_key: bytes, ttl: float = 60.0) -> None:
        self._key = session_key
        self._ttl = ttl
        self._pending: dict[int, _Pending] = {}

    def feed(self, frame: bytes) -> tuple[int, int, bytes] | None:
        """Return (msg_id, flags, plaintext) when a message completes, else None."""
        chunk = DataChunk.decode(frame)
        self._gc()
        pending = self._pending.get(chunk.msg_id)
        if pending is None:
            pending = _Pending(total=chunk.total)
            self._pending[chunk.msg_id] = pending
        if chunk.total != pending.total:
            raise BleChatError(Err.UNKNOWN, "total mismatch")
        if chunk.seq == 0:
            pending.flags0 = chunk.flags
        pending.parts.setdefault(chunk.seq, chunk.chunk)
        if len(pending.parts) < pending.total or pending.flags0 is None:
            return None
        blob = b"".join(pending.parts[i] for i in range(pending.total))
        del self._pending[chunk.msg_id]
        aad = struct.pack("<IHHB", chunk.msg_id, 0, pending.total, pending.flags0)
        payload = crypto.decrypt(self._key, blob, aad)
        return chunk.msg_id, pending.flags0, compress.maybe_decompress(
            payload, bool(pending.flags0 & FLAG_COMPRESSED)
        )

    def progress(self, msg_id: int) -> int:
        """已连续收到的分片数（重组进度）。"""
        pending = self._pending.get(msg_id)
        if pending is None:
            return 0
        i = 0
        while i in pending.parts:
            i += 1
        return i

    def _gc(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._pending.items() if now - v.created > self._ttl]
        for k in dead:
            del self._pending[k]
