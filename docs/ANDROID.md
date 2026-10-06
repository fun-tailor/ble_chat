# Android 端（Join / 手机）· 设计 · 用户体验故事 · 排障

> 对应源码：`android/app/src/main/java/com/example/blechat/`
> 协议权威：仓库根 `PROTOCOL.md`（与 Python 端逐字节对齐）
> 真机环境：android（Android 12+），Host = Windows PC（`DESKTOP-xxxxxxx`）

---

## 1. 定位与范围

Android 端**只做 Join（客户端）**，不作 Host：

| 能力 | v1 | 说明 |
|---|---|---|
| 扫描 / 按 MAC 直连 | ✅ | 定位权限被拒时扫描恒为空，所以必须保留直连 |
| 握手（PSK / 口令输入） | ✅ | PBKDF2-HMAC-SHA256 ↔ Python `derive_psk` |
| 文本收发 | ✅ | 分片 / ACK / 重传 / 保活全按 `PROTOCOL.md` |
| 历史（Room）+ 昵称 | ✅ | 按 `network_id` 分区，3 天清理 |
| 系统分享（`ACTION_SEND` text/plain） | ✅ | 打开应用并把文本**预填进输入框**，等连上再发 |
| 图片 / 文件 / 作 Host | ❌ | 明确不在 v1 范围 |

**网络标识 v1 暂用 Host 的 BLE MAC**（`Repository.normalizeAddress`，大写冒号）。
`SPEC.md §2.2` 最终要求 `uuid5(host_device_id, service_uuid)` —— 待 Host 在 AUTH_OK 里
下发 device_id 后再切，并做一次历史 remap（见 §9 待决）。

---

## 2. 分层

```
ui/             Compose（MainScreen / HistoryScreen / SettingsScreen）
  └ ui/main/MainViewModel   把 BLE 世界翻译成"用户此刻该看到什么"（Phase + status + hint + 诊断）
session/        ChatSession：握手 / 收发 / ACK / 保活 / 断线；Keepalive 是纯判定函数
  └ Transport   接口；BleTransport 把 GattLink 包成它（单测塞 FakeTransport）
ble/            GattLink（真 BLE：连接 → 配对 → 探针 → 服务发现 → MTU → 订阅）、
                Bonder（BLE 配对 SMP）、Scanner、ConnTrace（病历/分类）、BleLog
protocol/       P / Ctrl / Err / 帧编解码 / Packer / Reassembler / Crypto（与 Python 对齐）
data/           Room 历史、AppPrefs、CredentialStore（Keystore 加密口令）
```

**唯一的写入点是设计约束**：UI 不直接碰 BLE。`MainViewModel` 是唯一把
`SessionEvent` 翻译成界面状态的地方，所以「界面显示什么」永远可以在
`Phase/status/hint/diagnostics` 四个流里对上。

---

## 3. 状态机

| 层 | 值 | 用户看到 |
|---|---|---|
| `Phase`（界面） | `IDLE` | 空态：「点扫描找 Host，或按 MAC 直连」 |
| | `SCANNING` | 「扫描中…」+ 设备 chips |
| | `CONNECTING` | 「正在连接 X…」 |
| | `HANDSHAKE` | 「握手中…」→「已发 AUTH，等 Host 确认…」→（要口令时弹框） |
| | `READY` | 绿点 +「已就绪」+ 可发送 |
| | `CLOSED` | 「连接结束：<原因>」+ **失败提示 + 诊断入口** |
| `SessionState`（协议） | `IDLE → CONNECTING → CONNECTED → HELLO_SENT → AUTH_SENT → READY`（`AUTH_FAIL`/`CLOSED` 为分支） | — |
| `GattLink` 阶段 | 连接 → **配对（可选）** → ATT 探针 → LL 探针 → 服务发现（**只发一次**）→ MTU → 订阅 CCCD | 一次尝试最多 25s + 订阅 2×5s |

**每一阶段都必须带超时**：真机上 `discoverServices()` / `requestMtu()` 的回调
**可能永远不来**（见 §7 故障档案），裸 `await()` 等于把界面挂死。

---

## 4. 一次连接到底做了什么（含探针）

