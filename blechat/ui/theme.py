from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

QSS_PATH = Path(__file__).with_name("style.qss")

LIGHT: dict[str, str] = {
    "bg": "#F3F3F3",
    "card": "#FFFFFF",
    "text": "#1B1B1B",
    "muted": "#5E5E5E",
    "accent": "#0078D4",
    "accent_hover": "#106EBE",
    "danger": "#C42B1C",
    "success": "#107C10",
    "warn": "#9D5D00",
    "border": "#E3E3E3",
    "input": "#FFFFFF",
    "hover": "#EAF3FC",
    "bubble_in": "#FFFFFF",
    "bubble_out": "#D3E8FF",
    "titlebar": "#FAFAFA",
    "shadow": "rgba(0,0,0,24)",
    "track": "#E0E0E0",
    # ---- 淡色主按钮（发送 / 对话框"确定"）--------------------------------
    # 以前主按钮是实心 `#0078D4` + 白字，在整个浅色界面里太"重"、也太扎眼。
    # 现在改成**淡蓝底 + 深蓝字**：仍然是唯一的强调色，但不再抢视线。
    # `select` 是文字/表格选中态的高亮（用户指定的那版淡蓝），和按钮同色系，
    # 保证"选中"和"主动作"看起来是一家人。
    "accent_soft": "#C8DCFF",
    "accent_soft_hover": "#B4CFFB",
    "accent_soft_border": "#A3C2EE",
    "on_accent_soft": "#0F4A85",
    "select": "#C8DCFF",
    "select_text": "#000000",
}

DARK: dict[str, str] = {
    "bg": "#202020",
    "card": "#2B2B2B",
    "text": "#F2F2F2",
    "muted": "#A6A6A6",
    "accent": "#4CC2FF",
    "accent_hover": "#6CCBFF",
    "danger": "#FF99A4",
    "success": "#6CCB5F",
    "warn": "#FCE100",
    "border": "#3A3A3A",
    "input": "#323232",
    "hover": "#333A41",
    "bubble_in": "#2F2F2F",
    "bubble_out": "#17405C",
    "titlebar": "#262626",
    "shadow": "rgba(0,0,0,48)",
    "track": "#3D3D3D",
    # 深色主题下"淡"是相对底色而言：比卡片亮一点点的蓝，字用浅蓝。
    "accent_soft": "#1E3C58",
    "accent_soft_hover": "#27496B",
    "accent_soft_border": "#33587C",
    "on_accent_soft": "#C7E3FF",
    "select": "#2F5D86",
    "select_text": "#FFFFFF",
}


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


def build_palette(colors: dict[str, str]) -> QPalette:
    p = QPalette()
    text = QColor(colors["text"])
    bg = QColor(colors["bg"])
    base = QColor(colors["input"])
    accent = QColor(colors["accent"])
    p.setColor(QPalette.ColorRole.Window, bg)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["card"]))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, bg)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(colors["danger"]))
    # 选中态用淡蓝高亮 + 黑字（默认的实心 accent 蓝会把选中文字压得很难读）。
    # 放进 palette 是为了让**没被 QSS 覆盖到**的地方（下拉弹层、输入法的候选、
    # 表格单元格……）也统一到这个颜色。
    p.setColor(QPalette.ColorRole.Highlight, QColor(colors["select"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["select_text"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["card"]))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["muted"]))
    p.setColor(QPalette.ColorRole.Link, accent)
    return p


def build_stylesheet(colors: dict[str, str]) -> str:
    text = QSS_PATH.read_text(encoding="utf-8")
    for key, value in colors.items():
        text = text.replace(f"@{key}@", value)
    return text


def apply_theme(app: QApplication, dark: bool | None = None) -> bool:
    if dark is None:
        dark = system_dark()
    colors = DARK if dark else LIGHT
    app.setPalette(build_palette(colors))
    app.setStyleSheet(build_stylesheet(colors))
    return dark
