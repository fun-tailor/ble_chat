package com.example.blechat.protocol

import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.Cipher
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec

/**
 * 与 Python `blechat/crypto.py` **逐字节一致**的密码学原语。
 *
 * - PSK    = PBKDF2-HMAC-SHA256(password, salt, iters, dkLen=32)
 * - AUTH   = HMAC-SHA256(PSK, nonce_c ‖ nonce_s)
 * - key    = HKDF-SHA256(ikm=PSK, salt=nonce_s, info="blechat-session-v1", L=32)
 * - 报文   = AES-256-GCM(key, nonce(12) ‖ ct ‖ tag(16), aad)
 *
 * 全部在 JVM 单测里用 Python 生成的向量校验（见 `CryptoVectorTest`）。
 */
object Crypto {
    const val ITERS = 200_000
    const val SALT_LEN = 16
    const val PSK_LEN = 32
    const val NONCE_LEN = 12
    const val TAG_LEN = 16
    val HKDF_INFO: ByteArray = "blechat-session-v1".toByteArray(Charsets.UTF_8)
    val VERIFY_LABEL: ByteArray = "verify".toByteArray(Charsets.UTF_8)

    private val rng = SecureRandom()

    fun randomBytes(n: Int): ByteArray = ByteArray(n).also { rng.nextBytes(it) }

    fun derivePsk(password: String, salt: ByteArray, iters: Int = ITERS): ByteArray {
        val spec = PBEKeySpec(password.toCharArray(), salt, iters, PSK_LEN * 8)
        return try {
            SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
                .generateSecret(spec)
                .encoded
        } finally {
            spec.clearPassword()
        }
    }

    fun hmacSha256(key: ByteArray, data: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data)
    }

    fun hmacAuth(psk: ByteArray, nonceC: ByteArray, nonceS: ByteArray): ByteArray =
        hmacSha256(psk, nonceC + nonceS)

    /** `HMAC(psk, "verify")` —— Python `crypto.psk_verifier`。存进 auth 参数供本地验密用。 */
    fun pskVerifier(psk: ByteArray): ByteArray = hmacSha256(psk, VERIFY_LABEL)

    /** RFC 5869 HKDF-Extract + Expand，SHA-256。 */
    fun hkdf(psk: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val prk = hmacSha256(if (salt.isEmpty()) ByteArray(32) else salt, psk)
        val out = ByteArray(length)
        var t = ByteArray(0)
        var offset = 0
        var counter = 1
        while (offset < length) {
            val block = hmacSha256(prk, t + info + byteArrayOf(counter.toByte()))
            val n = minOf(block.size, length - offset)
            System.arraycopy(block, 0, out, offset, n)
            t = block
            offset += n
            counter++
        }
        return out
    }

    fun hkdfSessionKey(psk: ByteArray, salt: ByteArray): ByteArray =
        hkdf(psk, salt, HKDF_INFO, PSK_LEN)

    /** 返回 `nonce(12) ‖ ciphertext ‖ tag(16)`。 */
    fun encrypt(key: ByteArray, plaintext: ByteArray, aad: ByteArray): ByteArray {
        val nonce = randomBytes(NONCE_LEN)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD(aad)
        return nonce + cipher.doFinal(plaintext)
    }

    /** 接收 `nonce(12) ‖ ciphertext ‖ tag(16)`，失败抛 [ProtoException]。 */
    fun decrypt(key: ByteArray, blob: ByteArray, aad: ByteArray): ByteArray {
        if (blob.size < NONCE_LEN + TAG_LEN) {
            throw ProtoException(Err.AUTH_FAILED, "ciphertext too short")
        }
        val nonce = blob.copyOfRange(0, NONCE_LEN)
        val ct = blob.copyOfRange(NONCE_LEN, blob.size)
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
            cipher.updateAAD(aad)
            cipher.doFinal(ct)
        } catch (e: Exception) {
            throw ProtoException(Err.AUTH_FAILED, "decrypt failed: ${e.message}")
        }
    }
}
