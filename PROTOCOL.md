# BLE Chat Protocol v1

> 本文件描述 `blechat` 实际实现的线格式，可直接用于 Android 等跨平台实现。
> 与 `SPEC.md` §4/§12 的差异集中列在最后一节。

## 1. 传输层

| 项 | 值 |
|---|---|
| Service | `0000a000-0000-1000-8000-00805f9b34fb` |
| CHAR_RX | `0000a001-…` 属性 Write \| WriteNR，Client → Host，承载**数据帧** |
| CHAR_TX | `0000a002-…` 属性 Indicate，Host → Client，承载**数据帧** |
| CHAR_CTRL | `0000a003-…` 属性 Write \| WriteNR \| Notify，**双向**，承载**控制帧** |

- MTU 协商目标 512，最小 23（WinRT `max_pdu_size` / bleak `mtu_size`）。
- 每片数据帧可用载荷 = `max(8, mtu - 3 - 16)`（3 为 ATT 头，16 为本协议数据帧头）。
- Client 侧写 CHAR_RX 优先 WriteNR，失败回退 WriteR；Host 侧一律用
  `notify_value_for_subscribed_client_async`（CHAR_TX 走 Indicate）。
- 本机 MAC 可用 `GattServer`/`BluetoothLEDevice` 查询，用于 UI 显示与直连。
- **Host 侧硬性要求**：`WriteRequested` 事件里必须**同步**取 `GetDeferral()`、
  启动 `GetRequestAsync()`、读 `args.Session`，之后才能返回事件回调；事件返回
  后再取会抛 `E_ILLEGAL_METHOD_CALL (0x8000000E)`，导致写请求永不完成，客户端
  收到 `GATT Protocol Error: Unlikely Error (0x0E)`。取到 request 后应**先
  `Respond()`** 再解析数据，解析失败也不能让客户端干等。

## 2. 帧格式

### 2.1 控制帧（CHAR_CTRL，双向）

```
offset  size  field
0       1     magic = 0xB1
1       1     type
2       2     len (u16 LE)
4       len   payload
```

| type | 名称 | 方向 | payload |
|---|---|---|---|
| 0x01 | HELLO | C→H | `device_id(16) ‖ name_len(u8) ‖ name(UTF-8) ‖ nonce_c(16) ‖ use_eph(u8) ‖ proto_ver(u8) [‖ eph_id(8)]` |
| 0x02 | CHALLENGE | H→C | `nonce_s(16) ‖ salt(16) ‖ iters(u32 LE) ‖ use_eph(u8)` |
| 0x03 | AUTH | C→H | `HMAC-SHA256(PSK, nonce_c‖nonce_s)` (32) |
| 0x04 | AUTH_OK | H→C | `session_id(u32 LE) ‖ hkdf_salt(16) [‖ name_len(u8) ‖ name(UTF-8)]` |
| 0x05 | AUTH_FAIL | H→C | `reason(u8)` |
| 0x06 | ACK | 接收方→发送方 | `msg_id(u32 LE) ‖ next_seq(u16 LE)` |
| 0x07 | BYE | 双向 | `reason(u8)` |
| 0x08 | PEERS | H→C | `count(u8) ‖ [device_id(16) ‖ name_len(u8) ‖ name(UTF-8)]*` |
| 0x09 | PROGRESS | 接收方→发送方 | `msg_id(u32 LE) ‖ acked(u16 LE) ‖ total(u16 LE)` |
| 0x0A | PING | 双向 | 空。READY 后每 20s 发一帧用于保活；接收方**必须回一帧 `PONG`** |
| 0x0B | RESYNC | 双向 | 空。**任一端进入 READY 时发一帧**，对端收到后无条件刷新一次状态/UI 并（Host 侧）重发 PEERS |
| 0x0C | PONG | 双向 | 空。收到 `PING` 的应答，发送方据此确认链路**双向**仍然可用 |

- `device_id` 是 16 字节 UUID（`keys/identity.json` 中的本机 id）。
- `name` 最多 64 字节（UTF-8 截断到字符边界），取 `Identity.display_name()`：
  配置了**设备别名**（`keys/identity.json` 的 `alias`，设置对话框可改）就发别名，
  否则发系统名。对端把它作为 `PEERS` 列表与气泡头的显示名。
