from __future__ import annotations

import asyncio
import sys

from blechat.app import BleChatApp

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import qInstallMessageHandler

def _qt_msg_handler(msg_type, context, message):
    if "CreateFontFaceFromHDC" in message:
        return
    sys.stderr.write(message + "\n")

def main() -> int:
    qInstallMessageHandler(_qt_msg_handler) # 忽略 font 警告
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("BLE Chat")
    qapp.setOrganizationName("BLE Chat")
    qapp.setQuitOnLastWindowClosed(False)

    from qasync import QEventLoop

    loop = QEventLoop(qapp)
    asyncio.set_event_loop(loop)
    with loop:
        BleChatApp(qapp)
        loop.run_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())