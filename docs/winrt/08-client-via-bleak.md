# 08 · 客户端侧：bleak 的 WinRT 后端

本项目**不直接写 winrt 客户端代码**，全部走 bleak 3.0.2。但 bleak 内部就是
WinRT，理解它等于理解另一半。

## 1. bleak 在 Windows 上的结构

```
bleak/backends/winrt/
  client.py     ← BleakClientWinRT（GattSession / BluetoothLEDevice）
  scanner.py    ← BleakScannerWinRT（BluetoothLEAdvertisementWatcher）
  util.py       ← ctypes 定时器 + hresult 检查
bleak/exc.py    ← BleakError / BleakGATTProtocolError / BleakDeviceNotFoundError
```

bleak 内部 import 的 winrt 命名空间（`grep '^\s*(from|import)\s+winrt'`）：

```
client.py:  winrt.system.Object
            winrt.windows.devices.bluetooth
            winrt.windows.devices.bluetooth.genericattributeprofile
            winrt.windows.devices.enumeration
            winrt.windows.foundation
            winrt.windows.storage.streams.Buffer
scanner.py: winrt.windows.devices.bluetooth.BluetoothAdapter
            winrt.windows.devices.bluetooth.advertisement
            winrt.windows.devices.radios.RadioState
            winrt.windows.foundation.EventRegistrationToken
```

## 2. 本项目用到的 bleak API（`blechat/ble/client.py`）

| 调用 | 位置 | 备注 |
| --- | --- | --- |
| `BleakScanner.discover(timeout, return_adv=True)` | `client.py:35` | 返回 `{address: (BLEDevice, AdvertisementData)}` |
| `BLEDevice(address, name, details)` | `client.py:49` | **构造新对象绕过扫描** |
| `BleakClient(device, disconnected_callback=..., timeout=...)` | `client.py:105-109` | |
| `await client.connect(timeout=12)` | `client.py:110` | |
| `client.mtu_size` | `client.py:112` | int，`None` → 23 |
| `await client.start_notify(uuid, cb)` | `client.py:113-114` | 订阅 TX / CTRL |
| `await client.write_gatt_char(uuid, data, response=...)` | `client.py:120/127/132` | |
| `await client.pair()` | `client.py:138` | 受 `system_pairing` 控制 |
| `await client.disconnect()` | `client.py:149` | |
| `client.services` | `client.py:158` | 用于直连后校验服务 UUID |
| `disconnected_callback` | `client.py:98-103` | 断链入口 |

## 3. 两条通知回调 + 断链回调都要回主线程

```python
# client.py:84-103
def dispatch(fn, *args):
    def run(): fn(*args)
    if threading.get_ident() == self._loop_thread:
        run()
    else:
        loop.call_soon_threadsafe(run)      # ← 与 Host 侧 _schedule 同一模式

def handle_notify(char, data):
    cb = self.on_data if str(char.uuid).lower() == CHAR_TX else self.on_ctrl
    if cb: dispatch(cb, bytes(data))

def handle_disconnect(_client=None):
    if self._closed: return                 # 幂等：close() 会先置位
    self._closed = True
    if self.on_closed: dispatch(self.on_closed, "disconnected")
```

三处细节：

1. **`_closed` 幂等闸门** —— 主动 `close()` 和对端掉线可能同时来，
   只发一次 `on_closed`。
2. **UUID 判方向用 `.lower()`**（`client.py:94`），Windows 给的 UUID 大小写不稳定。
3. `bleak` 的 `disconnected_callback` 来自 WinRT 线程 → 必须 `call_soon_threadsafe`。

## 4. `write-without-response` 的降级

```python
# client.py:125-132
if self._write_no_resp:
    try:
        await self._client.write_gatt_char(CHAR_RX, data, response=False)
        return
    except BleakError:
        log.warning("write without response failed, falling back to write with response")
        self._write_no_resp = False
await self._client.write_gatt_char(CHAR_RX, data, response=True)
```

原因：部分 Windows 栈/部分设备对 `WRITE_WITHOUT_RESPONSE` 不支持或不稳定。
先试无响应（快），失败**永久降级**到有响应（可靠，但慢且占链路）。
Host 侧特征声明了 `WRITE | WRITE_WITHOUT_RESPONSE`（`server.py:203`），两边对得上。

> 注意：Host 的控制帧 `CHAR_CTRL` 用的是 `response=True`
> （`client.py:120`），控制帧**不降级** —— 必须确认送达。

