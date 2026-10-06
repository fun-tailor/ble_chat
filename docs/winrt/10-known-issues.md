# 10 · 读代码时发现的 2 个真实问题（未修码）

以下是**读 `server.py` + 实测 pywinrt 行为时确认的缺陷**，目前代码未改。
复核方式：

```powershell
python docs/winrt/verify_notes.py     # 期望输出 ALL OK / EXIT=0
```

---

## 问题 1 · `server.py:339` 用了不存在的属性 `session_status`

### 现状

```python
# blechat/ble/server.py:336-345
def on_status(s, a) -> None:
    def fire() -> None:
        try:
            closed = int(a.session_status) != 0     # ← ⚠️
        except Exception:
            closed = True
        if not closed:
            if self.on_peer_connected:
                self.on_peer_connected(peer_id)
            return
        ...
```

### 事实

`a` 是 `GattSessionStatusChangedEventArgs`，其**只有 2 个属性**
（存根 `winrt\_winrt_windows_devices_bluetooth_genericattributeprofile.pyi:1126-1132`）：

```python
class GattSessionStatusChangedEventArgs(winrt.system.Object):
    @_property
    def error(self) -> BluetoothError: ...
    @_property
    def status(self) -> GattSessionStatus: ...
```

**没有 `session_status`** —— `session_status` 是 `GattSession` 的属性
（同文件 `:1093` 起）。`verify_notes.py` 实测：

```
arg has session_status? False
arg has status?         True
```

### 净效果（两层错误叠加）

1. `int(a.session_status)` **每次必抛 `AttributeError`** → 落进 `except Exception`
   → `closed = True` 恒真。
2. 于是 **`if not closed:` 分支（`on_peer_connected`）在 WinRT 侧是死代码** ——
   状态变更事件（含 ACTIVE）一律被当成"关闭"。
3. 即便改成 `a.status`，`int(...) != 0` 的判断**方向也反了**：
   `GattSessionStatus.CLOSED = 0`、`ACTIVE = 1`，所以
   `int(status) != 0` 的含义是"ACTIVE ⇒ 关闭"。正确写法是
   `closed = int(a.status) == 0`。

> 为什么没被测出来：`on_peer_connected` 在 `_track_session` 末尾
> （`server.py:360-361`）已经**无条件先调用一次**，所以 UI 上"对端已连接"
> 看起来正常；只有"断开后又恢复"这条路径受影响。dev 双机目前靠
> `_on_subscribed_changed` + 20s PING 维持表象。

### 修复建议

```python
def fire() -> None:
    try:
        closed = int(a.status) == 0        # CLOSED=0 → 关闭；ACTIVE=1 → 连接
    except Exception:
        closed = True                       # 取不到就保守当关闭
```

---

## 问题 2 · `is_closed_error()` 对**真实** pywinrt `RO_E_CLOSED` 漏判

### 现状

```python
# blechat/ble/server.py:49-61
RO_E_CLOSED = 0x80000013            # 2147483667（无符号）
RO_E_CLOSED_SIGNED = -2147483629    # 有符号

def is_closed_error(exc) -> bool:
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == RO_E_CLOSED:      # ← 无符号 vs 有符号
            return True
        if exc.errno in (RO_E_CLOSED, RO_E_CLOSED_SIGNED):     # ← errno 不是 HRESULT
            return True
    text = str(exc)
    return "0x80000013" in text or "80000013" in text          # ← 文本里没有 hex
```

### 实测（`verify_notes.py` 真机跑 `DataWriter.close()` 后再写）

```
shape: OSError(22, 'The object has been closed.', None, -2147483629)
e.winerror = -2147483629        ← 有符号
e.errno    = 22                 ← errno 是 Win32 映射（EINVAL），不是 HRESULT
str(e)     = '[WinError -2147483629] The object has been closed.'
'e.errno in (0x80000013, -2147483629)' -> False
'80000013 in str(e)'            -> False
is_closed_error(real) = False   ← ❌ 三个分支全 miss
is_link_dead(real)    = False   ← ❌ 跟着 miss
```

三条分支逐条失效原因：

| 分支 | 为什么 miss |
| --- | --- |
| `winerror == 0x80000013` | 实际是 `-2147483629`（有符号），永远不等 |
| `errno in (…)` | `errno = 22`，HRESULT 根本不在 `errno` 里 |
| `"80000013" in text` | pywinrt 的 `str()` 只有十进制 `-2147483629` |

### 为什么单测是绿的

```python
# tests/test_regressions.py:40-42
def test_is_closed_error_still_works():
    assert is_closed_error(OSError(0x80000013, "The object has been closed."))
    assert is_link_dead(OSError(0x80000013, "The object has been closed."))
```

`OSError(两个参数)` 的构造语义是 `OSError(errno, strerror)` →
**`errno = 0x80000013`，`winerror = None`**，走的是第二条 `errno` 分支 → 通过。

**即：测试造的 OSError 形状 ≠ pywinrt 实际抛的形状**，
属于"测了自己想测的，没测真机上会遇到的"。

### 连带影响

`is_link_dead()` 第一条判定就是 `is_closed_error(exc)`（`server.py:72`），
所以 WinRT 关闭类错误**不会被判为链路死** → 会话停 `READY` →
`_pump`/PING 挂住 → 上层永不重连（正是当年引入 `is_link_dead` 要解决的症状）。

实际能被兜住的只剩文本分支（`server.py:74-84`），而
`'[WinError -2147483629] The object has been closed.'`
**不匹配其中任何一条**（没有 `gatt protocol error` / `not connected` /
`unreachable`…）→ 彻底漏。

### 修复建议

```python
RO_E_CLOSED = 0x80000013
RO_E_CLOSED_SIGNED = -2147483629

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

要点：**`winerror & 0xFFFFFFFF`** 统一有符号/无符号，再和常量比。

### 同步要补的回归测试

```python
def test_is_closed_error_matches_real_pywinrt_shape():
    """真实形状：OSError(22, msg, None, -2147483629)（winerror 有符号、errno 是 22）。"""
    real = OSError(22, "The object has been closed.", None, -2147483629)
    assert is_closed_error(real)
    assert is_link_dead(real)
    # 兼容旧的两参数构造
    assert is_closed_error(OSError(0x80000013, "The object has been closed."))
```

---

## 影响面小结

| 问题 | 现场表现 | dev 是否已遇到 | 建议 |
| --- | --- | --- | --- |
| 1 `a.session_status` | `on_peer_connected` 在状态事件里永不触发；`closed` 恒真 | 表象被 `server.py:360` 的首连调用掩盖 | 修（`a.status` + `== 0`） |
| 2 `is_closed_error` 漏判 | WinRT 对象关闭时不重连、会话卡 READY | 未直接报（被文本分支/PING 掩盖） | 修 + 补真实形状测试 |

> 这两处都在 `blechat/ble/server.py`，改动量各 1–3 行，
> 不影响协议、不影响两机互通。**是否一并修由 dev 定。**