```
connectGatt(autoConnect=false)
   │
   ├─ onConnectionStateChange(CONNECTED)          ← LL 建立（回调里**什么都不发**）
   │
   ├─ ⓪ BLE 配对（可选，默认开）createBond() + 等 ACTION_BOND_STATE_CHANGED（12s）
   │     └─ 成功 / 失败 / 超时都只记账，**绝不中断**这次尝试（失败时的 ATT 表现正是要看的）
   │
   ├─ ① ATT 探针 requestMtu(517)  ← **队列里第一条**，紧接着连接回调就发
   │     ├─ 5.5s 内有 onMtuChanged ⇒ ATT 层活着（顺带把 MTU 谈好）
   │     └─ 没有 ⇒ 再看 LL 探针
   │
   ├─ ② LL 探针 readRemoteRssi()  ← 排在 ATT 探针**之后**（它也占那个唯一的命令槽）
   │           ├─ LL 有答复 ⇒ ATT_SILENT（对端主机栈不处理 ATT）⇒ 立刻收工
   │           └─ LL 也无   ⇒ LL_DEAD（链路层其实已经断了）⇒ 立刻收工
   │
   ├─ ③ requestConnectionPriority(BALANCED)   ← 放在探针之后，不干扰探针的入队
   │
   ├─ ④ discoverServices() —— **只发一次**（超时就收工，重发会被队列丢掉）
   │     ├─ 有 onServicesDiscovered → 校验 a000/a001/a002/a003 → READY
   │     └─ 无回调 ⇒ DISCOVERY_DROPPED（ATT 通、服务表没人服务）⇒ 收工
   │
   ├─ ⑤ MTU（①里已谈好时直接跳过）
   │
   └─ 失败 ⇒ ConnTrace.classify() ⇒ 标题 + 该动哪一端 + 自动补救一次
```

**两个探针为什么必须这么排（而且是串行）**：它们一个只走控制器（`readRemoteRssi`）、
一个必须经对端主机栈（`requestMtu`），与「服务发现有没有回调」组合，就把原本混在一起的
故障分开了（§6 表）。但**两者在协议栈里都要占那个唯一的 GATT 命令槽**
（`bta_gattc_enqueue`，见 §7.5 与 §7.6）：真机日志里两个调用只隔 **1ms**，
所以必须 ATT 探针先发、LL 探针后发，中间不插任何 GATT 调用 —— 否则"ATT 没答复"
这个结论可能只是**我们自己的探针把队列占了**。

**BLE 配对与 App 共享密码是两件事**（用户最常问的一个点）：

| | BLE 配对（`Bonder`） | App 共享密码（`ChatSession` AUTH） |
|---|---|---|
| 谁参与 | 手机蓝牙栈 ↔ PC 蓝牙栈 | 本 App ↔ PC 上的 Host 程序 |
| 何时 | 连上之后、**任何 ATT 操作之前** | 链路 READY **之后**（GATT 全通了才轮到） |
| 输密码 | 系统弹窗（Windows 一般走 Just Works，不弹） | 本 App 的口令框 |
| 有什么用 | 双方交换 IRK，从此认得出彼此轮换的随机地址（RPA） | 证明"我知道这个网络的密码" |

> 所以"点连接没让我输密码"有两种原因，而且都能从病历里一眼分辨：
> ① 链路没到 READY（口令阶段还没轮到）—— 病历里 `readyAt` 为空、结论是 `ATT_SILENT` 之类；
> ② 这台 Host 的口令**已记住**（`CredentialStore` 里 salt 校验通过）—— 状态栏会写
> `正在认证（已记住口令）…`，要重输只需在诊断外「取消配对」或重新输一次。

**自动补救（只做一次，之后老实把建议摆出来）**：

| 结论 | 自动动作 | 等待 |
|---|---|---|
| `NO_CONNECT` 且地址来自**旧记录** | 按 Host **名字**重新扫一遍拿当前地址（Windows 的 LE 地址会轮换） | 3s |
| `ATT_SILENT` / `LL_DEAD` 且**配对开关是关的** | 打开配对再试（换「配对状态」这一个自变量） | 8s / 3s |
| `ATT_SILENT` / `DISCOVERY_DROPPED` / `SERVICE_MISSING`（已试过配对） | 换 `autoConnect=true` 建链 + 发现服务前 `refresh()` 清一次 GATT 缓存表 | 8s |
| `LL_DEAD` / 其它 | 换 `autoConnect=true` 建链 | 3s |
| 已经到过 READY | 不自动重连（那是"掉线"，交给用户） | — |

> 等待时间是按真机证据定的：我们 `close()` 之后协议栈要 `start link idle timer = 1 sec`
> 才把链路放干净，所以不能立刻重连；对端那侧要等更久（§7.3.1）。
> 补救动作是**换形状**、而且**一次只换一个自变量**：原样重试几乎必然复现同一个失败，
> 一次改三样则又回到"不知道哪个起作用"。

---

## 5. 用户体验故事（可直接当验收剧本）

### 故事 A · 晚岚把 Host 开着，手机点一下就连上

> 手机打开 App → 点「扫描 Host」→ 列表出现 `DESKTOP-xxxxxxx` → 点它 →
> 状态「正在连接…」→ 弹出「输入共享密码」→ 输完 → 「已就绪」→ 发消息。
>
> **看不见的部分**：连接后先做服务发现、再协商 MTU（517）、再订阅两条通道
> （`a002` indication / `a003` notify），全部成功才算 READY。

### 故事 B · 扫到了但连不上（**本轮要解决的主诉**）