- `eph_id` 只在 `use_eph=1` 时存在，8 位大写 HEX，不足补 `\x00`。
- `proto_ver = 1`；Host 校验不匹配即 `PROTO_VER_MISMATCH` + BYE 并断开。
- `AUTH_OK` 末尾的 `name` 是 Host 的**昵称**（`Identity.display_name()`，与 `HELLO.name`
  同规则）。它是**可选追加**字段：解析时先按 `session_id(4) ‖ hkdf_salt(16)` = 20 字节
  取定值，再读剩余字节里的 `name_len ‖ name`；没有剩余字节（20 字节的老帧）就是空名。
  ⚠️ 方向是 H→C，而老 Client 的 `AuthOk.decode` 是 `len == 20` 的严格解析，
  **老 Client 收到新 Host 的 20+N 字节帧会解码失败** —— 双端同源同版本时无影响，
  跨版本组合需要先升级 Client。
  协议里原先只有 C→H 的 `HELLO.name`，Host 没有任何反向携带昵称的字段，
  导致 client 侧对 Host 的显示始终是 BLE 广播名（代号）。
- **保活与半开链路判定（PING / PONG）**：READY 后每 `KEEPALIVE_INTERVAL=20s`
  发一帧 `PING`，收到 `PING` **必须回一帧 `PONG`**。

  为什么必须回：判断"链路还活着"不能只看**发得出去**。对端软件已经下线、
  Windows 却还把那条 LE 链路留在"已连接"状态时（半开链路），
  Host 侧 `notify_value_for_subscribed_client_async` 会**静默成功** ——
  帧写进本地缓冲就算完成，既不抛错也不返回失败。于是两端都停在 READY：
  一边显示已连接、另一边发送按钮灰，而且**永远**不会自己恢复。
  `PONG` 是唯一能低成本证伪它的信号。

  判定阈值（两端对称）：
  ```
  每次收到 PONG        → 计数清零，截止时间 = now + 20s + 8s（写超时）
  截止时间过去一轮     → 记为一次 miss
  miss <= 2（宽限）    → 只记 DEBUG，继续等（对端可能只是忙/慢）
  miss 再连续攒到 3    → 判链路已死 → 本地 close() → 上层退避重连
  ```
  实际动手大约在 80~140s 之后，足够保守；上层看到的提示是
  「连接断开：对方已离线」，细节（`no PONG from peer after N keepalives`）只进日志。

  ⚠️ 旧版本按 §7 静默忽略未知类型 `PONG`，收到它不会出错；但它们**不回**
  `PONG`，所以与新版本混用时这一侧不会主动判死（退回"只靠发送失败判定"的老行为）。
  要拿到完整的半开链路自愈能力，需要两端都是本版本。

### 2.2 数据帧（CHAR_RX / CHAR_TX）

```
offset  size  field
0       1     magic = 0xB2
1       4     msg_id (u32 LE)
5       2     seq (u16 LE)
7       2     total (u16 LE)
9       1     flags
10      2     aad_len (u16 LE) = 9
12      4     aad_crc32 (u32 LE) = CRC32(msg_id ‖ seq ‖ total ‖ flags)
16      N     ciphertext chunk
```

`flags`：bit0 = 压缩；bit1 = 图片；bit2 = 最后一片；bit3 = **文件**；bit4+ 保留。

- `kind_of_flags()` 优先级：**文件 > 图片 > 文本**（bit3 与 bit1 互斥）。
- `aad_len` 必须等于 9，否则拒绝（为将来扩展保留）。
- `aad_crc32` 覆盖前 9 字节 AAD，任何一位翻转都会 `CRC_MISMATCH` → BYE 断开。

## 3. 握手

```
C→H: HELLO {device_id, name, nonce_c(16), use_eph, proto_ver [, eph_id]}
H→C: CHALLENGE {nonce_s(16), salt(16), iters(u32), use_eph}
C→H: AUTH {HMAC-SHA256(PSK, nonce_c‖nonce_s)}
H→C: AUTH_OK {session_id(u32), hkdf_salt(16)[, name]} | AUTH_FAIL {reason}
```

PSK 派生：

```
普通: PSK = PBKDF2-HMAC-SHA256(password, salt, iters, dklen=32)
      salt/iters 来自 CHALLENGE（即 Host 的 networks.json auth 参数）
临时: PSK = eph_key（32B 直接使用，跳过 PBKDF2）
```

session_key：

```
session_key = HKDF-SHA256(PSK, salt=nonce_s(=AUTH_OK.hkdf_salt), info="blechat-session-v1", L=32)
```

