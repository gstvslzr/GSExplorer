# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Theme engine: built-in themes + user JSON themes in ~/.config/gsexplorer-plus/themes/.

A theme is {"name": str, "base": "light"|"dark", "colors": {key: "#rrggbb"}}.
Missing colour keys fall back to the built-in Light or Dark theme per "base".
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from .config import themes_dir

KEYS = ("window", "surface", "alt", "sidebar", "text", "muted", "border", "accent",
        "accent_text", "selection", "selection_text", "hover", "danger")

BUILTIN: dict[str, dict] = {
    "Light": {"base": "light", "colors": {
        "window": "#f3f4f7", "surface": "#ffffff", "alt": "#f8f9fb", "sidebar": "#ebedf2",
        "text": "#1d2129", "muted": "#687082", "border": "#d8dce4", "accent": "#2f6fde",
        "accent_text": "#ffffff", "selection": "#d9e6fc", "selection_text": "#0f2f66",
        "hover": "#edf1f8", "danger": "#c62828"}},
    "Dark": {"base": "dark", "colors": {
        "window": "#1a1c21", "surface": "#212429", "alt": "#25282e", "sidebar": "#1d2025",
        "text": "#e6e8ec", "muted": "#99a0ac", "border": "#353a44", "accent": "#5b9cff",
        "accent_text": "#0b1424", "selection": "#2a3f63", "selection_text": "#e8f0ff",
        "hover": "#2a2e36", "danger": "#ff6b6b"}},
    "Midnight": {"base": "dark", "colors": {
        "window": "#0e1524", "surface": "#121b2c", "alt": "#162034", "sidebar": "#101828",
        "text": "#dbe4f3", "muted": "#8898b2", "border": "#24324b", "accent": "#3fc1b0",
        "accent_text": "#04201c", "selection": "#1b4850", "selection_text": "#e2fbf7",
        "hover": "#192540", "danger": "#ff7a7a"}},
    "High Contrast": {"base": "dark", "colors": {
        "window": "#000000", "surface": "#000000", "alt": "#0d0d0d", "sidebar": "#000000",
        "text": "#ffffff", "muted": "#e0e0e0", "border": "#ffffff", "accent": "#ffd400",
        "accent_text": "#000000", "selection": "#ffd400", "selection_text": "#000000",
        "hover": "#262626", "danger": "#ff5555"}},
}

SYSTEM = "system"


def _valid_color(v) -> bool:
    return isinstance(v, str) and QColor(v).isValid()


def ensure_user_themes() -> None:
    """On first run, export the built-in themes as editable JSON templates."""
    d = themes_dir()
    if os.path.isdir(d):
        return
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        for name, t in BUILTIN.items():
            fn = os.path.join(d, name.lower().replace(" ", "-") + ".json")
            with open(fn, "w", encoding="utf-8") as f:
                json.dump({"name": name, "base": t["base"], "colors": t["colors"]}, f, indent=2)
        with open(os.path.join(d, "README.txt"), "w", encoding="utf-8") as f:
            f.write("GSExplorer+ themes. Copy any .json file, change \"name\" and colours,\n"
                    "then pick it from View > Theme. Files here override built-ins with the same name.\n"
                    "Keys: " + ", ".join(KEYS) + "\n")
    except OSError:
        pass


def load_themes() -> dict[str, dict]:
    themes = {k: {"base": v["base"], "colors": dict(v["colors"])} for k, v in BUILTIN.items()}
    d = themes_dir()
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    except OSError:
        files = []
    for fn in files:
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                t = json.load(f)
            name = str(t.get("name") or os.path.splitext(fn)[0])[:60]
            base = "dark" if t.get("base") == "dark" else "light"
            colors = dict(BUILTIN["Dark" if base == "dark" else "Light"]["colors"])
            for k, v in (t.get("colors") or {}).items():
                if k in KEYS and _valid_color(v):
                    colors[k] = v
            themes[name] = {"base": base, "colors": colors}
        except (OSError, ValueError, AttributeError):
            continue  # skip malformed theme files
    return themes


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False