> 点 `DESKTOP-xxxxxxx` → 十几秒后「连接失败」。
>
> **现在的行为**（不再是干巴巴一句超时）：
> 1. 连接面板里直接出现一段**红色处置建议**，例如
>    「连上了，但对端 ATT 层零回复 → 请在 PC 上把蓝牙关掉再打开…」；
> 2. 右上角出现「诊断」，点开是完整病历（每一步 + 两个探针的结论 + 判据一行），
>    可一键**复制**发给开发者；
> 3. 若失败形态是"可补救"的，App 会**先自己试一次**（按名字重找 / 换建链方式），
>    再决定要不要麻烦用户。

### 故事 C · 上一条消息刚发完就掉线（"半分钟内失联"）

> 连上、发了几条、然后突然「连接结束」。
>
> **现在的行为**：`Session` 每轮保活都留一行
> `保活 #n 发 PING（misses=… 距上次 PONG …ms）`，
> 收到 PONG 会打 `收到 PONG，保活计数清零`。
> 于是日志能直接回答"是 PING 没发出去（手机侧发送队列卡）还是对端没回 PONG（对端放弃）"。

### 故事 D · 昨天还能连，今天点「重连上次」就失败

> 因为 **Windows 的 LE 广播地址会轮换**（同一天实测见过
> `8C:C6:81:9C:B9:7B` / `65:68:F3:BB:6F:02` / `76:0D:02:17:85:B9` / `4B:AA:42:17:14:18`），
> 存下来的 MAC 很可能已经属于"空气"。
>
> **现在的行为**：「重连上次」先按 **Host 名字**扫 4s 拿到当前地址，
> 扫不到才退回旧地址（并标明"很可能已失效"）；病历里 `地址来源=stale-mac` 时，
> 结论会直接写「点『重连上次』按名字重解析」。

### 故事 E · 朋友在微信里发了一段文字，我想发到 PC

> 微信里「分享 → BLE Chat」→ App 打开、文本**预填在输入框**（不自动发）→
> 连上 → 点发送。v1 不做自动重发，避免"以为发了其实没发"。

### 故事 F · 为什么点连接没让我输密码？（用户实测提出的疑问）

> 状态「正在连接…」→ 十几秒 → 「连接失败」，**全程没有输密码这一步**。
>
> **现在会直接回答**：失败提示里写明「共享密码（AUTH）阶段在链路就绪之后，
> 链路没到 READY，所以还没轮到输密码」；诊断弹窗的「背景」一段还会说明
> 这一步该由谁来做（PC 侧重启 Host / 手机侧打开配对）。
>
> 顺带说明第二个疑惑「整个连接过程 PC 端好像没有消息」：**这是预期的**。
> PC 的 Host 只在收到 **CCCD 写 / 特征写**时打日志，而 ATT 都没通，
> 自然什么都不会收到 —— PC 日志静默**不能**证明 PC 没收到连接（§7.2）。
>
> 另外：如果口令**已经被记住**（上次输对过），第二次连接会直接用记住的密钥，
> 状态栏写「正在认证（已记住口令）…」，也不会弹框。这是设计如此。

### 故事 G · 配对开关怎么用

> 连接面板里有一个默认打开的「连接前先配对」：
> - 打开时：连上之后先做一次 BLE 配对（系统弹窗，Windows 一般直接 Just Works 不弹）；
>   配对成功后**以后每次连接都立刻跳过**（已经配对过）；
> - 关掉时：跳过配对，直接探测。用于对照实验 —— 想知道"配对到底有没有用"，
>   就把它关掉再连一次，对比两份病历的 `配对:` 行与结论。
>
> 「取消配对」放在诊断弹窗里：PC 侧如果把手机忘了、手机上还留着密钥，
> 之后连接会尝试加密然后失败（"半配对"），只能清掉重来。

---

## 6. 故障分类表（`ble/ConnTrace.classify()`）

| 现象（病历判据） | 结论 `DiagKind` | 该动哪一端 | 处置 |
|---|---|---|---|
| `connectGatt` 后连接回调没来 | `NO_CONNECT` | 两端 | 旧 MAC ⇒ 按名字重扫；否则确认 PC 在广播（`advertising=STARTED`） |
| 连上、LL 探针**有**答复、ATT 全静默、**配对没成功** | `ATT_SILENT` | **两端** | 手机侧先打开「连接前先配对」；同时按 PC 侧清单试（重启 Host / 关开蓝牙） |
| 连上、LL 探针**有**答复、ATT 全静默、配对成功（或没试） | `ATT_SILENT` | **PC** | PC 上**重启 Host** / 关开一次蓝牙 / 点 PC 版「重置蓝牙」。手机侧栈级证据见 §7.5 |
| 连上、LL 探针**也无**答复 | `LL_DEAD` | 手机 | 重试；靠近 PC、重启手机蓝牙 |
| ATT 探针**有**答复、服务发现没回调 | `DISCOVERY_DROPPED` | **PC** | 对端服务表没人服务（Windows：MTU 由系统栈答、服务表要 Host 的 provider 答）⇒ 重启 Host；再不行关开蓝牙 |
| 服务发现成功但表里没有 `a000` | `SERVICE_MISSING` | 两端 | 本地缓存表过期 ⇒ 清缓存重连；PC 侧确认 Host 在广播 |
| 服务表齐了、但 MTU 一直协商不成 | `MTU_FAILED` | 两端 | 对端答了 MTU 请求但没放大 ⇒ 重试 / 重启 Host |
| 表齐全但 CCCD 写不通 | `SUBSCRIBE_FAILED` | 手机 | 重试 |
| 初始化阶段 status≠0 就被断开 | `REMOTE_DISCONNECT` | 两端 | 带 status 原文（133/8 常见） |
| 口令不匹配 | `AUTH_FAILED` | 手机 | 向 Host 确认密码 |

