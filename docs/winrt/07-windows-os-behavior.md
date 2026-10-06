# 07 · Windows OS 层行为（不是 API，是系统脾气）

这一章记录的都是**必须实测才知道**的 Windows 行为，每条都标了本项目怎么应对。

## 1. 广播状态机与 `ABORTED`

`GattServiceProvider.advertisement_status` 取值：

```
CREATED=0  STOPPED=1  STARTED=2  ABORTED=3  STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA=4
```

**核心坑：已有对端连上来时，Windows 可能把状态变成 `ABORTED`，这是正常的。**

如果照状态字面判断"广播挂了"，watchdog 会每 5s `stop_advertising()` 一次，
**把在线客户端踢掉**（表现为对端 `Unlikely Error` / `RO_E_CLOSED`、
server 端 UI 反复重建目标菜单）。

本项目应对（`server.py` 的 `advertising_ok` / `revive`）：

```python
# ① 状态是 STARTED 就直接算健康
if self.advertising_status in ("STARTED", "STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA"):
    return True
# ② 状态不正常时，"有会话"才勉强算健康 —— 但对端必须**最近说过话**
live = [pid for pid in self._sessions if self.idle_seconds(pid) < SESSION_IDLE_TRUST]  # 60s
if live:
    return True
```

> **第八轮补的坑**：以前这里是 `if self._sessions: return True`（有会话就算健康）。
> 对端机器**睡眠/崩溃**时 WinRT 可能既不回调 `session_status_changed` 也不改
> `subscribed_clients`，`_sessions` 里留下一条**僵尸会话** ⇒ `advertising_ok`
> 恒为真 ⇒ watchdog 永远不去恢复广播 ⇒ 对端唤醒后怎么都连不上，而 host 这边
> 看着一切正常。判据换成"对端 60s 内说过话"（两端各有 20s 一跳的 PING）。

配套的 `revive()` 冷却（`REVIVE_COOLDOWN = 30.0`）与 **hard 路径**：

```python
if self._sessions and not self.stale_sessions():
    return True                        # 真有对端：根本不动广播，动了就是踢人
if hard:                               # 本机睡眠唤醒后
    return await self.restart(reason="hard revive")   # ← 整段重建 provider
now = time.monotonic()
if now - self._last_revive < REVIVE_COOLDOWN:
    return self.advertising_ok         # 30s 冷却，避免反复 stop/start
```

**实测（`lab_server_restart.py`，本机单机即可复核）**：

```
start #1     -> advertising=STARTED
restart #1   -> ok=True advertising=STARTED      ← 同进程第二次 create_async 也能成
restart #2   -> ok=True advertising=STARTED
soft revive  -> ok=True advertising=STARTED
advertising status=ABORTED, retrying
start_advertising attempt 1 failed: [WinError -2147483634] A method was called at an unexpected time.
```

两条结论：
1. **`GattServiceProvider` 可以在同一个进程里反复重建**（旧的 stop 掉 + 等 0.5s 让
   COM 引用回收），所以"唤醒后服务注册丢了"是可以自愈的；
2. **第一次 `start_advertising` 可能报 `E_ILLEGAL_METHOD_CALL (-2147483634)`**
   （"A method was called at an unexpected time"），紧接着重试就成功 ——
   `_advertise()` 的重试循环不是摆设。

广播状态变化本身会触发 `advertisement_status_changed` 事件，本项目**只记日志、
不据此动作** —— 这是有意的。

## 2. 睡眠 / 唤醒：广播会悄悄停，**客户端的链路也会变成"缓存链路"**

Windows 睡眠唤醒后，GATT 广播常常**已经不在了**，但没有任何显式通知。
表现：本机看着"还在跑"，对端扫描不到。

客户端侧更隐蔽（第八轮实测的根因）：

* 进程里的 Python 对象**原样保留**，但底下的 GATT 会话/设备对象已经全变了；
* WinRT 残留的 `GattSession`（`maintain_connection=True`）让 Windows 认为
  "这条链路还有人在用" ⇒ 新的 `connect()` 走**缓存路径**：
  **1~2s 就"连上"了**，而 GATT 表是旧的/空的（真连一次要 ~4.8s）；
* **重启进程立刻正常** ⇒ 脏状态在栈这一侧；`gc.collect()` 丢引用**清不掉**它，
  因为 bleak 关会话时只是 `GattSession.close()`，**从不把
  `maintain_connection` 置回 False**。

本项目应对（`blechat/ble/radio.py` + `client.py`）：

```python
session.maintain_connection = False   # 明确告诉 Windows：没人再维持这条链路
session.close()
device.close()
```

