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
    p.setColor(QPalette.ColorRole.Highlight, accent)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
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
