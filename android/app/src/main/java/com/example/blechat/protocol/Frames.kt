package com.example.blechat.protocol

import java.util.UUID

fun UUID.toProtoBytes(): ByteArray =
    java.nio.ByteBuffer.allocate(16)
        .putLong(mostSignificantBits)
        .putLong(leastSignificantBits)
        .array()

fun protoBytesToUuid(b: ByteArray): UUID {
    require(b.size >= 16) { "uuid needs 16 bytes" }
    val bb = java.nio.ByteBuffer.wrap(b)
    return UUID(bb.long, bb.long)
}

/** 与 Python `str(UUID(...))` 一致：小写 + 短横线。 */
fun UUID.toProtocolString(): String = toString().lowercase()

/** 按 PROTOCOL.md 的 16 字节 UUID 语义解析（带不带短横线都接受）。 */
fun uuidFromString(text: String): UUID = UUID.fromString(text)

internal fun utf8Trunc(text: String, limit: Int): ByteArray {
    val data = text.toByteArray(Charsets.UTF_8)
    if (data.size <= limit) return data
    // 与 Python `data[:limit].decode("utf-8","ignore")` 对齐：只有**真的截断**时才丢掉
    // 末尾的半个多字节字符（不截断就原样返回，否则会把最后一个完整字符吃掉）。
    var raw = data.copyOf(limit)
    while (raw.isNotEmpty() && (raw.last().toInt() and 0xC0) == 0x80) {
        raw = raw.copyOf(raw.size - 1)
    }
    if (raw.isNotEmpty() && (raw.last().toInt() and 0x80) != 0) {
        raw = raw.copyOf(raw.size - 1)
    }
    return raw
}

// ---------------------------------------------------------------- 控制帧

data class CtrlFrame(val type: Int, val payload: ByteArray) {
    override fun equals(other: Any?): Boolean =
        other is CtrlFrame && type == other.type && payload.contentEquals(other.payload)

    override fun hashCode(): Int = 31 * type + payload.contentHashCode()
}

fun encodeCtrl(type: Int, payload: ByteArray = ByteArray(0)): ByteArray {
    if (payload.size > 0xFFFF) {
        throw ProtoException(Err.MSG_TOO_LARGE, "control payload too large")
    }
    return LeWriter().u8(P.CTRL_MAGIC).u8(type).u16(payload.size).raw(payload).toByteArray()
}

fun decodeCtrl(frame: ByteArray): CtrlFrame {
    if (frame.size < 4 || (frame[0].toInt() and 0xFF) != P.CTRL_MAGIC) {
        throw ProtoException(Err.UNKNOWN, "bad control frame")
    }
    val type = frame[1].toInt() and 0xFF
    val len = (frame[2].toInt() and 0xFF) or ((frame[3].toInt() and 0xFF) shl 8)
    if (frame.size < 4 + len) {
        throw ProtoException(Err.UNKNOWN, "truncated control frame")
    }
    return CtrlFrame(type, frame.copyOfRange(4, 4 + len))
}

// ---------------------------------------------------------------- 各 type 的 payload

data class Hello(
    val deviceId: UUID,
    val name: String,
    val nonceC: ByteArray,
    val useEph: Boolean = false,
    val protoVer: Int = P.PROTO_VER,
    val ephId: String = "",
) {
    fun encode(): ByteArray {
        val w = LeWriter()
        w.raw(deviceId.toProtoBytes())
        val nameBytes = utf8Trunc(name, P.MAX_NAME_LEN)
        w.u8(nameBytes.size).raw(nameBytes)
        w.raw(nonceC)
        w.u8(if (useEph) 1 else 0).u8(protoVer)
        if (useEph) {
            val id = utf8Trunc(ephId, 8)
            w.raw(if (id.size < 8) id + ByteArray(8 - id.size) else id)
        }
        return w.toByteArray()
    }

    companion object {
        fun decode(payload: ByteArray): Hello {
            if (payload.size < 22) throw ProtoException(Err.UNKNOWN, "hello too short")
            val r = LeReader(payload)
            val deviceId = protoBytesToUuid(r.raw(16))
            val nlen = r.u8()
            if (payload.size < 17 + nlen + 16 + 2) {
                throw ProtoException(Err.UNKNOWN, "hello truncated")
            }
            val name = String(r.raw(nlen), Charsets.UTF_8)
            val nonceC = r.raw(16)
            val useEph = r.u8() == 1
            val protoVer = r.u8()
            var ephId = ""
            if (useEph) {
                if (r.remaining() < 8) throw ProtoException(Err.UNKNOWN, "hello eph truncated")
                ephId = String(r.raw(8), Charsets.US_ASCII)
            }
            return Hello(deviceId, name, nonceC, useEph, protoVer, ephId)
        }
    }
}

data class Challenge(
    val nonceS: ByteArray,
    val salt: ByteArray,
    val iters: Int,
    val useEph: Boolean = false,
) {
    fun encode(): ByteArray =
        LeWriter().raw(nonceS).raw(salt).u32(iters).u8(if (useEph) 1 else 0).toByteArray()

    companion object {
        fun decode(payload: ByteArray): Challenge {
            if (payload.size != 16 + 16 + 4 + 1) {
                throw ProtoException(Err.UNKNOWN, "challenge bad length")
            }
            val r = LeReader(payload)
            return Challenge(r.raw(16), r.raw(16), r.u32(), r.u8() == 1)
        }
    }
}