**连续失败 ≥ 2 次时额外给一句减速建议**：PC 侧上一次连接可能还占着 GATT 服务
（Windows 自己要 30~60s 释放），建议等半分钟再试 —— 见 §7.3.1。

> 这一层是**纯 Kotlin**（不依赖 Android API），所以 `ConnTraceTest` 能直接拿
> 真机日志的形状做断言 —— 分类逻辑本身是被单测钉住的。

---

## 7. 真机故障档案（2026-10-04）

### 7.1 日志原样（关键几行）

```
12:54:57  connectGatt 76:0D:02:17:85:B9 requestMtu=517
12:54:57  onClientRegistered() - status=0 clientIf=8
12:55:22  cancelOpen()                       ← 25s 内**没有** onClientConnectionState
12:55:22  openLink 失败: 连接超时（25s 内未完成 GATT 初始化）
12:55:42  扫描到 DESKTOP-xxxxxxx addr=4B:AA:42:17:14:18 rssi=-71
12:57:02  connectGatt 4B:AA:42:17:14:18 ; onClientConnectionState status=0   ← LL 连上（243ms）
12:57:02  discoverServices() - device: 4B:AA:42:17:14:18
12:57:03  onConnectionUpdated() interval=6 latency=0 timeout=500 status=0      ← LL 双向通
12:57:06  服务发现 4000ms 内无回调，重试         ← ATT 层零回复
12:57:11  服务发现 4000ms 内无回调，重试
12:57:15  服务发现 4000ms 内无回调，重试
12:57:16  cancelOpen() ; openLink 失败: 服务发现 4000ms 内无回调（已重试 3 次）
```

### 7.2 当时的分析

* `onConnectionUpdated` 证明**链路层是活的、双向都通**（它是控制器层的参数更新）；
* 而 **ATT 层一个回调都没有**：既没有 `onSearchComplete`，也没有 `onMtuChanged`；
* PC 侧 `app.log` 在这段时间**完全静默** —— 但要注意：PC 的 Host 只有在收到
  **CCCD 写 / 特征写**时才打日志，单纯"服务发现"在 PC 日志里本来就不体现，
  所以**PC 日志静默不能证明 PC 没收到连接**；
* 「重启 client 进程/App 就能连上」的现象说明状态是**有时效的**，不是代码写死的错。

### 7.3 结论与当时的困境

* 这一形态（LL 活、ATT 静默）**只有两种可能**：对端主机栈不处理 ATT，或者
  本机 GATT 栈把请求丢了。当时**无法区分**，因为日志里只有一个"超时"。
* 于是本轮把「区分」做成了代码：两个探针 + 三种下发形态 + 病历分类（§4/§6）。
* PC 侧的一个强旁证：PC 日志在 `08:15:12–08:20:34` 有 **12 次
  `peer … disconnected`，间隔都是 ~31s** —— 31s ≈ **ATT 事务超时 30s**，
  与"请求发出去没人应答、Windows 到点把会话拆了"完全吻合。

### 7.3.1 另一个必须考虑的可能：**越急着重连越连不上**

同一个旁证还有第二种读法：Windows 侧的 GATT 服务在异常断开后**可能还占着上一次的
连接**，而它自己要 **30~60s** 才彻底释放。如果这段时间里我们不停地重连，
每次新连接都会撞上那个占用 —— 表现就是"怎么点都连不上"。

这个读法能解释档案里最反直觉的一件事：**08:22 连续失败 → 08:25:50 重启 App
（距最后一次失败 3 分 23 秒）→ 一次成功**。中间隔的 3 分钟里没有任何尝试，
PC 侧正好把占用释放掉了。也就是说"重启 App 就好了"可能是**等待**的功劳，
不是重启的功劳。

所以本轮在 Android 侧加了一条**减速建议**（不是硬性拦截用户的按钮）：
连续失败 ≥ 2 次时，处置建议里会补一句

> 已连续失败 N 次：PC 侧上一次连接可能还占着 GATT 服务（Windows 自己要 30~60 秒才释放），
> **建议等半分钟再试**，并在 PC 上把蓝牙关掉再打开一次。

复测时请特意验证这一条：**失败后等 40 秒再点一次**，看是否比连点更容易成功。

### 7.4 本轮改动（针对这份档案）