## 5. 直连为什么不需要定位权限

```python
# client.py:47-49
def direct_device(address: str, name: str = "") -> BLEDevice:
    """按地址直连（绕过扫描，定位权限被拒时依然可用）。"""
    return BLEDevice(address, name or address, None)
```

bleak 侧（`backends/winrt/client.py:219-232`）：

```python
if self._device_info is None:
    device = await BleakScanner.find_device_by_address(...)   # ← 走扫描，需要权限
    ...
    self._device_info = args.bluetooth_address
```

而 `__init__` 里（`client.py:160-163`）：

```python
if isinstance(address_or_ble_device, BLEDevice):
    self._device_info = _address_to_int(address_or_ble_device.address)   # ← 直接给地址
else:
    self._device_info = None
```

**传 `BLEDevice` 对象 → `_device_info` 已有值 → `connect()` 里那段扫描被跳过**，
于是 `BluetoothLEDevice.FromBluetoothAddressAsync(address)` 直接按地址连，
**不经过 `BluetoothLEAdvertisementWatcher`，不查定位权限**。

这就是 `TESTING.md` §8「关掉定位权限 → 扫描为空 → 手动填 MAC → 直连成功」能成立的机制。

代价：`BLEDevice.details=None`，拿不到广播数据（服务 UUID 列表等）。
所以本项目在 `connect()` 之后补了 `services_ok()`（`client.py:153-163`）——
**直连可能连到别的设备**，必须验证服务 UUID。

## 6. 扫描结果怎么排序

```python
# client.py:43
out.sort(key=lambda d: (not d.match_service, not bool(d.name), d.name))
```

优先级：**播了本服务 UUID > 有名字 > 名字字典序**。
`FoundDevice.match_service`（`client.py:26-28`）比对 `SERVICE_UUID`，
同样要 `.lower()`（`client.py:28`）。

## 7. bleak 的 watcher 状态轮询（了解即可）

bleak `scanner.py:305-313`：

```python
while self.watcher.status == BluetoothLEAdvertisementWatcherStatus.CREATED:  # 等启动
    ...
if self.watcher.status == BluetoothLEAdvertisementWatcherStatus.ABORTED:
    ...   # 通常 = 权限被拒 / 适配器不可用
if self.watcher.status != BluetoothLEAdvertisementWatcherStatus.STARTED:
    raise BleakError(f"Unexpected watcher status: {self.watcher.status.name}")
```

> 注释原文：*"no events for status changes, so we have to poll :-("* ——
> 这就是为什么**定位权限被拒时扫描不报错、只是空**：watcher 起来了但拿不到数据。

## 8. `BleakClientWinRT.connect()` 内部（实读源码，排障用）

`backends/winrt/client.py` 里这几条决定了我们看到的日志：

| 事实 | 行号 | 对排查的意义 |
| --- | --- | --- |
| `BLEDevice` → `_device_info = _address_to_int(address)`，连接时直接 `from_bluetooth_address_async` | 160-163 / 199 | **不会再扫一次**；设备表里没有这个地址就挂在那儿 |
| 整段（建 requester → 建 session → 取服务 → 等 session ACTIVE）都包在 `async with timeout(timeout)` 里 | 384-436 | 超时表现为 `TimeoutError: (空文本) <- CancelledError` |
| `self._session.maintain_connection = True` | 366 | **关会话前不会置回 False** —— 唤醒后"缓存链路"的根因 |
| `use_cached_services=False` → `get_gatt_services_…(UNCACHED)` | 371-376 | 现场发现；真连一次在这台机器上 ~4.8s，**1~2s 就返回必然是缓存** |
| `_get_services()` 对 `UNREACHABLE` 重试 10 次、每次等 1s | 686-719 | 所以"连不上"有时会白等 10s+ |
| `disconnect()`：`if self.services is None: return`（**直接返回**） | 453-458 | 半连接状态**不会**关 `_session`/`_requester`；看到泄漏的会话先查这里 |
| `disconnect()` 里 `await asyncio.sleep(0.1)`（"GattDeviceService.Close() 有时会挂死"） | 481-484 | `client disposed in ~110ms` 就是这么来的，正常 |

> 因此本项目在 `BleakTransport.close()` 里总是**先 `self._client = None` 再
> `disconnect()`**，并在 `connect()` 里"先登记再连"，保证失败路径也一定 dispose。

