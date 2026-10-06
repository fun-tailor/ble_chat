# 02 · GATT Server（Host 侧）全流程

Windows 上做 GATT **server** 只有一条路：`GattServiceProvider`。
本项目 `blechat/ble/server.py` 就是这条路径的完整实现，下面按"API 顺序"重讲一遍。

## 1. 建服务

```python
result = await GattServiceProvider.create_async(UUID_SERVICE)   # server.py:195
if result.error != 0:
    raise RuntimeError(...)                                     # server.py:196-197
provider = result.service_provider                              # server.py:198
service = provider.service                                      # server.py:200
```

要点：

- `create_async` 是**静态**方法（挂在元类上，见 `01-packages-and-imports.md` §2）。
- 返回的是 `GattServiceProviderResult`，成员 **`error` + `service_provider`**
  （`error == 0` 才成功；非 0 是 Win32/HRESULT 码）。
- `GattServiceProviderResult` 自身只有 `as_ / error / service_provider` 三个成员。

## 2. 建特征（Characteristic）

```python
params = GattLocalCharacteristicParameters()          # server.py:207
params.characteristic_properties = props              # IntFlag 位或
params.read_protection_level  = GattProtectionLevel.PLAIN
params.write_protection_level = GattProtectionLevel.PLAIN
cres = await service.create_characteristic_async(uuid_, params)   # server.py:211
if cres.error != 0: raise ...
char = cres.characteristic                            # server.py:214
```

- `service` 的类型是 `GattLocalService`，实例方法只有：
  `as_ / characteristics / create_characteristic_async / uuid`。
- `GattLocalCharacteristicResult` 成员：`as_ / characteristic / error`。
- `GattLocalCharacteristicParameters` 成员：
  `as_ / characteristic_properties / presentation_formats / read_protection_level /
   static_value / user_description / write_protection_level`。
- `GattProtectionLevel`：`PLAIN=0`（本项目用它，避免 Windows 强制加密配对）。

本项目的三个特征（`blechat/ble/uuid_defs.py:7-15`）：

| UUID | 属性 | 用途 |
| --- | --- | --- |
| `0000a000-…` service | — | 服务 |
| `0000a001-…` RX | `WRITE \| WRITE_WITHOUT_RESPONSE` | client → host 数据/控制 |
| `0000a002-…` TX | `INDICATE` | host → client（**indicate 不是 notify**） |
| `0000a003-…` CTRL | `WRITE \| WRITE_WITHOUT_RESPONSE \| NOTIFY` | 控制帧 |

> 用 `INDICATE`（有 ATT 层确认）而不是 `NOTIFY`，是为了让 host 侧能感知对端是否还在。

## 3. 挂事件

`GattLocalCharacteristic` 的可挂事件（`dir()` 实测）：

```
add_read_requested / remove_read_requested
add_write_requested / remove_write_requested
add_subscribed_clients_changed / remove_subscribed_clients_changed
```

`GattServiceProvider` 的可挂事件：

```
add_advertisement_status_changed / remove_advertisement_status_changed
```

本项目用法（`server.py:216-221`）：

```python
char.add_write_requested(self._make_write_handler("rx"))
char.add_write_requested(self._make_write_handler("ctrl"))
for char in self._chars.values():
    char.add_subscribed_clients_changed(self._on_subscribed_changed)
provider.add_advertisement_status_changed(self._on_adv_status)
```

### 3.1 写事件回调的**正确姿势**（最容易踩）

```python
def handler(sender, args) -> None:          # server.py:271
    deferral = None
    try:
        deferral = args.get_deferral()      # ← 必须同步取
        session = args.session               # ← 必须同步取
        peer_id = session_peer_id(session)
        request_op = args.get_request_async()   # ← 必须同步取
    except Exception as exc:
        ...                                  # server.py:280-283
        return

    async def handle():
        try:
            request = await request_op        # 只有 await 可以推迟
            request.respond()                 # 先应答，避免客户端死等
            data = from_buffer(request.value)
            ...
        finally:
            self._release_deferral(deferral)  # server.py:306

    self._schedule(lambda: asyncio.ensure_future(handle()))   # server.py:309
```

