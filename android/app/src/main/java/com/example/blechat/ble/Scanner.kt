package com.example.blechat.ble

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.ParcelUuid
import androidx.core.content.ContextCompat
import com.example.blechat.protocol.P
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.Dispatchers
import java.util.UUID

/**
 * BLE 扫描。默认只匹配广播里带 `SERVICE_UUID` 的设备；没有定位权限导致扫描为空时，
 * 上层应支持按 MAC 直连（见 [GattLink.connect]）。
 */
class BleScanner(private val context: Context) {

    private val manager: BluetoothManager?
        get() = context.getSystemService(BluetoothManager::class.java)

    fun hasScanPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_SCAN) ==
            PackageManager.PERMISSION_GRANTED

    fun hasConnectPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * @param onlyMine true（默认）= 只回调带本项目 SERVICE_UUID 的广播；
     *                 false = 所有 BLE 设备（用于诊断）。
     * @param timeoutMs 0 表示一直扫，由 collect 方取消。
     */
    @SuppressLint("MissingPermission")
    fun scan(onlyMine: Boolean = true, timeoutMs: Long = 0L): Flow<ScannedDevice> = callbackFlow {
        if (!hasScanPermission()) {
            throw SecurityException("缺少 BLUETOOTH_SCAN 权限")
        }
        val scanner = manager?.adapter?.bluetoothLeScanner
            ?: throw IllegalStateException("蓝牙不可用或未打开")

        val serviceUuid: UUID = UUID.fromString(P.SERVICE_UUID)
        val filters = if (onlyMine) {
            listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(serviceUuid)).build())
        } else {
            emptyList()
        }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        val seen = LinkedHashMap<String, ScannedDevice>()

        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                offer(result)
            }

            override fun onBatchScanResults(results: MutableList<ScanResult>) {
                results.forEach { offer(it) }
            }

            override fun onScanFailed(errorCode: Int) {
                trySend(
                    ScannedDevice(
                        name = "扫描失败 code=$errorCode",
                        address = "",
                        rssi = 0,
                    ),
                )
                close()
            }

            private fun offer(result: ScanResult) {
                val uuids = (result.scanRecord?.serviceUuids ?: emptyList())
                    .map { it.uuid.toString().lowercase() }
                val hasMine = uuids.any { it == P.SERVICE_UUID }
                if (onlyMine && !hasMine) return
                val dev = ScannedDevice(
                    name = result.scanRecord?.deviceName ?: result.device.name,
                    address = result.device.address,
                    rssi = result.rssi,
                    serviceUuids = uuids.mapNotNull { runCatching { UUID.fromString(it) }.getOrNull() },
                )
                if (seen[dev.address] != dev) {
                    seen[dev.address] = dev
                    trySend(dev)
                }
            }
        }

        scanner.startScan(filters, settings, callback)
        if (timeoutMs > 0) {
            kotlinx.coroutines.delay(timeoutMs)
            close()
        }
        awaitClose {
            try {
                scanner.stopScan(callback)
            } catch (_: Exception) {
                // 蓝牙可能已经关掉，或者压根没扫成功
            }
        }
    }.flowOn(Dispatchers.Main)

    /** 直接按 MAC 连：不依赖扫描，绕开「定位权限被拒导致扫描为空」。 */
    fun deviceFor(address: String) = manager?.adapter?.getRemoteDevice(address)
        ?: throw IllegalStateException("蓝牙不可用")
}
