# 设计说明 · 用户体验故事 · 排障指南

> 面向三个读者：① 接手代码的人（设计与决策）；② 提需求/测试的人（用户体验故事与验收）；
> ③ 真机报障的人（怎么开 DEBUG、看哪几行日志）。
>
> 配套文档：`SPEC.md`（需求与验收清单）、`PROTOCOL.md`（线格式，实现即权威）、
> `TESTING.md`（测试矩阵）。本文只讲**为什么这么改**和**出问题怎么定位**。

---

## 1. 系统一页图

```
                    ┌──────────────── 本进程（单进程，qasync 桥接）────────────────┐
                    │                                                            │
  PyQt6 UI  ◄──────►│  BleChatApp (app.py)                                       │
  (ui/)             │   · _sync_ui()  ← 所有 UI 状态的**唯一写入点**              │
                    │   · _watchdog_tick()  每 5s：广播掉了就 revive、client 挂了  │
                    │     就重启连接任务                                          │
                    │   · _client_loop()    退避重连（3→6→12→24→30s）              │
                    │   · _modal()          模态框只在"没有任务在册"时执行         │
                    └───────┬────────────────────────────────┬───────────────────┘
                            │                                │
              Host 角色     │                                │     Join 角色
                            ▼                                ▼
                 HostService (session.py)            PeerSession (session.py)
                 · 每个对端一条 PeerSession           · 一条链路一个会话
                 · 握手/分片/ACK/重传/保活            · 同左（角色对称）
                            │                                │
                            ▼                                ▼
                 GattServer  (ble/server.py)         BleakTransport (ble/client.py)
                 · WinRT GattServiceProvider          · bleak + WinRT 后端
                 · 广播 + GATT server + 订阅表         · 每次连接前重新解析设备
                            │                                │
                            └─────────── BLE (L1) ───────────┘
                                    之上：PSK → HKDF → AES-GCM (L2)
```

**两条不变量**（改代码时请守住）：

1. **UI 只读状态，不持有状态** —— 任何 UI 变化都经过 `_sync_ui()` →
   `app_state.derive_ui_state()` 这个纯函数重新算。增量改写是历史上
   "按钮永久灰 / 进度环卡半圈 / badge 与实际不符"的根源。
2. **`network_id` 是历史的唯一分区键** —— 它一旦换号，界面就会"看不到"旧行
   （行还在库里）。所有可能改变它的路径都必须落 `last_network_id changed: A -> B`
   日志，并配套 `remap_network()` 把旧行搬过去。

---

## 2. 本轮（第五轮）三个问题的根因与修复

### 问题 1 · server 端关闭后，client 再也连不上（server 重开也没用）

**现象**（dev 日志）

```
17:09:43 WARNING bleak.backends.winrt.client: 63:BE:06:35:76:33: unhandled services changed event
17:09:49 WARNING blechat.session: send ctrl 10 failed: [WinError -2147483629] 该对象已关闭。
17:09:52 WARNING blechat.app: join attempt failed: OSError: [WinError -2147418113] 灾难性故障
17:10:15 WARNING blechat.app: join attempt failed: TimeoutError: TimeoutError
```

**根因（两条，叠加）**

| # | 根因 | 为什么症状是"重开 server 也没用" |
|---|---|---|
| A | WinRT 后端**只认 Windows 蓝牙设备表里的地址**，而设备表只有**扫描过**才刷新。对端重启后 MAC 没变，但设备表里那条记录的句柄已经过期。 | 拿旧地址硬连恒 `E_FAIL`/超时。重开 server 只是让对端重新广播 —— client 这边**没有重新解析设备**，永远撞同一堵墙。 |
| B | `BleakTransport.connect()` 里 `self._client = client` 写在 `await client.connect()` **之后**。连接自己抛异常时 `self._client` 还是 `None`，`except BaseException: await self.close()` 关的是 `None`。 | 那个半连接的 `BleakClient` 没人 dispose，WinRT 的 requester/GATT 会话留在栈上。**每失败一次多留一份**，于是后面每一次连接都更快地 `灾难性故障` —— 越点越坏。 |

**修复**（`blechat/ble/client.py`）

```python
# A：每次连接前重新解析（app._client_loop 里 force=True）
await transport.ensure_device(name=name, force=True)   # → resolve_device_info()
#    先 BleakScanner.find_device_by_address（刷设备表）
#    扫不到（定位权限被拒/没广播）→ 退回 direct_device()，老行为不丢

# B：先登记再连，失败也一定 dispose
self._client = client          # ← 提前到此
try:
    await client.connect(timeout=timeout)
    ...
except BaseException:
    await self.close()          # ← 关的就是上面那个半连接对象
    self._note_failure()
    raise
```

外加两条**自愈**：

* 一个连接任务只建**一个** `BleakTransport`（以前每次重试都 new 一个，
  上一条半连接对象直接泄漏）；
* 连续失败 `PORT_RESET_AFTER = 3` 次 → `_reset_port()`：换掉设备对象
  （逼下一次重新解析）+ 强制 `gc.collect()` 逼 WinRT COM 包装析构。

日志证据：

```
DEBUG blechat.ble.client: device resolved 63:BE:... -> name='HOST' source=scan rssi=-55
DEBUG blechat.app: connect attempt=2 addr=63:BE:... device_source=scan fails=0 port_resets=0
WARNING blechat.ble.client: port reset #1 for 63:BE:... after 3 consecutive failures (gc=1234)
WARNING blechat.app: join attempt failed after 12.3s: OSError: [WinError -2147418113] 灾难性故障 [errno=22(0x00000016) winerror=-2147418113(0x80004005)]
```

