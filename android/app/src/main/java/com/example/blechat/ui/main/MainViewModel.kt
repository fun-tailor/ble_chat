package com.example.blechat.ui.main

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.blechat.ble.BleLog
import com.example.blechat.ble.BleScanner
import com.example.blechat.ble.Bonder
import com.example.blechat.ble.ConnTrace
import com.example.blechat.ble.DiagKind
import com.example.blechat.ble.Diagnosis
import com.example.blechat.ble.GattLink
import com.example.blechat.ble.ScannedDevice
import com.example.blechat.data.Repository
import com.example.blechat.protocol.Crypto
import com.example.blechat.protocol.P
import com.example.blechat.session.BleTransport
import com.example.blechat.session.ChatSession
import com.example.blechat.session.SessionConfig
import com.example.blechat.session.SessionEvent
import com.example.blechat.session.SessionState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import java.util.UUID

/** 界面上的阶段。比 [SessionState] 粗，只表达「用户此刻该看到什么」。 */
enum class Phase { IDLE, SCANNING, CONNECTING, HANDSHAKE, READY, CLOSED }

/** 一条消息（历史 + 实时，同一条记录）。 */
data class UiMessage(
    val key: Long,
    val msgId: Int,
    val outgoing: Boolean,
    val kind: String,
    val text: String,
    val peerName: String?,
    val createdAt: Long,
)

/** 弹出的口令框请求。UI 调 [MainViewModel.submitPassword] 或 [MainViewModel.cancelPassword] 应答。 */
data class PasswordRequest(val networkId: String, val hostName: String)

class MainViewModel(app: Application) : AndroidViewModel(app) {
    private val repo = Repository.get(app)
    private val scanner = BleScanner(app)

    private val _phase = MutableStateFlow(Phase.IDLE)
    val phase: StateFlow<Phase> = _phase.asStateFlow()

    private val _status = MutableStateFlow("未连接")
    val status: StateFlow<String> = _status.asStateFlow()

    private val _messages = MutableStateFlow<List<UiMessage>>(emptyList())
    val messages: StateFlow<List<UiMessage>> = _messages.asStateFlow()

    private val _results = MutableStateFlow<List<ScannedDevice>>(emptyList())
    val results: StateFlow<List<ScannedDevice>> = _results.asStateFlow()

    private val _peerName = MutableStateFlow("")
    val peerName: StateFlow<String> = _peerName.asStateFlow()

    private val _composer = MutableStateFlow("")
    val composer: StateFlow<String> = _composer.asStateFlow()

    private val _passwordRequest = MutableStateFlow<PasswordRequest?>(null)
    val passwordRequest: StateFlow<PasswordRequest?> = _passwordRequest.asStateFlow()

    private val _snackbar = Channel<String>(Channel.BUFFERED)
    val snackbar = _snackbar.receiveAsFlow()

    /**
     * 最近一次连接尝试的**病历**（[com.example.blechat.ble.ConnTrace.dump]）。
     *
     * 连接失败时非空，UI 上「诊断」按钮会展示它 + 处置建议，用户可一键复制发出来。
     * 这是本项目排查「扫到但连不上」的主要抓手：一次失败到底卡在哪一步，
     * 只看这一屏就够，不用再让用户去翻 logcat。
     */
    private val _diagnostics = MutableStateFlow<String?>(null)
    val diagnostics: StateFlow<String?> = _diagnostics.asStateFlow()

    /**
     * 失败后给用户的**处置建议**（人话，可能多行）。UI 在连接面板里直接显示，
     * 比一闪而过的 snackbar 有用得多 —— 用户看着它去动 PC 那一端。
     */
    private val _hint = MutableStateFlow<String?>(null)
    val hint: StateFlow<String?> = _hint.asStateFlow()

