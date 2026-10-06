package com.example.blechat.protocol

/**
 * 线格式常量与控制帧类型。
 *
 * 与仓库根目录的 `PROTOCOL.md` 一一对应；与 Python 端 `blechat/protocol.py`
 * 必须保持逐字节一致，否则两端连不上。改动前先看 PROTOCOL.md §8 的差异表。
 */
object P {
    const val SERVICE_UUID = "0000a000-0000-1000-8000-00805f9b34fb"
    const val CHAR_RX = "0000a001-0000-1000-8000-00805f9b34fb"
    const val CHAR_TX = "0000a002-0000-1000-8000-00805f9b34fb"
    const val CHAR_CTRL = "0000a003-0000-1000-8000-00805f9b34fb"

    const val CTRL_MAGIC = 0xB1
    const val DATA_MAGIC = 0xB2
    const val DATA_HEADER = 16
    const val AAD_LEN = 9
    const val PROTO_VER = 1

    const val FLAG_COMPRESSED = 0x01
    const val FLAG_IMAGE = 0x02
    const val FLAG_LAST = 0x04
    const val FLAG_FILE = 0x08

    const val MAX_PAYLOAD = 5 * 1024 * 1024
    const val MAX_FILE = 2 * 1024 * 1024
    const val MAX_NAME_LEN = 64

    const val ACK_TIMEOUT_MS = 500L
    const val MAX_RETRIES = 5
    const val SEND_WINDOW = 4
    const val PROGRESS_EVERY = 8
    const val KEEPALIVE_INTERVAL_MS = 20_000L
    const val KEEPALIVE_GRACE_MISSES = 2
    const val KEEPALIVE_MAX_MISSES = 3
    const val SEND_TIMEOUT_MS = 8_000L
    const val HANDSHAKE_TIMEOUT_MS = 15_000L
    const val REASSEMBLY_TTL_MS = 60_000L
    const val PONG_GRACE_MS = KEEPALIVE_INTERVAL_MS + SEND_TIMEOUT_MS
    const val MAX_AUTH_FAILURES = 3

    /** 每片数据帧可用载荷 = max(8, mtu - 3 - 16)。 */
    fun chunkSize(mtu: Int): Int = maxOf(8, mtu - 3 - DATA_HEADER)
}

/** 控制帧 type（PROTOCOL.md §2.1）。未知 type 一律静默忽略（§7）。 */
object Ctrl {
    const val HELLO = 0x01
    const val CHALLENGE = 0x02
    const val AUTH = 0x03
    const val AUTH_OK = 0x04
    const val AUTH_FAIL = 0x05
    const val ACK = 0x06
    const val BYE = 0x07
    const val PEERS = 0x08
    const val PROGRESS = 0x09
    const val PING = 0x0A
    const val RESYNC = 0x0B
    const val PONG = 0x0C
}

/** 错误码（PROTOCOL.md §6）。与控制帧 type 是**两个编号空间**。 */
object Err {
    const val OK = 0x00
    const val NOT_READY = 0x01
    const val AUTH_FAILED = 0x02
    const val EPH_EXPIRED = 0x03
    const val EPH_NOT_FOUND = 0x04
    const val TOO_MANY_RETRIES = 0x05
    const val MSG_TOO_LARGE = 0x06
    const val CRC_MISMATCH = 0x07
    const val PROTO_VER_MISMATCH = 0x08
    const val SESSION_REPLACED = 0x09
    const val SERVER_SHUTDOWN = 0x0A
    const val UNKNOWN = 0x0B
    const val HANDSHAKE_TIMEOUT = 0x0C

    fun nameOf(code: Int): String = when (code) {
        OK -> "OK"
        NOT_READY -> "NOT_READY"
        AUTH_FAILED -> "AUTH_FAILED"
        EPH_EXPIRED -> "EPH_EXPIRED"
        EPH_NOT_FOUND -> "EPH_NOT_FOUND"
        TOO_MANY_RETRIES -> "TOO_MANY_RETRIES"
        MSG_TOO_LARGE -> "MSG_TOO_LARGE"
        CRC_MISMATCH -> "CRC_MISMATCH"
        PROTO_VER_MISMATCH -> "PROTO_VER_MISMATCH"
        SESSION_REPLACED -> "SESSION_REPLACED"
        SERVER_SHUTDOWN -> "SERVER_SHUTDOWN"
        UNKNOWN -> "UNKNOWN"
        HANDSHAKE_TIMEOUT -> "HANDSHAKE_TIMEOUT"
        else -> "ERR_$code"
    }
}
