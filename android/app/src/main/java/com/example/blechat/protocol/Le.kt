package com.example.blechat.protocol

import java.io.ByteArrayOutputStream

/**
 * 小端读写助手。
 *
 * 手写而不是用 `ByteBuffer`，因为协议里 `msg_id` 是 u32、Kotlin `Int` 是有符号的 ——
 * 用移位拼字节能保证与 Python `struct.pack("<I", …)` 的**位模式**完全一致。
 */
internal class LeWriter {
    private val out = ByteArrayOutputStream()

    fun u8(v: Int): LeWriter {
        out.write(v and 0xFF)
        return this
    }

    fun u16(v: Int): LeWriter {
        out.write(v and 0xFF)
        out.write((v ushr 8) and 0xFF)
        return this
    }

    fun u32(v: Int): LeWriter {
        out.write(v and 0xFF)
        out.write((v ushr 8) and 0xFF)
        out.write((v ushr 16) and 0xFF)
        out.write((v ushr 24) and 0xFF)
        return this
    }

    fun raw(b: ByteArray): LeWriter {
        out.write(b, 0, b.size)
        return this
    }

    fun toByteArray(): ByteArray = out.toByteArray()
}

internal class LeReader(private val buf: ByteArray, start: Int = 0) {
    var pos: Int = start
        private set

    fun remaining(): Int = buf.size - pos

    fun u8(): Int {
        need(1)
        return buf[pos++].toInt() and 0xFF
    }

    fun u16(): Int {
        need(2)
        val v = (buf[pos].toInt() and 0xFF) or ((buf[pos + 1].toInt() and 0xFF) shl 8)
        pos += 2
        return v
    }

    fun u32(): Int {
        need(4)
        val v = (buf[pos].toInt() and 0xFF) or
            ((buf[pos + 1].toInt() and 0xFF) shl 8) or
            ((buf[pos + 2].toInt() and 0xFF) shl 16) or
            ((buf[pos + 3].toInt() and 0xFF) shl 24)
        pos += 4
        return v
    }

    fun raw(n: Int): ByteArray {
        need(n)
        val out = buf.copyOfRange(pos, pos + n)
        pos += n
        return out
    }

    fun rest(): ByteArray = raw(remaining())

    private fun need(n: Int) {
        if (remaining() < n) {
            throw ProtoException(Err.UNKNOWN, "frame truncated (need $n, have ${remaining()})")
        }
    }
}
