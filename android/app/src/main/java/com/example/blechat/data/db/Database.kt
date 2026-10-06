package com.example.blechat.data.db

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Index
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.RoomDatabase

/**
 * 历史消息。列名与 SPEC.md §7 的 SQLite 表**逐列一致**，方便对照与将来导出。
 *
 * `content` 是 BLOB：text=UTF-8、image=原始字节、file=b""（只存元数据 + local_path）。
 */
@Entity(
    tableName = "messages",
    indices = [
        Index(value = ["created_at"]),
        Index(value = ["network_id", "created_at"]),
        Index(value = ["network_id", "msg_id"], unique = true),
    ],
)
data class MessageEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    @ColumnInfo(name = "msg_id") val msgId: Int,
    @ColumnInfo(name = "network_id") val networkId: String,
    /** `'in'` | `'out'` */
    @ColumnInfo(name = "direction") val direction: String,
    @ColumnInfo(name = "peer_id") val peerId: String?,
    @ColumnInfo(name = "peer_name") val peerName: String?,
    @ColumnInfo(name = "kind") val kind: String,
    @ColumnInfo(name = "content", typeAffinity = androidx.room.ColumnInfo.BLOB) val content: ByteArray,
    @ColumnInfo(name = "created_at") val createdAt: Long,
    @ColumnInfo(name = "file_name") val fileName: String?,
    @ColumnInfo(name = "file_size") val fileSize: Long?,
    @ColumnInfo(name = "mime_type") val mimeType: String?,
    @ColumnInfo(name = "local_path") val localPath: String?,
) {
    /** text → UTF-8 字符串；image/file → 占位符。用于列表与搜索展示。 */
    fun textOrPlaceholder(): String = when (kind) {
        "text" -> content.toString(Charsets.UTF_8)
        "image" -> "[图片]"
        "file" -> fileName?.let { "[文件] $it" } ?: "[文件]"
        else -> ""
    }

    override fun equals(other: Any?): Boolean = other is MessageEntity && other.id == id

    override fun hashCode(): Int = id.hashCode()
}

/** 一个「房间」。v1 的 network_id 先用 Host 的 BLE MAC（见 Repository.networkIdFor）。 */
@Entity(tableName = "networks")
data class NetworkEntity(
    @PrimaryKey @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "name") val name: String?,
    @ColumnInfo(name = "host_address") val hostAddress: String,
    @ColumnInfo(name = "host_device_id") val hostDeviceId: String?,
    @ColumnInfo(name = "service_uuid") val serviceUuid: String,
    @ColumnInfo(name = "last_connected_at") val lastConnectedAt: Long,
)

@Dao
interface MessageDao {
    /** `(network_id, msg_id)` 唯一，撞号（对端与我方同号）直接忽略 —— 与 Python `INSERT OR IGNORE` 一致。 */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(message: MessageEntity): Long

    @Query(
        "SELECT * FROM messages WHERE network_id = :networkId " +
            "ORDER BY created_at DESC, id DESC LIMIT :limit",
    )
    suspend fun recent(networkId: String, limit: Int = 500): List<MessageEntity>

    @Query("SELECT * FROM messages ORDER BY created_at DESC, id DESC LIMIT :limit")
    suspend fun recentAll(limit: Int = 500): List<MessageEntity>

    @Query(
        "SELECT * FROM messages WHERE network_id = :networkId AND " +
            "(CAST(content AS TEXT) LIKE '%' || :text || '%' OR " +
            "LOWER(COALESCE(file_name, '')) LIKE '%' || LOWER(:text) || '%') " +
            "ORDER BY created_at DESC, id DESC LIMIT :limit",
    )
    suspend fun search(networkId: String, text: String, limit: Int = 500): List<MessageEntity>

    @Query(
        "SELECT * FROM messages WHERE " +
            "CAST(content AS TEXT) LIKE '%' || :text || '%' OR " +
            "LOWER(COALESCE(file_name, '')) LIKE '%' || LOWER(:text) || '%'" +
            " ORDER BY created_at DESC, id DESC LIMIT :limit",
    )
    suspend fun searchAll(text: String, limit: Int = 500): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages WHERE network_id = :networkId")
    suspend fun countByNetwork(networkId: String): Int

    @Query("DELETE FROM messages WHERE id IN (:ids)")
    suspend fun deleteByIds(ids: List<Long>): Int

    @Query("DELETE FROM messages WHERE network_id = :networkId AND msg_id = :msgId")
    suspend fun deleteByMsgId(networkId: String, msgId: Int): Int

    /** 启动清理：`created_at < now - days*86400`。 */
    @Query("DELETE FROM messages WHERE created_at < :cutoff")
    suspend fun purgeBefore(cutoff: Long): Int

    @Query("SELECT network_id FROM messages ORDER BY created_at DESC, id DESC LIMIT 1")
    suspend fun mostRecentNetworkId(): String?
}

@Dao
interface NetworkDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(net: NetworkEntity)

    @Query("SELECT * FROM networks WHERE id = :id")
    suspend fun get(id: String): NetworkEntity?

    @Query("SELECT * FROM networks WHERE host_address = :address LIMIT 1")
    suspend fun findByAddress(address: String): NetworkEntity?

    @Query("SELECT * FROM networks ORDER BY last_connected_at DESC")
    suspend fun all(): List<NetworkEntity>

    @Query("SELECT * FROM networks ORDER BY last_connected_at DESC LIMIT 1")
    suspend fun mostRecent(): NetworkEntity?
}

@Database(
    entities = [MessageEntity::class, NetworkEntity::class],
    version = 1,
    exportSchema = false,
)
abstract class BleChatDatabase : RoomDatabase() {
    abstract fun messages(): MessageDao
    abstract fun networks(): NetworkDao

    companion object {
        const val NAME = "blechat.db"

        /** 默认保留 3 天，与 Python `History(days=3)` 一致。 */
        const val HISTORY_DAYS = 3L
    }
}
