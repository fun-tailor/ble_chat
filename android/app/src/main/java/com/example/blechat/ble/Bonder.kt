package com.example.blechat.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/** 一次 BLE 配对尝试的结果。 */
enum class BondOutcome {
    /** 本来就已经配对，什么都没做。 */
    ALREADY,

    /** 这次配对成功。 */
    BONDED,

    /** 等不到结果（对端没有确认 / 对端根本没弹窗）。 */
    TIMEOUT,

    /** 配对明确失败：对端拒绝、密钥不匹配、或 `createBond()` 压根没起来。 */
    FAILED,
}

/**
 * BLE **配对（bonding / SMP）**，与 App 自己的「共享密码」完全是两回事：
 *
 * | | BLE 配对（本类） | App 共享密码（`ChatSession` 的 AUTH） |
 * |---|---|---|
 * | 谁参与 | 手机蓝牙栈 ↔ PC 蓝牙栈 | 本 App ↔ PC 上的 Host 程序 |
 * | 何时发生 | `connectGatt` 之后、任何 ATT 操作**之前** | 链路 READY 之后（GATT 全都通了才轮到它） |
 * | 输密码 | 系统弹窗（该型号 PC 一般走 Just Works，不弹） | 本 App 的口令框 |
 * | 有什么用 | 双方交换/保存 IRK，**从此能认出对方的随机地址（RPA）** | 证明"我知道这个网络的密码" |
 *
 * 为什么要专门做这一步：
 *
 * 1. PC（Windows）的 LE 广播地址是**会轮换的可解析私有地址（RPA）**，手机这边也在用 RPA。
 *    不配对时双方都解不开对方的地址（手机日志里就是
 *    `btm_ble_conn_complete: unable to match and resolve random address`）。
 *    真机现象是：LL 层连得上、`readRemoteRssi` 有响应，但**ATT 层一个字节都不回**。
 *    配对是唯一能改变"对端认不认得出我们"的杠杆，而它是**手机侧就能做**的。
 * 2. 它同时回答了用户最直接的那个疑问：「点连接为什么没有输密码的阶段」——
 *    口令阶段在链路 READY 之后；而链路层的配对是系统弹窗，走 Just Works 时确实不弹。
 *
 * 本类**只在"链路上已经有一条 ACL"时被调用**（`GattLink` 在连接回调之后、在 ATT 探针
 * 之前调它），因此 SMP 直接跑在现成链路上，不会额外建一条连接。
 */
@SuppressLint("MissingPermission")
class Bonder(private val context: Context) {

    /** 本机看到的、某台设备的配对状态（拿不到就返回 null）。 */
    fun bondStateOf(device: BluetoothDevice): Int? =
        runCatching { device.bondState }.getOrNull()

    fun isBonded(device: BluetoothDevice): Boolean =
        bondStateOf(device) == BluetoothDevice.BOND_BONDED

    /**
     * 配对（如果还没配对）。
     *
     * 全程把每一步交给 [onEvent]，由调用方记进 [ConnTrace] —— 配对失败在真机上
     * 几乎只表现为"什么回调都没来"，所以"哪一步没来"必须留下证据。
     *
     * @param timeoutMs 等待配对结果的上限。取十来秒：Android 自己的 SMP 超时是 30s，
     *   而我们把配对当成**尝试之一**，不该让它把一次连接尝试拖死。
     */
    suspend fun bond(
        device: BluetoothDevice,
        timeoutMs: Long,
        onEvent: (String) -> Unit,
    ): BondOutcome {
        val before = bondStateOf(device)
        onEvent("配对: 配对前状态=${before?.let { ConnTrace.bondText(it) } ?: "未知"}")
        if (before == BluetoothDevice.BOND_BONDED) {
            onEvent("配对: 已经配对过，跳过")
            return BondOutcome.ALREADY
        }

        val gate = CompletableDeferred<Int>()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, intent: Intent?) {
                if (intent?.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return
                val d = deviceOf(intent) ?: return
                // 手机可能同时连着别的设备，只认这一台。
                if (!d.address.equals(device.address, ignoreCase = true)) return
                val st = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1)
                val prev = intent.getIntExtra(BluetoothDevice.EXTRA_PREVIOUS_BOND_STATE, -1)
                val variant = intent.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, Int.MIN_VALUE)
                val variantText = if (variant == Int.MIN_VALUE) "" else " variant=$variant"
                onEvent(
                    "配对: 状态变化 ${ConnTrace.bondText(prev)} -> ${ConnTrace.bondText(st)}$variantText",
                )
                when (st) {
                    BluetoothDevice.BOND_BONDED -> gate.complete(BluetoothDevice.BOND_BONDED)
                    // BONDING -> NONE 就是"失败"（对端拒绝 / 密钥不匹配 / 超时放弃）
                    BluetoothDevice.BOND_NONE -> if (prev == BluetoothDevice.BOND_BONDING) {
                        gate.complete(BluetoothDevice.BOND_NONE)
                    }
                }
            }
        }

        val filter = IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        return try {
            register(receiver, filter)
            val started = runCatching { device.createBond() }.getOrDefault(false)
            onEvent("配对: createBond() -> $started（对端可能弹确认框，也可能直接走 Just Works）")
            if (!started) {
                BondOutcome.FAILED
            } else {
                val st = withTimeoutOrNull(timeoutMs) { gate.await() }
                when (st) {
                    null -> {
                        onEvent("配对: ${timeoutMs}ms 内没有结果（对端没确认或没弹窗）")
                        BondOutcome.TIMEOUT
                    }
                    BluetoothDevice.BOND_BONDED -> BondOutcome.BONDED
                    else -> {
                        onEvent("配对: 被拒绝或密钥不匹配")
                        BondOutcome.FAILED
                    }
                }
            }
        } finally {
            runCatching { context.unregisterReceiver(receiver) }
        }
    }

    /**
     * 取消配对（清掉双方的密钥）。
     *
     * `BluetoothDevice.removeBond()` 是**非公开 API**，只能反射调；ROM 可能封掉它，
     * 所以失败时返回 false，让 UI 提示用户去系统设置里「忽略此设备」。
     *
     * 为什么值得放一个按钮：配对是双向记录。PC 侧如果把手机忘了，手机上还留着配对，
     * 之后的连接会尝试加密、然后因为对端没有密钥而失败 —— 这种"半配对"状态很难自己
     * 恢复，只能清掉重来。
     */
    fun removeBond(device: BluetoothDevice): Boolean = try {
        val m = device.javaClass.getMethod("removeBond")
        (m.invoke(device) as? Boolean) ?: false
    } catch (t: Throwable) {
        BleLog.w(TAG, "removeBond() 不可用: ${t.javaClass.simpleName} ${t.message}")
        false
    }

    @Suppress("DEPRECATION")
    private fun deviceOf(intent: Intent): BluetoothDevice? =
        intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE) as? BluetoothDevice

    @Suppress("UnspecifiedRegisterReceiverFlag")
    private fun register(receiver: BroadcastReceiver, filter: IntentFilter) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            context.registerReceiver(receiver, filter)
        }
    }

    private companion object {
        const val TAG = "Bond"
    }
}
