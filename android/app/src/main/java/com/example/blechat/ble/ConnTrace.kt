package com.example.blechat.ble

/**
 * 一次连接尝试的**病历**：按时间顺序记下每一步（我们发了什么、系统回调了什么、
 * 哪一步没有回调），并在最后给出一个**分类结论**。
 *
 * ## 为什么要专门做这个
 *
 * 真机上的失败几乎都是「回调没来」而不是「回调报错」：
 *
 * ```
 * 12:57:02.854  onClientConnectionState status=0        ← LL 连上了
 * 12:57:03.291  onConnectionUpdated interval=6 …        ← LL 参数更新成功（双向都通）
 * 12:57:06.865  服务发现 4000ms 内无回调                 ← ATT 层一个字节都没有
 * 12:57:16.092  断开（本地主动）
 * ```
 *
 * 同一句「超时」背后至少有 5 种完全不同的故障，处置也完全不同：
 *
 * | 现象 | 结论 | 该动哪一端 |
 * |---|---|---|
 * | `connectGatt` 后连接回调都不来 | 地址失效（Windows LE 地址轮换）/对端不在 | 手机：重扫 |
 * | 连上了、`readRemoteRssi` 有响应、`requestMtu` 无响应 | **ATT 静默**（链路活着，对端主机栈不处理请求） | PC：关开一次蓝牙 / 重启 Host |
 * | 同上，且这次**配对也没成功** | ATT 静默 + 对端认不出手机的随机地址（RPA） | 两端：先配对，再按上面的顺序 |
 * | 连上了、配对成功、`requestMtu` 无响应 | **ATT 静默**（配对不是原因，纯粹是对端服务层） | PC：关开一次蓝牙 / 重启 Host |
 * | 连上了、`requestMtu` 有响应、服务发现没回调 | 发现回调被系统丢弃 | 手机：换一种下发形态重试 / 清 GATT 缓存 |
 * | 发现了服务但表里没有 `a000` | 对端服务表过期（Host 重启过） | 手机：清缓存重连；PC：确认 Host 在广播 |
 * | 表齐全但 CCCD 写不通 | 订阅失败 | 手机：重试 |
 *
 * 本类**不依赖任何 Android API**（只用 Int/String），因此可以拿真实日志在
 * JVM 单测里复现分类逻辑（见 `ConnTraceTest`）。
 *
 * 用法：`GattLink` 在每次 `connect()` 开头 `ConnTrace(...)`，之后每个回调/动作都
 * `step()/mark…`，失败时 `classify()` 得到结论，把 `dump()` 交给日志与诊断弹窗。
 */