- Host 校验 AUTH 通过后**立即**清空 PSK；Client 收到 AUTH_OK 后同样清空。
- 使用临时密钥时，Host 在 AUTH 成功后**立刻**删除该 `eph_id`（一次性），
  过期/不存在分别回 `EPH_EXPIRED` / `EPH_NOT_FOUND`。
- 连续 3 次 AUTH 失败 → Host 发 BYE 并断开（`MAX_AUTH_FAILURES=3`）。
- READY 后 Client 自动触发系统 BLE 配对（`BleakClient.pair()`）；配对失败只影响
  L1，不影响已建立的应用层会话。
- PEERS 广播：任一 Host 会话进入 READY、或有会话关闭时，Host 向所有 READY 会话
  推送当前对端列表（`device_id + name`），Client 据此渲染对端芯片。

## 4. 数据发送

```
1. 明文 payload（text = UTF-8；image = 原始图片字节，推荐 PNG；file = 见下）
2. 若非图片/文件且 len>=128 且压缩收益>=10% → zlib，flags.bit0 = 1
3. blob = AES-256-GCM(session_key, nonce=12B random, aad)
        返回 nonce(12) ‖ ciphertext ‖ tag(16)
4. blob 按 max_chunk 切成 total 片，每片套数据帧头
5. flags.bit2 只在最后一片置 1
```

- **文件消息**（`flags.bit3 = 1`，**不压缩**）明文 layout：

  ```
  u32 meta_len (LE) ‖ JSON meta (UTF-8) ‖ file bytes
  meta = {"filename": str, "mime": str, "size": int}
  ```

  接收方 `FileMeta.decode(payload)` 还原；`meta.size` 与实际字节数不符即丢弃。
  端到端限制：**单文件 2 MB**（`MAX_FILE`，发送侧先判 `len(data) > MAX_FILE`
  → `MSG_TOO_LARGE`），图片仍 5 MB（`MAX_PAYLOAD`）。

- **AAD** 固定为 `struct.pack("<IHHB", msg_id, 0, total, flags0)`，即
  `msg_id ‖ seq=0 ‖ total ‖ flags(seq0)` 共 9 字节，整条消息只用这一份 AAD。
- **nonce(12B)** 放在 blob 开头 ⇒ 出现在第 0 片密文的最前面。
- **tag(16B)** 放在 blob 结尾 ⇒ 出现在最后一片密文的末尾。
- 单条消息上限 5 MB（`MSG_TOO_LARGE`），分片数上限 0xFFFF。

## 5. 可靠传输

| 项 | 值 |
|---|---|
| 发送窗口 | 4 片 |
| ACK 超时 | 500 ms |
| 最多重传 | 3 次（超限 → `TOO_MANY_RETRIES` + BYE） |
| ACK | 每收到**连续**新进度回一次 `ACK{msg_id, next_seq}` |
| PROGRESS | 每 8 片或收满时回 `PROGRESS{msg_id, acked, total}` |
| 重组 TTL | 60 s（超时丢弃半成品） |

- `next_seq` / `acked` 的语义是"已**连续**确认的分片数"（`0..total`），
  不是"下一个期望的下标"。
- 发送方 `acked` 单调递增；只有收到更大的 ACK 才重置 `retries` 与计时。
- **接收方合并 ACK**：同一 `msg_id` 上"还没来得及发出去"的进度只保留**最新**的
  那一个，由一个常驻任务逐条发出。这**不改变线格式**，只减少控制帧调度压力 ——
  每帧 spawn 一个发送任务会把事件循环排满 WinRT 异步写，真机表现就是
  host 端 UI 卡住（连日志都不再刷）。发送方的重传判定因此要看**合并后**的到达
  节奏：只要窗口内有 ACK 到达就不会触发重传。
- 未 READY 时收到数据帧 → `NOT_READY` + BYE。
- CRC 校验失败 → `CRC_MISMATCH` + BYE（不尝试恢复）。

## 6. 错误码

| code | 名称 | 说明 |
|---|---|---|
| 0x00 | OK | |
| 0x01 | NOT_READY | 未完成握手就收数据 |
| 0x02 | AUTH_FAILED | HMAC 校验失败 / 密码错误 |
| 0x03 | EPH_EXPIRED | 临时密钥过期 |
| 0x04 | EPH_NOT_FOUND | eph_id 不存在 |
| 0x05 | TOO_MANY_RETRIES | 分片重传超过 3 次 |
| 0x06 | MSG_TOO_LARGE | 文本/图片 > 5 MB，或文件 > 2 MB |
| 0x07 | CRC_MISMATCH | 分片 CRC 校验失败 |
| 0x08 | PROTO_VER_MISMATCH | proto_ver 不匹配 |
| 0x09 | SESSION_REPLACED | 同一 peer_id 上新握手顶替旧会话（`retire()`，不关传输层） |
| 0x0A | SERVER_SHUTDOWN | Host 主动关闭 |
| 0x0B | UNKNOWN | 未分类错误 |
| 0x0C | HANDSHAKE_TIMEOUT | 握手 15s 无控制帧进展（本地产生，BYE 也用它） |

