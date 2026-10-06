# 06 · 对象生命周期与身份

## 1. 对象被关掉之后：`RO_E_CLOSED`

WinRT 对象在**底层会话/连接结束**后被"关闭"，之后任何方法调用都抛
`RO_E_CLOSED`（`winerror = -2147483629`，见 `05-errors-and-hresults.md`）。

触发场景（本项目实测/推断）：

| 场景 | 谁被关 |
| --- | --- |
| 对端断开 / 被踢 | `GattSession`、`GattSubscribedClient` |
| `provider.stop_advertising()` 之后继续 notify | 服务端订阅条目 |
| `DataWriter.close()` / `DataReader.close()` 后继续用 | 显式关闭（复核脚本用的就是这个） |
| GATT server 重启（`GattServer.stop()`） | provider / chars / 所有 session |

**关键推论：`OSError` ≠ 立即崩，但必须当成"链路死"处理**，
否则会话停在 `READY`、UI 显示在线、实际发不出去 —— 这正是
`is_link_dead()` 存在的理由（`server.py:64`）。

### 竞态窗口

bleak 在 `max_pdu_size_changed_handler` 里明确写了这个坑
（bleak `backends/winrt/client.py:320-327`）：

> 这个事件可能在 GattSession **已经关闭之后**才从队列里出来，
> 此时读属性会抛 Windows 错误 —— 直接忽略即可。

含义：**事件里读 sender 属性也可能抛 `RO_E_CLOSED`**，
要包 `try/except OSError`。本项目 `_on_adv_status`（`server.py:373-381`）
对 `args.status` 的读取就是这么兜的。

## 2. 身份：`GattSession.device_id` 的坑（踩过）

```python
# blechat/ble/server.py:101-114
def session_peer_id(session) -> str:
    """GattSession → 稳定 peer_id。

    `GattSession.device_id` 返回的是 `BluetoothDeviceId` **对象**，每次访问都会
    生成新的包装对象，`str()` 得到的 repr（`<BluetoothDeviceId object at …>`）
    每次都不同 —— 直接当 peer_id 会导致同一次连接的每次写都新建会话、且
    `subscribed_clients` 匹配永远失败（服务端发不出任何通知）。
    这里取其 `.id` 字符串（设备接口路径，按设备稳定）。
    """
    dev = getattr(session, "device_id", "")
    ident = getattr(dev, "id", None)
    if ident:
        return str(ident)
    return str(dev)
```

要点：

- `BluetoothDeviceId` 成员：`as_ / id / is_classic_device / is_low_energy_device`。
- **永远用 `.id`**（字符串，形如 `\\?\BTHLEDevice#{…}`），不要用 `str(device_id_obj)`。
- 同一个 `device_id.id` 在**断开重连后可能复用** → 所以 `_track_session`
  要处理"同 peer 换了新 GattSession"的重绑（`server.py:329-334`），
  旧 session 的关闭事件必须**只认自己**（`server.py:347-348`），
  否则会把新会话误删。
- bleak 侧用的是 MAC 字符串当 peer id（`client.py:62` `self.peer_id = device.address`），
  与 Host 侧的 `device_id.id` **不是同一种东西** —— 这是设计上的差异，
  不是 bug：Host 面对的是 `GattSession`，Client 面对的是 `BLEDevice`。

## 3. 事件里"我是谁"的守卫模式

`_track_session` 里 `on_status` 的写法值得单独记（`server.py:336-354`）：

```python
def on_status(s, a) -> None:
    def fire() -> None:
        try:
            closed = int(a.session_status) != 0     # ← ⚠️ 见 §3.1
        except Exception:
            closed = True                            # 取不到 → 保守当关闭
        if not closed:
            if self.on_peer_connected:
                self.on_peer_connected(peer_id)
            return
        # 身份判断：只处理"我自己"的关闭事件
        if self._sessions.get(peer_id) is not session:   # ← 关键守卫
            return
        self._sessions.pop(peer_id, None)
        ...
    self._schedule(fire)
```

"闭包里记住 `session`，回来再比对是不是字典里那一个" —— 这是 WinRT 事件
**异步到达** + **对象可被替换**两个事实叠加后的必需写法。

### 3.1 ⚠️ `a.session_status` 不存在 —— 见 `10-known-issues.md`

`GattSessionStatusChangedEventArgs` 只有 `error` 和 `status`
（存根 `_winrt_…genericattributeprofile.pyi:1126-1132`），
**没有 `session_status`**（`session_status` 是 `GattSession` 的属性）。
所以 `int(a.session_status)` 每次都抛 `AttributeError` → 走 `except` → `closed=True`。
详见 `10-known-issues.md` 问题 1。

## 4. `maintain_connection`：谁负责保持连接

| 角色 | 机制 |
| --- | --- |
| Client | `session.maintain_connection = True`（bleak `client.py:366`）—— Windows 没有"显式 connect"，只有"这个程序持有的 GATT session" |
| Host | 无法设置（`GattSession.maintain_connection` 是给 server 侧用的对端会话），靠 **20s PING** 防电源管理断开（`session.py:27`、`session.py:170-178`） |

`GattSession` 相关成员：
`can_maintain_connection`（bool，设备是否支持）、`maintain_connection`（读写）、
`max_pdu_size`（ATT MTU）、`session_status`、`close()`。

## 5. 归纳：什么时候该"放弃并重连"

按 `is_link_dead()` 的判定，这几种情况必须关会话触发重连：

1. `PeerGone`（本项目自己抛的：订阅丢失 / 底层 notify 失败）
2. `RO_E_CLOSED`（WinRT 对象已销毁）
3. `GATT Protocol Error`（bleak 文本，通常是 `Unlikely Error` = 对端没了）
4. `not connected` / `unreachable` / `host is down`
5. 写操作超时（`SEND_TIMEOUT = 8s`，`session.py:30`）

**漏判的代价**：会话永远 `READY`、`_pump` 挂住、重连不上 —— dev 实测过的症状。
