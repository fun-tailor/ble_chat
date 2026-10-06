# 05 · 错误与 HRESULT：pywinrt 抛出的到底长什么样

## 1. 真实形状（本机实测）

WinRT 失败统一被 pywinrt 包成 **`OSError`**，三处信息**可能分别存放**：

```python
>>> w = DataWriter(); w.write_bytes(b"x"); w.close()
>>> w.write_bytes(b"y")
OSError(22, 'The object has been closed.', None, -2147483629)
>>> e.winerror
-2147483629          # ← 有符号 HRESULT
>>> e.errno
22                   # ← 不是 HRESULT！是 errno 映射（EINVAL）
>>> str(e)
'[WinError -2147483629] The object has been closed.'
>>> '80000013' in str(e)
False                # ← 文本里没有十六进制
```

结论表：

| 字段 | 值（RO_E_CLOSED 例） | 说明 |
| --- | --- | --- |
| `e.winerror` | `-2147483629` | **有符号** HRESULT；`0x80000013` |
| `e.errno` | `22` | Win32 错误码映射，**不等于** HRESULT |
| `str(e)` | `[WinError -2147483629] …` | 只有十进制，**没有** `0x80000013` |

常见 HRESULT（本项目遇到的）：

| 名称 | 无符号 | 有符号 | 含义 |
| --- | --- | --- | --- |
| `RO_E_CLOSED` | `0x80000013` | `-2147483629` | 对象已被关闭 |
| `E_ILLEGAL_METHOD_CALL` | `0x8000000E` | `-2147483634` | 事件返回后再取 deferral/session |
| `E_INVALIDARG` | `0x80070057` | `-2147024809` | 参数错（`read_bytes` 长度不符等） |

## 2. `is_closed_error` —— 本项目当前实现与**已知缺陷**

```python
# blechat/ble/server.py:44-61
RO_E_CLOSED = 0x80000013                 # 无符号
RO_E_CLOSED_SIGNED = -2147483629         # 有符号

def is_closed_error(exc):
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == RO_E_CLOSED:   # ❌ 实测永远 False
            return True
        if exc.errno in (RO_E_CLOSED, RO_E_CLOSED_SIGNED):  # ❌ errno 是 22
            return True
    text = str(exc)
    return "0x80000013" in text or "80000013" in text        # ❌ 文本里没有
```

**实测（`python docs/winrt/verify_notes.py`）：**

```
shape: OSError(22, 'The object has been closed.', None, -2147483629)
is_closed_error = False     ← 漏判
is_link_dead    = False     ← 跟着漏判
```

为什么**单测是绿的**（`tests/test_regressions.py:40-42`）：

```python
assert is_closed_error(OSError(0x80000013, "The object has been closed."))
# 两个参数的构造 → errno=0x80000013, winerror=None  ← 走的是 errno 分支
```

即：**测试构造的 OSError 形状 ≠ pywinrt 实际抛的形状**，于是测试通过、线上漏判。

### 正确判定

```python
def is_closed_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        we = getattr(exc, "winerror", None)
        if we is not None and (we & 0xFFFFFFFF) == RO_E_CLOSED:
            return True
        if exc.errno in (RO_E_CLOSED, RO_E_CLOSED_SIGNED):
            return True
    text = str(exc)
    return (
        "0x80000013" in text
        or "80000013" in text
        or "-2147483629" in text
        or "has been closed" in text.lower()
    )
```

> `winerror & 0xFFFFFFFF` 是把有符号统一到无符号的最稳写法，
> 兼容 `winerror` 为有符号、无符号、或 `None` 三种情况。

## 3. `is_link_dead` —— "链路已死"的综合判定

```python
# blechat/ble/server.py:64-85
def is_link_dead(exc) -> bool:
    if isinstance(exc, PeerGone): return True
    if is_closed_error(exc): return True          # ← 依赖 §2，当前漏
    text = str(exc).lower()
    if "gatt protocol error" in text: return True # bleak: GATT Protocol Error: Unlikely Error
    if "not connected" in text: return True
    if "unexpected" in text and "error" in text: return True
    if "device" in text and "connect" in text and "fail" in text: return True
    if "unreachable" in text or "host is down" in text: return True
    return False
```

为什么需要这么"宽"：**早期只认 `RO_E_CLOSED`**，结果
`GATT Protocol Error: Unlikely Error`（bleak 的 `BleakGATTProtocolError`，
`bleak/exc.py:324-337`，code `0x0E = UNLIKELY_ERROR`）只被 WARNING、会话保持 READY
→ 永远重连不上。这是 `TESTING.md` 里 dev 实测过的坑。

调用点：

| 位置 | 语义 |
| --- | --- |
| `session.py:220` `_send_ctrl_now` | 控制帧发送失败且链路死 → 关会话 |
| `session.py:624` `_do_send` | 数据帧发送失败且链路死 → 关会话 |
| `server.py:400/410` `send_ctrl`/`send_data` | 把 `OSError` 转成 `PeerGone` 抛出 |

配套的**写超时**（`session.py:28-30`）：

> WinRT 在链路被服务器踢掉时可能**永远不返回** → 必须 `asyncio.wait_for(8s)`，
> 否则 PING/`_pump` 挂住 → 会话永远停在 READY → 重连不上。

## 4. bleak 侧的异常类型

| 异常 | 触发 | 文本特征 |
| --- | --- | --- |
| `BleakError` | 通用 | `not connected`、`Unexpected watcher status: …` |
| `BleakGATTProtocolError` | ATT 层错误码 | `GATT Protocol Error: Unlikely Error` |
| `BleakDeviceNotFoundError` | 按地址找不到设备 | `Device with address … was not found.` |
| `BleakError` (GattCommunicationStatus) | `UNREACHABLE` / `PROTOCOL_ERROR` / `ACCESS_DENIED` | bleak `backends/winrt/client.py:111-134` |

`GattCommunicationStatus`：`SUCCESS=0 UNREACHABLE=1 PROTOCOL_ERROR=2 ACCESS_DENIED=3`。

## 5. 排查套路

1. **先打印三件套**：`repr(e)`、`e.winerror`、`e.errno`，别只看 `str(e)`。
2. **把 `winerror` 规范化**：`winerror & 0xFFFFFFFF` 再和常量比。
3. **不要只靠文本匹配** —— 本地化系统上消息可能不是英文（`is_closed_error`
   的注释 `server.py:52-54` 就是为这个留的三路判定）。
4. **单测要造真实形状**：
   `OSError(22, "…", None, -2147483629)`，不是 `OSError(0x80000013, "…")`。
