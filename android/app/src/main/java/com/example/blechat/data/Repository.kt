package com.example.blechat.data

import android.content.Context
import androidx.room.Room
import com.example.blechat.data.db.BleChatDatabase
import com.example.blechat.data.db.MessageEntity
import com.example.blechat.data.db.NetworkEntity

/**
 * 数据门面。PC 端对应 `config.json` + `networks.json` + `history.db` 三件套，
 * Android 这边是 SharedPreferences + Keystore + Room 三件套。
 */
class Repository private constructor(context: Context) {
    private val appContext = context.applicationContext

    val prefs = AppPrefs(appContext)
    val credentials = CredentialStore(appContext)

    private val db: BleChatDatabase = Room.databaseBuilder(
        appContext,
        BleChatDatabase::class.java,
        BleChatDatabase.NAME,
    ).build()

    // ---------------------------------------------------------------- network

    /**
     * v1 的 `network_id` = **Host 的 BLE MAC**（大写、冒号分隔）。
     *
     * SPEC §2.2 要求 `network_id = uuid5(host_device_id, service_uuid)`，
     * 但 PC 端目前写的是 `uuid4()`，AUTH_OK 也还没带 host device_id —— 这是已知待决项。
     * 先用 MAC 当键：它对「同一台 Host 重连」是稳定的，历史不会串；
     * 等 PC 端补上 host device_id 后再切到 uuid5 并做一次 remap。
     */
    fun networkIdFor(hostAddress: String): String = normalizeAddress(hostAddress)

    suspend fun rememberNetwork(
        hostAddress: String,
        serviceUuid: String,
        hostDeviceId: String? = null,
        name: String? = null,
    ): String {
        val id = networkIdFor(hostAddress)
        val existing = db.networks().get(id)
        db.networks().upsert(
            NetworkEntity(
                id = id,
                name = name ?: existing?.name,
                hostAddress = id,
                hostDeviceId = hostDeviceId ?: existing?.hostDeviceId,
                serviceUuid = serviceUuid,
                lastConnectedAt = System.currentTimeMillis(),
            ),
        )
        prefs.lastNetworkId = id
        prefs.lastHostAddress = id
        // 地址会轮换，名字不会 —— 一键重连要靠它重新解析地址
        if (!name.isNullOrBlank()) prefs.lastHostName = name
        return id
    }

    suspend fun recentNetworks(): List<NetworkEntity> = db.networks().all()

    suspend fun mostRecentNetwork(): NetworkEntity? = db.networks().mostRecent()

    // ---------------------------------------------------------------- messages

    suspend fun addIncoming(
        networkId: String,
        msgId: Int,
        peerId: String?,
        peerName: String?,
        kind: String,
        content: ByteArray,
        createdAt: Long = System.currentTimeMillis(),
    ): Long = db.messages().insert(
        MessageEntity(
            msgId = msgId,
            networkId = networkId,
            direction = "in",
            peerId = peerId,
            peerName = peerName,
            kind = kind,
            content = content,
            createdAt = createdAt,
            fileName = null,
            fileSize = null,
            mimeType = null,
            localPath = null,
        ),
    )

    suspend fun addOutgoing(
        networkId: String,
        msgId: Int,
        kind: String,
        content: ByteArray,
        createdAt: Long = System.currentTimeMillis(),
    ): Long = db.messages().insert(
        MessageEntity(
            msgId = msgId,
            networkId = networkId,
            direction = "out",
            peerId = prefs.deviceId,
            peerName = prefs.displayName,
            kind = kind,
            content = content,
            createdAt = createdAt,
            fileName = null,
            fileSize = null,
            mimeType = null,
            localPath = null,
        ),
    )

    suspend fun recent(networkId: String?, limit: Int = 500): List<MessageEntity> =
        if (networkId == null) db.messages().recentAll(limit) else db.messages().recent(networkId, limit)

    suspend fun search(networkId: String?, text: String): List<MessageEntity> =
        if (networkId == null) db.messages().searchAll(text)
        else db.messages().search(networkId, text)

    suspend fun deleteMessages(ids: List<Long>): Int = db.messages().deleteByIds(ids)

    /** 启动清理：保留 `days` 天（SPEC §7，Python 默认 3 天）。 */
    suspend fun purgeOlderThan(days: Long = BleChatDatabase.HISTORY_DAYS): Int {
        val cutoff = System.currentTimeMillis() - days * 86_400_000L
        return db.messages().purgeBefore(cutoff)
    }

    companion object {
        /** `AA:BB:CC:DD:EE:FF` 的各种写法归一到大写冒号形式。 */
        fun normalizeAddress(value: String): String =
            value.trim().uppercase().replace('-', ':')

        @Volatile
        private var instance: Repository? = null

        fun get(context: Context): Repository =
            instance ?: synchronized(this) {
                instance ?: Repository(context).also { instance = it }
            }
    }
}
