import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# pytest-qt 在 pytest_configure 里挑绑定；不指定的话它会先试 PySide6，
# 先把另一套 Qt6Core.dll 载进进程，之后 PyQt6 的 QtCore 就 DLL load failed。
os.environ.setdefault("PYTEST_QT_API", "pyqt6")
os.environ.setdefault("QT_API", "pyqt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
