package com.example.blechat.session

import com.example.blechat.ble.BleLog
import com.example.blechat.ble.LinkEvent
import com.example.blechat.protocol.Ack
import com.example.blechat.protocol.AuthOk
import com.example.blechat.protocol.Challenge
import com.example.blechat.protocol.Crypto
import com.example.blechat.protocol.Ctrl
import com.example.blechat.protocol.DataChunk
import com.example.blechat.protocol.Err
import com.example.blechat.protocol.Hello
import com.example.blechat.protocol.P
import com.example.blechat.protocol.Packer
import com.example.blechat.protocol.PeerEntry
import com.example.blechat.protocol.Progress
import com.example.blechat.protocol.ProtoException
import com.example.blechat.protocol.Reassembler
import com.example.blechat.protocol.decodeCtrl
import com.example.blechat.protocol.decodePeers
import com.example.blechat.protocol.encodeCtrl
import com.example.blechat.protocol.isLinkDead
import com.example.blechat.protocol.kindOfFlags
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.ThreadLocalRandom
import kotlin.math.max
import kotlin.math.min

/** 会话状态。与 Python `session.State` 对应（少一个 host 专用的 CHALLENGE_SENT）。 */
enum class SessionState {
    IDLE,
    CONNECTING,
    CONNECTED,
    HELLO_SENT,
    AUTH_SENT,
    AUTH_FAIL,
    READY,
    CLOSED,
}

/** 会话层推给 UI 的事件。 */
sealed interface SessionEvent {
    data class StateChanged(val state: SessionState) : SessionEvent

    /** `code` 是 [Err] 里的一员，`message` 是能直接贴到界面上的人话。 */
    data class Error(val code: Int, val message: String) : SessionEvent

    data class Message(val msgId: Int, val kind: String, val payload: ByteArray) : SessionEvent {
        override fun equals(other: Any?): Boolean =
            other is Message && msgId == other.msgId && kind == other.kind &&
                payload.contentEquals(other.payload)

        override fun hashCode(): Int = msgId * 31 + payload.contentHashCode()
    }

    data class Progress(val msgId: Int, val acked: Int, val total: Int) : SessionEvent

    /** Host 的昵称（AUTH_OK 里带下来）。 */
    data class HostName(val name: String) : SessionEvent

    data class Peers(val peers: List<PeerEntry>) : SessionEvent

    /** 对端刚进入 READY，要求**无条件**刷新一次状态/UI。不回包（PROTOCOL.md §7）。 */
    data object Resync : SessionEvent

    /** 会话结束。`reason` 是人话。 */
    data class Closed(val reason: String) : SessionEvent
}

data class SessionConfig(
    val localDeviceId: UUID,
    val localName: String,
    /** `(nonce_c, challenge_salt, iters, use_eph) -> psk`；返回 null 表示用户取消。 */
    val pskProvider: suspend (
        nonceC: ByteArray,
        salt: ByteArray,
        iters: Int,
        useEph: Boolean,
    ) -> ByteArray?,
    val handshakeTimeoutMs: Long = P.HANDSHAKE_TIMEOUT_MS,
)

/**
 * 客户端（Join 方）的协议会话：握手 → 收发 → 保活 → 断线。
 *
 * 所有状态只在**一条后台线程**上改（见 [dispatcher]），字段因此不需要加锁 ——
 * 这是 Python 端 asyncio 单线程语义在 Kotlin 里的对应物。
 */
