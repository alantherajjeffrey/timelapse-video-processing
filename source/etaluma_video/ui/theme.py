"""Themes: Terracotta (default since 0.7), Dark and Light.

Terracotta is a dark theme: near-black panels, cream text and a warm terracotta accent. The viewer
background stays near black in every theme so fluorescence keeps its contrast;
``VIEWER_BACKGROUND`` is the colour every image widget paints behind the frame. Widgets take
their colours from the palette (``palette(highlight)``, ``palette(mid)`` in style sheets), so they
follow whichever theme is active.
"""
from __future__ import annotations

import logging

from PySide6.QtGui import QColor, QPalette

log = logging.getLogger("etaluma.ui")

THEMES: tuple[str, ...] = ("terracotta", "dark", "light")
DEFAULT_THEME = "terracotta"

#: Near-black canvas behind preview and results images, identical in every theme.
VIEWER_BACKGROUND = "#0e0e10"

_TERRACOTTA = {
    "window": "#262624",
    "base": "#1f1e1d",
    "alt_base": "#30302e",
    "text": "#f0eee6",
    "muted": "#a6a39a",
    "button": "#30302e",
    "border": "#43423e",
    "accent": "#d97757",
    "accent_hover": "#e58b6d",
    "accent_text": "#ffffff",
    "tooltip_bg": "#30302e",
    "disabled": "#6e6b64",
}

_DARK = {
    "window": "#26272b",
    "base": "#1e1f22",
    "alt_base": "#2d2e33",
    "text": "#e6e6e6",
    "muted": "#9aa0a6",
    "button": "#2d2e33",
    "border": "#3d3f46",
    "accent": "#4c8dff",
    "accent_hover": "#6f9dff",
    "accent_text": "#101318",
    "tooltip_bg": "#2d2e33",
    "disabled": "#6b6f76",
}

_LIGHT = {
    "window": "#f3f3f4",
    "base": "#ffffff",
    "alt_base": "#ececee",
    "text": "#1c1d20",
    "muted": "#5f6368",
    "button": "#e7e7ea",
    "border": "#c4c5ca",
    "accent": "#1a6fe0",
    "accent_hover": "#1557b0",
    "accent_text": "#ffffff",
    "tooltip_bg": "#ffffff",
    "disabled": "#9aa0a6",
}

THEME_COLOURS = {"terracotta": _TERRACOTTA, "dark": _DARK, "light": _LIGHT}

#: Log level colours per theme (used by the log dock).
LOG_COLOURS = {
    "terracotta": {"DEBUG": "#8f8b80", "INFO": "#e8e4da", "WARNING": "#e3a14a", "ERROR": "#ff7b6b", "CRITICAL": "#ff7b6b"},
    "dark": {"DEBUG": "#8c9199", "INFO": "#d7d7d7", "WARNING": "#e0a83c", "ERROR": "#ff6b6b", "CRITICAL": "#ff6b6b"},
    "light": {"DEBUG": "#6b7076", "INFO": "#1c1d20", "WARNING": "#a86a00", "ERROR": "#c62828", "CRITICAL": "#c62828"},
}


def colours(theme: str) -> dict[str, str]:
    return dict(THEME_COLOURS.get(theme, THEME_COLOURS[DEFAULT_THEME]))


def log_colour(theme: str, level: str) -> str:
    table = LOG_COLOURS.get(theme, LOG_COLOURS[DEFAULT_THEME])
    return table.get(level.upper(), table["INFO"])


