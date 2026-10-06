package com.example.blechat.session

import com.example.blechat.protocol.P

/**
 * 保活判定：**纯函数**，把「这一轮 PING 该不该判链路已死」从会话对象里摘出来。
 *
 * 与 Python 端 `session.py` 的 `_keepalive_round()` 同语义：
 * - 距上次 PONG 还在 [P.PONG_GRACE_MS] 之内 ⇒ 正常；
 * - 超时但还在宽限 [P.KEEPALIVE_GRACE_MISSES] 轮之内 ⇒ 只记一次 misses；
 * - 宽限用尽后连续 [P.KEEPALIVE_MAX_MISSES] 轮没有 PONG ⇒ 判死（半开链路）。
 *
 * 真机上「连上后几十秒就失联」全靠这条判定区分是谁先放弃：如果日志里
 * 只有 PING 没有 PONG，就是**对端**没回；如果连 PING 都没发出去，那是我们这边
 * 的发送队列卡了（见 `GattLink.writeAndWait` 的超时/中毒逻辑）。
 */
internal object Keepalive {

    data class Decision(
        /** false = 判定链路已死，调用方应当 `linkDead()`。 */
        val alive: Boolean,
        val misses: Int,
        val deadline: Long,
        /** true = 这一轮是"新的一次错过"，值得打一条日志。 */
        val missedThisRound: Boolean,
    )

    fun round(
        now: Long,
        deadline: Long,
        misses: Int,
        intervalMs: Long = P.KEEPALIVE_INTERVAL_MS,
        graceMisses: Int = P.KEEPALIVE_GRACE_MISSES,
        maxMisses: Int = P.KEEPALIVE_MAX_MISSES,
    ): Decision {
        if (now < deadline) return Decision(true, misses, deadline, false)
        if (misses < graceMisses) {
            return Decision(true, misses + 1, now + intervalMs, true)
        }
        val next = misses + 1
        if (next - graceMisses >= maxMisses) {
            return Decision(false, next, deadline, true)
        }
        return Decision(true, next, now + intervalMs, true)
    }
}