1. **分诊**：LL 探针 + ATT 探针，把 `ATT_SILENT` / `LL_DEAD` / `DISCOVERY_DROPPED` 分开；
2. **探针排到最前面 + 服务发现只发一次**（22:03 的日志证伪了"三种形态重发"，见 §7.5）；
3. **快速失败**：探针在队首，5.5s 没答复就直接给结论（不再烧满十来秒还不知道谁的问题）；
4. **自动补救一次**：换 `autoConnect=true` 建链 + 清缓存 / 按名字重找地址；等待时间按
   协议栈自己的 1s 释放窗口和对端的 30s ATT 超时来定（3s / 8s）；
5. **设备身份记账**：`bondState` / `device.type` / `addressType` / 广播名字全部进病历；
6. **诊断入口**：失败提示常驻面板 + 「诊断」弹窗可复制；
7. **日志**：控制帧收发、保活每一轮、扫描到的每个设备、`refresh()` 结果、断开原因；
8. 顺带修掉 `BleChat/BleChat/Link` 的重复前缀（`BleLog` 统一拼前缀）。

---

## 7.5 决定性证据（2026-10-04 22:03，手机装的是本轮**前一版**）

这一份 **ATT 层日志**把「ATT 静默」从推断变成了事实，也纠正了上一版的两个设计。

```
22:03:49.182  GATT ATT protocol channel with BDA: 7d:b1:99:52:fe:35 is connected
22:03:49.185  GATTC_Discover conn_id=0x0009, disc_type=1, s_handle=0x0001, e_handle=0xffff
22:03:49.201  discoverServices() - address=7D:B1:99:52:FE:35, connId=9     ← App 请求
22:03:52.610  [ERROR] bta_gattc_enqueue: already has a pending command     ← 第 2 次重发被丢
22:03:54.186  [ERROR] gatt_rsp_timeout lcid:4                              ← 手机 ATT 层自己超时
22:03:54.186  [WARNING] gatt_rsp_timeout retry discovery primary service
22:03:56.018  [ERROR] bta_gattc_enqueue: already has a pending command     ← 第 3 次也被丢
22:03:59.187  [ERROR] gatt_rsp_timeout lcid:4
22:03:59.427  [INFO] start link idle timer = 1 sec                         ← close 后 1s 才放干净
22:03:59.428  onSearchCompleted() - connId=9, status=129
```

### 结论 1 · 请求确实发出去了，对端没回答（**PC 侧**）

`GATTC_Discover … disc_type=1` 就是手机协议栈真正下发 ATT「Read By Group Type
(0x0001–0xffff)」的那一行；紧接着的 `gatt_rsp_timeout` 是**手机 ATT 层自己的 5 秒响应
超时**。也就是说：

* 手机侧：请求发出了、链路层是通的（同一段时间还有 `onClientConnUpdate`、
  `onPhyUpdate txPhy=2 rxPhy=2`）；
* 对端：**一个字节都没回**。

再加上「PC 的广播里一直带着 `0xA000`（nRF Connect 也看到 `0x180A, 0xA000`）」，
可以确定：**PC 的广告链路是活的，但它没有在服务这张 ATT 服务表**。
这正是 §6 里的 `ATT_SILENT` —— 现在它有栈级证据了。

> 注意区分：nRF Connect 截图里的 `Complete list of 16-bit Service UUIDs` 是**广播数据**，
> 只能证明"PC 在广播这个服务"，不能证明"连上去能发现服务"。要证明后者，得在
> nRF Connect 里**点 CONNECT**，再看 Client 页的服务表。

### 结论 2 · ATT 只有一条命令队列 ⇒ 上一版的"三种形态重发"是错的

`bta_gattc_enqueue: already has a pending command` 出现两次：第 2、3 次
`discoverServices()` **根本没被发出去**，往队列里塞了一下就被丢掉。

于是上一版的两个设计被证伪：

* 「换三种形态重发服务发现」→ 纯噪音，还让我们误以为"发现被系统丢了"；
* 「服务发现失败后再做 ATT 探针」→ 探针自己也会被这条 pending 命令挡住，
  "探针没答复"就再也分不清是**对端不回**还是**我们自己的队列堵着**。

**改法（本轮已落地）**：探针排在最前面（在队首 ⇒ 它的结论可信），服务发现只发一次，
真要重试就**换一条新链路**（新 clientIf）—— 那是 `MainViewModel` 的自动补救在做的事。

### 结论 3 · `close()` 之后协议栈还要约 1 秒才放干净

`start link idle timer = 1 sec`。所以自动重试不能"立刻"来：本轮把最短等待定到 3s，
对端类结论（`ATT_SILENT` / `DISCOVERY_DROPPED` / `SERVICE_MISSING`）给 8s。

---

## 7.6 第二轮真机（2026-10-04 22:21）· 探针串行 + 配对

用户贴回来的**新 APK**病历（这一版已经有探针和病历了）：