> 最后那行是新增的 `describe_exception()`：中文系统上 `str(OSError)` 只有
> 「灾难性故障」，看不出 HRESULT。现在 errno/winerror 都补十六进制，
> 一眼能分清 `E_FAIL(0x80004005)` / `RO_E_CLOSED(0x80000013)`。

---

### 问题 2 · 双方断开后点"加入网络"，只有 server 单方显示已连接（client 按钮灰）

**现象**：client 端提示 `join attempt failed: TimeoutError`，但 server 端状态栏
显示 1 个连接、还能在目标下拉里看到对端。

**根因**：**半开链路（half-open link）**。Windows 把 LE 链路留在"已连接"状态，
而对端软件已经不在了（或从来没完成握手）：

* 这一侧 `notify_value_for_subscribed_client_async` / `write_gatt_char` 会
  **静默成功** —— 帧写进本地缓冲就算完成，既不抛错也不返回失败；
* 于是两端**都**停在 READY（或一方 READY、一方还在重试），
  而且**永远不会自己恢复**。老代码只判"我发得出去"，判不出这种情况。

**修复**：引入 `PONG(0x0C)` —— 收到 `PING` 必须回一帧，发送方据此确认链路
**双向**仍然可用。判定节奏（两端对称）：

```
每 20s 发一帧 PING
  收到 PONG          → 计数清零，重新计时
  期限过一轮         → miss += 1
  miss <= 2（宽限）  → 只记 DEBUG（对端可能只是忙/慢）
  miss 再连续到 3    → 判链路已死 → close() → 上层退避重连
```

实测：**约 125 秒**发现半开链路，提示「连接断开：对方已离线」。

**另外两处配套修复**：

* `GattServer.start()` / `revive()` 在**没有 READY 会话**时调 `sweep_stale()`
  清掉残留订阅与僵尸会话（刚启动/刚恢复广播时不可能有在线对端）；
* `GattServer._subscribed()` 优先挑 `session_status` 还活着的订阅 ——
  WinRT 的 `subscribed_clients` 里可能同时存在幽灵条目和新条目，
  老代码取"第一个匹配"可能正好挑中幽灵，通知就发不出去。

> **兼容性**：旧版本按 `PROTOCOL.md §7` 静默忽略未知类型 `PONG`，收到不会出错；
> 但它们不回 `PONG`，所以混用时这一侧不会主动判死（退回"只靠发送失败判定"的老行为）。
> 要拿到完整自愈能力需要**两端都是本版本**。

---

### 问题 3 · client 重启后历史"被清空"（timing 不定）

**根因**：`network_id` 换号。行都还在 `history.db` 里，只是**按错误的 id 查**。
三个换号入口：

| 入口 | 触发条件 | 后果 |
|---|---|---|
| `_finalize_first_join` 铸新 `uuid4()` | `networks.find_by_host()` 匹配不上（地址写法不同：`8C:C6:…` / `8c-c6:…` / WinRT 设备路径） | 新 id 下查不到旧行 |
| `_host_credentials` 铸新 `uuid4()` | `config.last_network_id` 在 `networks.json` 里找不到、`_find_own_network()` 也没命中 | 同上 |
| 直接按 MAC 加入 | 还没 `_finalize_first_join` 时用的是临时的 `ad-hoc:{地址}` | 消息先写进临时 id，之后要靠 `remap_network()` 搬家 |

**修复**

1. `config.addr_keys()` + `find_by_host()` 按**归一化 MAC 集合**比对（分隔符/大小写/
   设备路径都算同一台）→ 从源头减少铸新 id；
2. `_load_recent_history()` / `_open_history()` 都走
   `_pick_history_network_id()`，**逐个候选试，谁有行用谁**：
   `[调用方指定] → [库里最近写入的 id] → [last_network_id]`，
   全空才返回 `None`（真的空库）；
3. 所有换号都经过 `_remember_network_id()`，落盘并打
   `last_network_id changed: A -> B`；
4. 新增 `History.count_by_network()`，配合 DEBUG 日志一次说清"行在哪"：

```
DEBUG blechat.app: history pick: first=None last_network_id='stale-uuid' tried=['stale-uuid','ad-hoc:63:BE:06:35:76:33'] picked='ad-hoc:63:BE:06:35:76:33'
DEBUG blechat.app: history rows by network_id: {'ad-hoc:63:BE:06:35:76:33': 12, 'stale-uuid': 3} (total=15)
```

> **怎么一眼分清**：「历史被清空」只有两种可能 ——
> ① `rows by network_id` 非空 ⇒ 行还在，是**换号**问题（看 `last_network_id changed`）；
> ② 表为空 ⇒ 真的没有行（首次运行 / 被 3 天清理 / 写库失败，看 `history add failed`）。

---

## 2.5 本轮（第六轮）三个问题的根因与修复

> 上一轮的修法**引入了新的退化**：重启 client 也很难连上、UI 又卡死。
> 三个根因如下，都在本轮修掉。

### 问题 A · 重启 client 几乎连不上 —— 对端地址会变

**证据**（dev 日志）：同一个 Host 在不同时间点分别是
`6A:15:F4:A1:41:CB`、`48:0A:97:77:32:3F`、`57:69:77:64:10:35`；
成功那几次 `device_source=scan`，失败那几次全是 `device_source=direct`。

**根因**：Windows 为目标设备分配/轮换**对外地址**（隐私地址）。
`networks.json` 里存的是上一次那个地址，于是"按地址连接"恒失败：

