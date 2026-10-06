"""无边框窗口的冒烟检查（会短暂显示一次窗口，1~2 秒后自动退出）。

跑它是因为这一类问题**只有真窗口能验**：
  * 无边框窗口在 Windows 上默认没有 `WS_THICKFRAME` ⇒ 拖边框改尺寸在系统层面不存在；
  * `WA_TranslucentBackground` 让窗口成为分层窗口，**全透明像素是鼠标穿透的**
    ⇒ 那一圈永远收不到 Qt 鼠标事件；
  * 最大化"一次点击被拆成两次状态变化"（图标/边距跟不上）。

用法：
    python tools/ui_smoke.py

退出码 0 = 全部通过；非 0 = 有 FAIL（每行都会打印实际值）。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from blechat.ui.main_window import (  # noqa: E402
    GWL_STYLE,
    HTBOTTOM,
    HTBOTTOMRIGHT,
    HTLEFT,
    HTTOP,
    HTTOPLEFT,
    WS_THICKFRAME,
    MainWindow,
)
from blechat.ui.theme import DARK, LIGHT, apply_theme  # noqa: E402

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail != "" else ""))
    if not ok:
        failures.append(name)


def window_style(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get.restype = ctypes.c_ssize_t
    get.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return int(get(hwnd, GWL_STYLE))


def client_equals_window(hwnd: int) -> tuple[bool, str]:
    user32 = ctypes.windll.user32
    wrect, crect = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(wrect))
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(crect))
    same = (
        (wrect.right - wrect.left) == crect.right
        and (wrect.bottom - wrect.top) == crect.bottom
    )
    return same, f"window={wrect.right - wrect.left}x{wrect.bottom - wrect.top} " \
                 f"client={crect.right}x{crect.bottom}"


def lparam_for(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def main() -> int:
    app = QApplication(sys.argv)
    dark = apply_theme(app, False)
    win = MainWindow()
    win.apply_colors(DARK if dark else LIGHT)
    win.set_mode("host")
    win.resize(900, 640)
    win.show()
    for _ in range(6):
        app.processEvents()

    hwnd = int(win.winId())
    style = window_style(hwnd)

    # 1. 原生改尺寸的前提：WS_THICKFRAME 被补上了
    check("WS_THICKFRAME 已注入", bool(style & WS_THICKFRAME), hex(style))

    # 2. WM_NCCALCSIZE 抵消了边框 ⇒ 客户区仍等于整窗（界面不会凭空缩一圈）
    same, detail = client_equals_window(hwnd)
    check("客户区 == 整窗", same, detail)

    # 3. 命中测试：四条边/四角都要回对应的 HT*，中间回 None（交给 Qt）
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    cx, cy = rect.left + (rect.right - rect.left) // 2, rect.top + (rect.bottom - rect.top) // 2
    expect = {
        "左边": (rect.left + 1, cy, HTLEFT),
        "上边": (cx, rect.top + 1, HTTOP),
        "右下角": (rect.right - 2, rect.bottom - 2, HTBOTTOMRIGHT),
        "左上角": (rect.left + 1, rect.top + 1, HTTOPLEFT),
        "下边": (cx, rect.bottom - 2, HTBOTTOM),
    }
    for label, (x, y, want) in expect.items():
        got = win._hit_test(lparam_for(x, y))
        check(f"命中测试·{label} -> {want}", got == want, f"got={got}")

    check("命中测试·正中 -> None（交给 Qt）", win._hit_test(lparam_for(cx, cy)) is None)

    # 4. 标题栏的最大化图标：一次点击只变一次
    before = win.geometry()
    icon_before = win.title_bar.max_button.text()
    win.toggle_maximized()
    for _ in range(4):
        app.processEvents()
    check("最大化后图标 = ❐", win.title_bar.max_button.text() == "❐",
          win.title_bar.max_button.text())
    check("最大化后 is_maximized = True", win.is_maximized is True)
    check("最大化后边距 = 0", win.layout().contentsMargins().left() == 0,
          win.layout().contentsMargins().left())
    screen = win.screen()
    if screen is not None:
        check("最大化铺满可用桌面", win.geometry() == screen.availableGeometry(),
              f"{win.geometry()} vs {screen.availableGeometry()}")
    win.toggle_maximized()
    for _ in range(4):
        app.processEvents()
    check("还原后图标 = ▢", win.title_bar.max_button.text() == "▢",
          win.title_bar.max_button.text())
    check("还原后回到原几何", win.geometry() == before, f"{win.geometry()} vs {before}")
    check("还原后边距 = 8", win.layout().contentsMargins().left() == 8,
          win.layout().contentsMargins().left())
    check("初始图标 = ▢", icon_before == "▢", icon_before)

    # 5. 连点两次 = 回到原状（幂等，不会"第二次才生效"）
    win.toggle_maximized()
    app.processEvents()
    win.toggle_maximized()
    app.processEvents()
    check("连点两次回到未最大化", win.is_maximized is False and win.geometry() == before,
          f"{win.geometry()} vs {before}")

    # 6. 发送按钮：纸飞机图标渲染出来了，而且在"不含下拉箭头"的左半区居中
    button = win.drop.send_button
    check("发送按钮有纸飞机", not button.icon_pixmap.isNull())
    cell = button.icon_cell()
    menu = button._menu_cell()
    check("下拉格在右侧", menu.left() >= cell.width() - 1,
          f"icon_cell={cell.width()} menu={menu.left()}..{menu.right()}")
    check("图标在左半区居中（重心不偏箭头）", cell.center().x() < button.width() / 2,
          f"icon_center={cell.center().x()} button_center={button.width() / 2}")

    # 7. 文件打开白名单
    from blechat.ui.widgets.chat_view import is_openable

    for name, want in (
        ("a.txt", True), ("报告.DOCX", True), ("x.xlsx", True), ("p.pdf", True),
        ("evil.py", False), ("evil.bat", False), ("evil.exe", False),
        ("evil.js", False), ("evil.lnk", False), ("page.html", False), ("v.svg", False),
        ("noext", False),
    ):
        check(f"openable {name} = {want}", is_openable(name) is want)

    QTimer.singleShot(0, app.quit)
    app.exec()
    win.force_close()

    print(f"\n{checks - len(failures)}/{checks} 通过")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