三条硬规则：

1. **`get_deferral()` / `session` / `get_request_async()` 必须在回调栈内同步拿到。**
   事件返回后再取会抛 `E_ILLEGAL_METHOD_CALL (0x8000000E)`
   （见 `server.py:272-273` 注释）。
2. **只有 `await request_op` 可以放到异步任务里**；deferral 要么同步释放，
   要么在 `finally` 里释放，**绝不能漏**，否则 Windows 会认为这次写没处理完。
3. `request.respond()` **先调**再解析 —— 解析失败时客户端还挂着（`server.py:292`）。

`GattWriteRequestedEventArgs` 成员：`as_ / get_deferral / get_request_async / session`。
`GattReadRequestedEventArgs` 成员：`as_ / get_deferral / get_request_async / session`（完全对称）。
`GattWriteRequest` 成员：`add_state_changed / as_ / offset / option / remove_state_changed /
respond / respond_with_protocol_error / state / value`。

### 3.2 订阅变化事件

```python
def _on_subscribed_changed(self, sender, args) -> None:   # server.py:363
    uuid_str = str(sender.uuid).lower()       # sender 是 GattLocalCharacteristic
    for sub in sender.subscribed_clients:
        peer_id = session_peer_id(sub.session)
        self._track_session(sub.session, peer_id)
```

- `sender` 是特征本身（不是 provider）。
- `subscribed_clients` 是 `GattSubscribedClient` 集合，
  成员：`as_ / max_notification_size / session` + `add_/remove_max_notification_size_changed`。

## 4. 广播

```python
adv = GattServiceProviderAdvertisingParameters()   # server.py:228
adv.is_connectable = True
adv.is_discoverable = True
provider.start_advertising_with_parameters(adv)    # server.py:233
```

- `GattServiceProviderAdvertisingParameters` 成员：
  `as_ / is_connectable / is_discoverable / service_data /
   use_low_energy_uncoded1_m_phy_as_secondary_phy / use_low_energy_uncoded2_m_phy_as_secondary_phy`。
- 也有不带参数的 `provider.start_advertising()`。
- `provider` 可用成员全集：`add_advertisement_status_changed / advertisement_status /
  as_ / remove_advertisement_status_changed / service / start_advertising /
  start_advertising_with_parameters / stop_advertising / update_advertising_parameters`。

**启动可能不立刻成功** —— 本项目重试 `ADVERTISE_RETRY = 8` 次、每次等
`ADVERTISE_WAIT = 0.25s`（`uuid_defs.py:17-18`），轮询到
`STARTED` 或 `STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA` 才算成功（`server.py:231-244`）。

广播状态语义见 `07-windows-os-behavior.md`（**`ABORTED` 是重点**）。

## 5. 出站通知

```python
char, sub = self._subscribed(CHAR_TX, peer_id)                  # server.py:404
await char.notify_value_for_subscribed_client_async(to_buffer(data), sub)
```

- 方法全名 `notify_value_for_subscribed_client_async(buffer, sub)` ——
  **必须指定是哪个 subscribed client**，不是广播给所有人。
- 还有 `notify_value_async(buffer)`（发给所有订阅者，本项目不用）。
- `GattClientNotificationResult` 成员：`as_ / bytes_sent / protocol_error / status / subscribed_client`。
- **写之前要自查长度**：`sub.max_notification_size`（本项目 `_check_notify_size`，
  `server.py:414-427`），超了只 WARNING 不抛。

MTU/分片大小推导见 `server.py:429-451`：

```
peer_mtu  = max(23, sub.session.max_pdu_size)      # ATT MTU，默认 23
peer_chunk = max(8, min(max_notification_size or (mtu-3), mtu) - 3 - DATA_HEADER)
```

## 6. 关闭

```python
provider.stop_advertising()   # server.py:182 / 251
session.close()               # server.py:461（GattSession.close，同步方法）
```

`GattSession` 可用成员：`add_max_pdu_size_changed / add_session_status_changed / as_ /
can_maintain_connection / close / device_id / maintain_connection / max_pdu_size /
remove_max_pdu_size_changed / remove_session_status_changed / session_status`。