`AUTH_FAIL` / `BYE` 的 payload 只有一个字节的 reason。

握手超时（0x0C）仅由发起握手的一方在本地产生：Client 发 HELLO 后若 15 秒内
没有收到任何控制帧（用户输密码期间暂停计时），即判定超时 → 发 BYE → 关闭
会话 → 上层按 3/6/12/24/30s 退避重连。Host 收到该 BYE 后正常清理会话。


## 7. 版本与兼容

- `proto_ver = 1`，在 HELLO 内协商，不匹配直接断开（不做降级）。
- 控制帧头带 magic + 长度；**未知 type 的控制帧一律静默忽略**（向后兼容，如 0x0A PING），
  数据帧带 magic + CRC，`aad_len != 9` 一律拒绝。
- **`RESYNC(0x0B)` 收到方不回包**：双方各自在进入 READY 时各发一次即可覆盖双向，
  回包会让两端 ping-pong 成环。旧版本收到后按上面的规则静默忽略。
- **`PONG(0x0C)` 只作为 `PING` 的应答**，不在别处主动发；收到 `PONG` 不做任何回包。
  旧版本忽略它 → 见 §2.1 的混用说明。
- 纯字节流实现，不依赖 WinRT/bleak，可直接移植到 Android `BluetoothGatt` /
  `BluetoothGattServer`。

## 8. 与 SPEC.md 的差异

