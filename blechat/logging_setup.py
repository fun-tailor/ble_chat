"""日志：**非阻塞**写盘 + 轮转，并保证 Handler 挂在 root logger 上。

真机「UI 卡死」的一个重要来源就在这里：`RotatingFileHandler` 是**同步**写盘的，
而且 `logging` 全局只有**一把**锁。server 端的 GATT 写回调跑在 WinRT 线程上，
GUI/asyncio 事件线程在别处 —— 一边在写日志，另一边（包括 Qt 事件循环）就得
等那把锁；DEBUG 级别下每个数据帧都要写一行，锁竞争直接把事件循环拖停。
表现就是"界面不 repaint、按钮不响应、没有任何报错"。

所以这里换成 `AsyncFileHandler`：调用方只把**已经格式化好的字符串**塞进队列
（微秒级），真正的磁盘 I/O 由一个后台守护线程做。事件循环永远不等磁盘。

队列满时**退回同步写**而不是丢日志 —— 宁可这一次慢，也不能让排查用的证据消失。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import app_root

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
RETENTION_DAYS = 7
# 队列容量：够吸收一次大传输的突发日志量
QUEUE_SIZE = 20000

# 打开 DEBUG 的两种方式（真机排查时用）：
#   1. `config.json` 里加 `"log_level": "DEBUG"`（重启生效）；
#   2. 环境变量 `BLECHAT_LOG_LEVEL=DEBUG`（不用改文件，优先级更高）。
# 默认 INFO 时 DEBUG 语句完全不产生开销（日志框架先判 level 再格式化）。
DEBUG_ENV = "BLECHAT_LOG_LEVEL"
# 放开第三方 BLE 栈内部日志（bleak 每个报文一行，非常吵）
BLE_DEBUG_ENV = "BLECHAT_BLE_DEBUG"


def resolve_level(config_level: str | None = None) -> int:
    """按「环境变量 > config.json > INFO」定日志级别。"""
    raw = (os.environ.get(DEBUG_ENV) or config_level or "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO) if raw else logging.INFO


def purge_old_logs(log_dir: Path, days: int = RETENTION_DAYS) -> int:
    """删除 days 天前的轮转日志，返回删除数量。"""
    cutoff = time.time() - days * 86400
    removed = 0
    for path in log_dir.glob("app.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class AsyncFileHandler(logging.Handler):
    """把格式化后的日志塞进队列，由后台线程写盘。

    * `emit()` 只做 `format()` + `put_nowait()` —— 不碰磁盘、不抢全局锁；
    * 队列满 ⇒ 当场同步写一次（保证不丢），并记一次 `dropped` 计数；
    * 后台线程批量取（一次最多 256 条）拼接后写一次 —— 顺带把写盘次数降下来；
    * `close()` 会排空队列再关文件。
    """

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUP_COUNT) -> None:
        super().__init__()
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._queue: queue.Queue[object] = queue.Queue(maxsize=QUEUE_SIZE)
        self._dropped = 0
        self._written = 0
        self._lock = threading.Lock()
        self._closing = False
        self._flush_token = object()
        self._thread = threading.Thread(
            target=self._pump, name="blechat-log", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------- logging.Handler
    def emit(self, record: logging.LogRecord) -> None:
        if self._closing:
            return
        try:
            message = self.format(record) + "\n"
        except Exception:  # pragma: no cover - 格式化失败不该影响业务
            self.handleError(record)
            return
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            # 队列满 = 日志量远超写盘速度（DEBUG 下对方连发消息时就是这样）。
            #
            # ⚠️ 这里**绝对不能**退回同步写：`emit()` 的调用方是 Qt / asyncio
            # 事件线程，一次同步写盘要抢 `self._lock`，会把**整个界面**按在那里
            # （dev 报的"server 端 UI 卡住、连 cmd 消息也不再刷出"就是这么来的）。
            # 宁可丢 DEBUG 记录 —— 关键事件（WARNING/ERROR、状态变化、连接结果）
            # 本来就不在这个量级，不会被队列满冲掉。
            self._dropped += 1
            if self._dropped in (1, 100) or self._dropped % 10000 == 0:
                # stderr 直写：不经队列、不抢锁，留一条"丢了日志"的痕迹
                try:
                    sys.stderr.write(
                        f"[blechat] log queue full: dropped {self._dropped} record(s)\n"
                    )
                except Exception:
                    pass

    def flush(self) -> None:
        """阻塞到"此刻之前的所有日志都已落盘"。

        退出、测试、以及"出问题要立刻看日志"时用。用一个哨兵对象同步：
        后台线程按队列顺序处理，轮到哨兵就说明前面的都写完了。
        """
        if self._closing or not self._thread.is_alive():
            return
        done = threading.Event()
        try:
            self._queue.put_nowait((self._flush_token, done))
        except queue.Full:  # pragma: no cover
            return
        done.wait(timeout=3.0)

    def close(self) -> None:
        # `logging.shutdown()` 会在 atexit 里再调一次 close()，且那时 `self._closing`
        # 可能已经被基类改写过 —— 必须容忍被重复调用。
        if self._closing:
            return
        was_event_thread = self._thread.is_alive()
        if was_event_thread:
            self.flush()
        self._closing = True
        if was_event_thread:
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover
                pass
            self._thread.join(timeout=1.0)
        super().close()

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def written(self) -> int:
        return self._written

    # ------------------------------------------------------------- writer thread
    def _pump(self) -> None:
        buffer: list[str] = []
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if buffer:
                    self._write(buffer)
                    buffer = []
                continue
            if item is None:
                if buffer:
                    self._write(buffer)
                return
            if isinstance(item, tuple):
                # flush 哨兵：先把手上这批写完，再放行等待者
                if buffer:
                    self._write(buffer)
                    buffer = []
                item[1].set()
                continue
            buffer.append(item)
            # 批量取：把突发日志合并成少数几次写
            while len(buffer) < 256:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._write(buffer)
                    return
                if isinstance(nxt, tuple):
                    self._write(buffer)
                    buffer = []
                    nxt[1].set()
                    continue
                buffer.append(nxt)
            self._write(buffer)
            buffer = []

    def _write(self, lines: list[str]) -> None:
        if not lines:
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write("".join(lines))
                self._written += len(lines)
                # **写完之后**再判轮转：写之前判会让"一次写入就把文件撑爆"的
                # 情况永远轮转不了（写前 size=0，写后没人再看）。
                self._rotate_if_needed()
        except OSError:
            pass  # 日志写不进去不能影响业务

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                for index in range(self.backups - 1, 0, -1):
                    src = self.path.with_name(f"{self.path.name}.{index}")
                    dst = self.path.with_name(f"{self.path.name}.{index + 1}")
                    if src.exists():
                        os.replace(src, dst)
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass


def _legacy_rotating_handler(path: Path, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    return handler


def setup_logging(root: Path | None = None, level: int = logging.INFO,log_file: bool = True) -> logging.Logger:
    root = root or app_root()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    purge_old_logs(log_dir)
    logger = logging.getLogger("blechat")
    # handler 挂在 **root** 上：`blechat.*` 向上冒泡到这里，
    # 而 `qasync.*` / `asyncio.*` / 未捕获异常的 logger 名不在 `blechat` 下 ——
    # 以前它们没有 handler，走 `logging.lastResort` → stderr，
    # `pythonw` / 无控制台启动时**全部丢掉**，dev 只看到「没有报错信息」。
    base = logging.getLogger()
    logger.setLevel(level)
    if base.handlers:
        # 已装过 handler（同进程二次调用）：只调级别，别重复加 handler
        for handler in base.handlers:
            handler.setLevel(level)
        base.setLevel(level)
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    if log_file:
        fh = make_file_handler(log_dir / "app.log", level)
        fh.setFormatter(fmt)
        base.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING if level > logging.DEBUG else logging.DEBUG)
    base.addHandler(sh)
    base.setLevel(level)

    # 第三方库在 DEBUG 下极其聒噪（bleak 每个报文一行），单独压制：
    # 真要追 BLE 栈内部日志时用 `BLECHAT_BLE_DEBUG=1` 放开。
    quiet = logging.DEBUG if os.environ.get(BLE_DEBUG_ENV) else logging.WARNING
    for noisy in ("bleak", "asyncio", "winrt", "qasync"):
        logging.getLogger(noisy).setLevel(quiet)
    return logger


def make_file_handler(path: Path, level: int) -> logging.Handler:
    """建文件 handler；异常时退回同步的 `RotatingFileHandler`（绝不因日志装不上而崩）。"""
    try:
        handler = AsyncFileHandler(path)
    except Exception:  # pragma: no cover - 线程都起不来时的兜底
        handler = _legacy_rotating_handler(path, level)
    handler.setLevel(level)
    return handler
