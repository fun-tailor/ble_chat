"""睡眠/唤醒之后的 Windows 蓝牙栈卫生。

进程里的 Python 对象在睡眠期间**原样保留**，但底下的东西全变了：

* radio 被系统关掉又打开，唤醒后要几秒才回到 `ON`；
* WinRT 那边可能留着上一轮的 `GattSession`（`maintain_connection=True`）——
  Windows 认为"这条链路还有人在用"，于是新的 `connect()` 走**缓存路径**：
  1~2s 就"连上"了，GATT 表却是旧的/空的。真机日志里就是连着 4 次
  `gatt table incomplete`、每次只差 ~2s，而**重启进程立刻正常** ——
  这说明脏状态在进程/栈这一侧，光重试同一批句柄永远好不了。

这里提供四个动作。**全部只做能做的事，失败只记日志不抛异常**：
调用方（连接循环）在唤醒之后无条件调用它们，成功与否只影响日志。

* `radio_state()` —— 读蓝牙 radio 的状态；
* `wait_radio_ready()` —— 唤醒后等 radio 回到 `ON` 再连；
* `release_gatt_session(addr)` —— 明确告诉 Windows「没人再维持这条链路」：
  `maintain_connection = False` + `close()` 会话 + `close()` 设备对象；
* `cycle_radio()` —— 关掉再打开蓝牙（最后手段）。**本机实测未打包的 Win32
  程序调 `Radio.set_state_async` 返回 `DENIED_BY_USER`**，所以这条路经常走不通；
  走不通时调用方应当引导用户自己关一下蓝牙开关（`open_bluetooth_settings()`）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

log = logging.getLogger("blechat.ble.radio")

# 唤醒后等 radio 回来：系统重新初始化蓝牙一般 1~5s，给足余量
RADIO_READY_TIMEOUT = 20.0
RADIO_READY_POLL = 0.5
# 单个 WinRT 调用（建 device / 建 session）的时限
WINRT_CALL_TIMEOUT = 6.0
# `cycle_radio` 里关掉多久再打开
RADIO_OFF_SECONDS = 3.0

_ON = 1
_OFF = 2
_DISABLED = 3
_ACCESS_ALLOWED = 1

_STATE_NAMES = {0: "UNKNOWN", 1: "ON", 2: "OFF", 3: "DISABLED"}


def supported() -> bool:
    """本平台能不能做这些事（非 Windows / 没有 winrt 时返回 False）。"""
    if sys.platform != "win32":
        return False
    try:
        import winrt.windows.devices.radios  # noqa: F401
    except Exception:
        return False
    return True


def _address_to_int(address: str) -> int:
    return int(str(address).replace(":", "").replace("-", ""), 16)


async def _bluetooth_radio():
    """拿到蓝牙 radio 对象；没有就返回 None。"""
    from winrt.windows.devices.radios import Radio, RadioKind

    radios = await asyncio.wait_for(Radio.get_radios_async(), WINRT_CALL_TIMEOUT)
    for radio in radios:
        if radio.kind == RadioKind.BLUETOOTH:
            return radio
    return None


async def radio_state(timeout: float = WINRT_CALL_TIMEOUT) -> str:
    """蓝牙 radio 的状态名：`ON` / `OFF` / `DISABLED` / `UNKNOWN` / `absent` / `unsupported`。"""
    if not supported():
        return "unsupported"
    try:
        radio = await asyncio.wait_for(_bluetooth_radio(), timeout)
    except Exception as exc:
        log.debug("radio_state failed: %s", exc)
        return "unknown"
    if radio is None:
        return "absent"
    try:
        return _STATE_NAMES.get(int(radio.state), f"state={int(radio.state)}")
    except Exception as exc:
        log.debug("radio.state failed: %s", exc)
        return "unknown"


async def wait_radio_ready(
    timeout: float = RADIO_READY_TIMEOUT, poll: float = RADIO_READY_POLL
) -> tuple[bool, str]:
    """等 radio 回到 `ON`，返回 `(是否就绪, 最后看到的状态)`。

    唤醒后立刻连接是白费：radio 还没回来，`BluetoothLEDevice` 拿到的全是
    上一代的对象。等一两秒比失败四次强。
    """
    if not supported():
        return True, "unsupported"
    waited = 0.0
    state = "unknown"
    while True:
        state = await radio_state()
        if state in ("ON", "unsupported"):
            return True, state
        if waited >= timeout:
            log.warning("bluetooth radio still %s after %.0fs", state, waited)
            return False, state
        await asyncio.sleep(poll)
        waited += poll


async def release_gatt_session(address: str, timeout: float = WINRT_CALL_TIMEOUT) -> str:
    """明确放弃 `address` 这条链路，返回一句可写进日志的描述。

    为什么需要：bleak 关会话时只是 `GattSession.close()`，**从不把
    `maintain_connection` 置回 False**。睡眠唤醒后残留的那条会话会让 Windows
    以为链路还被维持着，新的连接于是走缓存路径 —— 表是旧的/空的。
    这里把三件事都做掉：`maintain_connection=False` → `close()` 会话 →
    `close()` 设备对象。
    """
    if not supported():
        return "unsupported"
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.bluetooth.genericattributeprofile import GattSession

    steps: list[str] = []
    device = None
    session = None
    try:
        device = await asyncio.wait_for(
            BluetoothLEDevice.from_bluetooth_address_async(_address_to_int(address)), timeout
        )
    except Exception as exc:
        return f"device lookup failed: {exc}"
    if device is None:
        return "no cached device object"
    steps.append("device")
    try:
        session = await asyncio.wait_for(
            GattSession.from_device_id_async(device.bluetooth_device_id), timeout
        )
    except Exception as exc:
        steps.append(f"session lookup failed: {exc}")
    if session is not None:
        try:
            session.maintain_connection = False
            steps.append("maintain_connection=False")
        except Exception as exc:
            steps.append(f"maintain failed: {exc}")
        try:
            session.close()
            steps.append("session closed")
        except Exception as exc:
            steps.append(f"session close failed: {exc}")
    try:
        device.close()
        steps.append("device closed")
    except Exception as exc:
        steps.append(f"device close failed: {exc}")
    return ", ".join(steps)


async def cycle_radio(
    off_seconds: float = RADIO_OFF_SECONDS, timeout: float = WINRT_CALL_TIMEOUT
) -> tuple[bool, str]:
    """把蓝牙 radio 关掉再打开。返回 `(是否成功, 说明)`。

    **本机（未打包的 Win32 程序）实测 `Radio.set_state_async(ON)` 返回
    `DENIED_BY_USER`** —— 程序没有改 radio 状态的权限。所以这条路径成功是
    意外之喜，失败是常态：调用方拿到 `False` 时用 `open_bluetooth_settings()`
    引导用户自己关一下蓝牙开关（关掉再打开等价于一次彻底重置）。
    """
    if not supported():
        return False, "unsupported"
    from winrt.windows.devices.radios import RadioAccessStatus, RadioState

    try:
        radio = await asyncio.wait_for(_bluetooth_radio(), timeout)
    except Exception as exc:
        return False, f"radio lookup failed: {exc}"
    if radio is None:
        return False, "no bluetooth radio"

    async def set_state(state, label: str) -> tuple[bool, str]:
        try:
            status = await asyncio.wait_for(radio.set_state_async(state), timeout + 4.0)
        except Exception as exc:
            return False, f"{label} failed: {exc}"
        try:
            name = RadioAccessStatus(status).name
        except Exception:
            name = str(status)
        return status == RadioAccessStatus.ALLOWED, f"{label}={name}"

    ok, detail = await set_state(RadioState.OFF, "off")
    if not ok:
        return False, detail
    await asyncio.sleep(off_seconds)
    ok, detail_on = await set_state(RadioState.ON, "on")
    detail = f"{detail}, {detail_on}"
    if not ok:
        return False, detail
    ready, state = await wait_radio_ready(timeout=timeout)
    return ready, f"{detail}, radio={state}"


def open_bluetooth_settings() -> bool:
    """打开 Windows「蓝牙和其他设备」设置页（用户手动关一下蓝牙用）。"""
    try:
        if sys.platform != "win32":
            return False
        # `os.startfile` 走 ShellExecute，不需要额外依赖
        os.startfile("ms-settings:bluetooth")  # type: ignore[attr-defined]
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(  # noqa: S603,S607 - 固定命令，无用户输入
            ["cmd", "/c", "start", "", "ms-settings:bluetooth"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        log.debug("open_bluetooth_settings failed: %s", exc)
        return False


__all__ = [
    "RADIO_READY_TIMEOUT",
    "cycle_radio",
    "open_bluetooth_settings",
    "radio_state",
    "release_gatt_session",
    "supported",
    "wait_radio_ready",
]