def _palette(c: dict[str, str]) -> QPalette:
    p = QPalette()
    window, base, text = QColor(c["window"]), QColor(c["base"]), QColor(c["text"])
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(c["alt_base"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["tooltip_bg"]))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(c["button"]))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor("#ff5252"))
    p.setColor(QPalette.ColorRole.Link, QColor(c["accent"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(c["accent"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(c["accent_text"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    # style sheets and the histogram draw borders and axes with palette(mid)
    p.setColor(QPalette.ColorRole.Mid, QColor(c["border"]))
    p.setColor(QPalette.ColorRole.Midlight, QColor(c["alt_base"]))
    p.setColor(QPalette.ColorRole.Dark, QColor(c["border"]).darker(130))
    p.setColor(QPalette.ColorRole.Light, QColor(c["base"]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["disabled"]))
    return p


def stylesheet(theme: str) -> str:
    c = colours(theme)
    return f"""
    QMainWindow::separator {{ background: {c['border']}; width: 1px; height: 1px; }}
    QToolBar {{ background: {c['window']}; border-bottom: 1px solid {c['border']}; padding: 4px 6px; spacing: 6px; }}
    QToolBar QToolButton {{ border: 1px solid transparent; border-radius: 7px; padding: 4px 9px; }}
    QToolBar QToolButton:hover {{ border-color: {c['border']}; background: {c['alt_base']}; }}
    QToolBar QToolButton:checked {{ border-color: {c['accent']}; background: {c['alt_base']}; color: {c['text']}; }}
    QToolBar QToolButton:disabled {{ color: {c['disabled']}; }}
    QDockWidget {{ titlebar-close-icon: none; }}
    QDockWidget::title {{ background: {c['alt_base']}; padding: 5px 8px; border-bottom: 1px solid {c['border']};
                          color: {c['muted']}; }}
    QTabBar::tab {{ background: {c['window']}; border: 1px solid {c['border']}; border-bottom: none;
                    padding: 4px 10px; }}
    QTabBar::tab:selected {{ background: {c['alt_base']}; color: {c['text']}; }}
    QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 10px; padding-top: 6px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {c['muted']}; }}
    QStatusBar {{ border-top: 1px solid {c['border']}; }}
    QStatusBar QLabel {{ color: {c['muted']}; }}
    QProgressBar {{ border: 1px solid {c['border']}; border-radius: 5px; text-align: center; max-height: 14px; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
    QPlainTextEdit, QTextEdit, QListWidget, QTreeWidget, QTreeView, QListView {{
        background: {c['base']}; border: 1px solid {c['border']}; border-radius: 5px; }}
    QTreeView::item:selected, QListView::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {c['base']}; border: 1px solid {c['border']};
        border-radius: 5px; padding: 2px 5px; min-height: 20px; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c['accent']}; }}
    QPushButton {{ background: {c['button']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 4px 12px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:pressed {{ background: {c['alt_base']}; }}
    QPushButton:disabled {{ color: {c['disabled']}; }}
    QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']}; border: 1px solid {c['accent']};
        font-weight: 600; padding: 8px 20px; border-radius: 8px; }}
    QPushButton[role="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton[role="link"] {{ background: transparent; border: none; color: {c['text']}; text-align: left;
        padding: 4px 2px; }}
    QPushButton[role="link"]:hover {{ color: {c['accent']}; }}
    QToolButton[role="section"] {{ border: none; border-bottom: 1px solid {c['border']}; background: transparent;
        padding: 7px 4px; font-weight: 600; color: {c['text']}; text-align: left; }}
    QToolButton[role="section"]:hover {{ color: {c['accent']}; }}
    QLabel[role="title"] {{ font-size: 18pt; font-weight: 700; color: {c['text']}; }}
    QLabel[role="subtitle"] {{ font-size: 11pt; color: {c['muted']}; }}
    QLabel[role="heading"] {{ color: {c['muted']}; font-weight: 600; letter-spacing: 0.5px; }}
    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="viewer"] {{ background: {VIEWER_BACKGROUND}; border: 1px solid {c['border']}; border-radius: 6px;
        color: {c['muted']}; }}
    """


def apply_theme(app, theme: str = DEFAULT_THEME) -> str:
    """Apply the Fusion style with the named palette. Returns the theme actually applied."""
    name = theme if theme in THEMES else DEFAULT_THEME
    try:
        app.setStyle("Fusion")
    except Exception:  # pragma: no cover - platform dependent
        log.debug("Fusion style is not available; the default style is kept.")
    app.setPalette(_palette(colours(name)))
    app.setStyleSheet(stylesheet(name))
    return name
