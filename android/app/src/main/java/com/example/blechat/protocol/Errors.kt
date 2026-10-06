package com.example.blechat.protocol

/** 协议/链路层异常。`code` 是 [Err] 里的一员。 */
class ProtoException(val code: Int, message: String) : Exception(message)

/** 「对端已经不在了，该关会话触发重连」的判定，对应 Python 的 `is_link_dead()`。 */
fun isLinkDead(message: String): Boolean {
    val low = message.lowercase()
    return low.contains("gatt protocol error") ||
        low.contains("not connected") ||
        low.contains("not subscribed") ||
        low.contains("has been closed") ||
        low.contains("object closed") ||
        low.contains("device disconnected") ||
        low.contains("connection failed") ||
        message.contains("该对象已关闭")
}