```
── 连接病历 #1 ──
目标: DESKTOP-xxxxxxx  79:60:24:CB:95:48  (地址来源: scan-pick)
设备: name=DESKTOP-xxxxxxx bondState=NONE(未配对) type=LE addrType=-
+3ms    I connectGatt（来源=scan-pick, autoConnect=false）
+353ms  I LL 探针 readRemoteRssi issued=true
+356ms  I requestConnectionPriority(BALANCED) -> true
+358ms  I cb onReadRemoteRssi rssi=-73 status=0
+359ms  I ATT 探针 requestMtu(517) started=true
+5862ms I LL 探针已有答复（rssi=-73）
+5863ms E LL 活着但 ATT 探针 5500ms 无答复 ⇒ 对端主机栈不处理 ATT
+5869ms W 断开回调 status=0 newState=0 -> 链路已关闭（本地）
结论: 连上了，但对端 ATT 层零回复  [ATT_SILENT / 该动: PC 侧]
判据: llAlive=true attSeen=false llProbe=答复 attProbe=无答复 services=0次(无回调) mtu=- lived=5519ms
```

**结论没变**：LL 活着（RSSI `-73dBm`）、ATT 零回复 ⇒ 仍是 `ATT_SILENT`。
但这一份病历暴露了**两个上一版没看到的问题**，都改掉了：

### ① 我们自己的 LL 探针可能把 ATT 探针的命令槽占了

`+353ms` 发 `readRemoteRssi`，`+358ms` 收到答复，`+359ms` 才发 `requestMtu` —— 两者
只隔 **1ms**。而 `requestMtu()` 返回 `true` 只代表 Java 层受理了，真正入队发生在
**BTA 层的 `bta_gattc_enqueue`**，上一条的队列槽要等 BTA 回调完才释放（§7.5 结论 2）。

那 1ms 的缝，完全可能让**我们自己的探针**被丢掉 —— 一旦如此，"对端没答复"这个
结论就不成立了。**改法**：探针串行下发（ATT 探针先发 → LL 探针后发 → 再等结论），
`requestConnectionPriority` 也挪到探针之后。

> 这不是"猜"：`bta_gattc_enqueue` 的行为是 22:03 的日志直接证明的，我们只是把它
> 用在了自己的两个探针之间。代价为零，收益是"结论永远成立"。

### ② 这一次 `bondState=NONE(未配对)` —— 目前手机侧唯一没试过的自变量

Windows 与手机的 LE 地址**都是会轮换的可解析私有地址（RPA）**。不配对（没交换 IRK）
时双方都解不开对方的地址（手机日志里的
`btm_ble_conn_complete: unable to match and resolve random address`）。
而"对端认不出我们"的表现，恰好就是本项目的现象：**LL 连得上、ATT 一个字节都不回**。

这条线索以前一直没被当成自变量试过，因为它需要**手机侧主动发起配对**。
本轮把它做成产品里的一个开关（默认开），并且：

* 配对发生在**任何 ATT 操作之前**（SMP 走自己的 L2CAP 通道，不占 ATT 队列）；
* 配对成功/失败/超时**都不中断**这次尝试 —— 我们要的正是"配对失败时的 ATT 表现"这个对照；
* 病历里多一行 `配对: 发起=… 前=… 后=… 结果=…`，判据行多一个 `bond=`；
* 结论会区分：配对没成功 ⇒ `ATT_SILENT / 该动: 两端`（先开配对）；配对成功 ⇒ 仍指 PC。

### ③ nRF Connect 那张截图是 **SERVER 页**，不是 PC 的服务表

截图里 `DISCONNECTED / NOT BONDED`，页签停在 **SERVER**，看到的是
`0x1801 Generic Attribute` + `0x1800 Generic Access` —— 这两条是**任何 BLE 设备自己
（也就是手机）都必须有的**本地服务。它**不能**证明 PC 有服务表。

要真正证明"PC 的服务表在不在"，必须：

1. 在 nRF Connect 里点右上角 **CONNECT**（不是 SERVER 页）；
2. 等它连上（左上角变成 `CONNECTED`）；
3. 切到 **CLIENT** 页，看有没有 `Unknown Service 0xA000`（以及 `a001/a002/a003`）。

三种结果的读法：

| nRF 的结果 | 说明 |
|---|---|
| CLIENT 页有 `0xA000` | PC 的服务表是好的 ⇒ 差异在**安卓侧**（本 App），要继续查 |
| CLIENT 页空 / 一直转圈 / 报错 | **连 nRF 也发现不了** ⇒ PC 的服务表这一层确实没在服务，`ATT_SILENT` 判定成立 |
| 根本连不上 | 连 PC 的连接都建立不了，问题更靠下（广播/发现） |

> 顺带说明：截图里 nRF 顶部显示的 MAC 是 `69:51:3F:18:D6:11`，而本 App 这次连的是
> `79:60:24:CB:95:48` —— **同一个名字、两个地址**，这就是地址轮换的现场证据
> （病历里的 `addrType` 一栏就是为它准备的；`getAddressType()` 是 API 35 才公开的，
> 更早的版本靠反射，拿不到会显示"未知"）。


