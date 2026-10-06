package com.example.blechat.session

import com.example.blechat.ble.LinkEvent
import com.example.blechat.protocol.Ack
import com.example.blechat.protocol.AuthOk
import com.example.blechat.protocol.Challenge
import com.example.blechat.protocol.Crypto
import com.example.blechat.protocol.Ctrl
import com.example.blechat.protocol.DataChunk
import com.example.blechat.protocol.Err
import com.example.blechat.protocol.Hello
import com.example.blechat.protocol.Packer
import com.example.blechat.protocol.ProtoException
import com.example.blechat.protocol.Reassembler
import com.example.blechat.protocol.decodeCtrl
import com.example.blechat.protocol.encodeCtrl
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.filterIsInstance
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.UUID
import kotlin.random.Random

/**
 * 用一个「Python Host 的 Kotlin 复刻」驱动 [ChatSession]，验证握手 / 收发 / ACK。
 *
 * 这是协议层不碰真 BLE 就能做的端到端验证。
 */
class ChatSessionTest {

    private class FakeTransport : Transport {
        override val maxChunk: Int = 228
        private val _events = MutableSharedFlow<LinkEvent>(extraBufferCapacity = 64)
        override val events: SharedFlow<LinkEvent> = _events.asSharedFlow()
        val ctrlOut = Channel<ByteArray>(Channel.UNLIMITED)
        val dataOut = Channel<ByteArray>(Channel.UNLIMITED)
        var shutdownReason: String? = null

        override suspend fun open() {
            _events.emit(LinkEvent.Ready(247, "AA:BB:CC:DD:EE:FF"))
        }

        override suspend fun sendCtrl(bytes: ByteArray) {
            ctrlOut.send(bytes)
        }

        override suspend fun sendData(bytes: ByteArray) {
            dataOut.send(bytes)
        }

        override fun shutdown(reason: String) {
            shutdownReason = reason
            _events.tryEmit(LinkEvent.Disconnected(reason))
        }

        fun injectCtrl(bytes: ByteArray) {
            _events.tryEmit(LinkEvent.Ctrl(bytes))
        }

        fun injectData(bytes: ByteArray) {
            _events.tryEmit(LinkEvent.Data(bytes))
        }
    }

    private val deviceA = UUID.fromString("01234567-89ab-cdef-0123-456789abcdef")
    private val password = "correct horse battery staple"
    private val salt = ByteArray(16) { it.toByte() }
    private val nonceS = ByteArray(16) { (it + 32).toByte() }
    private val hkdfSalt = ByteArray(16) { (it + 64).toByte() }
    private val hostPsk by lazy { Crypto.derivePsk(password, salt, 1000) }

    private fun config(psk: ByteArray? = hostPsk) = SessionConfig(
        localDeviceId = deviceA,
        localName = "设备A",
        pskProvider = { _, _, _, _ -> psk },
    )

    /**
     * 扮演 Python Host 走完 HELLO → CHALLENGE → AUTH → AUTH_OK，
     * 并断言客户端 READY 后立刻发了一帧 RESYNC。结束时 `ctrlOut` 是空的。
     */
    private fun handshake(t: FakeTransport, session: ChatSession, hostName: String = "宿主昵称") {
        runBlocking {
            withTimeout(5_000) {
                val helloFrame = t.ctrlOut.receive()
                assertEquals(Ctrl.HELLO, decodeCtrl(helloFrame).type)
                val hello = Hello.decode(decodeCtrl(helloFrame).payload)
                assertEquals(deviceA, hello.deviceId)
                assertEquals("设备A", hello.name)

                t.injectCtrl(
                    encodeCtrl(Ctrl.CHALLENGE, Challenge(nonceS, salt, 1000).encode()),
                )

                val authFrame = t.ctrlOut.receive()
                assertEquals(Ctrl.AUTH, decodeCtrl(authFrame).type)
                assertArrayEquals(
                    Crypto.hmacAuth(hostPsk, hello.nonceC, nonceS),
                    decodeCtrl(authFrame).payload,
                )

                t.injectCtrl(
                    encodeCtrl(Ctrl.AUTH_OK, AuthOk(0x002A0001, hkdfSalt, hostName).encode()),
                )

                session.state.first { it == SessionState.READY }

                val resync = t.ctrlOut.receive()
                assertEquals(Ctrl.RESYNC, decodeCtrl(resync).type)
            }
        }
    }

    private fun <T> awaitEvent(session: ChatSession, block: suspend () -> T?): T = runBlocking {
        withTimeout(5_000) {
            block() ?: throw AssertionError("没有等到预期事件")
        }
    }

