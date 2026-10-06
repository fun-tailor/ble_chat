package com.example.blechat.session

import com.example.blechat.protocol.P
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 保活判定。真机「连上后几十秒就失联」这一条，只能靠它区分是谁先放弃：
 *
 * - 日志里只有 PING 没有 PONG ⇒ **对端**没回（半开链路）；
 * - 连 PING 都发不出去 ⇒ 我们这边的发送队列卡了（见 GattLink.writeAndWait）。
 *
 * 判定本身是纯函数，所以能在这里把"第几轮判死"钉死。
 */
class KeepaliveTest {

    private val interval = P.KEEPALIVE_INTERVAL_MS
    private val grace = P.KEEPALIVE_GRACE_MISSES
    private val maxMisses = P.KEEPALIVE_MAX_MISSES

    @Test
    fun `PONG 按时回来时不动计数`() {
        val d = Keepalive.round(now = 1_000, deadline = 2_000, misses = 0)
        assertTrue(d.alive)
        assertEquals(0, d.misses)
        assertFalse(d.missedThisRound)
    }

    @Test
    fun `超过 deadline 但还在宽限期内 只记 misses`() {
        var misses = 0
        var deadline = 1_000L
        var now = 1_000L
        repeat(grace) {
            val d = Keepalive.round(now, deadline, misses)
            assertTrue("宽限期内不该判死", d.alive)
            assertTrue(d.missedThisRound)
            misses = d.misses
            deadline = d.deadline
            now = deadline // 下一轮又刚好到点
        }
        assertEquals(grace, misses)
    }

    @Test
    fun `宽限加最大次数用尽之后才判死`() {
        var misses = 0
        var deadline = 1_000L
        var now = 1_000L
        var aliveRounds = 0
        var dead = false
        repeat(grace + maxMisses + 2) {
            if (dead) return@repeat
            val d = Keepalive.round(now, deadline, misses)
            misses = d.misses
            deadline = d.deadline
            now = deadline
            if (d.alive) aliveRounds++ else dead = true
        }
        assertTrue("最终必须判死（否则半开链路会一直挂着）", dead)
        assertEquals(
            "判死前应当恰好放行 grace+maxMisses-1 轮",
            grace + maxMisses - 1,
            aliveRounds,
        )
    }

    @Test
    fun `收到 PONG 之后重新计时 不会因为累计 misses 被判死`() {
        // 连续错过 grace 轮（仍在容忍范围）
        var misses = 0
        var deadline = 1_000L
        repeat(grace) {
            val d = Keepalive.round(deadline, deadline, misses)
            misses = d.misses
            deadline = d.deadline
            assertTrue(d.alive)
        }
        // 对端回了一个 PONG：deadline 被推到"现在 + PONG_GRACE"，计数清零
        val pongAt = deadline
        deadline = pongAt + P.PONG_GRACE_MS
        misses = 0
        val d = Keepalive.round(pongAt + interval, deadline, misses)
        assertTrue(d.alive)
        assertEquals(0, d.misses)
        assertFalse(d.missedThisRound)
    }
}