加上"认出刚睡醒"（心跳之间墙钟跳 45s+，`power.StallDetector`）与"连接快得不像
真连过"（< 2.5s 且表里没本服务 ⇒ `hard_reset()`）。

## 3. 程序改不了蓝牙开关（Radio API 的权限）

"关掉再打开蓝牙适配器"是唤醒后卡死的最后手段，但**未打包的 Win32 程序没有权限**：

```python
from winrt.windows.devices.radios import Radio, RadioKind, RadioState, RadioAccessStatus
radio = next(r for r in await Radio.get_radios_async() if r.kind == RadioKind.BLUETOOTH)
# 只读没问题：state = 1 (ON) / 2 (OFF) / 3 (DISABLED)
await radio.set_state_async(RadioState.ON)   # 本机实测 -> RadioAccessStatus.DENIED_BY_USER (2)
```

* `Radio.request_access_async` 在 pywinrt **3.2.1 这个构建里根本没有**（静态方法没投影出来）；
* 只能**读** `radio.state`（用它做"唤醒后等 radio 回来"），改状态要走"打开
  `ms-settings:bluetooth` 让用户手动关一次"。

`TESTING.md` 的相关表格行：

| 现象 | 原因/处置 |
| --- | --- |
| 扫描列表永远为空 | 定位权限被拒 → 用「按地址直连」，或打开位置权限 |

## 3. 电源管理会掐掉空闲 LE 链路

Windows 对**空闲**的 BLE 连接做电源管理，一段时间没流量就断。
断开后会话仍显示 `READY`，下一次发送才报错 → 永远重连不上。

本项目应对：**20 秒 PING 保活**

```python
# blechat/session.py:27
KEEPALIVE_INTERVAL = 20.0  # READY 后的 PING 间隔（保活链路）
```

`PeerSession` 的保活循环在 `session.py:170-178`：
READY 后每 20s 发 `CtrlType.PING`（`protocol.py:47` `PING = 0x0A`），
`_send_ctrl_now()` 返回 False 就走 `_link_dead()`。

> Client 侧另有 `session.maintain_connection = True` 双保险（`06-object-lifetime.md` §4）。

## 4. 定位权限：扫描需要，直连不需要

Windows 把 BLE 扫描（`BluetoothLEAdvertisementWatcher`）归入**位置隐私**：
定位权限被拒时，扫描结果**恒为空**（不报错，就是空）。

- 设置 → 隐私和安全性 → 位置 → 打开，并允许"桌面应用"访问
  （Win10：设置 → 隐私 → 位置 → 允许桌面应用）。
- 本项目**支持按 MAC 直连绕过扫描**（`blechat/ble/client.py:47-49`
  `direct_device()`）—— 因为 bleak 在传入 `BLEDevice` 对象时会跳过
  `find_device_by_address` 扫描（bleak `backends/winrt/client.py:219-232`）。
- `TESTING.md` §8「按地址直连（定位权限被拒时）」就是这条的验收。

`PROTOCOL.md:206` 记录：`直连 | 无 | 支持按 MAC 直连（绕过扫描） | Windows 定位权限被拒时扫描恒为空`

## 5. 系统配对（`PairAsync`）在 Windows 上很脆

本机实测（写进 `config.py:60-62` 注释）：

> Windows 上 `PairAsync` 通常 **30s 后 `FAILED` 并把链路一起断掉**。

本项目应对：**`system_pairing: bool = False`** 默认关闭，
握手用应用层 PSK/HMAC，不依赖系统配对。

调用链：`app.py:832` `auto_pair=self.config.system_pairing` →
`session.py:387` `_do_pair()` → `client.py:134-142` `await self._client.pair()`。

`DevicePairingResultStatus` 里常见的失败值：
`AUTHENTICATION_FAILURE=9`、`AUTHENTICATION_TIMEOUT=7`、
`CONNECTION_REJECTED=4`、`FAILED=19`。

> bleak 注释：Windows 后端里 **unpair 也会导致断连**
> （`backends/winrt/client.py:636`）。

## 6. MTU / `max_notification_size`

- ATT 默认 MTU = **23**（本项目所有兜底都写 `max(23, …)`，
  `server.py:434/449`）。
- `GattSession.max_pdu_size` = 当前 MTU，连接后通常协商到 247/512。
- `GattSubscribedClient.max_notification_size` = 该订阅者能收的最大 notify/indicate 长度，
  与 MTU 不完全等价 → **两个都要看**（本项目 `peer_chunk()`，`server.py:439-451`）。
