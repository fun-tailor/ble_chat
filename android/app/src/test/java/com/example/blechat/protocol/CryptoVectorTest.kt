package com.example.blechat.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

internal fun hex(s: String): ByteArray {
    val clean = s.trim()
    require(clean.length % 2 == 0) { "odd hex length" }
    return ByteArray(clean.length / 2) {
        clean.substring(it * 2, it * 2 + 2).toInt(16).toByte()
    }
}

internal fun toHex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }

/**
 * 全部向量由 Python 参考实现生成（`tools/gen_vectors.py`），
 * 这是 Kotlin 端能和 PC Host 真机互通的**唯一保证**。
 */
class CryptoVectorTest {

    @Test
    fun pbkdf2MatchesPython() {
        val salt = hex("000102030405060708090a0b0c0d0e0f")
        assertArrayEquals(
            hex("a69b179e3add3c1e0aaf227a0eb3aa2aa8645ab86fecf6ca00c17512697c719e"),
            Crypto.derivePsk("correct horse battery staple", salt, 1000),
        )
        assertArrayEquals(
            hex("730b5141df115f64591c77274e5d76a3152e229c13ebef8105ebbcd0836838c5"),
            Crypto.derivePsk("口令!", salt, 1000),
        )
    }

    @Test
    fun pbkdf2DefaultItersMatchesPython() {
        val salt = hex("000102030405060708090a0b0c0d0e0f")
        assertEquals(Crypto.ITERS, 200_000)
        assertArrayEquals(
            hex("45fea9d79f583c568d79d99c35c95c34f9603a5fbd4f8dd36adf6453724b79c8"),
            Crypto.derivePsk("correct horse battery staple", salt),
        )
    }

    @Test
    fun hmacAuthAndHkdfMatchPython() {
        val salt = hex("000102030405060708090a0b0c0d0e0f")
        val psk = Crypto.derivePsk("correct horse battery staple", salt, 1000)
        val nonceC = hex("101112131415161718191a1b1c1d1e1f")
        val nonceS = hex("202122232425262728292a2b2c2d2e2f")

        assertArrayEquals(
            hex("fb069fdd9f9ab6babc92d308ec845af39a9d3b052a4a9715f05865bd729458fa"),
            Crypto.hmacAuth(psk, nonceC, nonceS),
        )
        assertArrayEquals(
            hex("e3f85a5dc40bc4dcc4869267f0b09251a4d946edea80b9e4c572f51cca2302e9"),
            Crypto.hkdfSessionKey(psk, nonceS),
        )
        assertArrayEquals(
            hex("284ac8aee2b962c9bf5b11c2f5656c7d1a7ff462fc22f79f85367484a4819aed"),
            Crypto.hmacSha256(psk, Crypto.VERIFY_LABEL),
        )
    }

    @Test
    fun aesGcmVectorFromPython() {
        val key = hex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
        val aad = hex("443322110000030001")
        val plain = "hello BLE chat 中文".toByteArray(Charsets.UTF_8)
        assertArrayEquals(plain, hex("68656c6c6f20424c45206368617420e4b8ade69687"))
        val blob = hex(
            "000102030405060708090a0b2f67ba77aac58057c861f4e3d09d58893b7b61a2" +
                "77550c8bf8c488498888fecd8c97b8e20c",
        )
        assertArrayEquals(plain, Crypto.decrypt(key, blob, aad))
        assertEquals(Crypto.NONCE_LEN + plain.size + Crypto.TAG_LEN, blob.size)
    }

    @Test
    fun aesGcmRoundTripAndAadBinding() {
        val key = bytes32()
        val aad = Packer.aadBytes(0x11223344, 0, 3, 1)
        val plain = "中文 payload ✓".toByteArray()
        val blob = Crypto.encrypt(key, plain, aad)
        assertEquals(Crypto.NONCE_LEN + plain.size + Crypto.TAG_LEN, blob.size)
        assertArrayEquals(plain, Crypto.decrypt(key, blob, aad))
        try {
            Crypto.decrypt(key, blob, aad + byteArrayOf(1))
            fail("wrong aad must not decrypt")
        } catch (e: ProtoException) {
            assertEquals(Err.AUTH_FAILED, e.code)
        }
    }

    @Test
    fun shortBlobIsAuthFailed() {
        try {
            Crypto.decrypt(bytes32(), ByteArray(20), ByteArray(0))
            fail("short blob must not decrypt")
        } catch (e: ProtoException) {
            assertEquals(Err.AUTH_FAILED, e.code)
        }
    }

    private fun bytes32() = ByteArray(32) { it.toByte() }
}