---

## 8. 复测清单

前置：PC 上 Host 正在跑（PC 日志应有 `GATT server started, advertising=STARTED`）；
手机装本轮 APK。

### 8.1 抓日志（**必须用宽 filter**，这是本项目最重要的工具）

只看 `BleChat` 会漏掉系统蓝牙栈的证据（`bt_att` / `BtGatt` 才是 ATT 层的真相）：

```bat
adb logcat -c
adb logcat -v time | findstr /i "BleChat BluetoothGatt BtGatt bt_att bt_bta btif bt_stack bt_ble BluetoothLeScanner GattService"
```

再复现一次失败，然后把输出贴回来。特别要看：

- **有没有 `gatt_rsp_timeout`** —— 这一行出现就说明"请求发出去了、对端没回"，
  是本项目最关键的一行证据（§7.5）；
- **有没有 `GATTC_ConfigureMTU`** —— 我们的 ATT 探针（MTU 请求）到底有没有真的下发到
  控制器。**没出现**就说明丢在手机自己的队列里（那就是 Android 侧的问题，不是 PC 的）；
- `GATTC_Discover … disc_type=1`（服务发现真的下发了吗）；
- `bta_gattc_enqueue: already has a pending command`（我们自己的请求被队列丢掉了吗）；
- `onConnectionUpdated` / `onPhyUpdate`（LL 是否真的活着）；
- 我们打的 `LL 探针 readRemoteRssi issued=…` / `onReadRemoteRssi rssi=… status=…`；
- 我们打的 `ATT 探针 requestMtu(517) started=…` / `onMtuChanged …`；
- 我们打的 `配对: createBond() -> …` / `配对: 状态变化 …`（配对走到哪一步）；
- 我们打的判据一行 `判据: llAlive=… attProbe=… services=… bond=…`。

> **顺序本身也是证据**：`ATT 探针` 必须出现在 `LL 探针`**之前**，也必须在
> `discoverServices` 之前（§7.6 ①②）。

### 8.1.1 用 nRF Connect 交叉验证 PC 的服务表（**5 分钟，能一刀切开是哪一端**）

1. 打开 nRF Connect → 扫描 → 点 `DESKTOP-xxxxxxx`；
2. 点右上角 **CONNECT**，等左上角变成 `CONNECTED`；
3. 切到 **CLIENT** 页（**不是 SERVER 页**），看有没有 `0xA000`；
4. 结果按 §7.6 ③ 的表读。

**这一步比任何日志都直接**：如果 nRF 也发现不了服务，`ATT_SILENT` 的 PC 侧结论
就被第三方工具独立证实了。

### 8.2 逐项验收

- [ ] 扫到 `DESKTOP-xxxxxxx` → 点它 → **能弹出「输入共享密码」**（说明握手帧发出去了）
- [ ] 连上后发一条文本，PC 端能收到；PC 端回一条，手机能收到
- [ ] 连续发 20 条 / 一条长文本（>500 字）→ 不丢、不乱序
- [ ] 断开重连两次 → 都成功（验证 `clientIf` 没泄漏）
- [ ] 「重连上次」在**关掉再打开 PC Host**之后仍能连上（按名字重解析地址 + 清缓存）
- [ ] 失败时：面板里出现红色处置建议 + 右上角「诊断」可点开、可复制
- [ ] 面板里的「连接前先配对」开关默认是**开**的；关掉/打开会持续保存
- [ ] 病历里 `配对:` 一行如实反映本次配对（成功/超时/未尝试），判据行有 `bond=`
- [ ] 贴回来的 logcat 里能看到我们打的**判据一行**（`llProbe=… attProbe=… services=… bond=…`）
- [ ] 失败路径的日志顺序应当是：`已连接` →（配对）→ `ATT 探针 requestMtu(517) started=true`
      → `LL 探针 readRemoteRssi issued=true` →（有答复）`onMtuChanged` →
      `discoverServices started=true`。**ATT 探针必须排在最前**（§7.5 / §7.6 ①）
- [ ] 失败后应当能看到**自动补救一次**的痕迹：要么 `bondFirst=true`（换配对状态），
      要么 `autoConnect=true` + `探针前清缓存 refresh() -> true/false`
- [ ] **失败后等 40 秒再点一次**，与"连点三次"对比：前者应当更容易成功
      （见 §7.3.1 的"占用 30~60s"假设）
- [ ] 40 秒内不掉线（验证保活：日志应每 20s 一条 `保活 #n 发 PING`，
      并伴随 `收到 PONG，保活计数清零`）

### 8.3 如果还是「ATT 静默」（**当前真机就是这个结论，见 §7.5 / §7.6**）

先做**手机侧**唯一还没试过的那件事：确认面板里的「连接前先配对」是**开**的，
连一次，看病历里 `配对: … 结果=` 是什么。