def resolve(name: str, themes: dict[str, dict]) -> tuple[str, dict]:
    if name == SYSTEM or name not in themes:
        key = "Dark" if system_prefers_dark() else "Light"
        return key, themes.get(key, BUILTIN[key])
    return name, themes[name]


def _palette(c: dict) -> QPalette:
    p = QPalette()
    q = {k: QColor(v) for k, v in c.items()}
    for group in (QPalette.Active, QPalette.Inactive):
        p.setColor(group, QPalette.Window, q["window"])
        p.setColor(group, QPalette.WindowText, q["text"])
        p.setColor(group, QPalette.Base, q["surface"])
        p.setColor(group, QPalette.AlternateBase, q["alt"])
        p.setColor(group, QPalette.Text, q["text"])
        p.setColor(group, QPalette.Button, q["window"])
        p.setColor(group, QPalette.ButtonText, q["text"])
        p.setColor(group, QPalette.ToolTipBase, q["surface"])
        p.setColor(group, QPalette.ToolTipText, q["text"])
        p.setColor(group, QPalette.PlaceholderText, q["muted"])
        p.setColor(group, QPalette.Link, q["accent"])
        p.setColor(group, QPalette.Mid, q["border"])
        p.setColor(group, QPalette.Midlight, q["hover"])
        p.setColor(group, QPalette.Light, q["surface"])
        p.setColor(group, QPalette.Dark, q["border"])
        p.setColor(group, QPalette.BrightText, q["danger"])
    p.setColor(QPalette.Active, QPalette.Highlight, q["selection"])
    p.setColor(QPalette.Active, QPalette.HighlightedText, q["selection_text"])
    inactive_sel = QColor(q["selection"])
    inactive_sel.setAlpha(150)
    p.setColor(QPalette.Inactive, QPalette.Highlight, inactive_sel)
    p.setColor(QPalette.Inactive, QPalette.HighlightedText, q["selection_text"])
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, q["muted"])
    p.setColor(QPalette.Disabled, QPalette.Base, q["window"])
    p.setColor(QPalette.Disabled, QPalette.Window, q["window"])
    p.setColor(QPalette.Disabled, QPalette.Button, q["window"])
    return p


