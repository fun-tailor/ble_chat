package com.example.blechat.protocol

/**
 * 与 `blechat/protocol.py` 的 `pack_message` / `Reassembler` 对齐。
 *
 * 关键点（错了就连不上）：
 * 1. **整条**消息先 AES-GCM 加密，再切成片 —— 不是逐片加密；
 * 2. GCM 的 AAD 是 `struct.pack("<IHHB", msg_id, 0, total, flags_of_seq0)`，
 *    即 **seq 恒为 0**、total 是总片数、flags 是第 0 片的 flags（含 FLAG_LAST 与否）；
 * 3. 每一片自己的 `aad_crc32` 用的是**本片**的 `(msg_id, seq, total, flags)`，
 *    只是做完整性自检，不进 GCM。
 */
object Packer {

    fun pack(
        msgId: Int,
        plaintext: ByteArray,
        sessionKey: ByteArray,
        image: Boolean,
        allowCompress: Boolean,
        maxChunk: Int,
        file: Boolean = false,
    ): List<ByteArray> {
        if (plaintext.size > P.MAX_PAYLOAD) {
            throw ProtoException(
                Err.MSG_TOO_LARGE,
                "${plaintext.size} > ${P.MAX_PAYLOAD}",
            )
        }
        if (maxChunk < 8) throw ProtoException(Err.UNKNOWN, "max_chunk too small")
        val binary = image || file
        val (payload, didCompress) =
            Compress.maybeCompress(plaintext, allowCompress && !binary)
        var flagsBase = 0
        if (didCompress) flagsBase = flagsBase or P.FLAG_COMPRESSED
        if (image) flagsBase = flagsBase or P.FLAG_IMAGE
        if (file) flagsBase = flagsBase or P.FLAG_FILE

        val blobLen = Crypto.NONCE_LEN + payload.size + Crypto.TAG_LEN
        val total = (blobLen + maxChunk - 1) / maxChunk
        if (total > 0xFFFF) throw ProtoException(Err.MSG_TOO_LARGE, "too many chunks")

        val flags0 = flagsBase or if (total == 1) P.FLAG_LAST else 0
        val aad = aadBytes(msgId, 0, total, flags0)
        val blob = Crypto.encrypt(sessionKey, payload, aad)

        val out = ArrayList<ByteArray>(total)
        for (seq in 0 until total) {
            val piece = blob.copyOfRange(seq * maxChunk, minOf((seq + 1) * maxChunk, blob.size))
            val flags = flagsBase or if (seq == total - 1) P.FLAG_LAST else 0
            out.add(DataChunk(msgId, seq, total, flags, piece).encode())
        }
        return out
    }

    /** `struct.pack("<IHHB", msg_id, seq, total, flags)`。 */
    internal fun aadBytes(msgId: Int, seq: Int, total: Int, flags: Int): ByteArray =
        LeWriter().u32(msgId).u16(seq).u16(total).u8(flags).toByteArray()
}

private class Pending(val total: Int) {
    var flags0: Int? = null
    val parts = HashMap<Int, ByteArray>()
    var createdMs: Long = System.currentTimeMillis()
}

/** 重组 + 解密 + 解压。完整时返回 `(msg_id, flags0, plaintext)`，否则 `null`。 */
class Reassembler(
    private val sessionKey: ByteArray,
    private val ttlMs: Long = P.REASSEMBLY_TTL_MS,
) {
    private val pending = HashMap<Int, Pending>()

    /** @return Triple(msgId, flags, plaintext)，消息未完整时返回 null。 */
    fun feed(frame: ByteArray): Triple<Int, Int, ByteArray>? {
        val chunk = DataChunk.decode(frame)
        gc()
        var p = pending[chunk.msgId]
        if (p == null) {
            p = Pending(chunk.total)
            pending[chunk.msgId] = p
        }
        if (chunk.total != p.total) {
            pending.remove(chunk.msgId)
            throw ProtoException(Err.UNKNOWN, "total mismatch")
        }
        if (chunk.seq == 0) p.flags0 = chunk.flags
        if (!p.parts.containsKey(chunk.seq)) p.parts[chunk.seq] = chunk.chunk
        val flags0 = p.flags0 ?: return null
        if (p.parts.size < p.total) return null

        var size = 0
        for (i in 0 until p.total) {
            size += (p.parts[i] ?: throw ProtoException(Err.UNKNOWN, "missing seq $i")).size
        }
        val full = ByteArray(size)
        var off = 0
        for (i in 0 until p.total) {
            val piece = p.parts[i]!!
            System.arraycopy(piece, 0, full, off, piece.size)
            off += piece.size
        }
        pending.remove(chunk.msgId)

        val aad = Packer.aadBytes(chunk.msgId, 0, p.total, flags0)
        val payload = Crypto.decrypt(sessionKey, full, aad)
        val plain = Compress.maybeDecompress(payload, flags0 and P.FLAG_COMPRESSED != 0)
        return Triple(chunk.msgId, flags0, plain)
    }

    /** 已**连续**收到的分片数（用于回 ACK / PROGRESS）。 */
    fun progress(msgId: Int): Int {
        val p = pending[msgId] ?: return 0
        var i = 0
        while (p.parts.containsKey(i)) i++
        return i
    }

    fun pendingIds(): Set<Int> = pending.keys

    private fun gc() {
        val now = System.currentTimeMillis()
        pending.entries.removeAll { now - it.value.createdMs > ttlMs }
    }
}