/**
 * AUTH_OK：`session_id(u32 LE) ‖ hkdf_salt(16) [‖ name_len(u8) ‖ name]`。
 *
 * 末尾的 `name` 是 Host 的昵称（PROTOCOL.md §2.1）。老 Host 只发 20 字节，
 * 这里按 `>= 20` 解析，没有剩余字节就是空名。
 */
data class AuthOk(val sessionId: Int, val hkdfSalt: ByteArray, val name: String = "") {
    fun encode(): ByteArray {
        val nameBytes = utf8Trunc(name, P.MAX_NAME_LEN)
        return LeWriter().u32(sessionId).raw(hkdfSalt).u8(nameBytes.size).raw(nameBytes).toByteArray()
    }

    companion object {
        fun decode(payload: ByteArray): AuthOk {
            if (payload.size < 20) throw ProtoException(Err.UNKNOWN, "auth_ok bad length")
            val r = LeReader(payload)
            val sid = r.u32()
            val salt = r.raw(16)
            var name = ""
            if (payload.size > 20) {
                val nlen = payload[20].toInt() and 0xFF
                if (payload.size >= 21 + nlen) {
                    name = String(payload, 21, nlen, Charsets.UTF_8)
                }
            }
            return AuthOk(sid, salt, name)
        }
    }
}

data class Ack(val msgId: Int, val nextSeq: Int) {
    fun encode(): ByteArray = LeWriter().u32(msgId).u16(nextSeq).toByteArray()

    companion object {
        fun decode(payload: ByteArray): Ack {
            if (payload.size != 6) throw ProtoException(Err.UNKNOWN, "ack bad length")
            val r = LeReader(payload)
            return Ack(r.u32(), r.u16())
        }
    }
}

data class Progress(val msgId: Int, val acked: Int, val total: Int) {
    fun encode(): ByteArray = LeWriter().u32(msgId).u16(acked).u16(total).toByteArray()

    companion object {
        fun decode(payload: ByteArray): Progress {
            if (payload.size != 8) throw ProtoException(Err.UNKNOWN, "progress bad length")
            val r = LeReader(payload)
            return Progress(r.u32(), r.u16(), r.u16())
        }
    }
}

data class PeerEntry(val deviceId: UUID, val name: String)

fun encodePeers(peers: List<PeerEntry>): ByteArray {
    val w = LeWriter()
    w.u8(minOf(peers.size, 255))
    for (p in peers.take(255)) {
        w.raw(p.deviceId.toProtoBytes())
        val nb = utf8Trunc(p.name, P.MAX_NAME_LEN)
        w.u8(nb.size).raw(nb)
    }
    return w.toByteArray()
}

fun decodePeers(payload: ByteArray): List<PeerEntry> {
    if (payload.isEmpty()) return emptyList()
    val count = payload[0].toInt() and 0xFF
    val out = ArrayList<PeerEntry>(count)
    val r = LeReader(payload, 1)
    repeat(count) {
        if (r.remaining() < 17) return out
        val id = protoBytesToUuid(r.raw(16))
        val nlen = r.u8()
        val name = if (r.remaining() >= nlen) String(r.raw(nlen), Charsets.UTF_8) else ""
        out.add(PeerEntry(id, name))
    }
    return out
}

// ---------------------------------------------------------------- 数据帧

data class DataChunk(
    val msgId: Int,
    val seq: Int,
    val total: Int,
    val flags: Int,
    val chunk: ByteArray,
) {
    fun encode(): ByteArray {
        val aad = LeWriter().u32(msgId).u16(seq).u16(total).u8(flags).toByteArray()
        require(aad.size == P.AAD_LEN) { "aad must be 9 bytes" }
        val crc = crc32(aad)
        return LeWriter()
            .u8(P.DATA_MAGIC)
            .u32(msgId)
            .u16(seq)
            .u16(total)
            .u8(flags)
            .u16(P.AAD_LEN)
            .u32(crc)
            .raw(chunk)
            .toByteArray()
    }

    companion object {
        fun decode(frame: ByteArray): DataChunk {
            if (frame.size < P.DATA_HEADER) throw ProtoException(Err.UNKNOWN, "data frame too short")
            val r = LeReader(frame)
            val magic = r.u8()
            if (magic != P.DATA_MAGIC) throw ProtoException(Err.UNKNOWN, "bad data magic")
            val msgId = r.u32()
            val seq = r.u16()
            val total = r.u16()
            val flags = r.u8()
            val aadLen = r.u16()
            val crc = r.u32()
            if (aadLen != P.AAD_LEN) throw ProtoException(Err.UNKNOWN, "unsupported aad_len")
            val aad = LeWriter().u32(msgId).u16(seq).u16(total).u8(flags).toByteArray()
            if (crc32(aad) != crc) {
                throw ProtoException(Err.CRC_MISMATCH, "crc mismatch seq=$seq")
            }
            return DataChunk(msgId, seq, total, flags, r.rest())
        }
    }
}

internal fun crc32(data: ByteArray): Int {
    val c = java.util.zip.CRC32()
    c.update(data)
    return c.value.toInt()
}

const val KIND_TEXT = "text"
const val KIND_IMAGE = "image"
const val KIND_FILE = "file"

/** 优先级：文件 > 图片 > 文本。 */
fun kindOfFlags(flags: Int): String = when {
    flags and P.FLAG_FILE != 0 -> KIND_FILE
    flags and P.FLAG_IMAGE != 0 -> KIND_IMAGE
    else -> KIND_TEXT
}
