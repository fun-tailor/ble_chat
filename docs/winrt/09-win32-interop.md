# 09 · Win32 互操作（非 WinRT，但同属 Windows 层）

blechat 还有三处不走 WinRT、而是直接打 Win32 / 注册表的地方。

## 1. DPAPI 加密 PSK（`blechat/credentials.py`）

SPEC 要求"不存密码本身"→ 本项目存的是**DPAPI 加密后的 PSK**
（`keys/credentials.json`），密文只有**本机 + 本 Windows 用户**可解。

### 关键常量

```python
DESCRIPTION = b"blechat-psk-v1"      # credentials.py:21，绑定密文用途
flags = 0x01                          # CRYPTPROTECT_UI_FORBIDDEN：禁止弹窗
```

`CRYPTPROTECT_UI_FORBIDDEN` 必须加 —— 否则在服务/异步上下文里
`CryptProtectData` 可能弹 UI 或直接失败。

### 结构与内存（`credentials.py:24-31`）

```python
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]

def _to_blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
```

**`buf` 是局部变量，但 `_DataBlob` 只持有裸指针** —— 目前靠"函数内立即调用完 API"
保证 `buf` 仍被 `ctypes` 的参数保活机制覆盖。若将来把 `_to_blob` 的结果
存起来延后使用，**必须让 `buf` 和 blob 同生命周期**（这是 ctypes 的经典坑）。

### 成对调用与释放

```python
crypt32.CryptProtectData(byref(src), DESCRIPTION, None, None, None, 0x01, byref(out))
...
ctypes.string_at(out.pbData, out.cbData)      # 复制出来
finally:
    ctypes.windll.kernel32.LocalFree(out.pbData)   # ← 用 LocalFree，不是 free/CoTaskMemFree
```

| 要点 | 说明 |
| --- | --- |
| 两个 API **都**用 `LocalFree(out.pbData)` | `credentials.py:56`、`credentials.py:84` |
| 任何失败都返回 `None` | `credentials.py:51/73` —— 上层降级为弹密码框，**不阻塞启动** |
| 密文 base64 后存 JSON | `credentials.py:101` |
| `save_psk` / `get_psk` / `forget` / `forget_all` 带 `root=` 参数 | 便于测试隔离 |

> **安全边界**：DPAPI 是**用户级**——同一 Windows 账户下的别的进程能解。
> 换用户 / 复制文件到另一台机器 → 解不开 → `get_psk` 返回 `None` → 弹密码框。
> 这是有意的降级路径（`credentials.py:5`）。

## 2. 读注册表判主题（`blechat/ui/theme.py:51-63`）

```python
def system_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return False
```

| 项 | 值 |
| --- | --- |
| 键 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` |
| 值名 | `AppsUseLightTheme`（**0 = 暗色，1 = 亮色**） |
| 判定 | `int(value) == 0` → 暗色 |
| 另一个值 | `SystemUsesLightTheme`（系统 UI/任务栏，本项目不读） |
| 失败 | `except Exception: return False` → 亮色兜底 |

注意读的是 **`HKCU`**（当前用户，不需要权限）；**Win7 没有这个键**
→ `OpenKey` 抛 `FileNotFoundError` → 走 except → 亮色，正好是合理默认。
`theme="auto"` 时才调用它（`theme.py`），`theme="dark"/"light"` 直接固定。

## 3. DirectWrite 字体：位图字体才是正确选择

`blechat/ui/style.qss` 给控件显式指定 `font-family`。Windows 上要点：

| 字体 | 说明 |
| --- | --- |
| `MS Sans Serif` / `Fixedsys` | **位图字体**，小尺寸锐利清晰、零抗锯齿糊边 |
| 通用字体关键字（`Arial`、`Segoe UI`、`sans-serif`…） | 走 DirectWrite **可变 hinting + gamma 校正**，小字号发虚 |

项目采用位图字体（`font-family` 见 `style.qss`），
是**有意**避开 DirectWrite 的平滑策略。相关经验值：

- 12–13px 正文 + 位图字体在 100% 缩放下最清晰；
- 高 DPI（125%/150%）下位图字体由系统放大，可能发虚 —— 需要在真机上选点确认；
- 不要靠 `QApplication.setFont` 改整体，`style.qss` 里逐控件指定才生效。

## 4. 隔离运行时**必须**避开的文件

两台 dev 机器上，这些是真实数据；冒烟/测试一律用隔离根
（`%LOCALAPPDATA%\Temp\opencode\smoke_*.py`），**不碰**：

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `keys/identity.json` | 设备身份密钥 | `blechat/identity.py` |
| `keys/credentials.json` | DPAPI 密文 PSK | `credentials.py:90` |
| `config.json` | 配置 | `config.py` |
| `networks.json` | 网络清单 | UI 层 |
| `history.db` | 聊天历史 | `history.py` |

`app_root()`（`config.py:15-19`）决定了这些文件的位置：
打包后在 exe 同级，源码运行在项目根 —— **测试必须传 `root=` 或
用 `sys.frozen` 模拟**，否则会在项目根留下真实状态。