    /**
     * 连接前是否先做一次 **BLE 配对**。默认开，存在 [com.example.blechat.data.AppPrefs]。
     *
     * 这是"LL 连得上、ATT 零回复"这一路现象里**手机侧唯一能自己动的杠杆**：
     * 手机和 PC 的 LE 地址都是会轮换的随机地址（RPA），只有配对过（交换 IRK）双方
     * 才能认出彼此 —— 而"认不出"的表现正好就是 ATT 不服务。见 [com.example.blechat.ble.Bonder]。
     */
    private val _bondFirst = MutableStateFlow(repo.prefs.bondFirst)
    val bondFirst: StateFlow<Boolean> = _bondFirst.asStateFlow()

    /** 本次尝试是否**到过** READY（到了就不是"连不上"，不该自动重连）。 */
    private var everReady = false

    /** 自动补救用掉几次（每次用户手点连接都会重置；一次就够，别把用户拖进循环）。 */
    private var autoRetryUsed = 0

    /**
     * 连续失败次数（到 READY 清零）。
     *
     * 为什么关心它：Windows 侧的 GATT 服务在链路异常后**可能还占着上一次的连接**，
     * 而它自己要 ~30~60s 才释放（真机旁证：PC 日志里 `peer … disconnected` 恰好
     * 每 ~31s 一次 ≈ ATT 事务超时 30s）。这种情况下**越急着重连越连不上**，
     * 所以连续失败时要把"等一会儿 / 去动 PC"明确讲给用户听。
     */
    private var consecutiveFailures = 0

    /** 当前地址是怎么来的：`scan-name` / `scan-pick` / `stale-mac` / `manual`。 */
    private var lastAddressSource: String = "manual"

    private var link: GattLink? = null
    private var session: ChatSession? = null
    private var scanJob: Job? = null
    private var reconnectJob: Job? = null
    private var eventJob: Job? = null
    private var passwordWaiter: CompletableDeferred<String?>? = null
    private var networkId: String? = null
    private var hostName: String = ""

    val localName: String get() = repo.prefs.displayName

    init {
        // SPEC §7：启动时清一次过期历史（默认 3 天）
        viewModelScope.launch {
            try {
                repo.purgeOlderThan()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _snackbar.trySend("清理历史失败：${e.message}")
            }
        }
    }

    // ------------------------------------------------------------ 来自系统分享

    /** `ACTION_SEND` 进来的文本：先塞进输入框，连上再发（v1 不自动重发）。 */
    fun onSharedText(text: String) {
        if (text.isBlank()) return
        _composer.value = if (_composer.value.isBlank()) text
        else _composer.value + "\n" + text
    }

    fun onComposeChanged(text: String) {
        _composer.value = text
    }

    // ------------------------------------------------------------------ 扫描

