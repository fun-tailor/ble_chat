from __future__ import annotations

import zlib

LEVEL = 6
MIN_SIZE = 128
GOOD_RATIO = 0.9


def maybe_compress(data: bytes, allow: bool = True) -> tuple[bytes, bool]:
    """Return (payload, compressed_flag). Never raises."""
    if not allow or len(data) < MIN_SIZE:
        return data, False
    try:
        out = zlib.compress(data, LEVEL)
    except Exception:
        return data, False
    if len(out) >= GOOD_RATIO * len(data):
        return data, False
    return out, True


def maybe_decompress(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    return zlib.decompress(data)
