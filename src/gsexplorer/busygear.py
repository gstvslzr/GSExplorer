"""Busy overlay with a turning gear: "the window is working on something".

Covers its parent widget (follows its size), blocks clicks underneath, and shows a message such as
"Preparing 120,000 results…". The gear only turns while the GUI thread is free, so the heavy work
it announces must run in a background thread (see DuplicatesWindow._run).

The overlay is opaque: it shows a dimmed snapshot of the window taken when it starts. Otherwise every
animation frame would also repaint the list underneath, which calls into Python ~1,000 times per frame;
with a worker thread holding Python's GIL, each of those calls can wait up to the 5 ms switch interval,
and the gear would barely move. Now each frame is one Python call.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .ringprogress import GREEN, TEAL, _mix


def gear_path(cx: float, cy: float, r_out: float, teeth: int = 9) -> QPainterPath:
    """Gear outline (teeth on a rim) with a centre hole, as one path (odd-even fill)."""
    r_root = r_out * 0.78
    path = QPainterPath()
    step = 2 * math.pi / teeth
    pts = []
    for k in range(teeth):
        a = k * step
        for frac, r in ((0.00, r_root), (0.12, r_out), (0.38, r_out), (0.50, r_root)):
            ang = a + frac * step
            pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
        pts.append(QPointF(cx + r_root * math.cos(a + step), cy + r_root * math.sin(a + step)))
    path.moveTo(pts[0])
    for p in pts[1:]:
        path.lineTo(p)
    path.closeSubpath()
    path.addEllipse(QPointF(cx, cy), r_out * 0.32, r_out * 0.32)
    path.setFillRule(Qt.OddEvenFill)
    return path


class BusyGear(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)      # Qt never repaints what is underneath
        self._snap = None
        self.text = ""
        self.detail = ""
        self.angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        parent.installEventFilter(self)
        self.hide()

    # ------------------------------------------------------------------ API
    def start(self, text: str, detail: str = "") -> None:
        self.text, self.detail = text, detail
        if not self.isVisible():
            self._snap = self.parentWidget().grab()           # one paint of the window, before work starts
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._timer.start()
        self.update()

    def set_text(self, text: str, detail: str = "") -> None:
        self.text, self.detail = text, detail
        self.update()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()
        self._snap = None

    def busy(self) -> bool:
        return self.isVisible()

    # ------------------------------------------------------------------ internals
    def eventFilter(self, obj, ev):
        if obj is self.parentWidget() and ev.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
        return False

    def mousePressEvent(self, ev):          # swallow clicks while busy
        ev.accept()

    def _tick(self) -> None:
        self.angle = (self.angle + 6) % 360
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._snap is not None:
            p.drawPixmap(0, 0, self._snap)
        bg = QColor(self.palette().window().color())
        bg.setAlpha(205 if self._snap is not None else 255)
        p.fillRect(self.rect(), bg)
        c = QPointF(self.width() / 2, self.height() / 2 - 30)
        r = 34.0
        p.save()
        p.translate(c)
        p.rotate(self.angle)
        p.setPen(QPen(_mix(TEAL, GREEN, 0.2).darker(130), 1.2))
        p.setBrush(_mix(TEAL, GREEN, 0.35))
        p.drawPath(gear_path(0, 0, r))
        p.restore()
        # small counter-rotating gear, meshing at the upper right
        c2 = QPointF(c.x() + r * 1.33, c.y() - r * 0.78)
        p.save()
        p.translate(c2)
        p.rotate(-self.angle * 9 / 6 + 20)
        p.setPen(QPen(_mix(TEAL, GREEN, 0.8).darker(130), 1.0))
        p.setBrush(_mix(TEAL, GREEN, 0.8))
        p.drawPath(gear_path(0, 0, r * 0.55, 6))
        p.restore()
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.15)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self.palette().windowText().color())
        p.drawText(QRectF(0, c.y() + r + 16, self.width(), 26), Qt.AlignHCenter | Qt.AlignTop, self.text)
        if self.detail:
            f2 = QFont(self.font())
            p.setFont(f2)
            col = QColor(self.palette().windowText().color())
            col.setAlpha(160)
            p.setPen(col)
            p.drawText(QRectF(0, c.y() + r + 44, self.width(), 22), Qt.AlignHCenter | Qt.AlignTop, self.detail)
        p.end()


class GearSpinner(QWidget):
    """Small turning gear for a status line ('working…'). Hidden when idle."""

    def __init__(self, parent=None, size: int = 18):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self) -> None:
        self.show()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self.angle = (self.angle + 9) % 360
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = min(self.width(), self.height()) / 2 - 1
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self.angle)
        p.setPen(Qt.NoPen)
        p.setBrush(_mix(TEAL, GREEN, 0.35))
        p.drawPath(gear_path(0, 0, r, 8))
        p.end()
