"""Segmented ring progress gauge (used by the scan / action progress popups).

  ┌──────────────┐   48 rounded segments, lit clockwise from 12 o'clock, teal → green.
  │   PROGRESS   │   Determinate: big "NN%" in the middle.
  │     68%      │   Busy (total unknown): a lit "comet" circles the ring and the
  │  COMPLETED   │   middle shows a live count instead of a percentage.
  │     ─^v─     │
  └──────────────┘
Self-contained colours (dark disc), so it looks the same on light and dark themes.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

TEAL = QColor("#17c3b2")
GREEN = QColor("#3ee38a")
OFF = QColor("#26374a")
DISC_IN = QColor("#132231")
DISC_OUT = QColor("#0a131c")
RIM = QColor("#1d3040")
LABEL = QColor("#8fa9bf")


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class RingProgress(QWidget):
    def __init__(self, parent=None, segments: int = 48):
        super().__init__(parent)
        self.n = segments
        self._target = 0.0
        self._shown = 0.0
        self._busy = False
        self._phase = 0.0
        self.top_text = "PROGRESS"
        self.bottom_text = "COMPLETED"
        self.center_text = ""             # busy mode: e.g. "1,234"
        self._gear = False                # draw a gear instead of the pulse mark
        self._gear_turning = False
        self._gear_angle = 0.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(200, 200)
        self._timer = QTimer(self)
        self._timer.setInterval(16)        # ~60 fps while animating; stops when idle
        self._timer.timeout.connect(self._step)

    # ------------------------------------------------------------------ API
    def setValue(self, fraction: float, top: str | None = None, bottom: str | None = None) -> None:
        """0.0–1.0. Leaves busy mode. The ring eases toward the value. *top*/*bottom* replace the
        "PROGRESS"/"COMPLETED" captions (e.g. "SYNCHRONIZING" / "1:23 LEFT")."""
        self._busy = False
        self._target = max(0.0, min(1.0, float(fraction)))
        self.top_text = "PROGRESS" if top is None else top
        self.bottom_text = "COMPLETED" if bottom is None else bottom
        self._timer.setInterval(16)
        self._timer.start()
        self.update()

    def setGear(self, shown: bool = True, turning: bool = True) -> None:
        """Show a gear under the percentage (instead of the pulse mark); it turns while *turning*."""
        self._gear, self._gear_turning = shown, shown and turning
        if self._gear_turning and not self._timer.isActive():
            self._timer.setInterval(33)
            self._timer.start()
        self.update()

    def gearTurning(self) -> bool:
        return self._gear_turning

    def setBusy(self, center: str = "", top: str = "SCANNING", bottom: str = "") -> None:
        """Total unknown: show a circling comet and *center* (e.g. a live count)."""
        self._busy = True
        self.center_text, self.top_text, self.bottom_text = center, top, bottom
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def value(self) -> float:
        return self._target

    def displayed_percent(self) -> int:
        return int(self._shown * 100 + 1e-9)

    def isBusy(self) -> bool:
        return self._busy

    def sizeHint(self) -> QSize:
        return QSize(260, 260)

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def showEvent(self, ev):
        if self._busy or self._gear_turning or self._shown != self._target:
            self._timer.start()
        super().showEvent(ev)

    # ------------------------------------------------------------------ animation
    def _step(self) -> None:
        if self._gear_turning:
            self._gear_angle = (self._gear_angle + 4.0 * self._timer.interval() / 16) % 360
        if self._busy:
            self._phase = (self._phase + 0.012) % 1.0          # one lap ≈ 1.3 s
        else:
            d = self._target - self._shown
            self._shown = self._target if abs(d) < 0.002 else self._shown + d * 0.25
            if self._shown == self._target:
                if self._gear_turning:
                    self._timer.setInterval(33)                 # only the gear moves: 30 fps is plenty
                else:
                    self._timer.stop()
        self.update()

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 4
        c = QPointF(self.width() / 2, self.height() / 2)
        r = side / 2

        g = QRadialGradient(c, r)
        g.setColorAt(0.0, DISC_IN)
        g.setColorAt(1.0, DISC_OUT)
        p.setPen(QPen(RIM, max(1.0, side * 0.006)))
        p.setBrush(g)
        p.drawEllipse(c, r, r)

        seg_len = r * 0.17
        seg_w = 2 * math.pi * (r * 0.84) / self.n * 0.62
        lit_f = self._shown * self.n
        for i in range(self.n):
            if self._busy:
                k = (self._phase - i / self.n) % 1.0           # distance behind the comet head (trail follows)
                trail = 1.0 - k / 0.30 if k <= 0.30 else 0.0
                col = _mix(OFF, _mix(TEAL, GREEN, 1 - k / 0.30), trail) if trail > 0 else OFF
                glow = trail
            else:
                amt = max(0.0, min(1.0, lit_f - i))           # 0 off · 1 on · fraction for the leading edge
                on = _mix(TEAL, GREEN, i / max(1, self.n - 1))
                col = _mix(OFF, on, amt)
                glow = amt
            p.save()
            p.translate(c)
            p.rotate(i * 360.0 / self.n)                       # 0 = 12 o'clock, clockwise
            rect = QRectF(-seg_w / 2, -r * 0.92, seg_w, seg_len)
            if glow > 0.05:
                gc = QColor(col)
                gc.setAlphaF(0.22 * glow)
                p.setPen(Qt.NoPen)
                p.setBrush(gc)
                p.drawRoundedRect(rect.adjusted(-seg_w * 0.35, -seg_w * 0.35, seg_w * 0.35, seg_w * 0.35),
                                  seg_w * 0.6, seg_w * 0.6)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(rect, seg_w * 0.18, seg_w * 0.18)
            p.restore()

        accent = _mix(TEAL, GREEN, 0.45)
        base = QFont(self.font())

        def text(s: str, y: float, px: float, color: QColor, bold=False, spacing=0.0):
            f = QFont(base)
            f.setPixelSize(max(6, int(px)))
            f.setBold(bold)
            if spacing:
                f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
            p.setFont(f)
            p.setPen(color)
            p.drawText(QRectF(c.x() - r * 0.7, c.y() + y - px, r * 1.4, px * 2), Qt.AlignCenter, s)

        text(self.top_text, -r * 0.33, r * 0.105, LABEL, spacing=r * 0.012)
        if self._busy:
            big = self.center_text or "…"
            text(big, -r * 0.02, r * (0.30 if len(big) <= 5 else 0.22 if len(big) <= 8 else 0.17), accent, bold=True)
        else:
            text(f"{self.displayed_percent()}%", -r * 0.02, r * 0.34, accent, bold=True)
        text(self.bottom_text, r * 0.27, r * 0.105, LABEL, spacing=r * 0.012)

        if self._gear:
            from .busygear import gear_path      # (busygear imports this module)
            gc = QPointF(c.x(), c.y() + r * 0.53)
            gr = r * 0.095
            p.save()
            p.translate(gc)
            p.rotate(self._gear_angle)
            p.setPen(QPen(accent.darker(125), max(1.0, r * 0.008)))
            p.setBrush(accent)
            p.drawPath(gear_path(0, 0, gr, 9))
            p.restore()
            p.save()                                         # small meshing gear, counter-rotating
            p.translate(QPointF(gc.x() + gr * 1.42, gc.y() - gr * 0.62))
            p.rotate(-self._gear_angle * 9 / 6 + 20)
            g2 = _mix(TEAL, GREEN, 0.85)
            p.setPen(QPen(g2.darker(125), max(1.0, r * 0.006)))
            p.setBrush(g2)
            p.drawPath(gear_path(0, 0, gr * 0.6, 6))
            p.restore()
            p.end()
            return
        # pulse / heartbeat mark
        w, h, y0 = r * 0.34, r * 0.12, c.y() + r * 0.50
        x0 = c.x() - w / 2
        pts = [(0, 0), (0.30, 0), (0.40, -0.7), (0.50, 0.9), (0.60, -1.0), (0.68, 0), (1, 0)]
        path = QPainterPath(QPointF(x0, y0))
        for fx, fy in pts[1:]:
            path.lineTo(QPointF(x0 + fx * w, y0 + fy * h))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, max(1.5, r * 0.022), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.end()
