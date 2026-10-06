package com.example.blechat.ble

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

//"DESKTOP-xxxxxxx" need adapt to other dev

/**
 * 连接病历的分类逻辑。**这一组的价值在于：它用真机日志的形状做断言。**
 *
 * 待复现的真机形态（2026-10-04 12:57，MIUI + Windows Host）：
 *
 * ```
 * 12:57:02.854  onClientConnectionState status=0       ← LL 连上
 * 12:57:03.291  onConnectionUpdated interval=6 …       ← LL 参数更新成功
 * 12:57:06.865  服务发现 4000ms 内无回调                ← ATT 层零回复
 * 12:57:16.092  本地断开
 * ```
 *
 * 结论必须是 [DiagKind.ATT_SILENT] 并且**指向 PC 侧**：当时的日志只能看出"超时"，
 * 用户既不知道该动手机还是动 PC，我们这边也只能猜。现在分类是确定的。
 */
class ConnTraceTest {

    private fun trace(
        addressSource: String = "scan-pick",
        startAt: Long = 1_000_000L,
    ) = ConnTrace(1, "4B:AA:42:17:14:18", "DESKTOP-xxxxxxx", addressSource, startAt)

    @Test
    fun `真机 12-57 形态 LL 活着 ATT 静默 判为 PC 侧问题`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.llProbeSent = true
        t.llProbeAnswered = true
        t.rssi = -71
        t.bumpServices()
        t.addShape("回调延迟150ms")
        t.attProbeSent = true
        t.attProbeAnswered = false
        t.errorMessage = "服务发现 3000ms 内无回调（已试 3 种下发形态）"

