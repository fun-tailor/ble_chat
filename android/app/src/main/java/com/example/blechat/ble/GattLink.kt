package com.example.blechat.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import com.example.blechat.protocol.Err
import com.example.blechat.protocol.P
import com.example.blechat.protocol.ProtoException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import java.util.UUID

/**
 * 一条 BLE GATT 链路：连接 → [配对] → **ATT 探针** → LL 探针 → 发现服务 → 协商 MTU
 * → 订阅 → 收发。
 *
 * 与 Python 端 `blechat/ble/client.py` 的分工一致：
 * - **写**：控制帧 → `CHAR_CTRL (a003)`（写-需响应）；数据帧 → `CHAR_RX (a001)`（先写-不需响应，失败退回需响应）
 * - **收**：`CHAR_TX (a002)` → [LinkEvent.Data]；`CHAR_CTRL (a003)` → [LinkEvent.Ctrl]
 *
 * ## 顺序为什么是「探针在前、而且串行」（真机日志定的规矩）
 *
 * ATT 只有**一条命令队列**。2026-10-04 22:03 的日志把这条规矩钉死了：
 *
 * ```
 * 22:03:49.185  GATTC_Discover conn_id=0x0009, disc_type=1       ← 请求确实发出去了
 * 22:03:52.610  bta_gattc_enqueue: already has a pending command  ← 第 2 次重发被丢掉
 * 22:03:54.186  gatt_rsp_timeout lcid:4                           ← 手机 ATT 层响应超时(5s)
 * 22:03:56.018  bta_gattc_enqueue: already has a pending command  ← 第 3 次也被丢掉
 * ```
 *
 * 22:21 的日志又补了第二条：**连 `readRemoteRssi` 都可能占那个槽**
 * （`+358ms onReadRemoteRssi` / `+359ms requestMtu` 只隔 1ms），而 `requestMtu()`
 * 返回 true 只代表 Java 层受理、真正入队是后面的 `bta_gattc_enqueue`。
 *
 * 结论有三条，直接决定了本类的写法：
 *
 * 1. **有命令挂着时，重发等于没发**。所以失败之后"换个形态再 discoverServices 三次"
 *    是纯噪音 —— 要重试就得**换一条新链路**（新 clientIf），那是
 *    `MainViewModel` 的自动补救在做的事。
 * 2. **探针必须排在最前面**，否则它自己也被队列丢掉，"探针没答复"就分不清是
 *    「对端主机栈不处理 ATT」还是「我们自己的队列堵着」。
 * 3. **探针之间也必须串行**：ATT 探针先发，LL 探针后发，中间不插任何 GATT 调用。
 *
 * ## 真机踩过的坑（MIUI / Android 14+）
 *
 * 1. **回调可能永远不来**：`discoverServices()` 发出去了、`onSearchComplete` 一个都没有
 *    （而 `onConnectionUpdated`/`onPhyUpdate` 证明 LL 层是通的）。每个阶段都必须
 *    「带超时」，绝不能裸 `await()`。
 * 2. **两个独立探针负责分诊**（详见 [ConnTrace] 顶部的表），**必须串行下发**：
 *    - `readRemoteRssi()` —— 纯控制器/HCI 操作，**不需要对端主机栈参与** ⇒ 证明 LL 活着；
 *      但它在协议栈里**照样要占那个唯一的 GATT 命令槽**，所以排在 ATT 探针之后。
 *    - `requestMtu()` —— 真 ATT 请求，**需要对端主机栈处理** ⇒ 证明 ATT 活着（排在队首）。
 * 3. **本地 GATT 缓存表可能是旧的**：PC 上的 Host 重启过之后，手机按**设备**缓存的表
 *    还指向上一次的服务（换个 `BluetoothGatt` 对象也带不走它）。自动重试路径会先
 *    `refresh()`（非公开 API，反射 + try/catch）。
 * 4. **写超时之后 GATT 队列状态未知**：绝不能继续发下一帧（迟到的回调会把新 gate
 *    提前 complete）。一旦写超时就判这条链路「中毒」，直接 [close]。
 * 5. **`gatt.close()` 必须在断开回调之后**：`disconnect()` 后立刻 `close()` 会泄漏一个
 *    clientIf；真机日志里这条路径末尾是 `start link idle timer = 1 sec` —— 关掉之后
 *    协议栈还要约 1 秒才真正放干净，所以重连之间要留间隔。
 */
@SuppressLint("MissingPermission")
class GattLink(private val context: Context) {

    private val _events = MutableSharedFlow<LinkEvent>(
        replay = 0,
        extraBufferCapacity = 256,
    )
    val events: SharedFlow<LinkEvent> = _events.asSharedFlow()

    @Volatile private var gatt: BluetoothGatt? = null
    @Volatile private var rxChar: BluetoothGattCharacteristic? = null
    @Volatile private var txChar: BluetoothGattCharacteristic? = null
    @Volatile private var ctrlChar: BluetoothGattCharacteristic? = null

    /** 协商到的 ATT MTU（默认 23）。 */
    @Volatile var mtu: Int = 23
        private set

    @Volatile var address: String = ""
        private set

    @Volatile private var writeNoResp: Boolean = true

    private val writeMutex = Mutex()

    /** 当前挂起的写操作（特征或描述符），由 binder 线程的回调 complete。 */
    @Volatile private var pendingWrite: CompletableDeferred<Int>? = null

    @Volatile private var connectedGate: CompletableDeferred<Unit>? = null
    @Volatile private var servicesGate: CompletableDeferred<Int>? = null
    @Volatile private var mtuGate: CompletableDeferred<Int>? = null
    @Volatile private var rssiGate: CompletableDeferred<Int>? = null