```
device_source=direct  →  拿一个没有广播数据的空壳去连
                      →  WinRT 一直等 → 19.5s 后
                         TimeoutError: (空文本) <- CancelledError
```

**修复**：`resolve_peer()` —— 地址只是"当前"地址，**名字才是稳定身份**：

```
1) 按地址扫 3s（DEVICE_SCAN_TIMEOUT）        → 命中就用它（最常见、最快）
2) 没命中 → 按名字 + 服务 UUID 扫 8s         → 地址变了也能找回来
3) 还没命中 → 返回 "missing"，上层**跳过这一轮**
             （不再拿旧地址硬连，省掉一个 12s 超时）
4) 命中后把新地址写回 networks.json（`host_address`），下次重启快查即可命中
```

顺带修掉两个浪费：不再调用 `BleakScanner.find_device_by_address`
（它会自己 `async with BleakScanner(...)` 起**第二个** watcher，和调用方的扫描
叠加 —— 每次连接前白等一个 3s 超时）；`peer_id` 随地址更新（它变了但
`network_id` 不变，历史不受影响）。

### 问题 B · 正在进行的连接被反复掐掉 ⇒ 越连越连不上

**证据**：日志里反复出现
`join attempt failed after 19.5s: TimeoutError: (空文本) <- CancelledError`，
而且 `_client_started_at` 被不停重置。

**根因**：`CLIENT_HUNG_SECONDS = 90` 把"还没 READY"当成挂死。
一次尝试的正常成本是

```
3s 地址扫描 + 8s 兜底扫描 + 12s connect + 8s notify + 15s 握手 ≈ 46s
```

watchdog（每 5s）和 `_on_window_activated`（**每次窗口获得焦点**）都会据此
`_drop_client_task()` → `task.cancel()` → 重来。用户点一下别的窗口再点回来，
连接就永远走不完。

**修复**：

* 新增 `_connect_started_at` 与 `CONNECT_GRACE_SECONDS = 110`（≈2× 最坏成本）；
  只有超过宽限才算"真挂死"；
* `_on_window_activated` 改成**只在没有连接任务时**才重启，
  不再对正在进行的连接 `force=True`；
* 退避期间清掉 `_connect_started_at`（纯等待 ≠ 挂起）。

### 问题 C · UI 又卡死 —— 日志同步写盘 + 连接churn

**根因**：`RotatingFileHandler` 是**同步**写盘，而 logging 全局只有**一把锁**。
server 端的 GATT 写回调跑在 WinRT 线程上，GUI/asyncio 线程在别处 ——
一边写日志，另一边（含 Qt 事件循环）就得等锁；DEBUG 下**每个数据帧一行**，
锁竞争把事件循环拖停（"界面不 repaint、按钮不响应、没有任何报错"）。

**修复**：`AsyncFileHandler` —— `emit()` 只 `format()` + `put_nowait()`
（微秒级），磁盘 I/O 交给后台守护线程，并**批量**写（一次最多 256 条）。
队列满时退回同步写，**不丢证据**。

实测（8500 条 ≈ 2MB 传输的日志量）：

```
mode         total     avg/call     p99/call    max/call
sync        1.719s      0.2004ms     0.7987ms     2.818ms
async       0.234s      0.0277ms     0.0751ms      1.63ms
→ 总耗时 7.3x，单次调用 p99 抖动 10x↓
```

### 问题 D（小）· 发送按钮旁边的进度环卡住一直 visible

**根因**：`finish_progress()` == `show_progress(1, 1)` —— 它只负责**显示**满格，
不负责隐藏。而 `_fail_pending()` / `_on_progress()` 把它当"收尾"用；
紧接着 `_sync_ui()` 又算出 `busy=False` 调 `set_busy(False)` → `progress.reset()`，
两股力量时序一乱，环就留在屏幕上。

**修复**：删掉那两处 `finish_progress()`，收尾统一交给 `_sync_ui()` →
`set_busy()`：`_pending` 空了就 `reset()`（隐藏），没空就保持。

---

## 2.6 本轮（第七轮）四个问题

> dev 反馈：**重连已经稳定（每次重启后一定能连上）**。
> 剩下：上边缘拖拽闪退、sleep 唤醒后连不上、UI 又卡死、两个 UI 细节。

### 问题 A · 按住上边缘往上拖 → 进程闪退（最硬的一条）

```
TypeError: unsupported operand type(s) for |=: 'int' and 'Edge'
  blechat/ui/main_window.py, in _edge_at
```

**根因**：这个 PyQt6 构建里 `Qt.Edge` 是 `enum.Flag`：

* 和 Python `int` 做 `|=` 直接抛 `TypeError`；
* `int(Qt.Edge.TopEdge)` **也不行**（`int()` 不接受 `Edge`）。

拿纯 int 只有一条路：读 `.value`。而它在 `mouseMoveEvent` 里抛出 ⇒ 未捕获异常
⇒ 进程被带走（Qt 会打印 "Exceptions caught in Qt event loop"，但 `pythonw`
下那行只进 stderr，用户看到的就是**静默闪退**）。

**修复**（两层）：

1. `_edge_bits()` 统一读 `.value`，全流程只用纯 int；
2. **给四个鼠标处理函数装兜底** `_guarded`：异常吞掉 + 写日志 + `event.ignore()`。
   为什么不能在 `event()` 里兜 —— PyQt6 是**直接调用这些虚函数**的，
   覆写 `event()` 根本看不到里面的异常。

### 问题 B · sleep 唤醒后连不上

**证据**（dev 日志）：