| 位置 | SPEC | 实现 | 原因 |
|---|---|---|---|
| HELLO | `…‖use_eph(u8)` | 之后追加 `proto_ver(u8)`，`use_eph=1` 时再追加 `eph_id(8)` | §10 要求版本协商；§5.4 要求携带 eph_id，原表没有字段位置 |
| AAD | "aad = header" | 固定 9 字节 `msg_id‖seq0‖total‖flags0` | header 含 CRC/nonce 相关字段，用固定 9 字节可让两端独立重算 |
| nonce/tag | "nonce 放首片前 / tag 放末片尾" | 相同（整条 blob 加密后再切片） | 一致 |
| ACK 方向 | 表内 `H→C` | 双向（接收方→发送方） | Client 发消息时也需要 ACK |
| PROGRESS 方向 | 表内 `H→C` | 双向 | 同上 |
| `next_seq` | 未定义语义 | "已连续确认的分片数" | 与重传窗口实现对齐 |
| session_key | `HKDF(PSK, salt=nonce_s, info=…)` | 相同；AUTH_OK 的 `hkdf_salt` 即 `nonce_s` | 一致 |
| PSK 派生 | Client 用 networks.json 的 salt/iters | 用 CHALLENGE 下发的 salt/iters（与 Host 相同） | 首次加入时本地还没有 auth 参数 |
| Host PSK | 常驻内存缓存 30 分钟 | v1.1 为内存缓存（密码本身不落盘，只存 verifier）；**v1.2 改为把 PSK 用 Windows DPAPI 加密写入 `keys/credentials.json`**（`config.persist_credentials`，默认开），重启免输密码，verifier 不匹配就地 `forget()` | 真机反馈"每次重启要输密码" |
| HELLO `name` | 未定义来源 | 取 `Identity.display_name()`：`alias`（设置里可改）优先，回退系统名 | v1.2 设备别名需求，线格式无需改 |
| `flags` bit3 | "bit3=保留" | **bit3 = 文件**；`kind_of_flags` 优先级 文件 > 图片 > 文本 | v1.2 文件传输 |
| 文件 payload | 无 | `u32 meta_len ‖ JSON{filename,mime,size} ‖ bytes`，不压缩，单文件 ≤ 2 MB | v1.2 文件传输 |
| SESSION_REPLACED | "预留" | 已实现：同 peer_id 新 HELLO → 旧会话 `retire()`（**不关传输层**）→ 新会话接管 | 修死会话被复用导致 `RO_E_CLOSED(0x80000013)` |
| 临时密钥 | `eph_keys.json` 存 hash + 过期 | 是；真实 key 只在内存，重启即失效（表现为 `EPH_NOT_FOUND`） | §3.4 "真实 eph_key 只在 UI 显示一次，不落盘" |
| 直连 | 无 | 支持按 MAC 直连（绕过扫描） | Windows 定位权限被拒时扫描恒为空 |
| 握手超时 | 未定义 | 本地错误码 `0x0C HANDSHAKE_TIMEOUT`：15s 无控制帧进展即 BYE+关闭，上层退避重连（用户输密码期间暂停） | 否则握手失败会永久卡在 HANDSHAKE |
| 系统配对 | §10/§14：握手成功后自动弹系统配对 | **默认不发起**（`config.json` 的 `system_pairing=false`）；两台 Windows 实测 `PairAsync` 约 30s 后 `FAILED`，且 Windows 会顺带把链路断开 → 发送在 30s 后失效、重连连续超时。本协议自身用 PSK+AES-GCM 端到端加密，不依赖链路加密；如确需可显式置 `system_pairing=true` 自担风险 | 真机实测回归 |
| 链路保活 | 未定义 | READY 后每 20s 发一帧 `PING(0x0A)`（空 payload），**收到 PING 必须回 `PONG(0x0C)`**；宽限 2 轮、之后连续 3 轮收不到 PONG 判链路已死 | Windows 可能对空闲 LE 链路做电源管理断开；更重要的是**半开链路**下 Host 的通知写会静默成功，只判"发得出去"永远发现不了对端已下线 —— 界面上就是"一侧已连接、另一侧按钮灰" |
| `AUTH_OK` name | 表内只有 `session_id‖hkdf_salt` | 末尾**可选追加** `name_len(u8)‖name`，承载 Host 昵称 | 协议里只有 C→H 的 `HELLO.name`，client 侧对 Host 的显示一直是广播名（代号）；⚠️ 老 Client 按 20 字节严格解析，跨版本需先升 Client |
| `RESYNC(0x0B)` | 无此帧 | 任一端进入 READY 时发一帧（空 payload），对端无条件刷新状态/UI 并（Host）重发 PEERS | 修"单方已连接"：一侧重启/睡眠唤醒后，另一侧要等 20s PING 才发现，期间按钮灰、状态不同步 |
| 链路已死判定 | 未定义 | 本地判定 `is_link_dead()`：`PeerGone` / `RO_E_CLOSED(0x80000013)` / 文本含 `GATT Protocol Error`、`not connected` 等 → **立即 `close()` 触发上层重连**；底层写一律套 `asyncio.wait_for(…, 8s)` | 以前只打 WARNING、会话停在 READY → 上层 watchdog 认为一切正常，**永远重连不上**（dev：server 重启后 client 发送按钮/进度条卡死、报 `GATT Protocol Error: Unlikely Error`） |
| Client 重连 | "3s→30s 退避重试" | 每次尝试先用**一次**全量扫描解析对端（`resolve_peer`：3s 按地址 → 8s 按名字+服务 UUID）；解析不到就**跳过这一轮**；连不上的 `BleakClient` **一律 disconnect（dispose）**；连接与 notify 各有硬时限；连续失败 3 次触发端口级自愈 | WinRT 只认设备表里的地址，而设备表只有**扫描过**才刷新；**且对端地址会变**（系统为目标设备轮换/重分配对外地址，真机日志里同一个 Host 先后是 `6A:15:F4:…`/`48:0A:97:…`/`57:69:77:…`）⇒ `networks.json` 里那个地址恒连不上（dev：「重启 client 也很难连接」）。稳定的身份只有**名字**，所以必须有名字兜底 |
| 对端地址学习 | 未定义 | 解析出的新地址会写回 `networks.json`（`host_address`），并把 `peer_id` 跟着更新 | 下次重启就能按地址快查命中，不必每次等一轮 8s 兜底扫描；`peer_id` 变了但 `network_id` 不变 ⇒ 历史不受影响 |
| Host 残留会话 | 未定义 | `GattServer.start()` / `revive()` 在**没有 READY 会话**时清掉残留订阅与僵尸会话（`sweep_stale`）；`_subscribed()` 优先挑底层还活着的订阅；同一 peer 出现两条订阅时记 DEBUG | 对端软件直接下线时 WinRT 可能两个事件都不回调，`subscribed_clients` 里留下幽灵条目 → 新连接撞上它 → 「双断开后点加入网络，server 单方显示已连接、client 按钮灰」 |
