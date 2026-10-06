"""睡眠/挂起检测：靠**墙钟跳变**发现"刚才系统睡了"。

为什么不用 monotonic：Windows 上 `time.monotonic()` / QTimer 的计时基准在
S3 睡眠期间的行为不一致（可能停、也可能继续走），拿它判断"睡没睡"不可靠；
而**墙钟**（`time.time()`）一定会跨过睡眠。所以我们只做一件事：

    每 15s 跳一次的心跳，如果这一跳和上一跳之间墙钟过去了 45s+，
    那就不是"定时器晚了"，而是**这台机器刚才被挂起了**。

阈值取得比心跳间隔大得多，是为了不把「UI 卡了一下 / 定时器被挤后」误判成睡眠。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 心跳 15s 一跳；超过 45s 就认为中间系统被挂起过
STALL_THRESHOLD = 45.0


@dataclass
class StallDetector:
    """喂墙钟时间，返回"这一跳之前被挂起了多少秒"。

    ```python
    det = StallDetector()
    gap = det.tick()          # 第一跳：0.0
    gap = det.tick()          # 正常：0.0
    ...                        # 系统睡了 30 分钟
    gap = det.tick()          # 1800.0 → 上层据此重置蓝牙栈
    ```
    """

    threshold: float = STALL_THRESHOLD
    _last: float | None = field(default=None, repr=False)

    def tick(self, now: float | None = None) -> float:
        """返回跳过的秒数（没跳过返回 0.0）。第一次调用恒返回 0.0。"""
        stamp = time.time() if now is None else now
        last, self._last = self._last, stamp
        if last is None:
            return 0.0
        gap = stamp - last
        if gap < 0:  # 系统时间被改回去了，不算挂起
            return 0.0
        return gap if gap >= self.threshold else 0.0


__all__ = ["STALL_THRESHOLD", "StallDetector"]
