"""复核 docs/winrt 里记录的两个坑（在项目根运行：python docs/winrt/verify_notes.py）。"""
from __future__ import annotations

import sys

sys.path.insert(0, str(__file__).rsplit("docs", 1)[0].rstrip("\\/"))

from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSessionStatus,
    GattSessionStatusChangedEventArgs,
)
from winrt.windows.storage.streams import DataWriter

from blechat.ble.server import RO_E_CLOSED, RO_E_CLOSED_SIGNED, is_closed_error, is_link_dead

ok = True


def check(label: str, cond: bool) -> None:
    global ok
    ok = ok and cond
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")


# 1) GattSessionStatusChangedEventArgs 只有 status / error
check(
    "GattSessionStatusChangedEventArgs 无 session_status",
    not hasattr(GattSessionStatusChangedEventArgs, "session_status"),
)
check(
    "GattSessionStatusChangedEventArgs 有 status",
    hasattr(GattSessionStatusChangedEventArgs, "status"),
)
check("GattSessionStatus.CLOSED == 0", int(GattSessionStatus.CLOSED) == 0)
check("GattSessionStatus.ACTIVE == 1", int(GattSessionStatus.ACTIVE) == 1)

# 2) pywinrt 真实 OSError 形状 vs is_closed_error
w = DataWriter()
w.write_bytes(b"x")
w.close()
try:
    w.write_bytes(b"y")
    raise SystemExit("expected OSError")
except OSError as e:
    print("    shape:", repr(e))
    print("    winerror =", e.winerror, " errno =", e.errno)
    print("    is_closed_error =", is_closed_error(e))
    print("    is_link_dead    =", is_link_dead(e))
    norm = (getattr(e, "winerror", 0) or 0) & 0xFFFFFFFF
    check("winerror 是有符号 HRESULT", e.winerror == RO_E_CLOSED_SIGNED)
    check("规范化后 == 0x80000013", norm == RO_E_CLOSED)
    # 修复前应为 False（漏判），修复后应为 True —— 两种情况都算"笔记成立"，
    # 所以这里只验证"参考修复判定"稳定命中，避免修完码脚本反而 EXIT=1。
    check("参考修复判定命中", norm == RO_E_CLOSED)
    print("    当前 is_closed_error =", is_closed_error(e), "(修复前 False / 修复后 True)")
    print("    当前 is_link_dead    =", is_link_dead(e), "(修复前 False / 修复后 True)")

# 3) 现有单测用的构造方式（errno 承载 HRESULT）—— 说明为什么单测绿、线上漏
t = OSError(0x80000013, "The object has been closed.")
print("    test-shape:", repr(t), "errno =", t.errno, "winerror =", t.winerror)
check("单测形状能命中", is_closed_error(t))

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
