# 04 · 缓冲区、地址与 UUID

## 1. `DataWriter` / `DataReader` / `IBuffer`

WinRT 不吃 `bytes`，只吃 `IBuffer`。转换封装在 `blechat/ble/server.py:88-98`：

```python
def to_buffer(data: bytes):
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()

def from_buffer(buf) -> bytes:
    r = DataReader.from_buffer(buf)
    out = bytearray(buf.length)
    r.read_bytes(out)
    return bytes(out)
```

要点：

- **`DataReader.from_buffer` 是静态方法**（在元类 `DataReader_Static` 上，
  `dir(DataReader)` 看不到 —— 见 `01-packages-and-imports.md` §2）。
- `detach_buffer()` 把写入区**转移**出来；`DataWriter` 之后不能再写。
- 读要用 `buf.length`（**不是** `capacity`）开 `bytearray`：
  实测 `DataWriter` 写 6 字节 → `length=6, capacity=136`（capacity 是分配粒度）。
- `read_bytes(out)` 要求 `len(out) == buf.length`，否则 WinRT 报参数错误。
- `IBuffer` 的 Python 成员只有 `as_ / length / capacity`。
- 用完可 `w.close()` / `r.close()` —— 关了再用会抛（见 `05-errors-and-hresults.md`）。

`DataWriter` 全成员：`as_ byte_order close detach_buffer detach_stream flush_async
measure_string store_async unicode_encoding unstored_buffer_length write_boolean
write_buffer write_buffer_range write_byte write_bytes write_date_time write_double
write_guid write_int16 write_int32 write_int64 write_single write_string write_time_span
write_uint16 write_uint32 write_uint64`

`DataReader` 全成员：`as_ byte_order close detach_buffer detach_stream
input_stream_options load_async read_boolean read_buffer read_byte read_bytes
read_date_time read_double read_guid read_int16 read_int32 read_int64 read_single
read_string read_time_span read_uint16 read_uint32 read_uint64 unconsumed_buffer_length
unicode_encoding`

## 2. 本机蓝牙地址（`adapter_address`）

```python
# blechat/ble/server.py:466-483
def _fmt_addr(value: int) -> str:
    if not value:
        return ""
    return ":".join(f"{(value >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))

async def adapter_address() -> str:
    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        return ""
    return _fmt_addr(int(adapter.bluetooth_address))
```

- `BluetoothAdapter.bluetooth_address` 是 **`UInt64`，大端 48 位**，
  格式化要 `reversed(range(6))` 从高字节取。
- 实测本机 `0x8cc6819cb97b` → `8C:C6:81:9C:B9:7B`。
- `get_default_async()` 在没有蓝牙时返回 `None`，**必须判空**。
- `BluetoothAdapter` 其它有用成员：
  `is_low_energy_supported / is_classic_supported / is_peripheral_role_supported /
   is_central_role_supported / is_extended_advertising_supported /
   is_advertisement_offload_supported / max_advertisement_data_length / device_id /
   get_radio_async / are_low_energy_secure_connections_supported`。
  本机实测：`LE=True classic=True periph=True central=True`。

## 3. UUID 书写规范

本项目（`blechat/ble/uuid_defs.py`）：

```python
SERVICE_UUID = "0000a000-0000-1000-8000-00805f9b34fb"
UUID_SERVICE = uuid.UUID(SERVICE_UUID)     # 传给 WinRT 用 uuid.UUID 对象
```

- **传给 WinRT API 用 `uuid.UUID` 对象**，不要传字符串（`server.py:195`、`server.py:211`）。
- **当 dict key 一律 `.lower()`**：`str(uuid_).lower()`
  （`server.py:214`、`server.py:365`、`client.py:94`）。Windows 侧 UUID 大小写不稳定，
  不小写化会导致 `subscribed_clients` 匹配失败。
- 16-bit 短 UUID 展开规则：`0000a000-0000-1000-8000-00805f9b34fb`
  = Bluetooth Base UUID + `0xA000`。
- bleak 侧读到的 `char.uuid` 是 `uuid.UUID`，同样要 `str(...).lower()` 比较
  （`client.py:94`）。

## 4. 传输方向约定（避免和 ATT 命名混淆）

| 逻辑方向 | 特征 | ATT 属性 | 代码 |
| --- | --- | --- | --- |
| client → host | `CHAR_RX` (`a001`) | WRITE / WRITE_WITHOUT_RESPONSE | `server.py:216` 收、`client.py:127` 发 |
| host → client | `CHAR_TX` (`a002`) | INDICATE | `server.py:404` 发、`client.py:113` 订 |
| 双向控制 | `CHAR_CTRL` (`a003`) | WRITE + NOTIFY | 双方都读写 |

> 命名是**站在 host 视角**的 RX/TX。客户端代码里 `CHAR_RX` 是"往 host 写"、
> `CHAR_TX` 是"从 host 读"—— 排查方向问题先确认视角。
