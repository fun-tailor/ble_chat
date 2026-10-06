from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .errors import BleChatError, Err

ITERS = 200_000
SALT_LEN = 16
PSK_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
HKDF_INFO = b"blechat-session-v1"
VERIFY_LABEL = b"verify"


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def derive_psk(password: str, salt: bytes, iters: int = ITERS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=PSK_LEN, salt=salt, iterations=iters)
    return kdf.derive(password.encode("utf-8"))


def psk_verifier(psk: bytes) -> bytes:
    return hmac.new(psk, VERIFY_LABEL, hashlib.sha256).digest()


def make_auth_params(password: str, iters: int = ITERS) -> dict:
    salt = new_salt()
    psk = derive_psk(password, salt, iters)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "iters": iters,
        "verifier": base64.b64encode(psk_verifier(psk)).decode("ascii"),
    }


def verify_password(password: str, auth: dict) -> bool:
    try:
        salt = base64.b64decode(auth["salt"])
        iters = int(auth["iters"])
        verifier = base64.b64decode(auth["verifier"])
    except Exception:
        return False
    psk = derive_psk(password, salt, iters)
    return hmac.compare_digest(psk_verifier(psk), verifier)


def psk_from_password(password: str, auth: dict) -> bytes:
    salt = base64.b64decode(auth["salt"])
    return derive_psk(password, salt, int(auth["iters"]))


def hkdf_session_key(psk: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=HKDF_INFO).derive(psk)


def hmac_auth(psk: bytes, nonce_c: bytes, nonce_s: bytes) -> bytes:
    return hmac.new(psk, nonce_c + nonce_s, hashlib.sha256).digest()


def verify_hmac(psk: bytes, nonce_c: bytes, nonce_s: bytes, digest: bytes) -> bool:
    return hmac.compare_digest(hmac_auth(psk, nonce_c, nonce_s), digest)


def encrypt(session_key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Return nonce(12) || ciphertext || tag(16)."""
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(session_key).encrypt(nonce, plaintext, aad)


def decrypt(session_key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise BleChatError(Err.AUTH_FAILED, "ciphertext too short")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(session_key).decrypt(nonce, ct, aad)
    except Exception as exc:
        raise BleChatError(Err.AUTH_FAILED, f"decrypt failed: {exc}") from exc


def new_ephemeral() -> tuple[str, bytes]:
    """Return (eph_id 8-hex-uppercase, 32B key)."""
    eph_id = os.urandom(4).hex().upper()
    return eph_id, os.urandom(32)


def eph_key_hash(key: bytes) -> str:
    return base64.b64encode(hashlib.sha256(key).digest()).decode("ascii")


def format_eph_key(key: bytes) -> str:
    raw = base64.b32encode(key).decode("ascii").rstrip("=")
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def parse_eph_key(text: str) -> bytes:
    raw = "".join(ch for ch in text.upper() if ch.isalnum())
    pad = "=" * (-len(raw) % 8)
    try:
        key = base64.b32decode(raw + pad)
    except Exception as exc:
        raise BleChatError(Err.AUTH_FAILED, "临时密钥格式错误") from exc
    if len(key) != 32:
        raise BleChatError(Err.AUTH_FAILED, "临时密钥长度错误")
    return key