    @Test
    fun handshakeReachesReadyAndSendsResync() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            assertEquals(SessionState.READY, session.state.value)
            assertEquals("宿主昵称", session.peerName)
            assertEquals(0x002A0001, session.sessionId)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun authFailIsReported() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            runBlocking {
                withTimeout(5_000) {
                    t.ctrlOut.receive()
                    t.injectCtrl(
                        encodeCtrl(Ctrl.CHALLENGE, Challenge(nonceS, salt, 1000).encode()),
                    )
                    t.ctrlOut.receive()
                    t.injectCtrl(
                        encodeCtrl(Ctrl.AUTH_FAIL, byteArrayOf(Err.AUTH_FAILED.toByte())),
                    )
                    session.state.first { it == SessionState.AUTH_FAIL }
                }
            }
            val err = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Error>().first()
            }
            assertEquals(Err.AUTH_FAILED, err.code)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun cancelledPasswordClosesSession() {
        val t = FakeTransport()
        val session = ChatSession(t, config(psk = null)) // 用户取消输密码
        try {
            session.start()
            runBlocking {
                withTimeout(5_000) {
                    t.ctrlOut.receive() // HELLO
                    t.injectCtrl(
                        encodeCtrl(Ctrl.CHALLENGE, Challenge(nonceS, salt, 1000).encode()),
                    )
                }
            }
            val closed = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Closed>().first()
            }
            assertEquals("cancelled", closed.reason)
            assertEquals(SessionState.CLOSED, session.state.value)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun sendTextIsAcknowledgedAndProgressCompletes() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)

            val text = "你好，BLE 中文消息测试"
            val msgId = runBlocking { session.sendText(text) }
            val key = Crypto.hkdfSessionKey(hostPsk, hkdfSalt)

            val re = Reassembler(key)
            var got: ByteArray? = null
            var total = 0
            while (got == null) {
                val frame = runBlocking { withTimeout(5_000) { t.dataOut.receive() } }
                total = DataChunk.decode(frame).total
                got = re.feed(frame)?.third
            }
            assertArrayEquals(text.toByteArray(Charsets.UTF_8), got)

            t.injectCtrl(encodeCtrl(Ctrl.ACK, Ack(msgId, total).encode()))
            val progress = awaitEvent(session) {
                session.events
                    .filterIsInstance<SessionEvent.Progress>()
                    .first { it.msgId == msgId && it.acked == it.total }
            }
            assertEquals(total, progress.total)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun receivedMessageIsEmittedAndAcked() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)

            val key = Crypto.hkdfSessionKey(hostPsk, hkdfSalt)
            val hostMsg = "Host 发过来的一句话"
            val frames = Packer.pack(
                0x1111,
                hostMsg.toByteArray(Charsets.UTF_8),
                key,
                image = false,
                allowCompress = true,
                maxChunk = t.maxChunk,
            )
            frames.forEach { t.injectData(it) }

            val msg = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Message>().first()
            }
            assertEquals(0x1111, msg.msgId)
            assertEquals("text", msg.kind)
            assertEquals(hostMsg, String(msg.payload, Charsets.UTF_8))

            val ackFrame = runBlocking { withTimeout(5_000) { t.ctrlOut.receive() } }
            val ack = decodeCtrl(ackFrame)
            assertEquals(Ctrl.ACK, ack.type)
            val decoded = Ack.decode(ack.payload)
            assertEquals(0x1111, decoded.msgId)
            assertEquals(frames.size, decoded.nextSeq)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun pingIsAnsweredWithPong() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            t.injectCtrl(encodeCtrl(Ctrl.PING))
            val reply = runBlocking { withTimeout(3_000) { t.ctrlOut.receive() } }
            assertEquals(Ctrl.PONG, decodeCtrl(reply).type)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun byeClosesSessionWithReason() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            t.injectCtrl(encodeCtrl(Ctrl.BYE, byteArrayOf(Err.SERVER_SHUTDOWN.toByte())))
            val closed = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Closed>().first()
            }
            assertEquals(Err.nameOf(Err.SERVER_SHUTDOWN), closed.reason)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun sendBeforeReadyThrows() {
        val t = FakeTransport()
        val session = ChatSession(t, config(psk = null))
        try {
            try {
                runBlocking { session.sendText("nope") }
                fail("未就绪必须抛异常")
            } catch (e: ProtoException) {
                assertEquals(Err.NOT_READY, e.code)
            }
        } finally {
            session.dispose()
        }
    }

    @Test
    fun unknownCtrlTypeIsIgnored() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            t.injectCtrl(encodeCtrl(0x7E, Random(1).nextBytes(5)))
            assertEquals(SessionState.READY, session.state.value)
            val err = runBlocking {
                withTimeoutOrNull(400) {
                    session.events.filterIsInstance<SessionEvent.Error>().first()
                }
            }
            assertNull("未知 type 不应报错", err)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun linkDisconnectClosesSessionWithHumanReason() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            t.shutdown("gatt protocol error 133")
            val closed = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Closed>().first()
            }
            assertEquals("对方已离线", closed.reason)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun resyncIsForwardedAsEventWithoutReply() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session)
            t.injectCtrl(encodeCtrl(Ctrl.RESYNC))
            val ev = awaitEvent(session) {
                session.events.filterIsInstance<SessionEvent.Resync>().first()
            }
            assertEquals(SessionEvent.Resync, ev)
            // 不回包（PROTOCOL.md §7）：不该再冒出任何控制帧
            val extra = runBlocking { withTimeoutOrNull(400) { t.ctrlOut.receive() } }
            assertNull("RESYNC 不应回包", extra)
        } finally {
            session.dispose()
        }
    }

    @Test
    fun hostNameIsUpdatedFromAuthOk() {
        val t = FakeTransport()
        val session = ChatSession(t, config())
        try {
            session.start()
            handshake(t, session, hostName = "我的电脑")
            assertEquals("我的电脑", session.peerName)
        } finally {
            session.dispose()
        }
    }
}