        val d = t.classify()
        assertEquals(DiagKind.ATT_SILENT, d.kind)
        assertEquals(DiagSide.PC, d.side)
        assertTrue("处置里必须给出 PC 侧的动作", d.hint.contains("蓝牙"))
        assertTrue(d.hint.contains("重置蓝牙") || d.hint.contains("关掉再打开"))
    }

    @Test
    fun `LL 探针也没有答复 说明链路层其实已经断了`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.llProbeSent = true
        t.llProbeAnswered = false
        t.attProbeSent = true
        t.attProbeAnswered = false
        t.errorMessage = "链路层无响应"

        val d = t.classify()
        assertEquals(DiagKind.LL_DEAD, d.kind)
        assertEquals(DiagSide.PHONE, d.side)
    }

    @Test
    fun `ATT 有回但服务发现没人应答 指向 PC 侧`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.llProbeSent = true
        t.llProbeAnswered = true
        t.attProbeSent = true
        t.attProbeAnswered = true
        t.mtu = 247
        t.bumpServices()
        t.errorMessage = "服务发现无回调"

        val d = t.classify()
        assertEquals(DiagKind.DISCOVERY_DROPPED, d.kind)
        // 对端是 Windows：MTU 由系统栈回答、服务发现要由 Host 的服务表回答，
        // 前者通后者不通 ⇒ 该动 PC（重启 Host 让 provider 重新注册）。
        assertEquals(DiagSide.PC, d.side)
        assertTrue(d.hint.contains("重启 Host"))
    }

    @Test
    fun `旧 MAC 连不上 处置必须是按名字重扫 而不是重试同一个地址`() {
        val t = trace(addressSource = "stale-mac")
        t.connectGattAt = 1_000_200
        t.errorMessage = "连接超时（25s 内未完成 GATT 初始化）"

        val d = t.classify()
        assertEquals(DiagKind.NO_CONNECT, d.kind)
        assertTrue("必须点明地址会轮换", d.hint.contains("轮换"))
        assertTrue("必须给出按名字重扫的按钮名", d.hint.contains("重连上次"))
    }

    @Test
    fun `扫描选中的地址连不上 处置是确认对端在广播`() {
        val t = trace(addressSource = "scan-pick")
        t.connectGattAt = 1_000_200
        t.errorMessage = "连接超时（25s 内未完成 GATT 初始化）"

        val d = t.classify()
        assertEquals(DiagKind.NO_CONNECT, d.kind)
        assertTrue(d.hint.contains("advertising=STARTED"))
    }

    @Test
    fun `服务表里没有 a000 判为缓存过期`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.servicesSeen = true
        t.servicesStatus = 0
        t.serviceMissing = true
        t.bumpServices()
        t.errorMessage = "对端服务表里没有 a000/a001/a002/a003（Host 可能刚重启，本地缓存表过期）"

        val d = t.classify()
        assertEquals(DiagKind.SERVICE_MISSING, d.kind)
    }

    @Test
    fun `初始化阶段就被对端断开 带上 status 说明`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.disconnectedAt = 1_001_000
        t.disconnectStatus = 133
        t.errorMessage = "远端设备已断开"

        val d = t.classify()
        assertEquals(DiagKind.REMOTE_DISCONNECT, d.kind)
        assertTrue(d.hint.contains("133"))
    }

    @Test
    fun `就绪的连接不该被误判成失败`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.servicesSeen = true
        t.attProbeAnswered = true
        t.mtu = 517
        t.cccdWritten = true
        t.readyAt = 1_002_000

        val d = t.classify()
        assertEquals(DiagKind.OK, d.kind)
        assertEquals(DiagSide.NONE, d.side)
    }

    @Test
    fun `病历 dump 带上判据与结论 空病历也不能抛`() {
        val empty = trace()
        val text = empty.dump()
        assertTrue(text.contains("连接病历 #1"))
        assertTrue(text.contains("结论:"))
        assertTrue(text.contains("处置:"))

        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.llProbeSent = true
        t.llProbeAnswered = true
        t.rssi = -70
        t.attProbeSent = true
        t.attProbeAnswered = false
        t.errorMessage = "服务发现超时"
        t.step("connectGatt（来源=scan-pick）")
        val dump = t.dump()
        assertTrue("判据行要能看出两个探针的结论", dump.contains("llProbe=答复"))
        assertTrue(dump.contains("attProbe=无答复"))
        assertTrue(dump.contains("结论: 连上了，但对端 ATT 层零回复"))
    }

    @Test
    fun `地址来源会影响结论里的建议`() {
        val stale = trace(addressSource = "stale-mac").also {
            it.connectGattAt = 1L
            it.errorMessage = "x"
        }.classify()
        val byName = trace(addressSource = "scan-name").also {
            it.connectGattAt = 1L
            it.errorMessage = "x"
        }.classify()
        assertTrue(stale.hint != byName.hint)
        assertEquals(DiagKind.NO_CONNECT, stale.kind)
        assertEquals(DiagKind.NO_CONNECT, byName.kind)
    }

    // ------------------------------------------------------------------ 配对

    /**
     * 2026-10-04 22:21 的真机形态 + 「先配对」也试过但没成功。
     *
     * 这时最该先做的一步在**手机侧**（打开「连接前先配对」），所以结论必须从
     * 「该动 PC」变成「两端都可能」—— 否则用户会照着 PC 侧清单反复重启蓝牙，
     * 而真正的自变量（双方认不出彼此的随机地址）一直没被碰过。
     */
    @Test
    fun `配对没成功的 ATT 静默 结论要带上手机侧的那一步`() {
        val t = attSilentShape()
        t.bondRequested = true
        t.bondBefore = 10
        t.bondAfter = 10
        t.bondOutcome = BondOutcome.TIMEOUT
        t.errorMessage = "对端 ATT 层零回复"

        val d = t.classify()
        assertEquals(DiagKind.ATT_SILENT, d.kind)
        assertEquals(DiagSide.BOTH, d.side)
        assertTrue("处置里要给出配对这一步", d.hint.contains("连接前先配对"))
        assertTrue("背景里要解释 RPA/配对", d.why.contains("配对"))
        assertTrue(d.why.contains("IRK"))
    }

    /** 配对成功了 ⇒ 不能再把锅推给配对，得老老实实说"是对端服务层"。 */
    @Test
    fun `配对成功后的 ATT 静默 不把锅推给配对`() {
        val t = attSilentShape()
        t.bondRequested = true
        t.bondBefore = 10
        t.bondAfter = 12
        t.bondOutcome = BondOutcome.BONDED
        t.errorMessage = "对端 ATT 层零回复"

        val d = t.classify()
        assertEquals(DiagKind.ATT_SILENT, d.kind)
        assertEquals("配对已成功，就只剩 PC 侧了", DiagSide.PC, d.side)
        assertTrue("不能再让用户去开配对开关", !d.hint.contains("连接前先配对"))
        assertTrue(d.why.contains("配对是成功的"))
    }

    /** 没做配对时也要说清"还有这个自变量没试过"。 */
    @Test
    fun `没做配对时 背景里要提一句可以试配对`() {
        val t = attSilentShape()
        t.bondRequested = false
        val d = t.classify()
        assertEquals(DiagKind.ATT_SILENT, d.kind)
        assertEquals(DiagSide.PC, d.side)
        assertTrue(d.why.contains("没有做 BLE 配对"))
    }

    // ------------------------------------------------------------------ 其它分叉

    @Test
    fun `服务表齐了但 MTU 没协商成 不该被说成订阅失败`() {
        val t = trace()
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.servicesSeen = true
        t.servicesStatus = 0
        t.attProbeSent = true
        t.attProbeAnswered = true
        t.attProbeStatus = 6
        t.mtu = null
        t.errorMessage = "MTU 协商失败（仍为 23），无法发送握手帧"

        val d = t.classify()
        assertEquals(DiagKind.MTU_FAILED, d.kind)
        assertTrue(d.why.contains("ATT 层活着"))
    }

    @Test
    fun `病历 dump 带配对行 取证命令与环境`() {
        val t = trace()
        t.envInfo = "Xiaomi 2201122C / Android 14 (API 34)"
        t.bondRequested = true
        t.bondBefore = 10
        t.bondAfter = 10
        t.bondOutcome = BondOutcome.TIMEOUT
        t.step("配对: createBond() -> true")
        t.connectGattAt = 1_000_200
        t.connectedAt = 1_000_400
        t.llProbeSent = true
        t.llProbeAnswered = true
        t.rssi = -73
        t.attProbeSent = true
        t.attProbeAnswered = false
        t.errorMessage = "对端 ATT 层零回复"

        val dump = t.dump()
        assertTrue("要有配对行", dump.contains("配对: 发起=true"))
        assertTrue(dump.contains("超时"))
        assertTrue("要带环境，复制出来才自足", dump.contains("Android 14"))
        assertTrue("要带取证命令", dump.contains("adb logcat -c"))
        assertTrue(dump.contains("bond="))
        // 地址类型：3 = 可解析私有地址（RPA）—— 地址轮换的直接证据
        t.addressType = 3
        assertTrue(t.dump().contains("RANDOM_IDENTITY"))
    }

    /** 复现 22:21 日志的骨架：LL 活着、ATT 探针 5.5s 无答复。 */
    private fun attSilentShape(): ConnTrace = trace().also {
        it.connectGattAt = 1_000_200
        it.connectedAt = 1_000_400
        it.llProbeSent = true
        it.llProbeAnswered = true
        it.rssi = -73
        it.attProbeSent = true
        it.attProbeAnswered = false
    }
}
