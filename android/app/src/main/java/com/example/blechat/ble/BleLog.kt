package com.example.blechat.ble

import android.util.Log

/**
 * 统一的调试日志前缀：`BleChat/<子模块>`，`adb logcat -s BleChat` 能一把抓。
 *
 * 以前调用方传的是完整的 `BleChat/Link`，这里再拼一次前缀，日志里就出现
 * `BleChat/BleChat/Link`（只是观感问题，但抓日志时容易看漏）。现在约定：
 * **调用方只传子模块名**（`Link` / `Session` / `MainVM` / `Trace`）。
 */
internal object BleLog {
    private const val ROOT = "BleChat"

    fun d(where: String, msg: String) = Log.d(tag(where), msg)
    fun i(where: String, msg: String) = Log.i(tag(where), msg)
    fun w(where: String, msg: String) = Log.w(tag(where), msg)
    fun e(where: String, msg: String) = Log.e(tag(where), msg)

    /** 多行文本按行打，保持每行都带 tag（否则 logcat 会把后续行算到别的 tag 上）。 */
    fun block(where: String, text: String) {
        text.lineSequence().filter { it.isNotEmpty() }.forEach { Log.i(tag(where), it) }
    }

    private fun tag(where: String): String =
        if (where.startsWith("$ROOT/")) where else "$ROOT/$where"
}
