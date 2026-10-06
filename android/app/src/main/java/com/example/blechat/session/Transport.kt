package com.example.blechat.session

import com.example.blechat.ble.GattLink
import com.example.blechat.ble.LinkEvent
import com.example.blechat.protocol.P
import kotlinx.coroutines.flow.SharedFlow

/**
 * 会话层与传输层的边界。
 *
 * 会话层（[ChatSession]）只认这几个方法，因此单测里可以塞一个
 * 记录帧 / 注入帧的 FakeTransport，完全不用真 BLE。
 */
interface Transport {
    /** 数据帧分片大小 = `max(8, mtu - 3 - 16)`，必须在 `open()` 之后才有意义。 */
    val maxChunk: Int

    /** 链路事件流：Ready / Data / Ctrl / Disconnected。 */
    val events: SharedFlow<LinkEvent>

    /** 建链 + 协商 MTU + 订阅。返回时链路已就绪。 */
    suspend fun open()

    /** 控制帧（写-需响应）。 */
    suspend fun sendCtrl(bytes: ByteArray)

    /** 数据帧（写-不需响应，失败退回需响应）。 */
    suspend fun sendData(bytes: ByteArray)

    /** 主动断开。 */
    fun shutdown(reason: String)
}

/** 真实 BLE 传输：把 [GattLink] 包成 [Transport]。 */
class BleTransport(
    private val link: GattLink,
    private val address: String,
    private val requestMtu: Int = 517,
    /** 地址来源（`scan-name` / `scan-pick` / `stale-mac` / `manual`），只进连接病历。 */
    private val addressSource: String = "manual",
    private val hostName: String? = null,
    /** true = 用 `autoConnect=true` 建链（换一种建链形状再试一次）。 */
    private val autoConnect: Boolean = false,
    /** true = 发现服务前先 `refresh()` 清一次本进程的 GATT 缓存表。 */
    private val clearCacheFirst: Boolean = false,
    /** true = 任何 ATT 操作之前先做一次 BLE 配对（SMP），见 [com.example.blechat.ble.Bonder]。 */
    private val bondFirst: Boolean = false,
) : Transport {
    override val maxChunk: Int get() = P.chunkSize(link.mtu)

    override val events: SharedFlow<LinkEvent> get() = link.events

    override suspend fun open() {
        link.connect(address, requestMtu, addressSource, hostName, autoConnect, clearCacheFirst, bondFirst)
    }

    override suspend fun sendCtrl(bytes: ByteArray) = link.writeCtrl(bytes)

    override suspend fun sendData(bytes: ByteArray) = link.writeData(bytes)

    override fun shutdown(reason: String) = link.close()
}
