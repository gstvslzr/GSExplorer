# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render resources/gsexplorer-plus.svg to a multi-size Windows .ico (16-256 px). Build time only.

Usage: QT_QPA_PLATFORM=offscreen python make_icon.py SVG OUT.ico   (needs PySide6 + Pillow)"""
import io
import sys

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

app = QGuiApplication([])
svg, out = sys.argv[1], sys.argv[2]
r = QSvgRenderer(svg)
assert r.isValid(), svg
img = QImage(256, 256, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
r.render(p)
p.end()
buf = QBuffer()
buf.open(QIODevice.WriteOnly)
img.save(buf, "PNG")
Image.open(io.BytesIO(bytes(buf.data()))).save(
    out, format="ICO", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