```
21:53:55 gatt table incomplete (attempt 1/4): GATT 表里没有 BLE Chat 服务（缓存是旧的？）
21:54:00 gatt table incomplete (attempt 2/4): …     ← 每次只差 ~2s 就放弃
21:54:07 gatt table incomplete (attempt 3/4): …
21:54:14 gatt table incomplete (attempt 4/4): …
```

**根因**：睡眠唤醒后 Windows 手里的 GATT 缓存是旧的。老代码的重试节奏是
`1s / 2s / 3s` 且**只是再连一次同一个地址** —— 换汤不换药，4 次都用同一批
陈旧句柄去发现服务，当然次次拿到空表。

**修复**：连续 `TABLE_RESET_AFTER = 2` 次"表不完整"就 **`_reset_port()`**
（丢掉设备对象 + 强制 GC，逼下一条链路重新解析）+ 等
`TABLE_RESET_BACKOFF = 2.5s` 再试 —— 也就是"换一条新链路"，而不是
"在同一条链路上再问一次"。

### 问题 C · UI 又卡死（"连 cmd 消息也不再刷出"）

两条独立原因，各自都能把事件循环按住：

| # | 根因 | 修复 |
|---|---|---|
| C1 | `AsyncFileHandler` 队列满时**退回同步写盘** —— 调用方是 Qt/asyncio 事件线程，一次同步写要抢文件锁，把整个界面按在那里 | 队列满**只丢日志**（`dropped` 计数 + stderr 直写一行"丢了 N 条"），**绝不**在调用线程上碰磁盘 |
| C2 | 每收一帧就 `_send_ctrl(ACK)` spawn 一个任务；17 片的突发 = 17 个 WinRT 异步写排满事件循环，watchdog / PONG / GUI 全被挤到后面 | ACK 改成**登记 + 常驻 worker 合并发**（`_post_ack` / `_ack_worker`，用 `asyncio.Event` 唤醒，空闲时完全不占事件循环） |

实测（9000 字节 ≈ 19 片，内存 transport）：控制帧总量不变（ACK 6 + PROGRESS 1），
但**任务数**从"每帧一个"降到 1 个常驻 worker。

> 同源修复：`_pump` 里每个数据帧后加 `await asyncio.sleep(0)` ——
> 连续 `await` 在"同步回调"型 transport 上不会真正切走事件循环，
> 一整个发送窗口发完才第一次看到 ACK，于是出现假重传
> （`resend msg=… from 0 (retry 1)`）。

### 问题 D（小）· 两个 UI 细节

1. **"打开文件所在目录"显示带编号的名字**：磁盘上确实是 `{msg_id}-{原名}`
   （防重名互相覆盖），但用户看到的是原名。改成用资源管理器
   `/select,"<路径>"` **在目录里高亮那个文件**（目录里显示的原名不变）；
   高亮失败就退化成打开目录；文件已被清理时把**原文件名**复制到剪贴板。
2. **`我 22:10` 和气泡左对齐**：自己的气泡靠右，头部却左对齐，看着错位。
   头部改成和气泡**同侧**贴边（用一个 HBox + stretch 顶住，而不是给 QLabel
   设对齐 —— `_card` 是 `QSizePolicy.Maximum`，不占满宽度就看不出右对齐）。

### 问题 E · server 侧 `respond failed: The object has been committed.`

**不是错误**：`request.value` 这个 getter 会**隐式结束 deferral**
（WinRT 的 DataReader 语义），系统随即自动应答一次；之后我们再去 `respond()`
自然撞 "already committed"。它意味着数据已经收到，只是我们在多此一举。

**修复**：记 `responded` 状态，日志改成 `respond skipped (系统应已自动应答)`，
`recv` 行附带 `responded=` —— 别再让它看着像故障。

---

## 2.7 本轮（第八轮）· sleep 唤醒还是连不上（这次是 connected 状态睡的）

> dev 反馈：**重连已经稳定**；但 client 在 **connected 状态下睡眠**、唤醒后
> **还是连不上**，"点加入网络也没有"，重启 client 进程才恢复正常。

### 关键证据：耗时

```
08:22:05 INFO  using persisted PSK …                              ← 睡着前那条链路
08:22:07 WARN  gatt table incomplete (attempt 1/4, streak=1)       ← 距上一条只有 2s
08:22:12 WARN  gatt table incomplete (attempt 2/4, streak=2)       ← 丢句柄 + gc 也没用
08:22:12 WARN  port reset #1 … after 0 consecutive failures        ← 日志本身也在误导
08:22:19 WARN  gatt table incomplete (attempt 3/4, streak=1)
08:22:27 WARN  gatt table incomplete (attempt 4/4, streak=2)
08:22:27 WARN  join attempt failed after 24.3s
08:25:50 （重启 client 进程）
08:25:59 INFO  connected … mtu=527 attempts=1 source=scan in 4.84s
08:25:59 DEBUG gatt table = 1800[…] 1801[…] 180a[…] a000[…]       ← 全都在
```

两个数字把结论钉死了：

1. **真连一次要 ~4.8s**（扫描 + 建链 + 现场发现服务），而唤醒后的失败只用
   **1~2s** 就返回了 ⇒ 它根本没上空中接口，WinRT 走的是**进程里那条缓存链路**
   （`GattSession.maintain_connection` 还是 True，Windows 认为链路还活着）；
2. **重启进程立刻正常** ⇒ 脏状态在进程/栈这一侧，而 `_reset_port()`
   （丢引用 + `gc.collect()`）**清不掉**它 —— 因为 bleak 关会话时只是
   `GattSession.close()`，**从不把 `maintain_connection` 置回 False**。

