package com.example.blechat.ble

import java.util.UUID

/** 扫描到的一台设备。`address` 是 `AA:BB:CC:DD:EE:FF` 形式的 MAC，可直接用于按 MAC 直连。 */
data class ScannedDevice(
    val name: String?,
    val address: String,
    val rssi: Int,
    val serviceUuids: List<UUID> = emptyList(),
) {
    /** 界面显示名：优先广播名，退化成 MAC。 */
    val displayName: String get() = name?.takeIf { it.isNotBlank() } ?: address
}

/** GATT 链路上的事件。全部由 [GattLink] 发出。 */
sealed interface LinkEvent {
    /** 链路就绪：服务已发现、MTU 已协商、两条订阅都写完 CCCD。 */
    data class Ready(val mtu: Int, val address: String) : LinkEvent

    /** 来自 `CHAR_TX (0000a002)` 的数据帧。 */
    data class Data(val bytes: ByteArray) : LinkEvent {
        override fun equals(other: Any?): Boolean =
            other is Data && bytes.contentEquals(other.bytes)

        override fun hashCode(): Int = bytes.contentHashCode()
    }

    /** 来自 `CHAR_CTRL (0000a003)` 的控制帧。 */
    data class Ctrl(val bytes: ByteArray) : LinkEvent {
        override fun equals(other: Any?): Boolean =
            other is Ctrl && bytes.contentEquals(other.bytes)

        override fun hashCode(): Int = bytes.contentHashCode()
    }

    /** 链路断开。`reason` 是人话，直接可以贴到界面上。 */
    data class Disconnected(val reason: String) : LinkEvent
}

/** 把 Android 的 GATT status / 异常消息翻译成人话（对齐 Python 端 `link_down_reason`）。 */
object BleStatus {
    const val GATT_SUCCESS = 0
    const val GATT_READ_NOT_PERMITTED = 2
    const val GATT_WRITE_NOT_PERMITTED = 3
    const val GATT_INSUFFICIENT_AUTHENTICATION = 5
    const val GATT_REQUEST_NOT_SUPPORTED = 6
    const val GATT_INVALID_OFFSET = 7
    const val GATT_INSUFFICIENT_AUTHORIZATION = 8
    const val GATT_INSUFFICIENT_ENCRYPTION = 15
    const val GATT_CONNECTION_CONGESTED = 142
    const val GATT_FAILURE = 128

    fun message(status: Int): String = when (status) {
        GATT_SUCCESS -> "成功"
        1 -> "GATT 写入失败"
        2 -> "该特征不允许读取"
        3 -> "该特征不允许写入"
        4 -> "请求参数不合法"
        5 -> "认证不足"
        6 -> "对端不支持该请求"
        7 -> "偏移量非法"
        8 -> "授权不足 / 未找到对象"
        13 -> "配对已被拒绝"
        15 -> "加密不足"
        19 -> "远端设备已断开"
        133 -> "GATT ERROR（133：通常是服务表过期或距离太远）"
        142 -> "链路拥塞"
        128 -> "GATT FAILURE"
        else -> "GATT status=$status"
    }

    /** 与 Python `is_link_dead()` 对齐：命中就该关会话触发重连。 */
    fun isLinkDead(message: String): Boolean {
        val low = message.lowercase()
        return low.contains("gatt protocol error") ||
            low.contains("not connected") ||
            low.contains("not subscribed") ||
            low.contains("has been closed") ||
            low.contains("object closed") ||
            low.contains("device disconnected") ||
            low.contains("connection failed") ||
            low.contains("status=133") ||
            low.contains("status=19") ||
            message.contains("该对象已关闭")
    }
}