class ChatSession(
    private val transport: Transport,
    private val config: SessionConfig,
) {
    private val executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "blechat-session").apply { isDaemon = true }
    }
    private val dispatcher = executor.asCoroutineDispatcher()
    private val scope = CoroutineScope(dispatcher + SupervisorJob())

    private val _state = MutableStateFlow(SessionState.IDLE)
    val state: StateFlow<SessionState> = _state.asStateFlow()

    /**
     * 会话事件。
     *
     * 用**无界 Channel** 而不是 `MutableSharedFlow`：SharedFlow 在没有订阅者时会把
     * 事件直接丢掉（replay=0），于是「先 `start()` 后订阅」的时序会漏帧；
     * Channel 无论如何都先存着，等订阅者来了再消费。
     */
    private val eventChannel = Channel<SessionEvent>(Channel.UNLIMITED)

    /** 会话事件流。**单消费者**：UI 与测试各自取用，不要同时 collect。 */
    val events: Flow<SessionEvent> = eventChannel.receiveAsFlow()

    // --- 握手 ---
    private var nonceC: ByteArray = ByteArray(0)
    private var nonceS: ByteArray = ByteArray(0)
    private var psk: ByteArray? = null
    private var sessionKey: ByteArray? = null
    private var reassembler: Reassembler? = null
    private var hsDeadline: Long = 0
    @Volatile private var waitingUser: Boolean = false

    /** AUTH_OK 下来的 session_id（u32 位模式）。 */
    var sessionId: Int = 0
        private set

    /** Host 昵称；AUTH_OK 没带时为空。 */
    var peerName: String = ""
        private set

    // --- 保活 ---
    private var pongDeadline: Long = 0
    private var pongMisses: Int = 0

    // --- 发送 ---
    private var msgCounter = ThreadLocalRandom.current().nextInt(1 shl 16)
    private val sessionCounter = ThreadLocalRandom.current().nextInt(1 shl 16)
    private val jobs = HashMap<Int, SendJob>()

    // --- 接收 ACK 合并 ---
    private val ackedSeen = HashMap<Int, Int>()
    private val ackPending = HashMap<Int, Triple<Int, Int, Boolean>>()

    private var closedFired = false

    private class SendJob(val msgId: Int, val frames: List<ByteArray>) {
        val total: Int = frames.size
        var cursor: Int = 0
        var acked: Int = 0
        var retries: Int = 0
        var lastAckAt: Long = System.currentTimeMillis()
    }

    // ---------------------------------------------------------------- 生命周期

    fun start() {
        if (_state.value != SessionState.IDLE) return
        scope.launch { collectTransport() }
        scope.launch { openLink() }
        scope.launch { ackWorkerLoop() }
        scope.launch { keepaliveLoop() }
    }

    /** 关闭会话（可重复调用）。 */
    fun close(reason: String) {
        if (_state.value == SessionState.CLOSED) {
            fireClosed(reason)
            return
        }
        setState(SessionState.CLOSED)
        jobs.clear()
        ackPending.clear()
        fireClosed(reason)
        scope.launch {
            runCatching { transport.shutdown(reason) }
            delay(200)
            scope.cancel()
        }
    }

    /** 停掉整个会话对象（UI 退出时调用）。 */
    fun dispose() {
        if (_state.value != SessionState.CLOSED) close("disposed")
        scope.cancel()
        runCatching { dispatcher.close() }
        runCatching { executor.shutdownNow() }
    }

    private fun fireClosed(reason: String) {
        if (closedFired) return
        closedFired = true
        eventChannel.trySend(SessionEvent.Closed(reason))
    }

    private fun setState(s: SessionState) {
        if (_state.value == s) return
        val prev = _state.value
        _state.value = s
        BleLog.i("Session", "$prev -> $s")
        eventChannel.trySend(SessionEvent.StateChanged(s))
    }

    private fun emitError(code: Int, message: String) {
        BleLog.w("Session", "error code=$code $message")
        eventChannel.trySend(SessionEvent.Error(code, message))
    }

    // ---------------------------------------------------------------- 建链

    private suspend fun openLink() {
        setState(SessionState.CONNECTING)
        try {
            transport.open()
        } catch (t: CancellationException) {
            throw t
        } catch (t: Throwable) {
            val msg = t.message ?: "连接失败"
            BleLog.e("Session", "openLink 失败: $msg")
            emitError(Err.NOT_READY, msg)
            close("连接失败：$msg")
        }
    }

    private suspend fun collectTransport() {
        transport.events.collect { ev ->
            if (_state.value != SessionState.CLOSED) {
                when (ev) {
                    is LinkEvent.Ready -> startHandshake()
                    is LinkEvent.Ctrl -> feedCtrl(ev.bytes)
                    is LinkEvent.Data -> feedData(ev.bytes)
                    is LinkEvent.Disconnected -> close(linkDownReason(ev.reason))
                }
            }
        }
    }

    // ---------------------------------------------------------------- 握手

    private suspend fun startHandshake() {
        setState(SessionState.CONNECTED)
        nonceC = Crypto.randomBytes(16)
        hsDeadline = System.currentTimeMillis() + config.handshakeTimeoutMs
        setState(SessionState.HELLO_SENT)
        scope.launch { watchdogLoop() }
        sendCtrlNow(Ctrl.HELLO, Hello(config.localDeviceId, config.localName, nonceC).encode())
    }

    private suspend fun watchdogLoop() {
        while (true) {
            delay(500)
            val st = _state.value
            if (st == SessionState.READY || st == SessionState.CLOSED) return
            if (waitingUser) {
                hsDeadline = System.currentTimeMillis() + config.handshakeTimeoutMs
                continue
            }
            if (System.currentTimeMillis() > hsDeadline) {
                emitError(
                    Err.HANDSHAKE_TIMEOUT,
                    "握手超时（${config.handshakeTimeoutMs / 1000}s 无进展，当前 $st）",
                )
                sendCtrlNow(Ctrl.BYE, byteArrayOf(Err.HANDSHAKE_TIMEOUT.toByte()))
                close("handshake timeout")
                return
            }
        }
    }

    private fun feedCtrl(raw: ByteArray) {
        val type: Int
        val payload: ByteArray
        try {
            val f = decodeCtrl(raw)
            type = f.type
            payload = f.payload
        } catch (e: ProtoException) {
            emitError(e.code, e.message ?: "控制帧错误")
            return
        }
        if (_state.value != SessionState.READY && _state.value != SessionState.CLOSED) {
            hsDeadline = System.currentTimeMillis() + config.handshakeTimeoutMs
        }
        // 每一帧控制帧都留一行 DEBUG：真机排查「握手卡在哪一步」全靠它。
        BleLog.d(
            "Session",
            "收控制帧 ${ctrlName(type)} ${payload.size}B state=${_state.value}",
        )
        try {
            when (type) {
                Ctrl.BYE -> {
                    val code = if (payload.isNotEmpty()) payload[0].toInt() and 0xFF else Err.UNKNOWN
                    emitError(code, "对端 BYE: ${Err.nameOf(code)}")
                    close(Err.nameOf(code))
                }
                Ctrl.ACK -> {
                    val a = Ack.decode(payload)
                    onAck(a.msgId, a.nextSeq)
                }
                Ctrl.PROGRESS -> {
                    val p = Progress.decode(payload)
                    onAck(p.msgId, p.acked)
                }
                Ctrl.RESYNC -> eventChannel.trySend(SessionEvent.Resync)
                Ctrl.PING -> scope.launch { sendCtrlNow(Ctrl.PONG) }
                Ctrl.PONG -> handlePong()
                Ctrl.CHALLENGE -> {
                    val ch = Challenge.decode(payload)
                    scope.launch { handleChallenge(ch) }
                }
                Ctrl.AUTH_OK -> handleAuthOk(AuthOk.decode(payload))
                Ctrl.AUTH_FAIL -> handleAuthFail(payload)
                Ctrl.PEERS -> eventChannel.trySend(SessionEvent.Peers(decodePeers(payload)))
                Ctrl.HELLO -> emitError(Err.UNKNOWN, "客户端收到 HELLO")
                else -> Unit // PROTOCOL.md §7：未知 type 静默忽略
            }
        } catch (e: ProtoException) {
            emitError(e.code, e.message ?: "控制帧处理失败")
        } catch (t: Throwable) {
            emitError(Err.UNKNOWN, t.message ?: "控制帧处理失败")
        }
    }

    private suspend fun handleChallenge(ch: Challenge) {
        if (_state.value != SessionState.HELLO_SENT) {
            emitError(Err.NOT_READY, "收到 CHALLENGE 但状态不对")
            return
        }
        nonceS = ch.nonceS
        waitingUser = true
        val derived = try {
            config.pskProvider(nonceC, ch.salt, ch.iters, ch.useEph)
        } finally {
            waitingUser = false
        }
        if (derived == null) {
            emitError(Err.AUTH_FAILED, "已取消密码输入")
            close("cancelled")
            return
        }
        psk = derived
        setState(SessionState.AUTH_SENT)
        sendCtrlNow(Ctrl.AUTH, Crypto.hmacAuth(derived, nonceC, ch.nonceS))
    }

    private fun handleAuthOk(ok: AuthOk) {
        val p = psk
        if (p == null) {
            emitError(Err.AUTH_FAILED, "未派生 PSK")
            return
        }
        sessionId = ok.sessionId
        sessionKey = Crypto.hkdfSessionKey(p, ok.hkdfSalt)
        psk = null // 握手完成即丢弃
        reassembler = Reassembler(sessionKey!!)
        if (ok.name.isNotEmpty() && ok.name != peerName) {
            peerName = ok.name
            eventChannel.trySend(SessionEvent.HostName(ok.name))
        }
        pongMisses = 0
        pongDeadline = System.currentTimeMillis() + P.PONG_GRACE_MS
        setState(SessionState.READY)
        // 我刚连上 → 通知对端无条件刷新一次（见 PROTOCOL.md §2.1 RESYNC）
        scope.launch { sendCtrlNow(Ctrl.RESYNC) }
    }

    private fun handleAuthFail(payload: ByteArray) {
        val code = if (payload.isNotEmpty()) payload[0].toInt() and 0xFF else Err.AUTH_FAILED
        setState(SessionState.AUTH_FAIL)
        emitError(code, "认证失败（${Err.nameOf(code)}）")
    }

    private fun linkDead(message: String) {
        if (_state.value == SessionState.CLOSED) return
        BleLog.e("Session", "linkDead: $message")
        // 掉线不是"错误"：提示走 Closed(reason)，不刷一条 UNKNOWN 原始串出来
        close(linkDownReason(message))
    }

    // ---------------------------------------------------------------- 保活

    private suspend fun keepaliveLoop() {
        var round = 0
        while (true) {
            if (_state.value != SessionState.READY) {
                delay(200)
                continue
            }
            delay(P.KEEPALIVE_INTERVAL_MS)
            if (_state.value != SessionState.READY) continue
            round++
            if (!keepaliveRound()) return
            // 每一轮都留一行：真机「连上后几十秒就失联」只能靠这个定位
            // （是 PING 没发出去、还是对端没回 PONG）。
            BleLog.d(
                "Session",
                "保活 #$round 发 PING（misses=$pongMisses 距上次 PONG " +
                    "${System.currentTimeMillis() - (pongDeadline - P.PONG_GRACE_MS)}ms）",
            )
            sendCtrlNow(Ctrl.PING)
        }
    }

    /**
     * 一轮保活：检查上一帧 PING 的 PONG 有没有按时回来。返回 false = 链路已死。
     *
     * 判定本身是纯函数 [Keepalive.round]（可单测），这里负责把结果落到字段 + 打日志。
     *
     * @param now 注入时钟，单测用（默认真实时间）。
     */
    internal fun keepaliveRound(now: Long = System.currentTimeMillis()): Boolean {
        if (_state.value != SessionState.READY) return false
        val d = Keepalive.round(now, pongDeadline, pongMisses)
        pongMisses = d.misses
        pongDeadline = d.deadline
        if (d.missedThisRound && d.alive) {
            BleLog.w(
                "Session",
                "保活错过一轮 PONG（misses=$pongMisses/${
                    P.KEEPALIVE_GRACE_MISSES + P.KEEPALIVE_MAX_MISSES
                }）",
            )
        }
        if (!d.alive) {
            linkDead("no PONG from peer after $pongMisses keepalives (half-open link)")
        }
        return d.alive
    }

    private fun handlePong() {
        if (pongMisses != 0) BleLog.i("Session", "收到 PONG，保活计数清零（此前 misses=$pongMisses）")
        pongMisses = 0
        pongDeadline = System.currentTimeMillis() + P.PONG_GRACE_MS
    }

    // ---------------------------------------------------------------- 接收

    private suspend fun feedData(raw: ByteArray) {
        val key = sessionKey
        val re = reassembler
        if (_state.value != SessionState.READY || re == null || key == null) {
            emitError(Err.NOT_READY, "未完成握手，数据帧被丢弃")
            sendCtrlNow(Ctrl.BYE, byteArrayOf(Err.NOT_READY.toByte()))
            return
        }
        val chunk: DataChunk
        val result: Triple<Int, Int, ByteArray>?
        try {
            chunk = DataChunk.decode(raw)
            result = re.feed(raw)
        } catch (e: ProtoException) {
            emitError(e.code, e.message ?: "数据帧错误")
            if (e.code == Err.CRC_MISMATCH) {
                sendCtrlNow(Ctrl.BYE, byteArrayOf(Err.CRC_MISMATCH.toByte()))
            }
            return
        }

        val msgId: Int
        val contig: Int
        if (result != null) {
            msgId = result.first
            contig = chunk.total
            eventChannel.trySend(SessionEvent.Message(msgId, kindOfFlags(result.second), result.third))
        } else {
            msgId = chunk.msgId
            contig = re.progress(msgId)
        }

        if (contig > (ackedSeen[msgId] ?: -1)) {
            ackedSeen[msgId] = contig
            val withProgress = contig % P.PROGRESS_EVERY == 0 || contig == chunk.total
            ackPending[msgId] = Triple(contig, chunk.total, withProgress)
        }
        if (ackedSeen.size > 64) {
            ackedSeen.keys.toList().take(32).forEach { ackedSeen.remove(it) }
        }
    }

    private fun onAck(msgId: Int, nextSeq: Int) {
        val job = jobs[msgId] ?: return
        if (nextSeq > job.acked) {
            job.acked = min(nextSeq, job.total)
            job.retries = 0
            job.lastAckAt = System.currentTimeMillis()
            job.cursor = max(job.cursor, job.acked)
            eventChannel.trySend(SessionEvent.Progress(msgId, job.acked, job.total))
        }
    }

    private suspend fun ackWorkerLoop() {
        while (true) {
            if (_state.value != SessionState.READY || ackPending.isEmpty()) {
                delay(50)
                continue
            }
            val batch = HashMap(ackPending)
            ackPending.clear()
            for ((msgId, triple) in batch) {
                val (contig, total, withProgress) = triple
                sendCtrlNow(Ctrl.ACK, Ack(msgId, contig).encode())
                if (withProgress) {
                    sendCtrlNow(Ctrl.PROGRESS, Progress(msgId, contig, total).encode())
                }
            }
        }
    }

    // ---------------------------------------------------------------- 发送

    /**
     * 发一条文本。**suspend**：内部切到会话线程再改 `msgCounter/jobs`，
     * 否则 UI 线程会跟 ACK 回调线程抢这些字段（Python 是 asyncio 单线程，没这个问题）。
     */
    suspend fun sendText(text: String): Int =
        sendPayload(text.toByteArray(Charsets.UTF_8), image = false)

    suspend fun sendImage(data: ByteArray): Int = sendPayload(data, image = true)

    suspend fun sendPayload(payload: ByteArray, image: Boolean = false, file: Boolean = false): Int =
        withContext(dispatcher) { sendPayloadOnSessionThread(payload, image, file) }

    private fun sendPayloadOnSessionThread(payload: ByteArray, image: Boolean, file: Boolean): Int {
        if (_state.value != SessionState.READY || sessionKey == null) {
            throw ProtoException(Err.NOT_READY, "尚未就绪")
        }
        if (payload.size > P.MAX_PAYLOAD) {
            throw ProtoException(Err.MSG_TOO_LARGE, "${payload.size} > ${P.MAX_PAYLOAD}")
        }
        val msgId = allocMsgId()
        val frames = Packer.pack(
            msgId,
            payload,
            sessionKey!!,
            image = image,
            allowCompress = true,
            maxChunk = transport.maxChunk,
            file = file,
        )
        val job = SendJob(msgId, frames)
        jobs[msgId] = job
        scope.launch { pump(job) }
        eventChannel.trySend(SessionEvent.Progress(msgId, 0, job.total))
        return msgId
    }

    private fun allocMsgId(): Int {
        msgCounter = (msgCounter + 1) and 0xFFFF
        return (sessionCounter shl 16) or msgCounter
    }

    private suspend fun pump(job: SendJob) {
        try {
            while (job.acked < job.total) {
                if (System.currentTimeMillis() - job.lastAckAt > P.ACK_TIMEOUT_MS) {
                    job.retries++
                    if (job.retries > P.MAX_RETRIES) {
                        throw ProtoException(Err.TOO_MANY_RETRIES, "msg ${job.msgId} 重传超限")
                    }
                    job.cursor = job.acked
                    job.lastAckAt = System.currentTimeMillis()
                }
                while (job.cursor < job.total && job.cursor - job.acked < P.SEND_WINDOW) {
                    withTimeout(P.SEND_TIMEOUT_MS) { transport.sendData(job.frames[job.cursor]) }
                    job.cursor++
                    yield() // 让 ACK 先被处理，避免假重传
                }
                delay(20)
            }
        } catch (t: CancellationException) {
            throw t
        } catch (t: TimeoutCancellationException) {
            linkDead("send_data timed out")
        } catch (e: ProtoException) {
            emitError(e.code, e.message ?: "发送失败")
            sendCtrlNow(Ctrl.BYE, byteArrayOf(e.code.toByte()))
        } catch (t: Throwable) {
            val msg = t.message ?: "发送失败"
            if (isLinkDead(msg)) linkDead(msg) else emitError(Err.UNKNOWN, msg)
        } finally {
            jobs.remove(job.msgId)
            if (job.acked >= job.total) {
                eventChannel.trySend(SessionEvent.Progress(job.msgId, job.total, job.total))
            }
        }
    }

    // ---------------------------------------------------------------- 底层发送

    /** 套 8s 超时的控制帧；链路已死则判死。返回是否发出去了。 */
    private suspend fun sendCtrlNow(type: Int, payload: ByteArray = ByteArray(0)): Boolean = try {
        BleLog.d("Session", "发控制帧 ${ctrlName(type)} ${payload.size}B state=${_state.value}")
        withTimeout(P.SEND_TIMEOUT_MS) { transport.sendCtrl(encodeCtrl(type, payload)) }
        true
    } catch (t: CancellationException) {
        throw t
    } catch (t: Throwable) {
        val msg = t.message ?: "发送控制帧失败"
        if (isLinkDead(msg) && _state.value != SessionState.CLOSED) {
            linkDead(msg)
        } else if (_state.value != SessionState.CLOSED) {
            emitError(Err.NOT_READY, msg)
        }
        false
    }

    private fun linkDownReason(message: String): String {
        if (message.contains("timeout", ignoreCase = true) || message.contains("超时")) {
            return "链路超时"
        }
        if (isLinkDead(message)) return "对方已离线"
        return message.trim().ifEmpty { "链路已断开" }
    }

    /** 控制帧 type → 名字（只用于日志）。 */
    private fun ctrlName(type: Int): String = when (type) {
        Ctrl.HELLO -> "HELLO"
        Ctrl.CHALLENGE -> "CHALLENGE"
        Ctrl.AUTH -> "AUTH"
        Ctrl.AUTH_OK -> "AUTH_OK"
        Ctrl.AUTH_FAIL -> "AUTH_FAIL"
        Ctrl.ACK -> "ACK"
        Ctrl.BYE -> "BYE"
        Ctrl.PEERS -> "PEERS"
        Ctrl.PROGRESS -> "PROGRESS"
        Ctrl.PING -> "PING"
        Ctrl.RESYNC -> "RESYNC"
        Ctrl.PONG -> "PONG"
        else -> "type=$type"
    }
}