### 修复：五个动作，按"轻 → 重"

| # | 动作 | 位置 |
|---|---|---|
| 1 | **认出"刚睡醒"**：心跳（15s）之间墙钟跳了 45s+ 就是被挂起过 | `power.StallDetector` |
| 2 | **唤醒即换链路**：睡着前 READY 的会话一定已经死了（链路监督超时 20~30s），主动关掉它立刻重连，而不是等 keepalive 3 次未应答（60~125s） | `app._on_stall_resume` |
| 3 | **退避立刻中断**：`asyncio.Event` 唤醒退避等待，退避次数清零 | `app._sleep_or_resume` |
| 4 | **连之前先清栈**：等 radio 回到 `ON` → `release_gatt_session()`（`maintain_connection=False` + 关会话 + 关设备）→ 静置 3s | `client._post_resume_reset` |
| 5 | **"缓存链路"判据**：`connect()` 在 `PHANTOM_CONNECT_SECONDS=2.5s` 内返回且表里没有本服务 ⇒ 立刻 `hard_reset()`，不再傻等 2 次 | `client.connect` |

另外两条同源修复：

* **host 侧的"僵尸会话撑着的假健康"**：以前 `advertising_ok` 只要 `_sessions`
  非空就返回 True。对端机器睡着后 WinRT 可能留下僵尸会话 ⇒ watchdog **永远**
  不去恢复广播 ⇒ 对端唤醒后一直连不上，而 host 看着一切正常。现在会话必须
  **最近 60s 内说过话**（两端各有 20s 一跳的 PING）才算活着；广播状态又不对时
  就走 `revive(hard=True)` → **整段重建 provider**（唤醒后 Windows 可能把服务
  注册丢了，`stop_advertising`+`start_advertising` 拉不回来）。
* **连接互斥**：旧任务被 `cancel()` 后可能还卡在 WinRT 里（`_drop_client_task`
  等 15s 就放弃它），两个 `connect` 撞在同一个对端上 ⇒ 两边都拿不到完整表。
  现在同一对端同一时刻只允许一条链路在建（`acquire_connect_slot`，超时放行）。

### 走不通的那条路：程序改不了蓝牙开关

最后手段是"关掉再打开蓝牙适配器"，但**本机实测**：

```
Radio.set_state_async(RadioState.ON) -> DENIED_BY_USER
```

未打包的 Win32 程序没有 radio 权限（`request_access_async` 在这个 pywinrt
构建里干脆没有）。所以代码里的策略是：**先试一次**（有的机器/打包方式允许），
失败就打开 `ms-settings:bluetooth` 让用户自己关一次，并在提示栏写清楚。
UI 上给了两个入口：状态栏的**「重置蓝牙」按钮**与 **Ctrl+R**（先前确认）。

### 本机可复核的部分（不需要第二台机器）

```powershell
# ① provider 能不能反复重建（hard revive 的核心动作）
C:\Python311\python.exe %LOCALAPPDATA%\Temp\opencode\lab_server_restart.py
#   start #1 → STARTED；restart #1/#2 → ok=True advertising=STARTED

# ② 真 app + 模拟唤醒（改 config/networks 到隔离 root，不动 dev 的持久化目录）
C:\Python311\python.exe %LOCALAPPDATA%\Local\Temp\opencode\smoke_resume9.py
#   system resume detected (gap 1800s) → restarting gatt server (hard revive)
#   → gatt server restarted: advertising=STARTED → host revive: True
#   → radio reset (auto=True): ok=False off=DENIED_BY_USER（指引按 Ctrl+R）
```

做不到的：**端到端连一次**（本机只有一个适配器，Windows 不把自己的广播
暴露给自己的扫描），所以"唤醒后 client 自己连回来"这一段仍需两台机器复测。

### 日志本身也在误导（顺手修掉）

`port reset #1 … after 0 consecutive failures` —— "表不完整"走的是
`table_fails` 计数，而日志固定打 `fail_streak`（那条路是 0）。现在按 cause 打
真实计数（`2 gatt table failure(s)` / `N cached-link (phantom) connect(s)`），
并且**失败日志自带耗时与真实表内容**：

```
gatt table incomplete (attempt 1/4, streak=1, elapsed=1.82s, table=<empty>): …
```

`table=` 里是 `<empty>` / `1800[…] 1801[…] 180a[…]`（只有系统服务）/
`<no client>` —— 这三种完全不同的故障，以前在日志里长得一模一样。
扫描日志也补了广播里的服务 UUID（`services=[…]`）：**它还在 ⇒ 对端软件活着，
问题在本机链路；它没了 ⇒ 对端根本没在广播本服务**，这是分水岭。

### 顺手治好的两个老毛病

* `test_retransmit_survives_packet_loss` 长期偶发红灯：45% 丢包下"某一帧连着
  丢几次"本身就有可观概率（一帧要走通需要**数据帧 + 它的 ACK** 都活下来，
  单窗口成功率只有 ~30%）。把 `MAX_RETRIES` 3 → **5**（2.5s 无进展才放弃），
  并把测试的随机种子固定 —— 真机上的收益是"偶发丢包不会再让整条会话被 BYE 掉"。
* `_drop_client_task` 的放弃时限 10s → 15s（给旧任务自己走完超时的余地，
  它占着连接名额）。

---

## 3. 用户体验故事（可当验收剧本）

### 故事 A · 晚岚把 server 关掉去吃饭，回来重新打开

