# blechat

蓝牙传消息(PyQt6)，基于 BLE，用于 pc 之间 传递文本、图片与文件（Windows）
软件开发，主要是遇到 没法联网的电脑 需要传递消息 和 小文件的场景(如果大的文件 建议走wifi)

## 目前状态
- 版本 0.0.1
- android 端还不可用(连接会断，加上多端交互)
- 还有一些 **bug**
    - 偶然 UI 会卡（重启软件）
    - OS sleep 唤醒后 无法直接重连（重启软件）
    - 边界调整窗口 鼠标会显示异常 (不影响操作)
- 有些功能没有完全测试（没有用到）


## 技术相关
- 开发库：PyQt6,qasync(异步),cryptography(加密),bleak 和 winrt
- 和 winrt 库相关在 `blechat\ble\server.py` ，bleak 用于 client 端，server 端用 winrt 库实现（没找到合适的 支持host(peripheral)的python库）
- 开发时没考虑打包，如要打包 `/assets/send.svg` 也要加入 

## 使用

- 入口是 **`main.py`**
- 一台 pc 选择 `Host` 模式（服务器）；另一台选择 `Join`模式 → 点`加入网络`(扫描) → `连接`加入(输入密码) → 成功连上
- 运行后会生成 配置文件，根目录有 `config.json`，`networks.json`, 还有db文件; `/keys/` 还有 `identity.json` 和 `credentitals.json`

## 软件截图

![UI](/assets/screenshot/UI.webp)


















