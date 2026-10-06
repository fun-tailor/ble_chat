package com.example.blechat.protocol

import java.io.ByteArrayOutputStream
import java.util.zip.DataFormatException
import java.util.zip.Deflater
import java.util.zip.Inflater

/**
 * 与 `blechat/compress.py` 对齐的可选压缩。
 *
 * Python 用 `zlib.compress(data, 6)` —— 是 **RFC1950 (zlib 头)** 格式，
 * 对应 Java 的 `Deflater(LEVEL, nowrap=false)`（默认就是带 zlib 头）。
 * 不要用 `nowrap=true`（那是 raw DEFLATE / RFC1951），两端会不兼容。
 */
object Compress {
    const val LEVEL = 6
    const val MIN_SIZE = 128
    const val GOOD_RATIO = 0.9

    /** 解压上限：正常文本远小于此，纯防解压炸弹。 */
    const val MAX_OUT = 16 * 1024 * 1024

    /** 返回 `(payload, compressedFlag)`；与 Python 一样不抛异常。 */
    fun maybeCompress(data: ByteArray, allow: Boolean = true): Pair<ByteArray, Boolean> {
        if (!allow || data.size < MIN_SIZE) return data to false
        val out = try {
            deflate(data)
        } catch (e: Exception) {
            return data to false
        }
        if (out == null || out.size.toDouble() >= GOOD_RATIO * data.size) return data to false
        return out to true
    }

    fun maybeDecompress(data: ByteArray, compressed: Boolean): ByteArray {
        if (!compressed) return data
        try {
            return inflate(data)
        } catch (e: DataFormatException) {
            throw ProtoException(Err.UNKNOWN, "decompress failed: ${e.message}")
        }
    }

    private const val MAX_COMPRESS_OUT = 1 shl 24

    /** 返回 null 表示压缩结果异常，调用方按 Python 的 `except` 分支退回原文。 */
    private fun deflate(data: ByteArray): ByteArray? {
        val d = Deflater(LEVEL)
        try {
            d.setInput(data)
            d.finish()
            val out = ByteArrayOutputStream()
            val buf = ByteArray(4096)
            while (!d.finished()) {
                val n = d.deflate(buf)
                if (n <= 0 && d.finished()) break
                out.write(buf, 0, n)
                if (out.size() > MAX_COMPRESS_OUT) return null
            }
            return out.toByteArray()
        } finally {
            d.end()
        }
    }

    private fun inflate(data: ByteArray): ByteArray {
        val inf = Inflater()
        try {
            inf.setInput(data)
            val buf = ByteArray(4096)
            val out = ByteArrayOutputStream()
            while (!inf.finished()) {
                val n = inf.inflate(buf)
                if (n == 0) {
                    if (inf.needsInput() || inf.needsDictionary()) {
                        throw DataFormatException("truncated input")
                    }
                } else {
                    out.write(buf, 0, n)
                    if (out.size() > MAX_OUT) throw DataFormatException("output too large")
                }
            }
            return out.toByteArray()
        } finally {
            inf.end()
        }
    }
}