class ConnTrace(
    /** 这是第几次尝试（从 1 开始，跨自动重试累计）。 */
    val attempt: Int,
    val address: String,
    val name: String?,
    /**
     * 地址是怎么来的：
     * `scan-name`（按名字重解析，最可靠）/ `scan-pick`（用户点扫描结果）/
     * `stale-mac`（用存下来的旧 MAC，很可能已失效）/ `manual`（手输）。
     */
    val addressSource: String,
    startAt: Long = System.currentTimeMillis(),
) {
    /** 一行病历。`level`：`I` 信息 / `W` 警告 / `E` 失败。 */
    data class Line(val atMs: Long, val level: Char, val text: String)

    private val lock = Any()
    private val lines = ArrayList<Line>(64)
    val startedAt: Long = startAt

    // ---------------------------------------------------------------- 记录

    private fun add(level: Char, text: String) {
        synchronized(lock) {
            if (lines.size < MAX_LINES) lines.add(Line(System.currentTimeMillis(), level, text))
        }
    }

    fun step(text: String) = add('I', text)
    fun warn(text: String) = add('W', text)
    fun fail(text: String) = add('E', text)

    /** 记一条来自系统回调的事实（`tag` 用回调名，便于 grep）。 */
    fun cb(text: String) = add('I', "cb $text")

    fun lines(): List<Line> = synchronized(lock) { lines.toList() }

    // ---------------------------------------------------------------- 事实

    /** 已经 `connectGatt()`（发出连接请求的时刻）。 */
    var connectGattAt: Long? = null

    /** `connectGatt` 返回的 `BluetoothGatt` 是否为 null（设备对象拿不到）。 */
    var connectGattOk: Boolean = false

    /** 收到 `onConnectionStateChange(CONNECTED)` 的时刻。 */
    var connectedAt: Long? = null

    /** 收到 `onConnectionStateChange(DISCONNECTED)` 的时刻与 status。 */
    var disconnectedAt: Long? = null
    var disconnectStatus: Int? = null

    /** 连接回调里拿到的 `newState`（0 = 断开，2 = 已连接）。 */
    var lastNewState: Int? = null

    /** `onReadRemoteRssi` 是否答复（**只走控制器，不需要对端主机栈参与**）。 */
    var llProbeSent: Boolean = false
    var llProbeAnswered: Boolean = false
    var llProbeStatus: Int? = null
    var rssi: Int? = null

    /** `onMtuChanged` 是否答复（**ATT 层请求，需要对端主机栈处理**）。 */
    var attProbeSent: Boolean = false
    var attProbeAnswered: Boolean = false
    var attProbeStatus: Int? = null
    var mtu: Int? = null

    /** 服务发现：下发了几次、有没有回调、状态、表里有没有本服务。 */
    private var servicesRequestCount: Int = 0
    var servicesSeen: Boolean = false
    var servicesStatus: Int? = null
    var serviceMissing: Boolean = false
    /** 每次下发用的形态，便于看哪一种最终成功。 */
    private val shapes = ArrayList<String>()

    fun bumpServices() {
        synchronized(lock) { servicesRequestCount++ }
    }

    fun addShape(shape: String) {
        synchronized(lock) { if (shapes.size < 8) shapes.add(shape) }
    }

    fun servicesRequests(): Int = synchronized(lock) { servicesRequestCount }

    fun serviceShapes(): List<String> = synchronized(lock) { shapes.toList() }


    /** 是否试过清 GATT 缓存（`refresh()` 反射调用）。 */
    var refreshTried: Boolean = false
    var refreshOk: Boolean = false

    /** 对端主动发 `Service Changed`（说明对端服务表变了）。 */
    var serviceChangedSeen: Boolean = false

    /** CCCD 订阅是否写完。 */
    var cccdWritten: Boolean = false

    /** 链路就绪（可以开始握手）。 */
    var readyAt: Long? = null

    /** 设备身份（连接前读的）。 */
    var bondState: Int? = null
    var deviceType: Int? = null
    var addressType: Int? = null
    var deviceName: String? = null

    /** 手机/系统信息（`Build.MODEL` + `Build.VERSION.RELEASE`），让复制出来的病历自带环境。 */
    var envInfo: String? = null

    // ---------------------------------------------------------------- 配对

    /**
     * BLE 配对（SMP）的情况。
     *
     * 为什么它和"连不上"直接相关：Windows 与手机的 LE 地址都是**会轮换的可解析私有地址
     * （RPA）**，只有配对过（交换并保存了 IRK）双方才能解开对方的地址。不配对时的真机
     * 表现恰好就是本项目这一路的现象：**LL 连得上、`readRemoteRssi` 有响应，ATT 一个
     * 字节都不回**（手机日志：`unable to match and resolve random address`）。
     */
    var bondBefore: Int? = null
    var bondRequested: Boolean = false
    var bondOutcome: BondOutcome? = null
    var bondAfter: Int? = null

    /** 配对**试过但没成功**（对端拒绝/超时/发起失败）。 */
    fun bondUnresolved(): Boolean =
        bondRequested && bondOutcome != null &&
            bondOutcome != BondOutcome.BONDED && bondOutcome != BondOutcome.ALREADY

    /** 配对**成功**（本次新配对成功，或本来就配对过）。 */
    fun bondSettled(): Boolean =
        bondOutcome == BondOutcome.BONDED || bondOutcome == BondOutcome.ALREADY

    fun bondOutcomeText(): String = when (bondOutcome) {
        BondOutcome.ALREADY -> "本来就已配对"
        BondOutcome.BONDED -> "本次配对成功"
        BondOutcome.TIMEOUT -> "超时（对端没有确认/没有弹窗）"
        BondOutcome.FAILED -> "被拒绝或密钥不匹配"
        null -> if (bondRequested) "未完成" else "未尝试"
    }

    /** 最终错误（人话）与错误码。 */
    var errorMessage: String? = null
    var errorCode: Int? = null

    /** `onConnectionStateChange` 里拿到的 status（非 0 就是失败原因）。 */
    var connectStatus: Int? = null

    // ---------------------------------------------------------------- 判据

    /** ATT 层是否**曾经**有过任何回应。 */
    fun attSeen(): Boolean = servicesSeen || attProbeAnswered

    /** 链路层是否是活的（要么有 LL 探针答复，要么服务发现本身有回应）。 */
    fun llAlive(): Boolean = llProbeAnswered || servicesSeen || attProbeAnswered

    /** 从发出连接到收到断开之间的毫秒数（没断开就到现在）。 */
    fun livedMs(now: Long = System.currentTimeMillis()): Long? {
        val start = connectedAt ?: return null
        return (disconnectedAt ?: now) - start
    }

    // ---------------------------------------------------------------- 分类

    /**
     * 给出结论。**纯函数**（只看上面这些标记），因此可以用真实日志做单测。
     */
    fun classify(): Diagnosis {
        if (readyAt != null) {
            return Diagnosis(
                DiagKind.OK,
                "链路就绪",
                "已连上并完成服务发现/MTU/订阅。",
                DiagSide.NONE,
                retrySameAddress = false,
            )
        }

        val err = errorMessage.orEmpty()
        val target = name ?: address

        // 0. 握手/认证类的失败发生在链路就绪之后，GattLink 不管，但会话层会把
        //    错误塞回来，这时按错误码判断更准。
        if (err.contains("认证失败") || err.contains("AUTH")) {
            return Diagnosis(
                DiagKind.AUTH_FAILED,
                "口令不对",
                "链路是通的，是共享密码不匹配。请向 Host 确认密码后重输。",
                DiagSide.PHONE,
                retrySameAddress = true,
            )
        }

        // 1. 连接请求发出去，连接回调压根没来。
        if (connectGattAt != null && connectedAt == null) {
            val hint = if (addressSource == "stale-mac") {
                "这个 MAC 是从本地记录里翻出来的，Windows 的 LE 广播地址会轮换，" +
                    "它很可能已经失效。请点「扫描 Host」重新选一个，或用「重连上次」" +
                    "（会按名字重新解析当前地址）。"
            } else {
                "连接请求没有得到响应。确认 PC 上的 Host 还在广播（PC 端日志应有 " +
                    "`advertising=STARTED`），并确认手机离 PC 够近，然后重试。"
            }
            return Diagnosis(DiagKind.NO_CONNECT, "连不上（连接回调没来）", hint, DiagSide.BOTH, true)
        }

        // 2. 连上过，但断开时的 status 说明是对端/协议层的问题。
        val dStatus = disconnectStatus
        if (dStatus != null && dStatus != 0 && dStatus != 19 && !attSeen()) {
            return Diagnosis(
                DiagKind.REMOTE_DISCONNECT,
                "对端在初始化阶段就断开了",
                "GATT status=${dStatus}（${BleStatus.message(dStatus)}）。" +
                    "若反复出现且 status=133/8，先在手机蓝牙设置里「忽略/取消配对」这台 PC，" +
                    "再到 PC 上确认 Host 在广播，然后重试。",
                DiagSide.BOTH,
                retrySameAddress = true,
            )
        }

        // 3. 核心分叉一：连上了，但**链路层其实已经不通**（LL 探针发出去了没答复）。
        if (connectedAt != null && !attSeen() && llProbeSent && !llProbeAnswered) {
            return Diagnosis(
                DiagKind.LL_DEAD,
                "连上了但链路层已经不响应",
                "连接回调说「已连接」，可 `readRemoteRssi()`（纯控制器操作，不需要对端" +
                    "主机栈参与）也没有答复 —— 说明这条链路其实已经断了，只是断开回调没来。" +
                    "请重试；若反复出现，把手机靠近 PC、重启手机蓝牙后再试。",
                DiagSide.PHONE,
                retrySameAddress = true,
            )
        }

        // 4. 核心分叉二：LL 活着但 ATT 全静默（**真机日志的形态**）。
        if (connectedAt != null && !attSeen()) {
            val llNote = when {
                llProbeAnswered && rssi != null -> "链路层正常（RSSI 读取成功 = ${rssi}dBm）"
                connectedAt != null -> "链路层看起来是连上的"
                else -> "链路层情况未知"
            }
            // 配对没成功 ⇒ 对端很可能"认不出我们"。这是**手机侧唯一能自己动**的杠杆，
            // 所以必须先说它，否则用户只会照着 PC 侧的清单反复重启蓝牙。
            val unresolved = bondUnresolved()
            val bondWhy = when {
                unresolved ->
                    "而且这次 **BLE 配对没有成功**（${bondOutcomeText()}）：手机和 PC 的 LE 地址都是" +
                        "会轮换的随机地址（RPA），配对过（交换 IRK）双方才能认出对方；" +
                        "没配对时对端认不出这台手机，表现正好就是「连得上、ATT 不服务」。"
                bondSettled() ->
                    "这次 BLE 配对是成功的（${bondOutcomeText()}），所以不是「认不出地址」这一类原因。"
                else ->
                    "这次没有做 BLE 配对（可以在连接面板打开「连接前先配对」再试一次）。"
            }
            val why = "$llNote，但 ATT 探针（MTU 请求）以及它后面的每一步都没有回应。" +
                "手机协议栈自己的 ATT 响应超时（真机日志 `gatt_rsp_timeout`，5 秒）已经证明：" +
                "**请求发出去了，是对端没有回答** —— 所以问题在 PC 那一侧的蓝牙栈。$bondWhy\n" +
                "另外两件事先说清楚：① **共享密码（AUTH）阶段在链路就绪之后**，链路没到 READY，" +
                "所以还没轮到输密码；② **PC 端此刻不会有任何日志** —— PC 只记录 CCCD/特征写入，" +
                "ATT 都没通，自然没有消息。"
            val bondAct = if (unresolved) {
                "\n④ 或先在手机蓝牙设置里把 $target 的配对删掉，回到本 App 打开「连接前先配对」再连一次。"
            } else {
                "\n④ 还不行就把手机蓝牙设置里 $target 的记录「忽略/取消配对」清掉，再让 PC 重新广播。"
            }
            return Diagnosis(
                DiagKind.ATT_SILENT,
                "连上了，但对端 ATT 层零回复",
                "按对症程度依次试，每试一步回来点一次连接：\n" +
                    "① 在 PC 上**退出并重启 Host 程序**（让 GattServiceProvider 重新注册，最对症）；\n" +
                    "② 在 PC 上把蓝牙**关掉再打开**（等 5 秒）；\n" +
                    "③ 在 PC 版 BLE Chat 里点「重置蓝牙」/ Ctrl+R；" +
                    bondAct,
                // 配对没成功时，最该先做的那一步在**手机侧**（打开"连接前先配对"），
                // 所以不能再把用户一股脑推去动 PC。
                if (unresolved) DiagSide.BOTH else DiagSide.PC,
                retrySameAddress = true,
                why = why,
            )
        }

        // 5. ATT 探针有回应（MTU 协商成功）却没有服务发现回调。
        //
        //    真机日志（22:03）把这一路的归属钉死了：
        //      GATTC_Discover … disc_type=1        ← 请求确实发出去了
        //      gatt_rsp_timeout lcid:4             ← 手机 ATT 层等满 5s 没等到响应
        //    而对端是 **Windows**：MTU 交换由系统蓝牙栈（bthserv）回答，服务发现要由
        //    App 注册的 GattServiceProvider 那张表来服务。前者通了、后者不通 ⇒
        //    对端"服务表这一层"没在服务（Host 没跑 / provider 丢了注册）。
        if (connectedAt != null && !servicesSeen && attProbeAnswered) {
            return Diagnosis(
                DiagKind.DISCOVERY_DROPPED,
                "ATT 通，但服务发现没人应答",
                "MTU 请求有回应（说明对端蓝牙栈是活的），但 discoverServices() 一直没有回应 —— " +
                    "服务表这一层没人服务。" +
                    "对端是 Windows 时，这几乎总是 **PC 侧**的问题：" +
                    "请在 PC 上重启 Host 程序（让 GattServiceProvider 重新注册）；" +
                    "不行就把蓝牙关掉再打开。若 PC 侧确认正常，再重启手机蓝牙。",
                DiagSide.PC,
                retrySameAddress = true,
                why = "MTU 交换在 Windows 上由**系统栈**回答，服务发现必须由 **Host 注册的 " +
                    "GattServiceProvider** 回答。前者通、后者不通 ⇒ 对端的服务表这一层没在服务。",
            )
        }

        // 6. 服务发现成功但表里没有本服务。
        if (servicesSeen && serviceMissing) {
            return Diagnosis(
                DiagKind.SERVICE_MISSING,
                "连上了，但对端没有 a000 服务",
                "服务表里没有本项目的服务 UUID —— 通常是 PC 上的 Host 刚重启过、" +
                    "而手机手里还是**上一次的缓存表**。已在重试时尝试清缓存；" +
                    "若仍失败，请在 PC 上确认 Host 正在广播（`advertising=STARTED`）。",
                DiagSide.BOTH,
                retrySameAddress = true,
            )
        }

        // 6. 服务表齐了，但 MTU 一直没协商成（ATT 探针回了非成功状态，后面三轮也没成）。
        //    注意顺序：必须先于"订阅失败"，否则会被误说成订阅的问题。
        if (servicesSeen && !serviceMissing && mtu == null) {
            return Diagnosis(
                DiagKind.MTU_FAILED,
                "服务找到了，但 MTU 协商没成功",
                "对端回答了 MTU 请求但不是成功状态，重试三轮也没成（仍为 23）。" +
                    "本 App 需要 >23 才能发握手帧。请重试；若反复失败，" +
                    "在 PC 上重启 Host 程序后再连。",
                DiagSide.BOTH,
                retrySameAddress = true,
                why = "ATT 探针（MTU 请求）有答复 status=$attProbeStatus —— 说明对端 ATT 层活着，" +
                    "只是不肯把 MTU 放大。",
            )
        }

        if (servicesSeen && !cccdWritten) {
            return Diagnosis(
                DiagKind.SUBSCRIBE_FAILED,
                "服务找到了，但订阅（写 CCCD）没成功",
                "请重试；若反复失败，重启手机蓝牙。",
                DiagSide.PHONE,
                retrySameAddress = true,
            )
        }

        return Diagnosis(
            DiagKind.OTHER,
            "连接失败",
            err.ifBlank { "原因不明，请把「连接诊断」里的病历发出来。" },
            DiagSide.BOTH,
            retrySameAddress = true,
        )
    }

    // ---------------------------------------------------------------- 输出

    /** 多行病历，直接给人看 / 复制。 */
    fun dump(): String {
        val head = buildString {
            append("── 连接病历 #").append(attempt).append(" ──\n")
            append("目标: ").append(name ?: "-").append("  ").append(address)
            append("  (地址来源: ").append(addressSource).append(")\n")
            append("设备: name=").append(deviceName ?: "-")
            append(" bondState=").append(bondState?.let { bondText(it) } ?: "-")
            append(" type=").append(deviceType?.let { typeText(it) } ?: "-")
            append(" addrType=").append(addressType?.let { addressTypeText(it) } ?: "未知")
            append('\n')
            append("配对: 发起=").append(bondRequested)
                .append(" 前=").append(bondBefore?.let { bondText(it) } ?: "-")
                .append(" 后=").append(bondAfter?.let { bondText(it) } ?: "-")
                .append(" 结果=").append(bondOutcomeText())
                .append('\n')
            envInfo?.let { append("环境: ").append(it).append('\n') }
        }
        val body = StringBuilder()
        for (l in lines()) {
            body.append('+').append(l.atMs - startedAt).append("ms ")
                .append(l.level).append(' ').append(l.text).append('\n')
        }
        val d = classify()
        val tail = buildString {
            append("结论: ").append(d.title).append("  [").append(d.kind)
                .append(" / 该动: ").append(sideText(d.side)).append("]\n")
            append("判据: llAlive=").append(llAlive())
                .append(" attSeen=").append(attSeen())
                .append(" llProbe=").append(if (llProbeSent) (if (llProbeAnswered) "答复" else "无答复") else "未发起")
                .append(" attProbe=").append(if (attProbeSent) (if (attProbeAnswered) "答复" else "无答复") else "未发起")
                .append(" services=").append(servicesRequests()).append("次")
                .append(if (servicesSeen) "(有回调)" else "(无回调)")
                .append(" shapes=").append(serviceShapes())
                .append(" serviceMissing=").append(serviceMissing)
                .append(" mtu=").append(mtu ?: "-")
                .append(" bond=").append(bondOutcomeText())
                .append(" lived=").append(livedMs()?.let { "${it}ms" } ?: "-")
                .append('\n')
            append("处置: ").append(d.hint).append('\n')
            if (d.why.isNotBlank()) append("背景: ").append(d.why).append('\n')
            // 把取证命令一起带上：用户复制这一屏就能自己抓一份能定位的日志。
            append("取证: adb logcat -c  然后  adb logcat -v time | findstr /i ")
                .append("BleChat BluetoothGatt BtGatt bt_att btif bt_stack BluetoothLeScanner")
                .append('\n')
        }
        return head + body + tail
    }

    companion object {
        /** 病历上限：再多也不影响结论，防止长跑把内存吃光。 */
        private const val MAX_LINES = 200

        fun bondText(state: Int): String = when (state) {
            10 -> "NONE(未配对)"
            11 -> "BONDING"
            12 -> "BONDED(已配对)"
            else -> "state=$state"
        }

        fun typeText(type: Int): String = when (type) {
            1 -> "CLASSIC"
            2 -> "LE"
            3 -> "DUAL"
            4 -> "UNKNOWN"
            else -> "type=$type"
        }

        /**
         * 地址类型。**这是判断"Windows 地址轮换"的直接证据**：
         * `1`/`3` 表示对端用的是随机地址（本项目的现场：同一个 PC 名字每次都换 MAC）。
         *
         * 取值来自 `BluetoothDevice` 的隐藏常量（`getAddressType()` 是 API 35 才公开的，
         * 之前只能靠反射拿，拿不到就是"未知"）。
         */
        fun addressTypeText(t: Int): String = when (t) {
            0 -> "PUBLIC(固定)"
            1 -> "RANDOM(随机/轮换)"
            2 -> "PUBLIC_IDENTITY(身份地址)"
            3 -> "RANDOM_IDENTITY(可解析私有 RPA)"
            4 -> "ANONYMOUS(匿名)"
            else -> "type=$t"
        }

        fun sideText(side: DiagSide): String = when (side) {
            DiagSide.NONE -> "无需处理"
            DiagSide.PHONE -> "手机侧"
            DiagSide.PC -> "PC 侧"
            DiagSide.BOTH -> "两端都可能"
        }
    }
}

/** 故障分类。日志与诊断弹窗都用它决定说什么。 */
enum class DiagKind {
    OK,
    NO_CONNECT,
    /** 链路层活着，但对端主机栈不处理 ATT 请求（PC 侧问题）。 */
    LL_DEAD,
    ATT_SILENT,
    DISCOVERY_DROPPED,
    SERVICE_MISSING,
    MTU_FAILED,
    SUBSCRIBE_FAILED,
    REMOTE_DISCONNECT,
    AUTH_FAILED,
    OTHER,
}

/** 该动哪一端。 */
enum class DiagSide { NONE, PHONE, PC, BOTH }

/**
 * 结论：一句标题 + 一段可执行的话。
 *
 * @param retrySameAddress 用**同一个地址**再试一次是否有意义
 *   （地址失效类必须重扫，重试同一个地址纯属浪费用户时间）。
 * @param why 为什么会这样（背景/推理）。UI 上不显示，只在病历 dump 里 ——
 *   用户要的是"我该做什么"，不是"你为什么这么判断"；但后者是排查时最需要的。
 */
data class Diagnosis(
    val kind: DiagKind,
    val title: String,
    val hint: String,
    val side: DiagSide,
    val retrySameAddress: Boolean,
    val why: String = "",
)
