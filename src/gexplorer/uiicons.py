"""Built-in stroke icons for chrome (toolbar, path bar), tinted to the active theme.

System icon themes ship many toolbar icons as dark "symbolic" glyphs that vanish
on dark themes. These original 24x24 stroke icons are re-rendered with the
theme's text colour whenever the theme changes, so chrome stays legible.
File and folder icons still come from the system icon theme.
"""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PATHS = {
    "back": "M15 5l-7 7 7 7",
    "forward": "M9 5l7 7-7 7",
    "up": "M12 19V5M5.5 11.5L12 5l6.5 6.5",
    "home": "M3.5 11L12 4l8.5 7M6 9v11h12V9M10 20v-5.5h4V20",
    "refresh": "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.5H15",
    "newtab": "M3.5 6.5h17v13h-17zM3.5 10h17M12 12.5v5M9.5 15h5",
    "newfolder": "M3.5 6.5h6l2 2h9v11h-17zM12 11.5v5M9.5 14h5",
    "filter": "M4 5h16l-6.2 7.6V18l-3.6 2v-7.4z",
    "search": "M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM15.3 15.3L20 20",
    "details": "M4 6h16M4 12h16M4 18h16",
    "grid": "M4.5 4.5h6v6h-6zM13.5 4.5h6v6h-6zM4.5 13.5h6v6h-6zM13.5 13.5h6v6h-6z",
    "eye": "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM12 9.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6z",
    "dual": "M3.5 5h17v14h-17zM12 5v14",
    "sidebar": "M3.5 5h17v14h-17zM9 5v14M5.5 8.5h1.5M5.5 11.5h1.5",
    "theme": "M12 3.5a8.5 8.5 0 1 0 0 17c1.3 0 1.8-1 1.3-2-.6-1.2.2-2.5 1.5-2.5H17a3.5 3.5 0 0 0 3.5-3.5c0-5-3.8-9-8.5-9zM7.5 11h.01M10 7.5h.01M14.5 7.5h.01",
    "queue": "M7 4v15M3.5 15.5L7 19l3.5-3.5M17 20V5M13.5 8.5L17 5l3.5 3.5",
    "sync": "M4.5 11a7.5 7.5 0 0 1 13.3-4.2M19.5 3.5v4.2h-4.2M19.5 13a7.5 7.5 0 0 1-13.3 4.2M4.5 20.5v-4.2h4.2",
    "preview": "M3.5 5h17v14h-17zM14.5 5v14M16.8 9h1.7M16.8 12h1.7M16.8 15h1.7",
    "connect": "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM3.5 12h17M12 3.5c2.3 2.4 3.4 5.2 3.4 8.5s-1.1 6.1-3.4 8.5c-2.3-2.4-3.4-5.2-3.4-8.5s1.1-6.1 3.4-8.5z",
    "close": "M6.5 6.5l11 11M17.5 6.5l-11 11",
    "plus": "M12 5v14M5 12h14",
}


def _svg(name: str, color: str, width: float = 1.9) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{PATHS[name]}"/></svg>').encode()


def icon(name: str, color: str, disabled_color: str | None = None) -> QIcon:
    ic = QIcon()
    for size in (16, 18, 20, 24, 32, 48):
        for mode, col in ((QIcon.Normal, color), (QIcon.Disabled, disabled_color or color)):
            pm = QPixmap(size, size)
            pm.fill(Qt.transparent)
            r = QSvgRenderer(QByteArray(_svg(name, col)))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            r.render(p, QRectF(0, 0, size, size))
            p.end()
            ic.addPixmap(pm, mode)
    return ic


def write_svg(name: str, color: str) -> str:
    """Write a tinted SVG to the cache dir (for stylesheet url()); returns its path."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "gexplorer", "icons")
    os.makedirs(d, exist_ok=True)
    data = _svg(name, color, 2.2)
    fn = os.path.join(d, f"{name}-{hashlib.sha1(data).hexdigest()[:10]}.svg")
    if not os.path.exists(fn):
        with open(fn, "wb") as f:
            f.write(data)
    return fn