    /** 断开/失败的真实原因。用于把「服务表不完整」和「链路中途掉线」区分开。 */
    @Volatile private var failReason: String? = null

    /** 有一次写超时 ⇒ 队列状态未知 ⇒ 这条链路作废。 */
    @Volatile private var poisoned = false

    @Volatile private var closed = false
    private var disconnectNotified = false

    var isConnected: Boolean = false
        private set

    /** 本次连接尝试的病历（供 UI 诊断弹窗与日志用）。 */
    @Volatile var trace: ConnTrace? = null
        private set

    /** 病历的分类结论。失败时由 [connect] 填好。 */
    @Volatile var diagnosis: Diagnosis? = null
        private set

    /** 第几条链路（跨自动重试累计，写进病历编号）。 */
    @Volatile private var attempts = 0

    /** 连上后、发现服务前是否先清一次 GATT 缓存（自动重试时打开）。 */
    @Volatile private var clearCacheFirst = false

    /** 连上后、ATT 探针前是否先做一次 BLE 配对（见 [Bonder] 的类注释）。 */
    @Volatile private var bondFirst = false

    private val bonder = Bonder(context)

    private val mainHandler = Handler(Looper.getMainLooper())

    private val callback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            val t = trace
            t?.lastNewState = newState
            t?.connectStatus = status
            if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothStatusCodes.SUCCESS) {
                isConnected = true
                t?.connectedAt = System.currentTimeMillis()
                BleLog.i(TAG, "已连接 status=$status（LL 建立，接下来按 ATT 探针 → 服务发现 的顺序走）")
                // ⚠️ 连接回调里**只**完成 gate，什么都不发。
                //
                // 这里是整个类最关键的一处克制。两条真机日志把它钉死了：
                //
                // ① 2026-10-04 22:03：ATT 只有**一条命令队列**
                //     22:03:49.185  GATTC_Discover …                       ← 请求发出去了
                //     22:03:52.610  bta_gattc_enqueue: already has a pending command
                //     22:03:56.018  bta_gattc_enqueue: already has a pending command
                //    只要有一条 GATT 命令挂着没回来，后面发的全部被丢掉。
                //
                // ② 2026-10-04 22:21（本版）：连 `readRemoteRssi` 都可能占着那个槽
                //     +353ms LL 探针 readRemoteRssi issued=true
                //     +358ms cb onReadRemoteRssi rssi=-73 status=0
                //     +359ms ATT 探针 requestMtu(517) started=true   ← 只隔 1ms
                //    `requestMtu()` 返回 true 只代表 Java 层受理了；真正入队是在
                //    `bta_gattc_enqueue`，而上一条的队列槽要等 BTA 层回调完才释放。
                //    这 1ms 的缝完全可能让**我们自己的探针**被丢掉，于是"对端没回应"
                //    这个结论就不成立了。
                //
                // 所以：探针必须**串行**下发 —— ATT 探针在前，LL 探针在它之后
                // （见 connect()），全程任何时刻只有一个 GATT 操作在飞。
                connectedGate?.complete(Unit)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED || status != 0) {
                val why = when {
                    status == 0 && closed -> "链路已关闭（本地）"
                    status == 0 -> "远端设备已断开"
                    else -> "status=$status ${BleStatus.message(status)}"
                }
                t?.disconnectedAt = System.currentTimeMillis()
                if (status != 0) t?.disconnectStatus = status
                t?.warn("断开回调 status=$status newState=$newState -> $why")
                BleLog.i(TAG, "断开 status=$status newState=$newState reason=$why")
                isConnected = false
                failReason = why
                failAllPending(why)
                notifyDisconnected(why)
                // 断开之后必须 close，否则 clientIf 一直被占着（见类注释坑 6）
                runCatching { g.close() }
                if (gatt === g) gatt = null
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val t = trace
            t?.servicesSeen = true
            t?.servicesStatus = status
            t?.cb("onServicesDiscovered status=$status")
            if (status != BluetoothStatusCodes.SUCCESS) {
                BleLog.w(TAG, "onServicesDiscovered status=$status ${BleStatus.message(status)}")
                servicesGate?.complete(status)
                return
            }
            val svc = g.getService(UUID.fromString(P.SERVICE_UUID))
            if (svc == null) {
                // 打印**实际拿到的表**：这是区分"空表"与"只有系统服务"的唯一证据
                val table = g.services.joinToString(" ") { it.uuid.toString().substring(4, 8) }
                t?.serviceMissing = true
                BleLog.w(TAG, "服务表里没有 ${P.SERVICE_UUID}；实际表=[$table]")
                servicesGate?.complete(SERVICE_MISSING)
                return
            }
            rxChar = svc.getCharacteristic(UUID.fromString(P.CHAR_RX))
            txChar = svc.getCharacteristic(UUID.fromString(P.CHAR_TX))
            ctrlChar = svc.getCharacteristic(UUID.fromString(P.CHAR_CTRL))
            if (rxChar == null || txChar == null || ctrlChar == null) {
                t?.serviceMissing = true
                BleLog.w(TAG, "服务表缺特征 rx=$rxChar tx=$txChar ctrl=$ctrlChar")
                servicesGate?.complete(SERVICE_MISSING)
                return
            }
            BleLog.d(TAG, "服务表就绪 ${P.SERVICE_UUID}")
            servicesGate?.complete(BluetoothStatusCodes.SUCCESS)
        }

        override fun onMtuChanged(g: BluetoothGatt, newMtu: Int, status: Int) {
            val t = trace
            if (t?.attProbeSent == true && !t.attProbeAnswered) {
                t.attProbeAnswered = true
                t.attProbeStatus = status
                t.cb("onMtuChanged(ATT 探针答复) mtu=$newMtu status=$status")
            } else {
                t?.cb("onMtuChanged mtu=$newMtu status=$status")
            }
            BleLog.d(TAG, "onMtuChanged mtu=$newMtu status=$status")
            if (status == BluetoothStatusCodes.SUCCESS) {
                mtu = newMtu
                t?.mtu = newMtu
            }
            mtuGate?.complete(status)
        }

        /** 纯 LL/HCI 操作，对端主机栈不参与 —— 用来证明链路层还活着。 */
        override fun onReadRemoteRssi(g: BluetoothGatt, rssi: Int, status: Int) {
            val t = trace
            t?.llProbeAnswered = true
            t?.llProbeStatus = status
            if (status == BluetoothStatusCodes.SUCCESS) t?.rssi = rssi
            t?.cb("onReadRemoteRssi rssi=$rssi status=$status")
            BleLog.d(TAG, "onReadRemoteRssi rssi=$rssi status=$status")
            rssiGate?.complete(status)
        }

        /** 对端主动说「服务表变了」—— 本地缓存表已失效，必须重新发现。 */
        override fun onServiceChanged(g: BluetoothGatt) {
            trace?.serviceChangedSeen = true
            trace?.warn("onServiceChanged：对端服务表变了，本地缓存表已失效")
            BleLog.w(TAG, "onServiceChanged：对端服务表变了")
        }

        override fun onPhyUpdate(g: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) {
            trace?.cb("onPhyUpdate tx=$txPhy rx=$rxPhy status=$status")
            BleLog.d(TAG, "onPhyUpdate tx=$txPhy rx=$rxPhy status=$status")
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            trace?.cb("onDescriptorWrite ${d.uuid.toString().substring(4, 8)} status=$status")
            completePending(status)
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            completePending(status)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            route(characteristic, value)
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            @Suppress("DEPRECATION")
            route(characteristic, characteristic.value ?: ByteArray(0))
        }
    }

    private fun completePending(status: Int) {
        val gate = pendingWrite ?: return
        pendingWrite = null
        gate.complete(status)
    }

    private fun route(ch: BluetoothGattCharacteristic, value: ByteArray) {
        val bytes = value.copyOf()
        when (ch.uuid.toString().lowercase()) {
            P.CHAR_TX -> _events.tryEmit(LinkEvent.Data(bytes))
            P.CHAR_CTRL -> _events.tryEmit(LinkEvent.Ctrl(bytes))
        }
    }

    // ---------------------------------------------------------------- 连接

    /**
     * 连上并完成全部初始化，返回最终 MTU。
     *
     * 顺序（每一步都是**上一步的回调到了才发下一步**，全程只有一个 GATT 操作在飞）：
     *
     * ```
     * 连接 → [BLE 配对] → [清缓存] → ATT 探针 → LL 探针 → 等 ATT 探针结论
     *      → 连接参数 → 服务发现（单次） → MTU → 订阅
     * ```
     *
     * @param requestMtu 期望 MTU；≤23 时跳过协商。
     * @param addressSource 地址来源（`scan-name` / `scan-pick` / `stale-mac` / `manual`），
     *   只进病历，用来判断「地址失效」类失败。
     * @param autoConnect true = 用 `autoConnect=true` 建链（控制器后台建链）。
     *   真机上遇到过「直接建链成功了但 ATT 层完全没反应」的形态，换这种建链方式
     *   是**另一种形状的尝试**，比原样重试一次有意义（见 `MainViewModel.onAttemptFailed`）。
     * @param clearCacheFirst true = 连上之后、发现服务之前先 `refresh()` 清掉本进程的
     *   GATT 缓存表。自动重试用它：Host 重启过之后缓存表就是旧的，
     *   而缓存是**按设备**存在协议栈里的，换一个 `BluetoothGatt` 对象也带不走它。
     * @param bondFirst true = 连上之后、**任何 ATT 操作之前**先做一次 BLE 配对（SMP）。
     *   配对成功意味着双方交换了 IRK，从此能认出彼此轮换的随机地址（RPA）——
     *   这是"连得上但 ATT 零回复"这一路现象里，**手机侧唯一能自己动的杠杆**。
     *   详情见 [Bonder]。
     */
    suspend fun connect(
        address: String,
        requestMtu: Int = 517,
        addressSource: String = "manual",
        hostName: String? = null,
        autoConnect: Boolean = false,
        clearCacheFirst: Boolean = false,
        bondFirst: Boolean = false,
    ): Int {
        // 坑：同一条链路对象不允许重复 connect；上一条必须先 close
        check(gatt == null) { "GattLink 被复用了，必须先 close() 再 connect()" }

        this.address = address
        closed = false
        disconnectNotified = false
        poisoned = false
        failReason = null
        mtu = 23
        writeNoResp = true
        this.clearCacheFirst = clearCacheFirst
        this.bondFirst = bondFirst
        connectedGate = CompletableDeferred()
        // 先建 gate 再 connectGatt —— 连接回调只负责 complete 它
        servicesGate = CompletableDeferred()
        mtuGate = null
        rssiGate = null

        attempts += 1
        val t = ConnTrace(attempts, address, hostName, addressSource)
        t.envInfo = "${Build.MANUFACTURER} ${Build.MODEL} / Android ${Build.VERSION.RELEASE} " +
            "(API ${Build.VERSION.SDK_INT})"
        trace = t
        diagnosis = null

        val device: BluetoothDevice = context.getSystemService(android.bluetooth.BluetoothManager::class.java)
            ?.adapter?.getRemoteDevice(address)
            ?: throw ProtoException(Err.NOT_READY, "蓝牙不可用")

        // 设备身份全部记账：配对状态/地址类型是"连不上"的两大常见嫌疑
        t.deviceName = runCatching { device.name }.getOrNull()
        t.bondState = runCatching { device.bondState }.getOrNull()
        t.deviceType = runCatching { device.type }.getOrNull()
        t.addressType = readAddressType(device)
        BleLog.i(
            TAG,
            "connectGatt $address requestMtu=$requestMtu 来源=$addressSource autoConnect=$autoConnect " +
                "bondFirst=$bondFirst name=${t.deviceName} " +
                "bond=${t.bondState?.let { ConnTrace.bondText(it) }} " +
                "type=${t.deviceType?.let { ConnTrace.typeText(it) }} " +
                "addrType=${t.addressType?.let { ConnTrace.addressTypeText(it) } ?: "未知"}",
        )
        t.step("connectGatt（来源=$addressSource, autoConnect=$autoConnect, bondFirst=$bondFirst）")

        val g = device.connectGatt(context, autoConnect, callback, BluetoothDevice.TRANSPORT_LE)
        t.connectGattAt = System.currentTimeMillis()
        t.connectGattOk = g != null
        gatt = g
        if (g == null) {
            t.fail("connectGatt 返回 null（设备对象拿不到）")
            return finishFailure(t, ProtoException(Err.NOT_READY, "connectGatt 失败：设备对象为空"))
        }

        // 总预算按"这次要做哪些事"算出来：配对是额外的一段，不能从别的步骤里偷时间
        // （否则最坏情况下外层超时会先到，"到底卡在哪一步"反而看不出来了）。
        val budget = CONNECT_TIMEOUT_MS + if (bondFirst) BOND_TIMEOUT_MS else 0L
        try {
            withTimeout(budget) {
                connectedGate?.await()
                // ① BLE 配对（可选）。放在**任何 ATT 操作之前**：SMP 走自己的 L2CAP 通道，
                //    不占 ATT 队列；而且它的结论会改写"对端为什么不在 ATT 上应答"。
                if (bondFirst) bondStep(device, t)
                // ② 清 GATT 缓存（可选，自动重试路径打开）。本地操作，不占 ATT 队列。
                if (clearCacheFirst && t.refreshTried.not()) {
                    val ok = refreshGattCache(g)
                    t.refreshTried = true
                    t.refreshOk = ok
                    t.step("探针前清缓存 refresh() -> $ok")
                }
                // ③ ATT 探针**排在最前面**，而且立刻发：队列里除了它什么都没有。
                startAttProbe(g, requestMtu)
                // ④ LL 探针排在 ATT 探针**之后**：`readRemoteRssi` 在协议栈里同样要占那个
                //    唯一的 GATT 命令槽（22:21 日志里两者只隔 1ms）。串行下发，结论才干净。
                probeLlAsync(g)
                // ⑤ 等 ATT 探针的结论（没答复就在这里按 ATT_SILENT 收口）
                awaitAttProbe(t)
                // ⑥ 连接参数：放在探针之后 —— 默认可能是 7.5ms(HIGH)，在 Windows 外设侧
                //    更容易掉线，换成 BALANCED。失败只记日志，不中断。
                val prio = runCatching { g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_BALANCED) }
                    .getOrDefault(false)
                t.step("requestConnectionPriority(BALANCED) -> $prio")
                // ⑦ 服务发现：**只发一次**。超时就收工（重发会被队列丢掉，纯噪音）。
                discoverOnce(g)
                // ⑧ MTU：探针已经协商好时直接返回（见 awaitMtu 开头的短路）。
                awaitMtu(g, requestMtu)
            }
        } catch (t2: kotlinx.coroutines.TimeoutCancellationException) {
            val why = failReason ?: "连接超时（${budget / 1000}s 内未完成 GATT 初始化）"
            t.fail(why)
            closeQuietly()
            return finishFailure(t, ProtoException(Err.NOT_READY, why))
        } catch (t2: Throwable) {
            t.fail("中断：${t2.message}")
            closeQuietly()
            return finishFailure(t, t2)
        }

        // 订阅放在总超时之外：它自己有界（写超时 + 重试），且报错信息要保持
        // 「写 CCCD 超时」这种可诊断的原文，不要被外层超时改写。
        try {
            subscribe(txChar!!, indication = true)
            subscribe(ctrlChar!!, indication = false)
        } catch (t2: Throwable) {
            t.fail("订阅失败：${t2.message}")
            closeQuietly()
            return finishFailure(t, t2)
        }

        t.cccdWritten = true
        t.readyAt = System.currentTimeMillis()
        t.step("链路就绪 mtu=$mtu（耗时 ${t.readyAt!! - t.startedAt}ms）")
        BleLog.i(TAG, "链路就绪 mtu=$mtu addr=$address")
        BleLog.block(TAG, t.dump())
        diagnosis = t.classify()
        _events.tryEmit(LinkEvent.Ready(mtu, address))
        return mtu
    }

    /**
     * BLE 配对这一步（可选）。
     *
     * **绝不让它把连接尝试搞死**：配对失败/超时只记账，后面照常做 ATT 探针 —— 因为
     * 我们要的正是"配对失败时的 ATT 表现"这个对照。
     */
    private suspend fun bondStep(device: BluetoothDevice, t: ConnTrace) {
        t.bondRequested = true
        t.bondBefore = bonder.bondStateOf(device)
        val outcome = bonder.bond(device, BOND_TIMEOUT_MS) { line ->
            t.step(line)
            BleLog.i(TAG, line)
        }
        t.bondOutcome = outcome
        t.bondAfter = bonder.bondStateOf(device)
    }

    /**
     * 读对端的地址类型。
     *
     * `BluetoothDevice.getAddressType()` 是 **API 35 才公开**的（lint 会拦），更早的版本
     * 只能反射那个隐藏方法。它值钱的地方在于：`RANDOM(1)` / `RANDOM_IDENTITY(3)` 就是
     * 「同一个 PC 名字每次都换 MAC」这件事的直接证据 —— 而地址轮换是本项目所有
     * `stale-mac` 类失败的总根源。拿不到就不记。
     */
    private fun readAddressType(device: BluetoothDevice): Int? {
        if (Build.VERSION.SDK_INT >= 35) {
            return runCatching { device.addressType }.getOrNull()
        }
        return try {
            val m = device.javaClass.getMethod("getAddressType")
            m.invoke(device) as? Int
        } catch (e: Throwable) {
            BleLog.d(TAG, "getAddressType() 反射不可用: ${e.javaClass.simpleName}")
            null
        }
    }

    /**
     * 失败收口：把病历打完、分类、写日志，然后抛出**带结论**的异常。
     *
     * 异常文本用「标题 → 处置第一行」：状态栏/提示条放得下，完整病历在
     * [diagnosis] / [trace] 里，UI 的「连接诊断」弹窗会展示全文。
     */
    private fun finishFailure(t: ConnTrace, cause: Throwable): Nothing {
        t.errorMessage = cause.message
        val d = t.classify()
        diagnosis = d
        BleLog.w(TAG, "连接失败[${d.kind}] ${d.title}")
        BleLog.block(TAG, t.dump())
        val short = d.hint.lineSequence().firstOrNull()?.takeIf { it.isNotBlank() } ?: ""
        throw ProtoException(Err.NOT_READY, if (short.isEmpty()) d.title else "${d.title} → $short")
    }

    /**
     * LL 探针：`readRemoteRssi()` 走控制器/HCI，**不需要对端主机栈**。
     *
     * ⚠️ 但它**照样要占那个唯一的 GATT 命令槽**（`bta_gattc_enqueue`），所以必须在
     * ATT 探针**之后**下发：真机日志里两个调用只隔 1ms，而 `requestMtu()` 的入队发生在
     * BTA 层 —— 谁先谁后决定了"ATT 没答复"这个结论到底成不成立。
     *
     * 结果只记账（[ConnTrace.llProbeAnswered]），失败不当错误 —— 有些 ROM 会直接
     * 拒绝这个调用，那时它不能作为"链路已死"的证据（[ConnTrace.llProbeSent] 会保持 false）。
     */
    private fun probeLlAsync(g: BluetoothGatt) {
        val t = trace ?: return
        val issued = runCatching { g.readRemoteRssi() }.getOrDefault(false)
        t.llProbeSent = issued
        t.step("LL 探针 readRemoteRssi issued=$issued")
        if (issued) {
            // 答复只记账；ATT 探针失败时用它区分"链路也断了"和"只有 ATT 不通"
            rssiGate = CompletableDeferred()
        }
    }

    /**
     * ATT 探针（**必须在服务发现之前，而且必须紧接着连接回调下发**）：
     * `requestMtu()` 是一条真 ATT 请求，必须由对端主机栈处理并回答。
     *
     * 为什么必须排在最前面（真机日志 2026-10-04 22:03 的依据）：
     *
     * ```
     * 22:03:49.185  GATTC_Discover conn_id=0x0009, disc_type=1     ← 请求发出去了
     * 22:03:52.610  bta_gattc_enqueue: already has a pending command ← 重发被丢掉
     * 22:03:54.186  gatt_rsp_timeout lcid:4                         ← 手机 ATT 层响应超时
     * ```
     *
     * ATT 只有**一条命令队列**：只要有一条命令挂着没回来，后面补发的全部被 enqueue
     * 丢掉。所以探针如果在服务发现之后发，它自己也会被丢掉，"探针没答复"就再也
     * 分不清是"对端不回"还是"我们自己的队列堵着"。
     *
     * 2026-10-04 22:21 的日志又补了一条：连 `readRemoteRssi` 都可能占着那个槽
     * （两个调用只隔 1ms），所以本类把它拆成"先发、后等"两步，中间**不插任何**
     * 别的 GATT 调用（LL 探针排在它后面，见 [connect]）。
     */
    private fun startAttProbe(g: BluetoothGatt, want: Int) {
        val t = trace
        t?.attProbeSent = true
        val gate = CompletableDeferred<Int>()
        mtuGate = gate
        val started = runCatching { g.requestMtu(want) }.getOrDefault(false)
        t?.step("ATT 探针 requestMtu($want) started=$started")
        BleLog.d(TAG, "ATT 探针 requestMtu($want) started=$started")
        if (!started) {
            // 发不出去（链路已断等）：不当作"对端静默"的证据，交给后面的步骤报错
            t?.attProbeSent = false
            t?.warn("ATT 探针未能发起（不作为对端静默的证据）")
        }
    }

    /**
     * 等 ATT 探针的答复（在 [startAttProbe] 之后）。
     *
     * 等待窗口 [PROBE_TIMEOUT_MS] 取 5.5s：手机协议栈自己的 ATT 响应超时是 5s
     * （日志里的 `gatt_rsp_timeout`），等满一个窗口再下结论。
     * 没有答复 ⇒ 抛出（结论由 [ConnTrace.classify] 给出：`ATT_SILENT` / `LL_DEAD`）。
     */
    private suspend fun awaitAttProbe(t: ConnTrace) {
        if (t.attProbeSent.not()) return
        val gate = mtuGate ?: return
        val st = withTimeoutOrNull(PROBE_TIMEOUT_MS) { gate.await() }
        // `st < 0` 是我们自己的哨兵值（[failAllPending] 用 -1 打断等待），不是对端的答复。
        if (st == null || st < 0) {
            val dead = failReason
            if (st != null && dead != null) {
                // 等待期间链路断了：这不是"对端静默"，按断开原因收口，
                // 否则会被误判成「ATT 静默 / 服务表没人服务」。
                t.fail("等待 ATT 探针期间链路断开：$dead")
                throw ProtoException(Err.NOT_READY, dead)
            }
            t.attProbeAnswered = false
            if (awaitLlProbe()) {
                t.fail("LL 活着但 ATT 探针 ${PROBE_TIMEOUT_MS}ms 无答复 ⇒ 对端主机栈不处理 ATT")
                throw ProtoException(Err.NOT_READY, "对端 ATT 层零回复")
            }
            t.fail("LL 探针与 ATT 探针都没有答复 ⇒ 链路层可能已经不通")
            throw ProtoException(Err.NOT_READY, "链路层无响应")
        }
        t.attProbeAnswered = true
        t.attProbeStatus = st
        t.step("ATT 探针有答复 status=$st mtu=$mtu（ATT 层活着）")
    }

    /**
     * 服务发现：**只发一次**，超时就收工。
     *
     * 不再"换三种形态重发"：真机日志证明重发会被 GATT 命令队列丢掉
     * （`already has a pending command`），除了制造噪音没有任何作用。
     * 真正需要"再试一次"的时候，必须**换一条新链路**（新的 clientIf）——
     * 那是 `MainViewModel` 的自动补救在做的事。
     */
    private suspend fun discoverOnce(g: BluetoothGatt) {
        val t = trace
        if (failReason != null) throw ProtoException(Err.NOT_READY, failReason!!)
        if (closed || gatt == null) throw ProtoException(Err.NOT_READY, "链路已关闭")
        val gate = CompletableDeferred<Int>()
        servicesGate = gate
        val started = runCatching { g.discoverServices() }.getOrDefault(false)
        t?.bumpServices()
        t?.addShape("ATT 探针之后单次下发")
        t?.step("discoverServices started=$started")
        BleLog.d(TAG, "discoverServices started=$started")
        if (!started) {
            t?.warn("discoverServices 未能发起")
            throw ProtoException(Err.NOT_READY, "discoverServices 未能发起")
        }
        val st = withTimeoutOrNull(DISCOVERY_TIMEOUT_MS) { gate.await() }
        when {
            st == null -> {
                t?.warn("服务发现 ${DISCOVERY_TIMEOUT_MS}ms 内无回调（ATT 探针已证明 ATT 层活着）")
                throw ProtoException(Err.NOT_READY, "服务发现无回调")
            }
            st == BluetoothStatusCodes.SUCCESS -> return
            st == SERVICE_MISSING -> throw ProtoException(Err.NOT_READY, serviceMissingMessage())
            else -> {
                t?.warn("服务发现失败 ${BleStatus.message(st)}")
                throw ProtoException(Err.NOT_READY, "服务发现失败：${BleStatus.message(st)}")
            }
        }
    }


    /** LL 探针的答复（在 [probeLlAsync] 里发起）。 */
    private suspend fun awaitLlProbe(): Boolean {
        val t = trace
        if (t?.llProbeSent != true) {
            t?.step("LL 探针未发起，无法判定链路层状态")
            return false
        }
        if (t.llProbeAnswered) {
            t.rssi?.let { rssi -> t.step("LL 探针已有答复（rssi=$rssi）") }
            return true
        }
        val gate = rssiGate ?: return false
        val st = withTimeoutOrNull(PROBE_TIMEOUT_MS) { gate.await() }
        if (st == null) {
            t.warn("LL 探针 ${PROBE_TIMEOUT_MS}ms 内无答复")
            return false
        }
        return true
    }

    /**
     * MTU 协商：每轮换一个 gate，最多 [MTU_ATTEMPTS] 轮，每轮 [MTU_TIMEOUT_MS]。
     *
     * 若 ATT 探针已经协商成功（[mtu] > 23），这里直接返回。
     */
    private suspend fun awaitMtu(g: BluetoothGatt, want: Int) {
        if (want <= 23 || mtu > 23) return
        val t = trace
        repeat(MTU_ATTEMPTS) { attempt ->
            if (mtu > 23) return
            if (failReason != null) throw ProtoException(Err.NOT_READY, failReason!!)
            if (attempt > 0) delay(MTU_RETRY_DELAY_MS)
            val gate = CompletableDeferred<Int>()
            mtuGate = gate
            val started = runCatching { g.requestMtu(want) }.getOrDefault(false)
            t?.step("requestMtu($want) 第${attempt + 1}/${MTU_ATTEMPTS} 次 started=$started")
            BleLog.d(TAG, "requestMtu($want) 第${attempt + 1}/${MTU_ATTEMPTS} 次 started=$started")
            if (started) {
                val st = withTimeoutOrNull(MTU_TIMEOUT_MS) { gate.await() }
                if (mtu > 23) return
                BleLog.w(
                    TAG,
                    if (st == null) "MTU 第${attempt + 1}次 ${MTU_TIMEOUT_MS}ms 内无回调，重试"
                    else "MTU 协商未通过 status=$st mtu=$mtu，重试",
                )
            } else {
                BleLog.w(TAG, "requestMtu 第${attempt + 1}次未能发起，重试")
            }
        }
        throw ProtoException(Err.NOT_READY, "MTU 协商失败（仍为 $mtu），无法发送握手帧")
    }

    /** 把「真的没这个服务」说清楚（[SERVICE_MISSING] 专用值，不是系统 status）。 */
    private fun serviceMissingMessage(): String =
        "对端服务表里没有 a000/a001/a002/a003（Host 可能刚重启，本地缓存表过期）"

    /**
     * 反射清本地 GATT 缓存表。
     *
     * `BluetoothGatt.refresh()` 是**非公开 API**（`@UnsupportedAppUsage`），但它是
     * 「Host 重启后手机拿着旧表」的唯一解法。ROM 可能已经去掉它，所以全程 try/catch，
     * 失败只记日志。
     */
    private fun refreshGattCache(g: BluetoothGatt): Boolean = try {
        val m = g.javaClass.getMethod("refresh")
        val ok = m.invoke(g) as? Boolean ?: false
        BleLog.d(TAG, "refresh() 反射调用 -> $ok")
        ok
    } catch (t: Throwable) {
        BleLog.d(TAG, "refresh() 不可用: ${t.javaClass.simpleName} ${t.message}")
        false
    }

    private suspend fun subscribe(ch: BluetoothGattCharacteristic, indication: Boolean) {
        val g = gatt ?: throw ProtoException(Err.NOT_READY, "链路已断开")
        val localOk = g.setCharacteristicNotification(ch, true)
        if (!localOk) {
            throw ProtoException(Err.NOT_READY, "本地通知注册失败（${ch.uuid}）")
        }
        val cccd = ch.getDescriptor(UUID.fromString(CCCD_UUID))
            ?: throw ProtoException(Err.NOT_READY, "特征缺少 CCCD 描述符")
        val payload = if (indication) {
            BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
        } else {
            BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        }
        BleLog.d(TAG, "写 CCCD ${ch.uuid} indication=$indication")
        trace?.step("写 CCCD ${ch.uuid.toString().substring(4, 8)} indication=$indication")
        writeAndWait(
            what = "写 CCCD(${ch.uuid.toString().substring(4, 8)})",
            // CCCD 写是幂等的（重复 enable 不会出错），允许重发一次。
            // 真机上第一次写常因队列忙而 5s 无回调，直接判死太早。
            retries = 1,
            timeoutMs = SUBSCRIBE_TIMEOUT_MS,
            start = {
                if (Build.VERSION.SDK_INT >= 33) {
                    g.writeDescriptor(cccd, payload)
                } else {
                    @Suppress("DEPRECATION")
                    cccd.value = payload
                    @Suppress("DEPRECATION")
                    if (g.writeDescriptor(cccd)) BluetoothStatusCodes.SUCCESS else OP_START_FAILED
                }
            },
        )
        BleLog.d(TAG, "CCCD 已写入 ${ch.uuid}")
    }

    /**
     * 发起一次 GATT 写并等回调。
     *
     * - 有 [retries] 时（仅用于幂等写，如 CCCD）：超时先重发，用尽才判死。
     * - 超时用尽 ⇒ **判定链路中毒并关闭**：迟到的回调会污染下一个 gate，
     *   与其冒着「悄悄跳过一次写」的风险继续跑，不如立刻断开重连。
     */
    private suspend fun writeAndWait(
        what: String,
        retries: Int = 0,
        timeoutMs: Long = OP_TIMEOUT_MS,
        start: () -> Int,
    ) {
        var attempt = 0
        var lastErr: String = "$what 未能发起"

        while (true) {
            if (failReason != null) throw ProtoException(Err.NOT_READY, failReason!!)
            if (poisoned) throw ProtoException(Err.NOT_READY, "链路已作废（此前写操作超时）")
            if (closed || gatt == null) throw ProtoException(Err.NOT_READY, "链路已关闭")

            val gate = CompletableDeferred<Int>()
            pendingWrite = gate
            try {
                val started = start()
                if (started != BluetoothStatusCodes.SUCCESS) {
                    lastErr = "$what 无法发起: ${BleStatus.message(started)}"
                } else {
                    val status = withTimeout(timeoutMs) { gate.await() }
                    if (status == BluetoothStatusCodes.SUCCESS) return
                    lastErr = "$what 被拒绝: ${BleStatus.message(status)}"
                }
            } catch (t: kotlinx.coroutines.TimeoutCancellationException) {
                lastErr = "$what 超时 ${timeoutMs}ms"
            } finally {
                // 必须在 finally 里摘掉自己的 gate，否则上一次的迟到回调会
                // 把下一次写的新 gate 提前 complete。
                if (pendingWrite === gate) pendingWrite = null
            }

            if (attempt >= retries) {
                poisoned = true
                BleLog.e(TAG, "$lastErr（已重试 $attempt 次），链路判为不可用")
                trace?.fail(lastErr)
                closeQuietly()
                throw ProtoException(Err.NOT_READY, "$lastErr，链路已关闭")
            }
            attempt++
            BleLog.w(TAG, "$lastErr —— 第 $attempt/$retries 次重试")
            delay(RETRY_DELAY_MS)
        }
    }

    // ---------------------------------------------------------------- 发送

    /** 控制帧 → `CHAR_CTRL`，写-需响应（与 Python `send_ctrl` 一致）。 */
    suspend fun writeCtrl(bytes: ByteArray) = writeMutex.withLock {
        val ch = ctrlChar ?: throw ProtoException(Err.NOT_READY, "未连接")
        val g = gatt ?: throw ProtoException(Err.NOT_READY, "未连接")
        writeCharacteristicOnce(g, ch, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT, "写 CTRL")
    }

    /**
     * 数据帧 → `CHAR_RX`。先试写-不需响应（吞吐高），失败就退回写-需响应
     * —— 与 Python `send_data` 的 `_write_no_resp` 回退逻辑一致。
     */
    suspend fun writeData(bytes: ByteArray) = writeMutex.withLock {
        val ch = rxChar ?: throw ProtoException(Err.NOT_READY, "未连接")
        val g = gatt ?: throw ProtoException(Err.NOT_READY, "未连接")
        if (writeNoResp) {
            try {
                writeCharacteristicOnce(
                    g,
                    ch,
                    bytes,
                    BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE,
                    "写 RX(无响应)",
                )
                return@withLock
            } catch (e: ProtoException) {
                writeNoResp = false
                BleLog.w(TAG, "写-不需响应失败，退回写-需响应: ${e.message}")
            }
        }
        writeCharacteristicOnce(g, ch, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT, "写 RX")
    }

    private suspend fun writeCharacteristicOnce(
        g: BluetoothGatt,
        ch: BluetoothGattCharacteristic,
        bytes: ByteArray,
        writeType: Int,
        what: String,
    ) {
        writeAndWait(what) {
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeCharacteristic(ch, bytes, writeType)
            } else {
                @Suppress("DEPRECATION")
                ch.writeType = writeType
                @Suppress("DEPRECATION")
                ch.value = bytes
                @Suppress("DEPRECATION")
                if (g.writeCharacteristic(ch)) BluetoothStatusCodes.SUCCESS else OP_START_FAILED
            }
        }
    }

    // ---------------------------------------------------------------- 关闭

    /** 主动关闭。可重复调用。 */
    fun close() {
        if (closed) return
        closeQuietly()
        notifyDisconnected("链路已关闭")
    }

    private fun closeQuietly() {
        if (closed) return
        closed = true
        isConnected = false
        val g = gatt
        gatt = null
        rxChar = null
        txChar = null
        ctrlChar = null
        failAllPending("链路已关闭")
        if (g != null) {
            runCatching { g.disconnect() }
            // 必须等断开回调再 close —— 立刻 close 会泄漏 clientIf（见类注释坑 6）。
            // 断开回调里会调 g.close()；这里给个兜底定时器。
            mainHandler.postDelayed({ runCatching { g.close() } }, CLOSE_GRACE_MS)
        }
    }

    private fun failAllPending(reason: String) {
        pendingWrite?.complete(19)
        pendingWrite = null
        connectedGate?.completeExceptionally(ProtoException(Err.NOT_READY, reason))
        servicesGate?.let { if (!it.isCompleted) it.complete(-1) }
        mtuGate?.let { if (!it.isCompleted) it.complete(-1) }
        rssiGate?.let { if (!it.isCompleted) it.complete(-1) }
    }

    private fun notifyDisconnected(reason: String) {
        if (disconnectNotified) return
        disconnectNotified = true
        _events.tryEmit(LinkEvent.Disconnected(reason))
    }

    companion object {
        /** 子模块名（[BleLog] 会拼成 `BleChat/Link`）。 */
        private const val TAG = "Link"

        const val CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
        private const val OP_START_FAILED = -1

        /** 「服务表里没有 a000」的专用返回值（与系统 GATT status 不冲突：负数）。 */
        private const val SERVICE_MISSING = -100

        /** 单次写操作（特征/描述符）的回调等待上限。 */
        private const val OP_TIMEOUT_MS = 10_000L

        /** CCCD 写的单次等待上限；配合 retries=1，最坏 5s + 400ms + 5s。 */
        private const val SUBSCRIBE_TIMEOUT_MS = 5_000L

        /** `connect()` 里「连接 + 服务发现 + MTU」的总预算（订阅在其外）。 */
        private const val CONNECT_TIMEOUT_MS = 25_000L

        /**
         * ATT 探针的答复等待上限。取 5.5s：手机协议栈自己的 ATT 响应超时是 **5s**
         * （真机日志 `gatt_rsp_timeout`），等满一个窗口才能说"对端没回应"。
         */
        private const val PROBE_TIMEOUT_MS = 5_500L

        /**
         * 服务发现的答复等待上限。探针已经证明 ATT 活着，正常发现 < 1s；
         * 超过这个时间就是"搜索没回来"，不要重发（会被队列丢掉）。
         */
        private const val DISCOVERY_TIMEOUT_MS = 4_000L

        private const val MTU_ATTEMPTS = 3
        private const val MTU_TIMEOUT_MS = 3_000L
        private const val MTU_RETRY_DELAY_MS = 400L

        /**
         * 等 BLE 配对结果的窗口。取 12s：Android 自己的 SMP 超时是 30s，而配对只是
         * **尝试之一**，不该把一次连接尝试拖死（连接总预算 25s）。
         */
        private const val BOND_TIMEOUT_MS = 12_000L

        private const val RETRY_DELAY_MS = 400L
        private const val CLOSE_GRACE_MS = 800L
    }
}