> 晚岚的两台机器：台式机（Host，`阿伟`）+ 笔记本（Join，`小A`）。
> 她关掉台式机上的软件去吃饭，笔记本还开着。
>
> **期望**：笔记本在 ~2 分钟内自己发现"对方离线"，状态栏变红、提示
> 「连接断开：对方已离线」，然后按 3→6→12… 秒退避重连；
> 台式机重新打开后，**不用碰笔记本**，它自己连回来，聊天区历史还在。
>
> **现在的行为**：
> 1. 台式机关闭 → 笔记本的 Host 会话/`BleakTransport` 掉线 → 上层退避重连；
> 2. 如果 Windows 把链路留成半开，最长 ~125s 后 PONG 探测判死 →
>    `连接断开：对方已离线`，进入退避；
> 3. 台式机重开 → 笔记本下一次重试**先重新解析设备**（刷新 Windows 设备表）
>    → 连上 → 双向 `RESYNC` → 两边状态一致；
> 4. 历史按 `_pick_history_network_id()` 取到同一批行，**不清空**。
>
> **验收看点**：笔记本提示里不出现 `UNKNOWN：peer … not subscribed to 0000a003…`
> 这种原始串；`app.log` 里有 `last_network_id changed:` 的话要能解释为什么换号。

### 故事 B · 两边都显示断开，晚岚点"加入网络…"

> **期望**：点一次就连上，server 端弹出「小A 已加入」，client 端发送按钮变亮。
>
> **以前**：server 端显示"1 conn"、client 端按钮灰（半开链路 + 订阅表里的幽灵条目）。
>
> **现在的行为**：
> 1. 点"加入网络…" → 扫描 6s（刷新设备表）→ 选中/输入地址；
> 2. 连接前再解析一次设备（`force=True`）；失败则 dispose 掉半连接对象、
>    退避、重试；连败 3 次触发端口自愈；
> 3. 握手成功 → 双方各发 `RESYNC` → 状态栏/目标下拉同步刷新；
> 4. 若对端一侧其实还留着幽灵会话，它在 READY 上最多 ~125s 被 PONG 探死并
>    广播新的 PEERS，两边重新对齐。
>
> **验收看点**：client 端不再出现"server 显示已连接但我这边按钮灰"的长时间僵持；
> 若真的僵持，日志里 `no PONG from …` / `duplicate subscriptions for …` /
> `peer … unsubscribed from all notify channels` 会直接指出是哪一侧、哪条通道。

### 故事 C · 晚岚重启笔记本上的 client，历史必须还在

> **期望**：重启后聊天区还是最近 10 条，历史窗口能搜到 3 天内全部。
>
> **现在的行为**：启动时 `_pick_history_network_id()` 优先取"库里最近写入的 id"，
> 所以**即使 `last_network_id` 是旧的/错的**也能显示最新那批；
> 同时 `_remember_network_id()` 把 id 修正并落盘，下次不再走回退路径。
>
> **验收看点**：`history pick: … picked=…` 与 `history rows by network_id: …`
> 两行日志能自证；历史窗口按天分组、可搜索、可复制/导出。

### 故事 D · 晚岚要报障，她需要什么

> 设置 → 「诊断日志」选**详细（DEBUG）**→ 立刻生效（不用重启）→ 复现一次问题 →
> 把 `logs/app.log` 发出来。
>
> **不需要**她理解 HRESULT、network_id、GATT 订阅表 —— 这些都在 DEBUG 行里。

### 故事 E · 晚岚合上笔记本去开了个会（第八轮加的）

> 笔记本（Join）已经连上台式机（Host）。她合盖 30 分钟，回来打开盖子。
>
> **期望**：**不用点任何东西**，一分钟内自己回到「已连接」；
> 聊天区历史还在，发送按钮自己恢复可用。
>
> **现在的行为**：
> 1. 盖子一打开，心跳发现墙钟跳了 30 分钟 ⇒ `system resume detected (wall-clock gap 1800s)`；
> 2. 提示栏：「系统已唤醒，正在重置蓝牙链路并重连…」；
> 3. 睡着前那条会话被**主动关掉**（不用等 keepalive 的 60~125s）；
> 4. 日志里依次出现 `post-resume: radio=ON (ready) before resolve` →
>    `transport marked for post-resume reset #1` →
>    `post-resume reset: radio=ON (ready) winrt release -> device, maintain_connection=False,
>    session closed, device closed; settle 3.0s` → `connected to … in X.XXs`；
> 5. 若 150s 还没连上（栈真的没自己回来）：先试一次自动重置蓝牙 radio；
>    本机没这个权限（`DENIED_BY_USER`）⇒ 提示栏告诉她**按 Ctrl+R 或点「重置蓝牙」**，
>    点下去会打开系统蓝牙设置页，把蓝牙关掉再打开即可。
>
> **她能自己判断"好没好"**：状态徽章回到 `READY`、提示栏不再有红色文字。

---

## 4. 排障指南

### 4.1 打开 DEBUG

三种方式，任选其一（优先级从高到低）：

| 方式 | 操作 | 生效时机 |
|---|---|---|
| 环境变量 | `set BLECHAT_LOG_LEVEL=DEBUG` 后启动 | 立即 |
| 设置对话框 | 设置 → 诊断日志 → 详细（DEBUG） | 点确定后立即（`_apply_log_level()`） |
| `config.json` | 加 `"log_level": "DEBUG"` | 下次启动 |

BLE 栈内部日志（bleak 每个报文一行，非常吵）需要额外开关：
`set BLECHAT_BLE_DEBUG=1`。

日志文件：`logs/app.log`（5 MB 轮转、保留 5 个备份、7 天清理；**异步写盘**，见 §2.5 问题 C）。

### 4.2 日志速查表

