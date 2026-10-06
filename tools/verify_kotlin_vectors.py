"""反向校验：Kotlin 端导出的产物，必须能被 Python 参考实现正确消费。

生成方：`./gradlew :app:testDebugUnitTest --tests '*KotlinInteropExport*'"
产出：`%TMP%/blechat-kotlin-vectors.txt`

    python tools/verify_kotlin_vectors.py

任何一条 FAIL 都说明 Kotlin 与 PC Host 不互通。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blechat import compress, crypto, protocol  # noqa: E402

SRC = Path(__import__("tempfile").gettempdir()) / "blechat-kotlin-vectors.txt"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    if not SRC.exists():
        print(f"缺少 {SRC}，请先跑 Kotlin 导出测试")
        return 2

    kv: dict[str, str] = {}
    for line in SRC.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()

    def hx(k: str) -> bytes:
        return bytes.fromhex(kv[k])

    print(f"读取 {SRC}（{len(kv)} 项）")

    key = bytes(range(32))
    plain = hx("pack_text_plain")
    frames = [hx(f"pack_text_frame{i}") for i in range(int(kv["pack_text_count"]))]

    # 1. Kotlin 的密文 → Python 解密 + 重组
    got = None
    re = protocol.Reassembler(key)
    for f in frames:
        r = re.feed(f)
        if r is not None:
            got = r
    check("python 解密+重组 Kotlin 的文本消息", got is not None and got[2] == plain)
    if got:
        check("flags0 = COMPRESSED", got[1] == protocol.FLAG_COMPRESSED, hex(got[1]))
        check("kind == text", protocol.kind_of_flags(got[1]) == "text")

    # 2. Kotlin 的图片消息（不压缩）
    img_frames = [hx(f"pack_image_frame{i}") for i in range(int(kv["pack_image_count"]))]
    got2 = None
    re2 = protocol.Reassembler(key)
    for f in img_frames:
        r = re2.feed(f)
        if r is not None:
            got2 = r
    check(
        "python 解密+重组 Kotlin 的图片消息",
        got2 is not None and protocol.kind_of_flags(got2[1]) == "image",
    )

    # 3. Kotlin 的 zlib 输出 → Python 解压
    cin, cout = hx("compress_input"), hx("compress_output")
    try:
        ok = compress.maybe_decompress(cout, True) == cin
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"       zlib 解压异常: {exc}")
    check("Python zlib 能解 Kotlin 的压缩结果", ok, f"{len(cin)} -> {len(cout)}")
    check("Kotlin 确实判定为应该压缩", kv.get("compress_flag") == "true")

    # 4. 密码学原语
    salt = bytes(range(16))
    nonce_c = bytes(range(16, 32))
    nonce_s = bytes(range(32, 48))
    psk = crypto.derive_psk("correct horse battery staple", salt, 1000)
    check("PBKDF2 一致", kv["psk"] == psk.hex())
    check("HMAC-AUTH 一致", kv["hmac_auth"] == crypto.hmac_auth(psk, nonce_c, nonce_s).hex())
    check("HKDF session_key 一致", kv["hkdf"] == crypto.hkdf_session_key(psk, nonce_s).hex())
    check("PSK verifier 一致", kv["psk_verifier"] == crypto.psk_verifier(psk).hex())

    # 5. HELLO 编码
    hello = protocol.Hello.decode(hx("hello"))
    check("HELLO device_id", hello.device_id == "01234567-89ab-cdef-0123-456789abcdef")
    check("HELLO name", hello.name == "设备A", hello.name)
    check("HELLO nonce_c", hello.nonce_c == nonce_c)
    check("HELLO proto_ver", hello.proto_ver == protocol.PROTO_VER)

    # 6. 反向最关键的一步：Kotlin 加密的 AES-GCM，Python 必须能解
    blob = hx("gcm_blob")
    try:
        pt = crypto.decrypt(hx("gcm_key"), blob, hx("gcm_aad"))
        ok = pt == hx("gcm_plain")
        detail = f"{len(blob)} bytes"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, str(exc)
    check("Python AES-GCM 能解 Kotlin 的密文", ok, detail)

    # 7. 反向：Python 的密文，Kotlin 侧的解码已在 JUnit 里验过，这里只再确认长度约定
    check(
        "blob 长度 = nonce(12) + pt + tag(16)",
        len(blob) == 12 + len(hx("gcm_plain")) + 16,
    )

    # 8. 复用同一份 plaintext 走 Python 自己的 pack，确认计数约定一致
    p_frames = protocol.pack_message(
        0x11223344, plain, key, image=False, allow_compress=True, max_chunk=64,
    )
    check(
        "Kotlin/Python 分片数一致",
        len(p_frames) == len(frames),
        f"kotlin={len(frames)} python={len(p_frames)}",
    )
    check(
        "Kotlin/Python 首片 flags 一致",
        protocol.DataChunk.decode(frames[0]).flags
        == protocol.DataChunk.decode(p_frames[0]).flags,
        f"{hex(protocol.DataChunk.decode(frames[0]).flags)} vs "
        f"{hex(protocol.DataChunk.decode(p_frames[0]).flags)}",
    )

    print()
    if failures:
        print(f"FAIL ({len(failures)}): " + ", ".join(failures))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