def _qss(c: dict, high_contrast: bool) -> str:
    bw = "2px" if high_contrast else "1px"
    return f"""
QMainWindow, QDialog {{ background: {c['window']}; }}
QWidget {{ font-size: 10pt; }}
QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
QTreeView, QListView, QPlainTextEdit, QTableView {{
  background: {c['surface']}; alternate-background-color: {c['alt']};
  color: {c['text']}; border: none; outline: 0; }}
QTreeView::item, QListView::item {{ padding: 2px 4px; border: none; }}
QTreeView::item:hover, QListView::item:hover {{ background: {c['hover']}; }}
QTreeView::item:selected, QListView::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
QHeaderView {{ background: {c['surface']}; border: none; }}
QHeaderView::section {{ background: {c['surface']}; color: {c['muted']}; border: none;
  border-bottom: 1px solid {c['border']}; border-right: 1px solid {c['border']}; padding: 4px 8px; }}
QHeaderView::section:hover {{ background: {c['hover']}; color: {c['text']}; }}
#Sidebar {{ background: {c['sidebar']}; border: none; color: {c['text']}; }}
#Sidebar::item {{ padding: 3px 2px; border-radius: 5px; }}
#Sidebar::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
#Sidebar::item:hover {{ background: {c['hover']}; }}
#PaneFrame {{ background: {c['surface']}; border: {bw} solid {c['border']}; border-radius: 8px; }}
#PaneFrame[active="true"] {{ border: 2px solid {c['accent']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 5px 12px; margin: 4px 2px 2px 2px;
  border-radius: 6px; border: none; }}
QTabBar::tab:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
QTabBar::tab:hover:!selected {{ background: {c['hover']}; color: {c['text']}; }}
QTabBar::close-button {{ subcontrol-position: right; }}
#Breadcrumb {{ background: {c['surface']}; border-bottom: 1px solid {c['border']}; }}
#Breadcrumb QToolButton {{ border: none; padding: 3px 6px; border-radius: 4px; color: {c['text']}; background: transparent; }}
#Breadcrumb QToolButton:hover {{ background: {c['hover']}; }}
#Breadcrumb QToolButton[current="true"] {{ font-weight: bold; }}
#Breadcrumb QLabel {{ color: {c['muted']}; }}
#PaneStatus {{ color: {c['muted']}; border-top: 1px solid {c['border']}; padding: 3px 8px; background: {c['surface']}; }}
#FilterBar {{ background: {c['surface']}; border-top: 1px solid {c['border']}; }}
#FKeyBar {{ background: {c['window']}; border-top: 1px solid {c['border']}; }}
#FKeyBar QPushButton {{ background: {c['window']}; color: {c['text']}; border: none;
  border-right: 1px solid {c['border']}; padding: 6px 4px; border-radius: 0; }}
#FKeyBar QPushButton:hover {{ background: {c['hover']}; }}
#FKeyBar QPushButton:pressed {{ background: {c['selection']}; color: {c['selection_text']}; }}
#CmdBar {{ background: {c['window']}; border-top: 1px solid {c['border']}; }}
#CmdBar QLabel {{ color: {c['muted']}; }}
#QueueButton {{ border: none; color: {c['muted']}; padding: 2px 8px; background: transparent; }}
#QueueButton[busy="true"] {{ color: {c['accent']}; font-weight: bold; }}
QLineEdit, QComboBox, QSpinBox {{ background: {c['surface']}; color: {c['text']}; border: {bw} solid {c['border']};
  border-radius: 5px; padding: 4px 6px; selection-background-color: {c['selection']};
  selection-color: {c['selection_text']}; }}
QLineEdit:focus, QComboBox:focus {{ border: {bw} solid {c['accent']}; }}
QPushButton {{ background: {c['surface']}; color: {c['text']}; border: {bw} solid {c['border']};
  border-radius: 6px; padding: 5px 14px; }}
QPushButton:hover {{ background: {c['hover']}; }}
QPushButton:default {{ background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']}; }}
QPushButton:disabled {{ color: {c['muted']}; }}
QPushButton:default:disabled {{ background: {c['window']}; color: {c['muted']}; border-color: {c['border']}; }}
QToolBar {{ background: {c['window']}; border: none; spacing: 2px; padding: 2px 4px; }}
QToolButton {{ border: none; border-radius: 5px; padding: 4px; color: {c['text']}; }}
QToolButton:hover {{ background: {c['hover']}; }}
QToolButton:checked {{ background: {c['selection']}; color: {c['selection_text']}; }}
QMenuBar {{ background: {c['window']}; color: {c['text']}; }}
QMenuBar::item {{ padding: 4px 9px; background: transparent; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {c['hover']}; }}
QMenu {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
QSplitter::handle {{ background: {c['window']}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 28px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 28px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QProgressBar {{ border: 1px solid {c['border']}; border-radius: 4px; background: {c['window']};
  text-align: center; color: {c['text']}; height: 14px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
QCheckBox, QRadioButton, QLabel, QGroupBox {{ color: {c['text']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {c['muted']}; }}
#Muted {{ color: {c['muted']}; }}
#Danger {{ color: {c['danger']}; }}
"""


def apply(app: QApplication, name: str, themes: dict[str, dict]) -> str:
    """Apply theme *name* (or "system"). Returns the concrete theme name used."""
    key, t = resolve(name, themes)
    app.setStyle("Fusion")
    app.setPalette(_palette(t["colors"]))
    app.setStyleSheet(_qss(t["colors"], high_contrast=(key == "High Contrast")))
    return key


def colors_for(name: str, themes: dict[str, dict]) -> dict:
    return resolve(name, themes)[1]["colors"]