- 若**配对成功**却仍然 ATT 静默 ⇒ 手机侧能做的已经做完，按下面清单动 PC；
- 若**配对超时/被拒** ⇒ 先在手机蓝牙设置里删掉 `DESKTOP-xxxxxxx` 的旧记录，
  再到 PC 上重新发起一次连接（Windows 会弹确认框），配对成功后再连。

PC 侧按顺序试，每试一步回来看手机是否连上：

1. PC 上**退出并重启 Host 程序**（让 `GattServiceProvider` 重新注册，这是最对症的一步）；
2. PC 上打开「蓝牙和其他设备」→ **把蓝牙开关关掉再打开**（等 5 秒）；
3. 或 PC 版 BLE Chat 里点 **「重置蓝牙」/ Ctrl+R**；
4. 若以上都无效：手机上「蓝牙设置 → 忽略/取消配对 `DESKTOP-xxxxxxx`」
   （或 App 诊断弹窗里的「取消配对」），再让 PC Host 重新广播一次。

**判断是否修好的依据**：手机日志里出现 `onServicesDiscovered`（或我们打的
`服务表就绪 0000a000-…`），而不是再出现 `gatt_rsp_timeout`。

---

## 9. 当前软件状态（诚实版）

### 已验证（本机可复现）

| 项 | 结果 |
|---|---|
| `:app:compileDebugKotlin` | ✅ 无警告 |
| `:app:testDebugUnitTest` | ✅ **68 项全绿**（协议向量 30 / 加密向量 6 / 互通导出 1 / 会话 12 / **病历分类 15** / **保活 4**） |
| `:app:assembleDebug` | ✅ `app-debug.apk` |
| `:app:lintDebug` | ✅ 仅版本升级类提示 |
| `python tools/verify_kotlin_vectors.py` | ✅ ALL OK（23 项） |

### 未验证（需要真机 + 两台机器）

* **连接流程从未在真机上跑通过**：22:21 那一份病历已经是"新 APK"了（有探针、有病历、
  有分类），所以 §4 的探针与分类**已经在真机上验证过有效**；但本轮新加的
  **配对（`Bonder`）**与**探针串行顺序**仍只有单测/静态推理覆盖，真机行为待 §8 复测。
* 手机侧从没成功走完过「握手 → READY」（历史日志里连口令框都没弹出来过），
  所以 `ChatSession` 的真机路径（保活、ACK、重传）**只有单测和互操作向量支撑**。
* PC 端本轮**没有改动**（用户已叫停），PC 只作为对照数据源。

### 已经确定的事（有栈级证据）

* 2026-10-04 22:03 那次失败：**手机把 ATT「Read By Group Type」发出去了，
  对端一个字节没回**（`gatt_rsp_timeout`）。链路层是通的（`onConnectionUpdated` /
  `onPhyUpdate`）。⇒ 问题在 PC 侧的 ATT/服务表这一层（详见 §7.5）。
* 2026-10-04 22:21 那次失败：**ATT 探针（MTU 请求）5.5s 无答复**，而 LL 探针 5ms 就
  回了 `rssi=-73`。⇒ 同一结论，而且这次是**我们自己的探针**给出的（§7.6）。
* PC 的**广告**是正常的（nRF Connect 能看到 `0x180A, 0xA000`）—— 所以"能扫到"与
  "能连上"在 Windows 上确实是两回事，前者不代表后者。

### 已知不确定

* 配对（SMP）到底能不能改变 PC 的 ATT 行为：**这是本轮唯一没验证过的自变量**。
  理论上它让双方能解析 RPA；但 Windows 的 `GattServiceProvider` 是否真的按
  "认得出设备"来决定是否服务 ATT，**只有真机能回答**。
* 22:21 那份病历里 `attProbe=无答复` 会不会其实是我们自己的 LL 探针占了命令槽：
  理论上可能（1ms 的缝），本轮已消除该因素；如果串行之后**仍然**无答复，
  就说明确实是 PC 侧不回 —— 这本身就是一次干净的判决。
* `refresh()` / `removeBond()` 都是**非公开 API**（反射）：若封掉，
  自动重试的"清缓存"和诊断里的"取消配对"会优雅降级（有日志、有提示）。
* `autoConnect=true` 的建链在部分 ROM 上会更慢（最长 ~30s），所以只在自动补救时用。

---

## 10. 待决（需要拍板）

1. `network_id` 是否按 `SPEC.md §2.2` 改成 `uuid5(host_device_id, service_uuid)`：
   需要 Host 在 `AUTH_OK` 里带上 device_id，并做一次历史 remap 迁移。
2. Host 侧 `CHAR_TX` 用的是 **INDICATE**（每次都要 ATT 确认）。若真机证实
   "发几条就掉线"来自指示确认超时，可评估改成 NOTIFY（**这是协议层改动，PC 端要同步**）。
3. 是否把「诊断」内容也写进文件（`files/diagnostics/`）方便直接 adb pull。