    fun startScan() {
        if (_phase.value == Phase.SCANNING) return
        _phase.value = Phase.SCANNING
        _status.value = "正在扫描…"
        _results.value = emptyList()
        scanJob = viewModelScope.launch {
            try {
                scanner.scan(onlyMine = true, timeoutMs = 15_000).collect { dev ->
                    val list = _results.value
                    if (list.none { it.address == dev.address }) {
                        _results.value = list + dev
                        // 首次见到才打一条：用来核对「点的那个名字」到底连的哪个 MAC
                        // （真机上见过连到 65:68:.. 这种随机地址，而 PC 广播的是 8c:c6:..）
                        BleLog.i(TAG, "扫描到 ${dev.name} addr=${dev.address} rssi=${dev.rssi}")
                    }
                }
                if (_phase.value == Phase.SCANNING) {
                    _status.value = if (_results.value.isEmpty()) {
                        "扫描无结果（可直接按 MAC 连接）"
                    } else {
                        "扫描到 ${_results.value.size} 个设备"
                    }
                    _phase.value = Phase.IDLE
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _snackbar.trySend("扫描失败：${e.message}")
                _phase.value = Phase.IDLE
            }
        }
    }

    fun stopScan() {
        scanJob?.cancel()
        scanJob = null
        if (_phase.value == Phase.SCANNING) _phase.value = Phase.IDLE
    }

    /** 用户拒绝了蓝牙权限：扫描走不通，但仍可按 MAC 直连（PEP：定位权限缺失同款兜底）。 */
    fun onPermissionDenied() {
        stopScan()
        _status.value = "未授权扫描"
        _snackbar.trySend("未授予蓝牙权限，无法扫描；可改用「按 MAC 连接」")
    }

    /** 上次连过的 Host，用作「一键重连」的默认值。 */
    fun localAddressHint(): String? = repo.prefs.lastHostAddress

    fun localHostNameHint(): String? = repo.prefs.lastHostName

    /**
     * 一键重连。
     *
     * **绝不能直接用存下来的 MAC**：Windows 的 LE 广播地址会轮换，实测同一天见过
     * `65:68:..` / `76:0D:..` / `4B:AA:..` 三个，而 PC 的身份地址一直是
     * `8C:C6:81:9C:B9:7B`。旧地址要么连不上，要么连到一台没在跑 Host 的机器
     * —— 表现就是「LL 连上了但 ATT 一个回调都不回来」。
     * 所以先按上次的 Host **名字**扫一小段，拿到当前地址再连；扫不到才退回旧地址。
     */
    fun reconnectLast() {
        val name = repo.prefs.lastHostName
        val addr = repo.prefs.lastHostAddress
        if (name.isNullOrBlank() || !scanner.hasScanPermission()) {
            if (addr != null) connectByMac(addr) else _snackbar.trySend("还没有连过的 Host")
            return
        }
        if (_phase.value == Phase.SCANNING || reconnectJob?.isActive == true) return
        stopScan()
        _phase.value = Phase.SCANNING
        _status.value = "正在按名字找 $name…"
        _results.value = emptyList()
        reconnectJob = viewModelScope.launch {
            var hit: ScannedDevice? = null
            try {
                scanner.scan(onlyMine = true, timeoutMs = RECONNECT_SCAN_MS).collect { dev ->
                    val list = _results.value
                    if (list.none { it.address == dev.address }) {
                        _results.value = list + dev
                        BleLog.i(TAG, "扫描到 ${dev.name} addr=${dev.address} rssi=${dev.rssi}")
                    }
                    if (hit == null && dev.name.equals(name, ignoreCase = true)) hit = dev
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                BleLog.w(TAG, "重连扫描失败: ${e.message}")
            }
            val found = hit
            if (found != null) {
                BleLog.i(TAG, "重连命中 $name -> ${found.address}（存的是 $addr）")
                reconnectJob = null
                delay(SCAN_SETTLE_MS)
                connect(found.address, found.name, "scan-name")
            } else {
                reconnectJob = null
                if (addr != null) {
                    BleLog.w(TAG, "重连没扫到 $name，退回旧地址 $addr（很可能已失效）")
                    _snackbar.trySend("没扫到 $name，改用旧地址 $addr")
                    delay(SCAN_SETTLE_MS)
                    connect(addr, name, "stale-mac")
                } else {
                    _phase.value = Phase.IDLE
                    _status.value = "未连接"
                    _snackbar.trySend("没扫到 $name，请手动扫描连接")
                }
            }
        }
    }

    fun connect(device: ScannedDevice) = connect(device.address, device.name, "scan-pick")

    /**
     * 发起一次连接。
     *
     * @param addressSource 地址来源，只进连接病历：`stale-mac` 时失败基本可以断定是
     *   「Windows 的 LE 地址轮换导致旧 MAC 失效」，[com.example.blechat.ble.ConnTrace]
     *   会据此给出「请按名字重扫」的结论。
     */
    fun connect(
        address: String,
        name: String?,
        addressSource: String = "manual",
        autoConnect: Boolean = false,
        clearCacheFirst: Boolean = false,
        bondFirst: Boolean = _bondFirst.value,
    ) {
        // 无条件先拆掉上一条链路。真机复现过：失败后旧 GattLink 没关，
        // 再点一次就是「同一条 PC、同一个 peer_id、两条 GATT 会话」互相覆盖，
        // PC 端日志表现为 `only a dead session` / 反复重绑，手机端则时好时坏。
        teardown("切换连接")
        lastAddressSource = addressSource
        everReady = false
        autoRetryUsed = 0
        _diagnostics.value = null
        _hint.value = null
        val pendingScan = scanJob
        stopScan()
        val normalized = Repository.normalizeAddress(address)
        viewModelScope.launch {
            // 扫描占着射频，边扫边连是 Android BLE 掉线的经典原因。
            // 必须等 stopScan 真正落地（awaitClose 在取消之后才跑），再给底层一点空闲。
            if (pendingScan != null) {
                try {
                    pendingScan.join()
                } catch (e: CancellationException) {
                    throw e
                }
                delay(SCAN_SETTLE_MS)
            }
            networkId = repo.rememberNetwork(normalized, P.SERVICE_UUID, name = name)
            hostName = name ?: normalized
            _peerName.value = ""
            BleLog.i(
                TAG,
                "连接请求 name=$hostName addr=$normalized 来源=$addressSource hadScan=${pendingScan != null}",
            )
            _status.value = "正在连接 $hostName…"
            _phase.value = Phase.CONNECTING
            loadHistory(networkId)
            try {
                val newLink = GattLink(getApplication())
                link = newLink
                val transport = BleTransport(
                    newLink,
                    normalized,
                    addressSource = addressSource,
                    hostName = name,
                    autoConnect = autoConnect,
                    clearCacheFirst = clearCacheFirst,
                    bondFirst = bondFirst,
                )
                val s = ChatSession(
                    transport = transport,
                    config = SessionConfig(
                        localDeviceId = localUuid(),
                        localName = repo.prefs.displayName,
                        pskProvider = ::providePsk,
                    ),
                )
                session = s
                eventJob = viewModelScope.launch { collectEvents(s) }
                s.start()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                BleLog.e(TAG, "connect($normalized) 失败: ${e.message}")
                teardown("连接失败：${e.message}")
                _snackbar.trySend("连接失败：${e.message}")
                _phase.value = Phase.IDLE
                _status.value = "未连接"
            }
        }
    }

    /**
     * 拆掉当前会话 + 链路，幂等。
     *
     * 顺序有讲究：先摘事件订阅（避免回调里再碰已释放的对象），
     * 再 `close()` 会话，再关 GATT，最后 `dispose()` 释放会话线程。
     */
    private fun teardown(reason: String) {
        stopScan()

        passwordWaiter?.let { w ->
            passwordWaiter = null
            w.complete(null)
        }
        _passwordRequest.value = null

        val job = eventJob
        eventJob = null
        job?.cancel()

        val s = session
        session = null
        val l = link
        link = null
        networkId = null

        try {
            s?.close(reason)
        } catch (e: Exception) {
            BleLog.w(TAG, "session.close 忽略: ${e.message}")
        }
        try {
            l?.close()
        } catch (e: Exception) {
            BleLog.w(TAG, "link.close 忽略: ${e.message}")
        }
        try {
            s?.dispose()
        } catch (e: Exception) {
            BleLog.w(TAG, "session.dispose 忽略: ${e.message}")
        }
    }

    /** 按 MAC 直连（扫描被定位权限挡住时的兜底，PC 端同款能力）。 */
    fun connectByMac(address: String) {
        val a = Repository.normalizeAddress(address)
        if (!Regex("^([0-9A-F]{2}:){5}[0-9A-F]{2}$").matches(a)) {
            _snackbar.trySend("MAC 格式不对，应形如 AA:BB:CC:DD:EE:FF")
            return
        }
        connect(a, null, "manual")
    }

    fun disconnect(reason: String = "手动断开") {
        teardown(reason)
        _phase.value = Phase.IDLE
        _status.value = "未连接"
        _peerName.value = ""
    }

    // ------------------------------------------------------------------ 口令

    private suspend fun providePsk(
        @Suppress("UNUSED_PARAMETER") nonceC: ByteArray,
        salt: ByteArray,
        iters: Int,
        useEph: Boolean,
    ): ByteArray? {
        val nid = networkId ?: return null
        if (useEph) {
            _snackbar.trySend("Host 要求临时密钥，v1 暂不支持")
            return null
        }

        val saved = repo.credentials.loadPassword(nid)
        if (saved != null && repo.credentials.saltMatches(nid, salt) &&
            repo.credentials.verifyPassword(nid, saved)
        ) {
            _status.value = "正在认证（已记住口令）…"
            return Crypto.derivePsk(saved, salt, iters)
        }
        if (saved != null) {
            // Host 换过密 / 本地参数对不上：丢掉旧口令，重新问一次
            repo.credentials.forget(nid)
        }

        _status.value = "等待输入共享密码…"
        val waiter = CompletableDeferred<String?>()
        passwordWaiter = waiter
        _passwordRequest.value = PasswordRequest(nid, hostName)
        val password = waiter.await()
        passwordWaiter = null
        _passwordRequest.value = null
        if (password.isNullOrBlank()) return null

        val psk = Crypto.derivePsk(password, salt, iters)
        repo.credentials.savePassword(nid, password)
        repo.credentials.saveAuth(nid, salt, iters, Crypto.pskVerifier(psk))
        return psk
    }

    fun submitPassword(password: String) {
        if (password.length < 4) {
            _snackbar.trySend("密码至少 4 位")
            return
        }
        val w = passwordWaiter ?: return
        passwordWaiter = null
        w.complete(password)
    }

    fun cancelPassword() {
        val w = passwordWaiter ?: return
        passwordWaiter = null
        _passwordRequest.value = null
        w.complete(null)
    }

    // ------------------------------------------------------------------ 事件

    private suspend fun collectEvents(s: ChatSession) {
        s.events.collect { ev -> handleEvent(s, ev) }
    }

    private suspend fun handleEvent(source: ChatSession, ev: SessionEvent) {
        when (ev) {
            is SessionEvent.StateChanged -> {
                BleLog.i(TAG, "state -> ${ev.state}")
                onState(ev.state)
            }
            is SessionEvent.Error -> {
                BleLog.w(TAG, "error code=${ev.code} ${ev.message}")
                _snackbar.trySend(ev.message)
                if (ev.code == com.example.blechat.protocol.Err.AUTH_FAILED) {
                    // 口令大概率是错的：下次必须重输（Python 端同款 lockout 思路的简化版）
                    networkId?.let { repo.credentials.forget(it) }
                }
            }
            is SessionEvent.Message -> onIncoming(ev)
            is SessionEvent.HostName -> {
                _peerName.value = ev.name
                if (_status.value.startsWith("已就绪")) _status.value = "已就绪 · ${ev.name}"
            }
            is SessionEvent.Progress -> Unit // v1 不做发送进度条
            is SessionEvent.Peers -> Unit // v1 只显示 hostName，不列 peer 表
            is SessionEvent.Resync -> Unit // 只要求刷新，UI 本身就是实时的
            is SessionEvent.Closed -> {
                BleLog.i(TAG, "closed: ${ev.reason}")
                // 先取病历（teardown 会把 link 置空），再决定要不要自动补救一次
                val diag = link?.diagnosis
                val dump = link?.trace?.dump()
                _phase.value = Phase.CLOSED
                _status.value = "连接结束：${ev.reason}"
                _peerName.value = ""
                // 会话已结束 → 立刻释放 GATT/线程，否则下一次点连接就是两条链路并存。
                // 加 `session === source` 是防止用户已经手动开了新连接再被误拆。
                viewModelScope.launch {
                    if (session === source) teardown(ev.reason)
                    onAttemptFailed(ev.reason, diag, dump)
                }
            }
        }
    }

    private fun onState(s: SessionState) {
        when (s) {
            SessionState.IDLE -> Unit
            SessionState.CONNECTING -> {
                _phase.value = Phase.CONNECTING
                _status.value = "正在连接…"
            }
            SessionState.CONNECTED, SessionState.HELLO_SENT -> {
                _phase.value = Phase.HANDSHAKE
                _status.value = "握手中…"
            }
            SessionState.AUTH_SENT -> {
                _phase.value = Phase.HANDSHAKE
                _status.value = "已发 AUTH，等 Host 确认…"
            }
            SessionState.AUTH_FAIL -> {
                _phase.value = Phase.HANDSHAKE
                _status.value = "认证失败"
            }
            SessionState.READY -> {
                _phase.value = Phase.READY
                _status.value = "已就绪"
                everReady = true
                autoRetryUsed = 0
                consecutiveFailures = 0
                _diagnostics.value = null
                _hint.value = null
            }
            SessionState.CLOSED -> {
                if (_phase.value != Phase.CLOSED) {
                    _phase.value = Phase.CLOSED
                    _status.value = "连接已关闭"
                }
            }
        }
    }

    /**
     * 一次尝试失败之后的收口：把病历交给 UI，并**自动补救一次**。
     *
     * 为什么要自动补一次：真机上最常见的两种失败都有便宜的补救办法 ——
     * ① 旧 MAC 失效（Windows 的 LE 地址轮换）⇒ 按名字重扫；
     * ② GATT 服务表缓存过期 / ATT 层被系统丢弃 ⇒ 换一条全新的链路再来一次
     *    （新 `BluetoothGatt` 会重新走一遍 clientIf 注册，并触发清缓存重发）。
     * 只补一次，之后就老实地把处置建议摆给用户，避免把用户拖进无限重连。
     */
    private fun onAttemptFailed(reason: String, diag: Diagnosis?, dump: String?) {
        consecutiveFailures++
        if (!dump.isNullOrBlank()) {
            _diagnostics.value = dump + "连续失败: $consecutiveFailures 次\n"
        }
        if (diag == null) return
        if (everReady) return // 连上过又断了，属于"掉线"，交给用户决定
        BleLog.w(
            TAG,
            "本次失败结论[${diag.kind}] ${diag.title}（该动：${ConnTrace.sideText(diag.side)}，" +
                "连续失败 $consecutiveFailures 次）",
        )
        _hint.value = diag.hint + slowDownAdvice()

        val canRetry = diag.kind == DiagKind.NO_CONNECT ||
            diag.kind == DiagKind.ATT_SILENT ||
            diag.kind == DiagKind.DISCOVERY_DROPPED ||
            diag.kind == DiagKind.SERVICE_MISSING ||
            diag.kind == DiagKind.MTU_FAILED ||
            diag.kind == DiagKind.LL_DEAD
        if (canRetry && autoRetryUsed == 0) {
            autoRetryUsed++
            val byName = diag.kind == DiagKind.NO_CONNECT &&
                lastAddressSource == "stale-mac" &&
                !repo.prefs.lastHostName.isNullOrBlank()
            // 换形状的原则：**一次只换一个自变量**，否则又回到"改了三样东西，不知道哪个起作用"。
            // ① 地址可能失效 → 按名字重新解析地址（换地址）；
            // ② 还没试过配对 → 打开配对（换配对状态）。这是 ATT 静默/链路静默下最值钱的一次
            //    尝试：它是手机侧唯一能改变的、与"对端认不认得出我们"直接相关的自变量；
            // ③ 其它 → 换建链方式（autoConnect=true）+ 清 GATT 缓存表。
            val tryBond = !_bondFirst.value &&
                (diag.kind == DiagKind.ATT_SILENT || diag.kind == DiagKind.LL_DEAD)
            val delayMs = autoRetryDelay(diag.kind)
            _status.value = when {
                byName -> "地址可能已失效，正在按名字重找 Host…"
                tryBond -> "改用「先配对再连」重试一次（等 ${delayMs / 1000} 秒）…"
                else -> "换一种方式重试一次（等 ${delayMs / 1000} 秒让对端把上一条连接放掉）…"
            }
            _snackbar.trySend(_status.value)
            viewModelScope.launch {
                delay(delayMs)
                if (byName) {
                    reconnectLast()
                } else {
                    // 地址还是上次那个：地址要是不对，连都连不上，根本走不到这一步。
                    val addr = repo.prefs.lastHostAddress
                    if (addr != null) {
                        connect(
                            addr,
                            repo.prefs.lastHostName,
                            "stale-mac",
                            autoConnect = !tryBond,
                            clearCacheFirst = !tryBond,
                            bondFirst = tryBond,
                        )
                    }
                }
            }
            return
        }

        // 补不回来了：把结论与处置摆出来，并留着诊断入口
        _status.value = "连接失败：${diag.title}"
        _snackbar.trySend(diag.hint.lineSequence().first())
        BleLog.block(TAG, dump ?: diag.title)
    }

    /**
     * 自动补救前的等待时间。
     *
     * 依据（真机日志 2026-10-04 22:03）：
     * - 我们 `close()` 之后协议栈要 `start link idle timer = 1 sec` 才把链路放干净，
     *   所以**立刻**重连容易撞上没放干净的旧链路 ⇒ 至少给 3 秒；
     * - "对端 ATT 没回应 / 服务表没人服务"这两种要等对端把上一条连接放掉
     *   （PC 侧旁证：`peer … disconnected` 每 ~31s 一次 ≈ ATT 事务超时 30s）⇒ 给 8 秒。
     */
    private fun autoRetryDelay(kind: DiagKind): Long = when (kind) {
        DiagKind.ATT_SILENT, DiagKind.DISCOVERY_DROPPED, DiagKind.SERVICE_MISSING -> 8_000L
        else -> 3_000L
    }

    /** UI 上的「诊断」按钮取这里的内容（可能为空）。 */
    fun diagnosticsText(): String? = _diagnostics.value

    // ------------------------------------------------------------------ 配对

    /** 连接面板上的「连接前先配对」开关。 */
    fun setBondFirst(on: Boolean) {
        if (_bondFirst.value == on) return
        _bondFirst.value = on
        repo.prefs.bondFirst = on
        BleLog.i(TAG, "连接前先配对 -> $on")
    }

    /**
     * 取消与上次那台 Host 的 BLE 配对（诊断弹窗里的「取消配对」按钮）。
     *
     * 为什么需要它：配对是**双向记录**。PC 侧如果把手机忘了、手机上还留着，
     * 之后的连接会尝试加密、然后因为对端没有密钥而失败 —— 这种「半配对」自己恢复不了。
     * `removeBond()` 是隐藏 API，允许失败，失败就引导用户去系统设置里「忽略」。
     */
    fun forgetBond() {
        val addr = repo.prefs.lastHostAddress
        if (addr.isNullOrBlank()) {
            _snackbar.trySend("还没有连接过 Host")
            return
        }
        val device = runCatching {
            getApplication<Application>()
                .getSystemService(android.bluetooth.BluetoothManager::class.java)
                ?.adapter?.getRemoteDevice(addr)
        }.getOrNull()
        if (device == null) {
            _snackbar.trySend("拿不到 $addr 的设备对象（请先扫描一次）")
            return
        }
        val ok = runCatching { Bonder(getApplication()).removeBond(device) }.getOrDefault(false)
        BleLog.i(TAG, "取消配对 $addr -> $ok")
        _snackbar.trySend(
            if (ok) {
                "已取消与 $addr 的配对，重新连接会再做一次"
            } else {
                "系统不允许 App 取消配对，请在手机蓝牙设置里「忽略」$addr"
            },
        )
    }

    /**
     * 连续失败到第 2 次时补一句"别急着连"。
     *
     * 依据：PC 侧一个异常断开的 GATT 服务可能还占着上一次的连接，而 Windows 自己
     * 要 ~30~60s 才释放（PC 日志里 `peer … disconnected` 恰好每 ~31s 一次）。
     * 这期间**连续重连只会不断刷新那个占用**，所以要把话说清楚。
     */
    private fun slowDownAdvice(): String = if (consecutiveFailures >= 2) {
        "\n\n已连续失败 $consecutiveFailures 次：PC 侧上一次连接可能还占着 GATT 服务" +
            "（Windows 自己要 30~60 秒才释放），**建议等半分钟再试**，" +
            "并在 PC 上把蓝牙关掉再打开一次。"
    } else {
        ""
    }

    private suspend fun onIncoming(ev: SessionEvent.Message) {
        val text = when (ev.kind) {
            "text" -> String(ev.payload, Charsets.UTF_8)
            "image" -> "[图片] ${ev.payload.size} 字节"
            else -> "[文件] ${ev.payload.size} 字节"
        }
        val msgId = ev.msgId
        val now = System.currentTimeMillis()
        _messages.value = _messages.value + UiMessage(
            key = -(msgId.toLong()),
            msgId = msgId,
            outgoing = false,
            kind = ev.kind,
            text = text,
            peerName = _peerName.value.ifBlank { hostName },
            createdAt = now,
        )
        val nid = networkId ?: return
        repo.addIncoming(
            networkId = nid,
            msgId = msgId,
            peerId = null,
            peerName = _peerName.value.ifBlank { hostName },
            kind = ev.kind,
            content = ev.payload,
            createdAt = now,
        )
    }

    // ------------------------------------------------------------------ 发送

    fun send() {
        val text = _composer.value.trim()
        if (text.isEmpty()) return
        val s = session
        if (s == null || _phase.value != Phase.READY) {
            _snackbar.trySend("还没连上，先选一个 Host")
            return
        }
        viewModelScope.launch {
            val msgId = try {
                s.sendText(text)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _snackbar.trySend(e.message ?: "发送失败")
                return@launch
            }
            val now = System.currentTimeMillis()
            _messages.value = _messages.value + UiMessage(
                key = msgId.toLong(),
                msgId = msgId,
                outgoing = true,
                kind = "text",
                text = text,
                peerName = repo.prefs.displayName,
                createdAt = now,
            )
            _composer.value = ""
            val nid = networkId ?: return@launch
            repo.addOutgoing(
                networkId = nid,
                msgId = msgId,
                kind = "text",
                content = text.toByteArray(Charsets.UTF_8),
                createdAt = now,
            )
        }
    }

    private suspend fun loadHistory(nid: String?) {
        val rows = repo.recent(nid)
        _messages.value = rows.map { row ->
            UiMessage(
                key = row.id,
                msgId = row.msgId,
                outgoing = row.direction == "out",
                kind = row.kind,
                text = row.textOrPlaceholder(),
                peerName = row.peerName,
                createdAt = row.createdAt,
            )
        }.sortedBy { it.createdAt }
    }

    private fun localUuid(): UUID = UUID.fromString(repo.prefs.deviceId)

    override fun onCleared() {
        disconnect("应用退出")
        super.onCleared()
    }

    private companion object {
        const val TAG = "MainVM"

        /** 扫描停止到开始连接之间的静默期（MIUI 上 stopScan 生效有延迟）。 */
        const val SCAN_SETTLE_MS = 400L

        /** 「重连上次」按名字重新解析地址时的扫描时长。 */
        const val RECONNECT_SCAN_MS = 4_000L
    }
}
