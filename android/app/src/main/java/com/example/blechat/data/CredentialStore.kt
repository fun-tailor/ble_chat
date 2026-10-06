package com.example.blechat.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 凭据存储 = 「口令（密）」 + 「auth 校验参数（不密）」。
 *
 * - 口令：用 Android Keystore 里的 AES-256-GCM 加密后再落盘，SharedPreferences 里只留
 *   `iv + ciphertext` 的 Base64。Keystore 密钥不可导出，卸载即失效。
 * - `salt/iters/verifier`：**不是秘密**（PC 端也是明文存 `networks.json.auth`），
 *   用来在下次连接前本地校验口令，省掉一次「输错 → AUTH_FAIL → 重来」的往返。
 *
 * 与 Python 的对应关系：
 * ```
 * credentials_store.save_psk(...)   <-> CredentialStore.savePassword(...)
 * net.auth = {salt, iters, verifier} <-> CredentialStore.saveAuth/loadAuth
 * crypto.verify_password(pw, auth)   <-> CredentialStore.verifyPassword(...)
 * ```
 */
class CredentialStore(context: Context) {
    private val appContext = context.applicationContext
    private val secretPrefs = appContext.getSharedPreferences(PREF_SECRET, Context.MODE_PRIVATE)
    private val authPrefs = appContext.getSharedPreferences(PREF_AUTH, Context.MODE_PRIVATE)

    // ------------------------------------------------------------------ 口令

    fun savePassword(networkId: String, password: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, loadOrCreateKey())
        val ct = cipher.doFinal(password.toByteArray(Charsets.UTF_8))
        secretPrefs.edit()
            .putString(networkId, Base64.encodeToString(cipher.iv + ct, Base64.NO_WRAP))
            .apply()
    }

    fun loadPassword(networkId: String): String? {
        val raw = secretPrefs.getString(networkId, null) ?: return null
        return try {
            val blob = Base64.decode(raw, Base64.NO_WRAP)
            val iv = blob.copyOfRange(0, 12)
            val ct = blob.copyOfRange(12, blob.size)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, loadOrCreateKey(), GCMParameterSpec(128, iv))
            cipher.doFinal(ct).toString(Charsets.UTF_8)
        } catch (e: Exception) {
            null // 密钥失效 / 数据损坏：当成没存过，让用户重输
        }
    }

    fun hasPassword(networkId: String): Boolean = secretPrefs.contains(networkId)

    fun forget(networkId: String) {
        secretPrefs.edit().remove(networkId).apply()
        authPrefs.edit().remove(networkId).apply()
    }

    // ------------------------------------------------------------- auth 参数

    /** 记下一次成功的配对参数（salt 取自 CHALLENGE，verifier 由派生出的 PSK 算出）。 */
    fun saveAuth(networkId: String, salt: ByteArray, iters: Int, verifier: ByteArray) {
        val text = listOf(
            Base64.encodeToString(salt, Base64.NO_WRAP),
            iters.toString(),
            Base64.encodeToString(verifier, Base64.NO_WRAP),
        ).joinToString("|")
        authPrefs.edit().putString(networkId, text).apply()
    }

    /** 返回 `Triple(salt, iters, verifier)`，没存过或格式不对返回 null。 */
    fun loadAuth(networkId: String): Triple<ByteArray, Int, ByteArray>? {
        val raw = authPrefs.getString(networkId, null) ?: return null
        val parts = raw.split("|")
        if (parts.size != 3) return null
        val iters = parts[1].toIntOrNull() ?: return null
        return try {
            Triple(Base64.decode(parts[0], Base64.NO_WRAP), iters, Base64.decode(parts[2], Base64.NO_WRAP))
        } catch (e: IllegalArgumentException) {
            null
        }
    }

    /**
     * 本地校验口令：`HMAC(PBKDF2(pw, salt, iters), "verify") == verifier`。
     * 与 Python `crypto.verify_password` 完全同算法。
     */
    fun verifyPassword(networkId: String, password: String): Boolean {
        val (salt, iters, verifier) = loadAuth(networkId) ?: return true // 没有校验参数就放行，交给服务器判定
        val psk = com.example.blechat.protocol.Crypto.derivePsk(password, salt, iters)
        return constantTimeEquals(com.example.blechat.protocol.Crypto.pskVerifier(psk), verifier)
    }

    /** 已存的 auth 参数里的 salt 是否就是本次 CHALLENGE 的 salt（不同则说明 Host 换过密）。 */
    fun saltMatches(networkId: String, salt: ByteArray): Boolean {
        val stored = loadAuth(networkId)?.first ?: return false
        return constantTimeEquals(stored, salt)
    }

    private fun constantTimeEquals(a: ByteArray, b: ByteArray): Boolean {
        if (a.size != b.size) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].toInt() xor b[i].toInt())
        return diff == 0
    }

    private fun loadOrCreateKey(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEY_STORE).apply { load(null) }
        (ks.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEY_STORE)
        gen.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    private companion object {
        const val ANDROID_KEY_STORE = "AndroidKeyStore"
        const val KEY_ALIAS = "blechat_psk"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val PREF_SECRET = "blechat_secret"
        const val PREF_AUTH = "blechat_auth"
    }
}
