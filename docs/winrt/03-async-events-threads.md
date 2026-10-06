# 03 · 异步、事件回调与线程模型

## 1. `xxx_async` → 直接 `await`

pywinrt 把 WinRT 的 `IAsyncOperation<T>` 包成**可 await 的对象**：

```python
>>> op = BluetoothAdapter.get_default_async()
>>> type(op).__name__
'_IAsyncOperation'
>>> hasattr(op, '__await__')
True
>>> res = await op          # BluetoothAdapter
```

项目里的用法：

```python
result = await GattServiceProvider.create_async(UUID_SERVICE)   # server.py:195
cres   = await service.create_characteristic_async(uuid_, params)  # server.py:211
adapter = await BluetoothAdapter.get_default_async()            # server.py:477
request  = await request_op                                     # server.py:287
```

规则：

- `await` 的返回值就是 WinRT 结果对象本身（不是 `(result, error)` 元组）；
  创建类的 API 用 `result.error` 判成功。
- **取消**：`asyncio.cancel()` 一个 await WinRT 的任务，pywinrt 会尝试取消底层操作；
  不保证立刻返回 —— 所以本项目所有底层写都再包一层
  `asyncio.wait_for(..., timeout=SEND_TIMEOUT)`（`session.py:208-211`、`server.py:398`）。

## 2. 事件回调**不在** asyncio 线程

WinRT 的事件（`add_write_requested`、`add_session_status_changed`、
`add_advertisement_status_changed`…）由 **COM 线程池（MTA）** 触发，
和 `asyncio` 主线程不是同一个线程。直接在里面改 asyncio 状态是**竞态**。

本项目的统一做法：`GattServer._schedule()`（`server.py:261-268`）

```python
def _schedule(self, fn):
    loop = self._loop
    if loop is None:
        return
    if threading.get_ident() == self._loop_thread:
        fn()                        # 已经在事件循环线程 → 直接跑
    else:
        loop.call_soon_threadsafe(fn)  # 否则投递回去
```

所有事件回调都**只**做"包一层 `fire()` 然后 `_schedule(fire)`"：

| 回调 | 位置 |
| --- | --- |
| `_make_write_handler` → `handle()` | `server.py:270-314` |
| `_on_subscribed_changed` → `fire()` | `server.py:363-371` |
| `_on_adv_status` → `fire()` | `server.py:373-381` |
| `on_status`（session 状态） → `fire()` | `server.py:336-354` |

**为什么值得记**：PyQt6 slot 里抛异常会直接把进程打死（0xC0000409、无 traceback），
所以"回调里只调度、不执行"这条纪律同时保护了 asyncio 和 Qt 两条链路。

> bleak 侧是同样的模式：`loop.call_soon_threadsafe(handle_session_status_changed, args)`
> （bleak `backends/winrt/client.py:318`）。

## 3. deferral：把"还没处理完"告诉 Windows

写/读请求事件是**同步回调**，Windows 默认认为你同步处理完了。
要异步处理必须先 `get_deferral()` 拿到令牌，处理完 `complete()`：

```python
deferral = args.get_deferral()      # server.py:276（同步）
...
finally:
    deferral.complete()             # server.py:321
```

- **`get_deferral()` 必须在事件回调栈内调用**。事件返回后再取 →
  `E_ILLEGAL_METHOD_CALL (0x8000000E)`（`server.py:272-273` 注释）。
- `complete()` 可能抛（对象已关）→ 本项目只 `log.debug` 吞掉（`server.py:323`）。
- 三条释放路径都要覆盖：解析失败（`server.py:282`）、调度失败（`server.py:312`）、
  正常结束（`server.py:306`）。

## 4. 同步 vs 异步的边界清单

| 操作 | 必须同步（事件栈内） | 可以异步 |
| --- | --- | --- |
| `args.get_deferral()` | ✅ | |
| `args.session` | ✅ | |
| `args.get_request_async()` | ✅（**取 op 对象**） | `await request_op` |
| `request.respond()` | | ✅（但建议尽早） |
| `from_buffer(request.value)` | | ✅ |
| `char.notify_value_for_subscribed_client_async(...)` | | ✅ |
| `provider.start_advertising_with_parameters(...)` | | ✅（本项目在 async `_advertise` 里） |
| `session.close()` | | ✅（同步方法，但在事件里调要先 `_schedule`） |

## 5. 事件注册的 token / 反注册

WinRT 的 `add_xxx` 返回 `EventRegistrationToken`，反注册要传回去：

```python
self._session_status_changed_token = self._session.add_session_status_changed(handler)
...
self._session.remove_session_status_changed(self._session_status_changed_token)
```

本项目 **Host 侧不反注册**（`GattServer.stop()` 直接丢弃 provider/chars，
靠对象生命周期自然回收）；bleak 内部则严格成对管理
（`client.py:342-346` / `client.py:470-474`）。这是"谁持有谁负责"的差异，
不是遗漏 —— `server.stop()` 之后 `_provider = None`，事件源已经不可达。

## 6. 一个隐蔽陷阱：事件可能"不来"

`GattSession` 已经是 `ACTIVE` 时，`session_status_changed` **不会**补发一次
（bleak 在 `client.py:355-360` 特意手工 `event.set()` 补齐这个缺口）。

含义：**不要靠事件做"初始状态同步"**，要主动读一次当前状态。
本项目 `GattServer` 靠的是"收到写帧才 `_track_session`"这条被动路径，
所以不依赖初始事件；但读 `advertising_status` 属性时同样要读**当前值**而不是等事件
（`server.py:137-143`）。