- 发送前自查长度：`_check_notify_size()`（`server.py:414-427`），超了 WARNING。
- `max_pdu_size` 会在连接过程中变化（`add_max_pdu_size_changed`），
  不要缓存成常数。

## 7. Radio / 适配器状态

`RadioState`：`UNKNOWN=0 ON=1 OFF=2 DISABLED=3`
（bleak 用它判断"蓝牙开关关了"，`backends/winrt/scanner.py:22`）。

**读得到、改不了**（本机实测，pywinrt 3.2.1）：

```python
RadioState:        UNKNOWN=0 ON=1 OFF=2 DISABLED=3
RadioAccessStatus: UNSPECIFIED=0 ALLOWED=1 DENIED_BY_USER=2 DENIED_BY_SYSTEM=3
bluetooth radio state=1 (ON)
await radio.set_state_async(RadioState.ON)   ->  DENIED_BY_USER
Radio.request_access_async                   ->  这个构建里**没有**（静态方法未投影）
```

所以"程序自己关一下蓝牙再打开"在未打包的 Win32 上**做不到**；本项目用它只做
两件事：读状态（`radio_state()`）、以及试一次（成功了算意外之喜，
`DENIED` 就打开 `ms-settings:bluetooth` 让用户手动关）。
`BluetoothAdapter.radio` 在本构建里也没有（`get_default_async()` 只给地址）。

扫描 watcher 自身也有状态（`BluetoothLEAdvertisementWatcherStatus`）：
`CREATED=0 STARTED=1 STOPPING=2 STOPPED=3 ABORTED=4` ——
bleak 在 `scanner.py:305-330` 里**轮询**它（注释："no events for status changes, so we have to poll"），
`ABORTED` 通常意味着权限被拒/适配器不可用。

> 另外：bleak 的 WinRT **扫描器只吃广播事件**，没有 `DeviceInformation`
> 缓存回退（`grep find_all_async backends/winrt/scanner.py` 无命中）——
> 所以"扫描里能看到设备"就说明**它此刻真的在广播**，不用怀疑是设备表里的旧条目。

## 8. 随机地址与广播数据

`BluetoothLEAdvertisementReceivedEventArgs` 成员：
`advertisement / advertisement_type / bluetooth_address / bluetooth_address_type /
is_anonymous / is_connectable / is_directed / is_scan_response / is_scannable /
primary_phy / raw_signal_strength_in_dbm / secondary_phy / timestamp /
transmit_power_in_dbm`。

- `bluetooth_address_type`：`PUBLIC=0 RANDOM=1 UNSPECIFIED=2` ——
  对端广播可能是**随机地址**，与配对时的地址不一定一致。
- `is_connectable` 为 False 的广播只是"可见"，连不上。

## 9. 速查：症状 → 根因 → 本项目对策

| 症状 | OS 层根因 | 对策位置 |
| --- | --- | --- |
| 扫描恒为空 | 定位权限被拒 | `client.py:47` 直连；`TESTING.md` §8 |
| 对端突然收不到通知 | 广播 `ABORTED` 被误判 → watchdog 踢人 | `server.py:145-161` |
| 唤醒后对端发现不了 | 广播被系统停掉 | `server.py:170-187` revive + 冷却 |
| 唤醒后**自己连不上**、`connect` 只用 1~2s 就"成功"但表是旧的/空的 | WinRT 残留 `GattSession(maintain_connection=True)` ⇒ 走缓存链路 | `radio.release_gatt_session()` + `client.hard_reset()`；`power.StallDetector` |
| 唤醒后 host 看着正常、对端却连不上 | 僵尸会话让 `advertising_ok` 恒真 ⇒ watchdog 不恢复广播 | `server.advertising_ok` 的 60s 活跃判据 + `revive(hard=True)` 重建 provider |
| 想"关掉再打开蓝牙"却做不到 | `Radio.set_state_async` → `DENIED_BY_USER` | `radio.cycle_radio()` 试一次 + `open_bluetooth_settings()`（Ctrl+R） |
| 长时间无流量后断链 | LE 电源管理 | `session.py:27` 20s PING |
| 握手能过但系统配对总失败 | `PairAsync` 30s FAILED + 断链 | `config.py:62` `system_pairing=False` |
| 会话卡在 READY 发不出去 | WinRT 写可能永不返回 | `session.py:30` `SEND_TIMEOUT=8s` |
| 对端 Unlikely Error 反复重连 | 广播 stop/start 太频繁 | `server.py:37` 30s 冷却 |
| 丢包下偶发 `TOO_MANY_RETRIES` | 一帧要走通需"数据帧 + 它的 ACK"都活下来 | `session.py:21` `MAX_RETRIES=5` |
