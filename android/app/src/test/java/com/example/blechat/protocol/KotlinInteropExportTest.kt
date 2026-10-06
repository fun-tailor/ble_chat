package com.example.blechat.protocol

import org.junit.Test
import java.io.File

/**
 * 把 Kotlin 端生成的产物导出成文件，交给 Python 参考实现做**反向**校验
 * （Kotlin pack → Python Reassembler、Kotlin zlib → Python zlib、Kotlin HMAC/HKDF/PBKDF2）。
 *
 * 用 `./gradlew :app:testDebugUnitTest --tests '*KotlinInteropExport*'` 生成，
 * 再跑 `python tools/verify_kotlin_vectors.py` 消费。
 *
 * 导出本身不断言（断言在 Python 那边），但会 fail 如果写不出文件。
 */
class KotlinInteropExportTest {

    @Test
    fun exportKotlinGeneratedVectors() {
        val key = ByteArray(32) { it.toByte() }

        val text = "这是一段重复的文本，用来验证压缩。".repeat(20).toByteArray()
        val (compressed, compressedFlag) = Compress.maybeCompress(text)

        val plain = "hello BLE chat 中文测试 from kotlin".repeat(6).toByteArray()
        val frames = Packer.pack(0x11223344, plain, key, image = false, allowCompress = true, maxChunk = 64)

        val blob = ByteArray(500) { ((it * 37 + 11) and 0xFF).toByte() }
        val imageFrames = Packer.pack(2, blob, key, image = true, allowCompress = false, maxChunk = 100)

        val salt = ByteArray(16) { it.toByte() }
        val psk = Crypto.derivePsk("correct horse battery staple", salt, 1000)
        val nonceC = ByteArray(16) { (it + 16).toByte() }
        val nonceS = ByteArray(16) { (it + 32).toByte() }

        val hello = Hello(
            java.util.UUID.fromString("01234567-89ab-cdef-0123-456789abcdef"),
            "设备A",
            nonceC,
        )

        val gcmKey = ByteArray(32) { it.toByte() }
        val gcmAad = Packer.aadBytes(0x11223344, 0, 3, 1)
        val gcmPlain = "hello BLE chat 中文".toByteArray(Charsets.UTF_8)
        // 固定 nonce 走 Kotlin 的 encrypt 路径不可行（随机），这里导出 round-trip 用的明文/密钥/AAD，
        // 由 Python 校验「用同一 aad 解密 Kotlin 的 blob」——见下方 gcm_blob。
        val gcmBlob = Crypto.encrypt(gcmKey, gcmPlain, gcmAad)

        val lines = buildString {
            appendLine("pack_text_plain=${toHex(plain)}")
            appendLine("pack_text_count=${frames.size}")
            frames.forEachIndexed { i, f -> appendLine("pack_text_frame$i=${toHex(f)}") }
            appendLine("pack_image_count=${imageFrames.size}")
            imageFrames.forEachIndexed { i, f -> appendLine("pack_image_frame$i=${toHex(f)}") }
            appendLine("compress_input=${toHex(text)}")
            appendLine("compress_flag=$compressedFlag")
            appendLine("compress_output=${toHex(compressed)}")
            appendLine("psk=${toHex(psk)}")
            appendLine("hmac_auth=${toHex(Crypto.hmacAuth(psk, nonceC, nonceS))}")
            appendLine("hkdf=${toHex(Crypto.hkdfSessionKey(psk, nonceS))}")
            appendLine("psk_verifier=${toHex(Crypto.hmacSha256(psk, Crypto.VERIFY_LABEL))}")
            appendLine("hello=${toHex(hello.encode())}")
            appendLine("gcm_key=${toHex(gcmKey)}")
            appendLine("gcm_aad=${toHex(gcmAad)}")
            appendLine("gcm_plain=${toHex(gcmPlain)}")
            appendLine("gcm_blob=${toHex(gcmBlob)}")
        }

        val out = File(
            System.getProperty("java.io.tmpdir"),
            "blechat-kotlin-vectors.txt",
        )
        out.writeText(lines, Charsets.UTF_8)
        assertTrue("failed to write ${out.absolutePath}", out.length() > 0L)
    }

    private fun assertTrue(msg: String, cond: Boolean) {
        if (!cond) throw AssertionError(msg)
    }
}