| 关键字 | 含义 | 下一步 |
|---|---|---|
| `connect attempt=N addr=… device_source=scan\|direct\|missing` | 第 N 次连接尝试 + 设备解析来源 | `direct`/`missing` = **没扫到**；看下一行的 `peer address changed` |
| `peer address changed: A -> B (name=… strategies=[name+service])` | **对端地址变了**，已按名字找回 | 正常自愈；新地址会写回 `networks.json` |
| `peer X ('名字') not found in a 8s scan (N device(s) seen, strategies=['none'])` | 扫描里没有对端（没开 / 不在范围） | 这一轮被**跳过**（不再白等 12s 超时）；确认对方在广播 |
| `peer address learned: A -> B (network=…)` | 学到的地址已落盘 | 下次重启可按地址快查命中 |
| `peer 'X' matched N candidates […]` | 名字匹配到多个同类实例 | 若选错，给对方改个不同的别名 |
| `device resolved … source=scan rssi= services=[…]` | 设备解析成功 + **广播里有哪些服务 UUID** | `services=[…0000a000…]` = 对端软件活着 ⇒ 问题在本机链路；`services=[<none>]` = 对端没在广播本服务 |
| `join attempt failed after X.Xs: <异常全文>` | 本次尝试失败的**完整**原因（含 HRESULT 十六进制） | `0x80004005` 设备表/句柄；`0x80000013` 对象已关闭（对端走了）；`TimeoutError <- CancelledError` = 被**取消**（看有没有人在掐连接） |
| `port reset #N for … after 2 gatt table failure(s)` | 触发端口级自愈（`after` 后面是**真实**原因计数） | 频繁出现说明 WinRT 侧状态很脏，整段留档 |
| `gatt table incomplete (attempt i/4, streak=N, elapsed=X.XXs, table=…)` | 连上了但 GATT 表不对；**`elapsed` + `table` 是判据** | `elapsed < 2.5s` ⇒ WinRT 走了缓存链路（见 `phantom`）；`table=<empty>` 空表 / `1800[…] 1801[…] 180a[…]` 只有系统服务 / `<no client>` 没有客户端对象 |
| `connect returned in X.XXs with table=… -> winrt 走了缓存链路（phantom #N）` | **睡眠唤醒后的典型一行**：快得不像真连过 | 已自动 `hard reset`（`maintain_connection=False` + 关会话/设备）；若唤醒后反复出现 ⇒ 按 Ctrl+R 重置蓝牙 |
| `hard reset (…): winrt release -> device, maintain_connection=False, session closed, device closed` | 明确放弃了那条缓存链路 | 正常自愈路径 |
| `system resume detected (wall-clock gap Ns)` | 认出"系统刚睡醒"（心跳之间墙钟跳了 45s+） | 之后应看到 `post-resume` 系列与 `connected` |
| `post-resume: radio=ON (ready) before resolve` | 唤醒后先等蓝牙 radio 回来 | `NOT READY` ⇒ radio 没恢复，`_check_resume_stall` 会在 150s 后出手 |
| `post-resume reset: … settle 3.0s` | 放弃旧链路 + 静置 | 正常 |
| `resume with session in state=ready: closing it to force a clean reconnect` | 唤醒后主动关掉睡着前那条会话 | 正常（不等 keepalive） |
| `still not connected Ns after resume -> resetting bluetooth radio` | 唤醒 150s 仍未连上 | 看下一行 `radio reset (auto=True): ok=…`；`DENIED_BY_USER` 是**本机权限问题**，手动按 Ctrl+R |
| `advertising status=… and every session idle > 60s -> needs revive` | host 侧：会话都是僵尸、广播也不正常 | 随后会 `restarting gatt server (hard revive)` |
| `gatt server restarted (…): advertising=STARTED` | provider 整段重建成功（真机已验证可重建两次） | 正常自愈路径 |
| `old client task did not stop within 15s, abandoning it` | 旧连接任务掐不动（卡在 WinRT 里） | 新任务会去抢连接名额（`connect slot … still held`）；偶尔出现可接受 |
| `connect slot for … still held after 20s … proceeding anyway` | 同一对端有两条链路在建（旧任务没退干净） | 撞车会导致"点了加入网络没反应"；连续出现请留档 |
| `retrying gatt discovery in X.Xs` | 表不完整，等待后换新链路重试 | 正常自愈 |
| `recv rx from …: N bytes (responded=False)` | `False` = 系统已自动应答（不是故障） | 正常 |
| `respond skipped (…)` | 同上，以前会误报成 `respond failed` | 正常 |
| `service 0000a000 missing on …; gatt table = …` | 打印了实际 GATT 表 | 对比 `0000a000/1/2/3` 是否齐全 |
| `no PONG from … (miss i/3)` | **半开链路**：发得出去、收不到回应 | 若同时另一边显示已连接 ⇒ "单方已连接"，会在 ~125s 后自愈 |
| `link dead: …` | 判定链路已死 → 关会话触发重连 | 正常自愈路径 |
| `session replaced for …` | 同一 peer 重新握手，旧会话作废 | 正常（对端重启/重连） |
| `peer … unsubscribed from all notify channels` | 订阅全没了，摘掉会话 | 正常掉线路径 |
| `duplicate subscriptions for …` | 同一 peer 有两条订阅（幽灵条目） | WinRT 订阅表脏了；`_subscribed()` 会优先挑活的 |
| `%s: swept N stale peer record(s)` | 启动/恢复广播时清掉了残留 | 正常 |
| `client task hung for Ns, restarting` | 只有超过 `CONNECT_GRACE_SECONDS` 才会出现 | 若频繁出现，把这段前后的 `connect attempt` 一起留档 |
| `last_network_id changed: A -> B` | **历史"被清空"的第一嫌疑人** | 对照 B 是否在 `networks.json` 里 |
| `history pick: … picked=…` / `history rows by network_id: …` | 历史按哪个 id 取、各 id 多少行 | 表非空 = 换号问题；表空 = 真没行 |
| `history remapped N rows: A -> B` | 历史搬家成功 | 正常归一 |
| `msg_id collision in …` | 撞号，已改存负数主键 | 正常保护（不丢行） |
| `state: mode=… peer_addr=… history=…` | 每 4 分钟的完整状态快照 | 事后复盘时对时间点用 |
| `[blechat] log queue full: dropped N record(s)` | 日志队列被突发流量灌满，DEBUG 记录被丢 | 正常保护（**绝不阻塞界面**）；关键事件不受影响。持续出现说明 DEBUG 下流量太大 |
| `unhandled exception in mouseXxxEvent` | 鼠标处理里出过异常，已被兜底吞掉 | 界面没崩，但这是真 bug —— 把这段日志发出来 |

