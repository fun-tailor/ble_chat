package com.example.blechat.data

import android.content.Context
import android.os.Build
import java.util.UUID

/**
 * 轻量键值存储（SharedPreferences）。存的是**明文配置**，
 * 只有 PSK 这类密钥材料走 [PskStore]（Android Keystore 加密）。
 */
class AppPrefs(context: Context) {
    private val prefs = context.applicationContext
        .getSharedPreferences("blechat_prefs", Context.MODE_PRIVATE)

    /** 本机 device_id。首次启动随机生成，之后保持不变（HELLO 里带的就是它）。 */
    val deviceId: String
        get() = prefs.getString(KEY_DEVICE_ID, null) ?: run {
            val fresh = UUID.randomUUID().toString()
            prefs.edit().putString(KEY_DEVICE_ID, fresh).apply()
            fresh
        }

    /** 昵称。默认取设备型号，用户可在设置里改（HOST 只有 AUTH_OK 会带昵称，client 侧自己定）。 */
    var displayName: String
        get() = prefs.getString(KEY_NAME, null) ?: Build.MODEL?.takeIf { it.isNotBlank() } ?: "Android"
        set(value) {
            prefs.edit().putString(KEY_NAME, value.trim().take(32)).apply()
        }

    /** 最近一次连过的 network_id（用于首屏自动选中）。 */
    var lastNetworkId: String?
        get() = prefs.getString(KEY_LAST_NETWORK, null)
        set(value) {
            prefs.edit().putString(KEY_LAST_NETWORK, value).apply()
        }

    /**
     * 最近一次连过的 Host MAC（用于一键重连、按 MAC 直连）。
     *
     * **注意：Windows 的 LE 广播地址会轮换**（实测同一天见过
     * `65:68:..` / `76:0D:..` / `4B:AA:..` 三个，而 PC 的身份地址一直是
     * `8C:C6:81:9C:B9:7B`），所以这个 MAC 很可能已经失效。
     * 一键重连应优先按 [lastHostName] 重新扫描解析（见 `MainViewModel.reconnectLast`）。
     */
    var lastHostAddress: String?
        get() = prefs.getString(KEY_LAST_ADDRESS, null)
        set(value) {
            prefs.edit().putString(KEY_LAST_ADDRESS, value).apply()
        }

    /** 最近一次连过的 Host 名字（`DESKTOP-XXXX`）。地址会轮换，名字不会 —— 重连靠它。 */
    var lastHostName: String?
        get() = prefs.getString(KEY_LAST_HOST_NAME, null)
        set(value) {
            prefs.edit().putString(KEY_LAST_HOST_NAME, value).apply()
        }

    /** 首次启动时是否已经引导过权限（只提示一次）。 */
    var permissionAsked: Boolean
        get() = prefs.getBoolean(KEY_PERMISSION_ASKED, false)
        set(value) {
            prefs.edit().putBoolean(KEY_PERMISSION_ASKED, value).apply()
        }

    /**
     * 连接前是否先做一次 **BLE 配对**（SMP）。默认开。
     *
     * 为什么默认开：手机和 PC 的 LE 地址都是会轮换的随机地址（RPA），只有配对过
     * （交换并保存了 IRK）双方才能认出对方。本项目"LL 连得上、ATT 一个字节都不回"
     * 的现象里，这是**手机侧唯一能自己动的杠杆**（见 `Bonder`）。配对成功之后这一项
     * 几乎是零成本的 —— 之后每次连接都直接跳过。
     */
    var bondFirst: Boolean
        get() = prefs.getBoolean(KEY_BOND_FIRST, true)
        set(value) {
            prefs.edit().putBoolean(KEY_BOND_FIRST, value).apply()
        }

    private companion object {
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_NAME = "display_name"
        const val KEY_LAST_NETWORK = "last_network_id"
        const val KEY_LAST_ADDRESS = "last_host_address"
        const val KEY_LAST_HOST_NAME = "last_host_name"
        const val KEY_PERMISSION_ASKED = "permission_asked"
        const val KEY_BOND_FIRST = "bond_first"
    }
}
