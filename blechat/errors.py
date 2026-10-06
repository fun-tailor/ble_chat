from __future__ import annotations

from enum import IntEnum


class Err(IntEnum):
    OK = 0x00
    NOT_READY = 0x01
    AUTH_FAILED = 0x02
    EPH_EXPIRED = 0x03
    EPH_NOT_FOUND = 0x04
    TOO_MANY_RETRIES = 0x05
    MSG_TOO_LARGE = 0x06
    CRC_MISMATCH = 0x07
    PROTO_VER_MISMATCH = 0x08
    SESSION_REPLACED = 0x09
    SERVER_SHUTDOWN = 0x0A
    UNKNOWN = 0x0B
    HANDSHAKE_TIMEOUT = 0x0C


class BleChatError(Exception):
    def __init__(self, code: Err, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.name)

    @property
    def message(self) -> str:
        return str(self)


def error_text(code: int) -> str:
    try:
        return Err(code).name
    except ValueError:
        return f"UNKNOWN_{code:#04x}"