### 4.3 四类问题分别抓哪几行

```powershell
# A. 重启后连不上 / 地址变了
Select-String -Path logs\app.log -Pattern "connect attempt|device_source|peer address changed|peer address learned|not found in a|port reset"

# B. 连接被反复掐掉（越连越连不上）
Select-String -Path logs\app.log -Pattern "join attempt failed|CancelledError|client task hung|reconnect in"

# C. 单方已连接 / 按钮灰 / 掉线
Select-String -Path logs\app.log -Pattern "no PONG|unsubscribed|duplicate subscriptions|swept|session replaced|link dead"

# D. 历史被清空
Select-String -Path logs\app.log -Pattern "last_network_id changed|history pick|rows by network_id|remapped|history add failed"

# E. 睡眠唤醒后连不上（第八轮）
Select-String -Path logs\app.log -Pattern "system resume detected|post-resume|hard reset|phantom|gatt table incomplete|port reset|radio reset"
```

### 4.4 现场隔离（不改动 dev 的持久化目录）

任何冒烟/复现脚本都应把 `app_root` 指到临时目录，**不要**在项目根跑，
以免删掉 dev 的 `keys/identity.json`、`config.json`、`networks.json`、`history.db`：

```python
import blechat.config as cfg, blechat.app as app_mod, blechat.history as hist
cfg.app_root = app_mod.app_root = hist.app_root = lambda: ROOT   # 临时目录
```

---

## 5. 代码地图（改哪里）

| 想改什么 | 去哪里 |
|---|---|
| UI 布局/控件 | `blechat/ui/`（`main_window.py` 是骨架，`widgets/` 是零件） |
| **UI 显示什么状态** | `blechat/app_state.py` 的 `derive_ui_state()`（纯函数，先改这里再改 UI） |
| 应用编排（谁在什么时候启动/重连） | `blechat/app.py`（`_start_mode` / `_client_loop` / `_watchdog_tick` / `_client_hung`） |
| 握手 / 分片 / ACK / 保活 | `blechat/session.py`（Host 与 Client **共用**） |
| 线格式 / 帧 / 文件 meta | `blechat/protocol.py`（改完同步 `PROTOCOL.md`） |
| Windows GATT server 行为 | `blechat/ble/server.py`（WinRT 的坑见 `docs/winrt/`） |
| **唤醒后的蓝牙栈卫生**（radio / 释放会话 / 重置 radio） | `blechat/ble/radio.py` |
| 睡眠检测 | `blechat/power.py`（`StallDetector`） |
| bleak 客户端 + **对端解析** | `blechat/ble/client.py`（`resolve_peer` / `scan_candidates` / `BleakTransport`） |
| 历史/归一/清理 | `blechat/history.py` |
| 配置与网络记录 | `blechat/config.py` |
| 日志级别 / 轮转 / **异步写盘** | `blechat/logging_setup.py`（`AsyncFileHandler`） |

### 改代码时的四条经验（踩过的坑）

1. **`qasync` + 模态框**：绝不在协程里直接 `dialog.exec()`，必须走
   `BleChatApp._modal()`。否则当前任务一直挂在 `_current_tasks` 上，
   别的任务一 step 就 `RuntimeError: Cannot enter into task …`，
   表现为"扫描/连接全部哑掉且没有报错"。
2. **WinRT 事件回调里同步取 deferral/session/request**：事件返回后再取会抛
   `E_ILLEGAL_METHOD_CALL(0x8000000E)`，客户端的写请求永远不完成
   （表现为 `GATT Protocol Error: Unlikely Error`）。
3. **别按对象身份判断 WinRT 会话**：`args.session` / `sub.session` 每次都返回
   **新的包装对象**，底层是同一条会话。按身份判断会让每一帧都走"换绑"分支
   （每帧同步写日志 + 泄漏回调）→ GUI 线程被拖死。
   判据要用 `session_status`（见 `_track_session` / `_session_alive`）。
4. **别把"还没 READY"当成"挂死"**：一次连接尝试的正常成本接近 46s
   （3+8 扫描 + 12 连接 + 8 notify + 15 握手）。任何"超时重来"的逻辑都必须
   按**单次尝试**计时并留够宽限（`_connect_started_at` + `CONNECT_GRACE_SECONDS`），
   否则会把正在进行的 `connect()` 反复取消 —— 越掐越连不上。
   同理：**日志 handler 不能同步写盘**（见 §2.5 问题 C）。
