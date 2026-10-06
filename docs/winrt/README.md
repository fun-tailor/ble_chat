# WinRT / Windows 知识库（blechat 实战笔记）

本目录记录本项目在 Windows 上做 BLE GATT 时**真正踩过并验证过**的 WinRT 与 OS 层知识。
所有结论都可在本机复核：`python docs/winrt/verify_notes.py`。

> 环境：Windows 10/11，Python 3.11.3，pywinrt（Python/WinRT）**3.2.1**，bleak **3.0.2**。

## 目录

| 文件 | 内容 |
| --- | --- |
| [01-packages-and-imports.md](01-packages-and-imports.md) | pip 包名 ↔ import 路径、静态方法在元类上、枚举、apartment |
| [02-gatt-server.md](02-gatt-server.md) | Host 侧 `GattServiceProvider` 全流程（本项目 Server 就是这么写的） |
| [03-async-events-threads.md](03-async-events-threads.md) | `await xxx_async`、事件回调线程、`get_deferral()` 时机 |
| [04-buffers-and-data.md](04-buffers-and-data.md) | `DataWriter`/`DataReader`/`IBuffer`、本机 MAC、UUID |
| [05-errors-and-hresults.md](05-errors-and-hresults.md) | `OSError` 的真实形状、`RO_E_CLOSED`、`is_closed_error`/`is_link_dead` |
| [06-object-lifetime.md](06-object-lifetime.md) | WinRT 对象被关掉后的行为、`GattSession` 身份（`device_id` 坑） |
| [07-windows-os-behavior.md](07-windows-os-behavior.md) | 广播状态机、`ABORTED`、睡眠/电源管理、定位权限、配对、MTU |
| [08-client-via-bleak.md](08-client-via-bleak.md) | 客户端侧：bleak 的 WinRT 后端、扫描、按 MAC 直连、`maintain_connection` |
| [09-win32-interop.md](09-win32-interop.md) | DPAPI、注册表、DirectWrite 字体坑（非 WinRT 但同属 Windows 层） |
| [10-known-issues.md](10-known-issues.md) | **读代码时发现的 2 个真实问题**（附复现脚本） |
| [verify_notes.py](verify_notes.py) | 复核脚本，可重复执行 |

## 一分钟速查

- **只有 `blechat/ble/server.py` 直接 import winrt**；客户端走 bleak（bleak 自己依赖 winrt-*）。
- pywinrt 抛的错误是 `OSError`，**HRESULT 以有符号形式放在 `winerror`**：
  `RO_E_CLOSED` 实际是 `winerror = -2147483629`，不是 `0x80000013`。
- 事件回调**不在 asyncio 线程**，必须 `loop.call_soon_threadsafe()` 回主线程。
- `get_deferral()` / `args.session` / `args.get_request_async()` 必须在事件回调**同步**取，否则 `E_ILLEGAL_METHOD_CALL (0x8000000E)`。
- `GattSession.device_id` 是**新包装对象**，`str()` 每次都不同 → 取 `.id`。
- 有对端连着时 `advertisement_status` 可能是 `ABORTED`，**属于正常**，不要据此 stop/start 广播；
  但"有会话"本身**不等于**健康 —— 对端机器睡眠后 WinRT 会留下**僵尸会话**
  （判据：对端 60s 内说过话）。
- 睡眠唤醒后，**客户端**那条链路会变成"缓存链路"：`connect` 只用 1~2s 就"成功"、
  GATT 表却是旧的/空的。根因是 WinRT 残留的 `GattSession(maintain_connection=True)`，
  `gc.collect()` 清不掉 —— 必须 `maintain_connection = False` + 关会话/设备。
- `Radio.set_state_async` 在未打包的 Win32 上返回 **`DENIED_BY_USER`**（改不了蓝牙开关）；
  `BluetoothAdapter.radio` / `Radio.request_access_async` 在本构建里不存在。只能读状态。
- `GattServiceProvider` **可以在同一进程里反复重建**（stop + 等 0.5s + `create_async`），
  所以"唤醒后服务注册丢了"能自愈；第一次 `start_advertising` 可能报
  `E_ILLEGAL_METHOD_CALL (-2147483634)`，重试即可。
- Windows 会对空闲 LE 链路做电源管理 → 20s PING 保活（`session.py:27`）。
- 扫描要定位权限；**按 MAC 直连不需要**（bleak 传 `BLEDevice` 时跳过扫描）；
  bleak 的 WinRT 扫描器**只吃广播事件**（没有设备表缓存回退）——
  扫到就是真的在广播。
