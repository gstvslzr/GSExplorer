# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronize: live progress window.

Opened by the Synchronize tool when a run starts. Shows, for the whole run (all tasks of the profile):

* a ring with the % done, a turning gear and a countdown of the estimated time left;
* which way data is moving right now (left → right, right → left, deleting on one side, snapshot);
* the operation in progress with its full source and destination paths, and a bar for the current file;
* files done of total, data done of total, speed, elapsed, remaining, and per-direction counts.

It reads the jobs' live state (fileops.Job.sync, filled by JobManager._do_sync) on a 4 Hz timer, so it
costs nothing on the worker thread and keeps updating while a large file is being copied.

Estimate ("time left"): remaining bytes ÷ the transfer rate over the last 5 s. Each file also counts as
64 KiB of work, so a run of many small files or of deletions only still gets a sensible estimate. The
first 2 s show "estimating"; while paused the clock stops.
"""
from __future__ import annotations

import collections
import os
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QTextOption
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import APP_NAME, fsutil
from . import syncengine as se
from .busygear import GearSpinner
from .fileops import SYNC_PER_FILE
from .ringprogress import GREEN, TEAL, RingProgress, _mix

PER_FILE = SYNC_PER_FILE     # weight of one file operation in the % and the estimate (bytes-equivalent)
RATE_WINDOW = 5.0           # seconds of history for the speed
RED = QColor("#d9534f")

DIR_TEXT = {"lr": "left → right", "rl": "right → left", "delete-left": "on the left",
            "delete-right": "on the right", "snapshot": "source → new snapshot"}
DELETE_HOW = {"trash": "moved to Trash", "folder": "moved to the versions folder", "none": "deleted permanently"}


def fmt_duration(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def describe_op(op: dict | None, task: se.Task | None) -> tuple[str, str, str, str, str]:
    """(verb, from-label, from-path, to-label, to-path) for the operation in progress."""
    if not op:
        return "Preparing…", "", "", "", ""
    k, d = op["kind"], op.get("dir", "")
    arrow = f" ({DIR_TEXT[d]})" if d in ("lr", "rl", "snapshot") else ""
    if k == "copy":
        return "Copying file" + arrow, "From", op["src"], "To", op["dst"]
    if k == "symlink":
        return "Copying symbolic link" + arrow, "From", op["src"], "To", op["dst"]
    if k == "mkdir":
        return "Creating folder" + arrow, "From", op["src"], "To", op["dst"]
    if k == "delete":
        how = DELETE_HOW.get(op.get("dst") or (task.versioning if task else ""), "deleted")
        side = "left" if d == "delete-left" else "right"
        where = ""
        if (op.get("dst") or "") == "folder" and task is not None and task.versions_dir:
            where = task.versions_dir
        return f"Deleting on the {side} ({how})", "Path", op["src"], "Moved to" if where else "", where
    if k == "hardlink":
        return "Unchanged — hard link to the previous snapshot (no data copied)", "Source", op["src"], \
            "Link", op["dst"]
    if k == "scan":
        return "Scanning the source for the snapshot", "Folder", op["src"], "Snapshot", op["dst"]
    if k == "hook":
        return f"Running the {op.get('dst') or ''}-task command".replace("the -task", "the task"), \
            "Command", op["src"], "", ""
    if k == "prune":
        return "Removing an old snapshot (keep limit)", "Path", op["src"], "", ""
    if k == "finish":
        return "Finishing (versions clean-up, saving sync state)", "Folder", op["src"], "", ""
    return k, "Path", op.get("src", ""), "", ""


# --------------------------------------------------------------------------- widgets

class DirectionStrip(QWidget):
    """LEFT folder ──▶▶▶── RIGHT folder; the chevrons run the way data is moving now."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.left = self.right = ""
        self.left_cap, self.right_cap = "LEFT", "RIGHT"
        self.direction = ""
        self.running = False
        self.phase = 0.0
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def set_roots(self, left: str, right: str, snapshot: bool = False) -> None:
        self.left, self.right = left, right
        self.left_cap, self.right_cap = ("SOURCE", "SNAPSHOTS") if snapshot else ("LEFT", "RIGHT")
        self.update()

    def set_state(self, direction: str, running: bool) -> None:
        changed = (direction, running) != (self.direction, self.running)
        self.direction, self.running = direction, running
        if running and direction and not self._timer.isActive():
            self._timer.start()
        elif not (running and direction):
            self._timer.stop()
        if changed:
            self.update()

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def _tick(self) -> None:
        self.phase = (self.phase + 0.04) % 1.0
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pal = self.palette()
        fg = pal.windowText().color()
        dim = QColor(fg)
        dim.setAlpha(110)
        accent = _mix(TEAL, GREEN, 0.45)
        bw = w * 0.36
        d = self.direction
        boxes = ((QRectF(1, 4, bw, h - 8), self.left_cap, self.left, "left"),
                 (QRectF(w - bw - 1, 4, bw, h - 8), self.right_cap, self.right, "right"))
        for rect, cap, path, side in boxes:
            dest = (d == "lr" and side == "right") or (d == "rl" and side == "left") or \
                (d == "snapshot" and side == "right")
            src = (d == "lr" and side == "left") or (d == "rl" and side == "right") or \
                (d == "snapshot" and side == "left")
            dele = d == f"delete-{side}"
            border = RED if dele else accent if (dest or src) else dim
            fill = QColor(border)
            fill.setAlpha(40 if (dest or dele) else 16 if src else 0)
            p.setPen(QPen(border, 2.0 if (dest or dele) else 1.2))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 7, 7)
            f = QFont(self.font())
            f.setPointSizeF(max(6.0, f.pointSizeF() * 0.78))
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
            p.setFont(f)
            p.setPen(RED if dele else accent if (dest or src) else dim)
            tag = cap + ("  ·  DELETING" if dele else "  ·  WRITING" if dest else "  ·  READING" if src else "")
            p.drawText(rect.adjusted(10, 5, -8, 0), Qt.AlignLeft | Qt.AlignTop, tag)
            f2 = QFont(self.font())
            p.setFont(f2)
            p.setPen(fg)
            txt = p.fontMetrics().elidedText(fsutil.display_path(path), Qt.ElideMiddle, int(rect.width() - 20))
            p.drawText(rect.adjusted(10, 0, -8, -7), Qt.AlignLeft | Qt.AlignBottom, txt)
        # the link between the boxes
        x0, x1, y = bw + 10, w - bw - 10, h / 2
        if x1 - x0 < 30:
            p.end()
            return
        moving = d in ("lr", "rl", "snapshot")
        p.setPen(QPen(accent if moving else dim, 2, Qt.SolidLine if moving else Qt.DashLine, Qt.RoundCap))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        if moving:
            sign = -1 if d == "rl" else 1
            n = max(2, int((x1 - x0) / 22))
            p.setPen(Qt.NoPen)
            for i in range(n):
                t = ((i + (self.phase if self.running else 0.0)) / n) % 1.0
                x = x0 + (x1 - x0) * (t if sign > 0 else 1 - t)
                alpha = int(255 * (0.25 + 0.75 * (1 - abs(t - 0.5) * 2)))
                c = QColor(accent)
                c.setAlpha(alpha)
                p.setBrush(c)
                s = 6.0
                path = QPainterPath()
                path.moveTo(x + sign * s, y)
                path.lineTo(x - sign * s * 0.4, y - s)
                path.lineTo(x - sign * s * 0.4, y + s)
                path.closeSubpath()
                p.drawPath(path)
        elif d.startswith("delete"):
            p.setPen(QPen(RED, 2.4, Qt.SolidLine, Qt.RoundCap))
            cx = x0 + 14 if d == "delete-left" else x1 - 14
            p.drawLine(QPointF(cx - 6, y - 6), QPointF(cx + 6, y + 6))
            p.drawLine(QPointF(cx - 6, y + 6), QPointF(cx + 6, y - 6))
        p.end()


class PathView(QPlainTextEdit):
    """Read-only full path that wraps anywhere (no character is altered, so copying gives the real path)."""

    def __init__(self, parent=None, lines: int = 2):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setWordWrapMode(QTextOption.WrapAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QPlainTextEdit { background: transparent; }")
        self.document().setDocumentMargin(1)
        fm = self.fontMetrics()
        self.setFixedHeight(fm.lineSpacing() * lines + 6)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self._text = ""

    def set_path(self, path: str) -> None:
        text = fsutil.display_path(path) if path else ""
        if text != self._text:
            self._text = text
            self.setPlainText(text)
            self.setToolTip(text)

    def text(self) -> str:
        return self._text


# --------------------------------------------------------------------------- the window

class SyncRunDialog(QDialog):
    """Progress of one Synchronize run (one or more queued sync jobs)."""
    closed = Signal()

    def __init__(self, parent, profile: str, jobs: list, cfg=None, log_path: str = "", manager=None):
        super().__init__(parent)
        self.setWindowTitle(f"Synchronizing — {profile} — {APP_NAME}")
        self.setModal(False)
        self.setMinimumWidth(780)
        self.jobs = list(jobs)                       # fileops.Job, in run order
        self.cfg = cfg
        self.log_path = log_path
        self.manager = manager                       # JobManager: re-announce state so queue/status agree
        self._last_eta: float | None = None
        self.ended = False
        self.outcome = ""                            # ok | problems | cancelled (when done)
        self._t0 = time.monotonic()
        self._paused_since = 0.0
        self._paused_total = 0.0
        self._hist: collections.deque = collections.deque()     # (running seconds, work units)
        self._bhist: collections.deque = collections.deque()    # (running seconds, work units, bytes)
        self._weights = []
        for j in self.jobs:
            plan = j.payload[0]
            s = plan.summary()
            st_ops = s["copy_lr"] + s["copy_rl"] + s["del_l"] + s["del_r"]
            if plan.task.mode == "snapshot":
                st_ops += sum(1 for i in plan.items if i.action == se.LINK)
            self._weights.append((s["bytes"], st_ops))
        self.total_bytes = sum(b for b, _ in self._weights)
        self.total_ops = sum(n for _, n in self._weights)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        self.gear = GearSpinner(self, 22)
        self.gear.start()
        head.addWidget(self.gear)
        self.title = QLabel("<b>Synchronizing…</b>")
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.title.setFont(f)
        head.addWidget(self.title, 1)
        self.task_lbl = QLabel("")
        self.task_lbl.setObjectName("Muted")
        head.addWidget(self.task_lbl)
        lay.addLayout(head)

        body = QHBoxLayout()
        self.ring = RingProgress()
        self.ring.setFixedSize(230, 230)
        self.ring.setValue(0.0, "SYNCHRONIZING", "ESTIMATING")
        self.ring.setGear(True, True)
        body.addWidget(self.ring, 0, Qt.AlignTop)
        right = QVBoxLayout()
        self.strip = DirectionStrip()
        right.addWidget(self.strip)
        self.action = QLabel("Preparing…")
        fa = self.action.font()
        fa.setBold(True)
        self.action.setFont(fa)
        self.action.setWordWrap(True)
        right.addWidget(self.action)
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(2)
        self.from_lbl, self.to_lbl = QLabel("From"), QLabel("To")
        for lbl in (self.from_lbl, self.to_lbl):
            lbl.setObjectName("Muted")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
            lbl.setMinimumWidth(60)
        self.from_path, self.to_path = PathView(lines=2), PathView(lines=2)
        g.addWidget(self.from_lbl, 0, 0)
        g.addWidget(self.from_path, 0, 1)
        g.addWidget(self.to_lbl, 1, 0)
        g.addWidget(self.to_path, 1, 1)
        g.setColumnStretch(1, 1)
        right.addLayout(g)
        fb = QHBoxLayout()
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.file_bar.setTextVisible(False)
        self.file_bar.setFixedHeight(10)
        fb.addWidget(self.file_bar, 1)
        self.file_txt = QLabel("")
        self.file_txt.setObjectName("Muted")
        self.file_txt.setMinimumWidth(190)
        self.file_txt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fb.addWidget(self.file_txt)
        right.addLayout(fb)
        right.addStretch(1)
        body.addLayout(right, 1)
        lay.addLayout(body)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        lay.addWidget(line)
        sg = QGridLayout()
        sg.setHorizontalSpacing(18)
        self.stat = {}
        cells = (("files", "Files"), ("data", "Data"), ("speed", "Speed"), ("elapsed", "Elapsed"),
                 ("lr", "Copied →"), ("rl", "Copied ←"), ("deleted", "Deleted"), ("remaining", "Remaining"),
                 ("linked", "Linked"), ("errors", "Errors"), ("warnings", "Warnings"), ("task", "Task"))
        for i, (key, cap) in enumerate(cells):
            c = QLabel(cap)
            c.setObjectName("Muted")
            v = QLabel("—")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            r, col = divmod(i, 4)
            sg.addWidget(c, r, col * 2)
            sg.addWidget(v, r, col * 2 + 1)
            self.stat[key] = v
        for col in (1, 3, 5, 7):
            sg.setColumnStretch(col, 1)
        lay.addLayout(sg)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.hide()
        lay.addWidget(self.summary)

        btns = QHBoxLayout()
        self.auto_close = QCheckBox("Close this window when a run finishes without problems")
        self.auto_close.setChecked(bool(self.cfg["sync_close_when_done"]) if self.cfg is not None else False)
        self.auto_close.toggled.connect(self._auto_close_changed)
        btns.addWidget(self.auto_close, 1)
        self.b_pause = QPushButton("Pause")
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_bg = QPushButton("Run in background")
        self.b_bg.setToolTip("Hide this window; the run continues (also listed in the operations queue).")
        self.b_bg.clicked.connect(self.hide)
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.clicked.connect(self.ask_cancel)
        self.b_problems = QPushButton("Show problems…")
        self.b_problems.clicked.connect(self.show_problems)
        self.b_log = QPushButton("Open log")
        self.b_log.clicked.connect(self.open_log)
        self.b_close = QPushButton("Close")
        self.b_close.clicked.connect(self.close)
        for b in (self.b_pause, self.b_bg, self.b_cancel, self.b_problems, self.b_log, self.b_close):
            btns.addWidget(b)
        for b in (self.b_problems, self.b_log, self.b_close):
            b.hide()
        lay.addLayout(btns)

    # ------------------------------------------------------------------ state
    def _auto_close_changed(self, on: bool) -> None:
        if self.cfg is not None:
            self.cfg["sync_close_when_done"] = bool(on)

    def current_job(self):
        for j in self.jobs:
            if j.state in ("running", "paused"):
                return j
        for j in self.jobs:
            if j.state == "queued":
                return j
        return self.jobs[-1] if self.jobs else None

    def paused(self) -> bool:
        return any(j.state == "paused" for j in self.jobs)

    def progress_units(self) -> tuple[float, float, int, int, int]:
        """(work done, work total, bytes done, files done, files total) over all jobs (see PER_FILE)."""
        done = bytes_done = files_done = 0
        for j, (b, n) in zip(self.jobs, self._weights):
            st = j.sync or {}
            if j.state == "done":
                jb, jn = b, n
            else:
                jb = min(j.done_bytes, b) if b else 0
                jn = min(st.get("done_ops", 0), n)
            bytes_done += jb
            files_done += jn
            done += jb + jn * PER_FILE
        total = self.total_bytes + self.total_ops * PER_FILE
        return done, total, bytes_done, files_done, self.total_ops

    def fraction(self) -> float:
        done, total, *_ = self.progress_units()
        if total <= 0:
            finished = sum(1 for j in self.jobs if j.state in ("done", "failed", "cancelled"))
            return finished / max(1, len(self.jobs))
        return min(1.0, done / total)

    def elapsed(self) -> float:
        now = time.monotonic()
        paused = self._paused_total + ((now - self._paused_since) if self._paused_since else 0.0)
        return max(0.0, now - self._t0 - paused)

    def rate(self) -> float:
        """Work units per second over the last RATE_WINDOW seconds of running time (0 = unknown)."""
        if len(self._hist) < 2:
            return 0.0
        (t0, u0), (t1, u1) = self._hist[0], self._hist[-1]
        return (u1 - u0) / (t1 - t0) if t1 - t0 >= 0.75 else 0.0

    def byte_rate(self) -> float:
        if len(self._bhist) < 2:
            return 0.0
        (t0, _u0, b0), (t1, _u1, b1) = self._bhist[0], self._bhist[-1]
        return (b1 - b0) / (t1 - t0) if t1 - t0 >= 0.75 else 0.0

    def eta(self) -> float | None:
        if self.elapsed() < 2.0:
            return None
        r = self.rate()
        if r <= 0:
            return None
        done, total, *_ = self.progress_units()
        return max(0.0, (total - done) / r)

    # ------------------------------------------------------------------ refresh (4 Hz)
    def refresh(self) -> None:
        if self.ended:
            return
        now = time.monotonic()
        paused = self.paused()
        if paused and not self._paused_since:
            self._paused_since = now
            self._hist.clear()
            self._bhist.clear()
        elif not paused and self._paused_since:
            self._paused_total += now - self._paused_since
            self._paused_since = 0.0
            self._hist.clear()
            self._bhist.clear()
        done, total, bytes_done, files_done, files_total = self.progress_units()
        if not paused:
            el = self.elapsed()
            self._hist.append((el, done))
            self._bhist.append((el, done, bytes_done))
            while len(self._hist) > 2 and self._hist[-1][0] - self._hist[0][0] > RATE_WINDOW:
                self._hist.popleft()
            while len(self._bhist) > 2 and self._bhist[-1][0] - self._bhist[0][0] > RATE_WINDOW:
                self._bhist.popleft()
        job = self.current_job()
        frac = self.fraction()
        eta = self.eta()
        if eta is not None and not paused:
            self._last_eta = eta
        if paused:
            bottom = "—" if self._last_eta is None else f"{fmt_duration(self._last_eta)} LEFT"
        elif job is not None and job.state == "queued":
            bottom = "WAITING"
        else:
            bottom = "ESTIMATING" if eta is None else f"{fmt_duration(eta)} LEFT"
        self.ring.setValue(frac, "PAUSED" if paused else "SYNCHRONIZING", bottom)
        self.ring.setGear(True, not paused)
        if paused:
            self.gear.stop()
        elif not self.gear.isVisible():
            self.gear.start()

        task = job.payload[0].task if job is not None else None
        idx = self.jobs.index(job) + 1 if job is not None else 0
        if task is not None:
            self.strip.set_roots(task.left, task.right, task.mode == "snapshot")
            self.task_lbl.setText(f"Task {idx} of {len(self.jobs)}: “{task.name}” · {se.MODE_LABELS[task.mode]}")
            self.stat["task"].setText(f"{idx} of {len(self.jobs)}")
        st = (job.sync or {}) if job is not None else {}
        op = st.get("op")
        if job is not None and job.state == "queued":
            title = "Waiting for the operations queue…"
            verb, fl, fp, tl, tp = "Another operation is running first; this run starts when it ends.", "", "", "", ""
            direction = ""
        else:
            title = "Paused" if paused else "Synchronizing…"
            verb, fl, fp, tl, tp = describe_op(op, task)
            direction = (op or {}).get("dir", "")
        self.title.setText(f"<b>{title}</b>")
        self.strip.set_state(direction, not paused)
        self.action.setText(verb)
        self.from_lbl.setText(fl)
        self.from_path.set_path(fp)
        self.to_lbl.setText(tl)
        self.to_path.set_path(tp)
        self.from_lbl.setVisible(bool(fl))
        self.from_path.setVisible(bool(fl))
        self.to_lbl.setVisible(bool(tl))
        self.to_path.setVisible(bool(tl))
        if op and op["kind"] == "copy":
            size = max(0, op.get("size", 0))
            d = min(op.get("done", 0), size) if size else 0
            self.file_bar.setRange(0, 1000)
            self.file_bar.setValue(int(1000 * d / size) if size else 1000)
            self.file_txt.setText(f"{fsutil.human_size(d)} of {fsutil.human_size(size)}"
                                  + (f"  ({int(100 * d / size)}%)" if size else ""))
            self.file_bar.setVisible(True)
        else:
            self.file_bar.setVisible(False)
            self.file_txt.setText("")

        self.stat["files"].setText(f"{files_done:,} of {files_total:,}")
        self.stat["data"].setText(f"{fsutil.human_size(bytes_done)} of {fsutil.human_size(self.total_bytes)}")
        br = self.byte_rate()
        self.stat["speed"].setText("paused" if paused else (f"{fsutil.human_size(int(br))}/s" if br > 0 else "—"))
        self.stat["elapsed"].setText(fmt_duration(self.elapsed()))
        self.stat["remaining"].setText("paused" if paused else ("estimating…" if eta is None else fmt_duration(eta)))
        sums = {"lr": 0, "rl": 0, "deleted": 0, "linked": 0}
        planned = {"lr": 0, "rl": 0, "del": 0, "link": 0}
        for j in self.jobs:
            s = j.sync or {}
            for k in sums:
                sums[k] += s.get(k, 0)
            for k in planned:
                planned[k] += (s.get("planned") or {}).get(k, 0)
        self.stat["lr"].setText(f"{sums['lr']:,}" + (f" of {planned['lr']:,}" if planned["lr"] else ""))
        self.stat["rl"].setText(f"{sums['rl']:,}" + (f" of {planned['rl']:,}" if planned["rl"] else ""))
        self.stat["deleted"].setText(f"{sums['deleted']:,}" + (f" of {planned['del']:,}" if planned["del"] else ""))
        self.stat["linked"].setText(f"{sums['linked']:,}" + (f" of {planned['link']:,}" if planned["link"] else ""))
        errs = sum(len(j.errors) for j in self.jobs)
        warns = sum(len(j.warnings) for j in self.jobs)
        self.stat["errors"].setText(f"<span style='color:#d33'><b>{errs}</b></span>" if errs else "0")
        self.stat["warnings"].setText(str(warns))
        if self.manager is not None and job is not None and job.state in ("running", "paused"):
            # job updates are rate-limited at the source; re-send the latest so the queue button and the
            # Sync window's status line show the same % as this window (also while paused)
            self.manager.job_updated.emit(job)
        self.b_pause.setText("Resume" if paused else "Pause")
        self.b_pause.setEnabled(any(j.state in ("running", "paused") for j in self.jobs))

    # ------------------------------------------------------------------ controls
    def toggle_pause(self) -> None:
        if self.paused():
            for j in self.jobs:
                j.resume()
        else:
            for j in self.jobs:
                j.pause()
        self.refresh()

    def ask_cancel(self) -> None:
        if self.ended:
            return
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          "Stop the synchronization?\n\nFiles already copied stay. The file being copied "
                          "is discarded (never left half-written). Tasks not started yet are skipped.",
                          QMessageBox.NoButton, self)
        stop = box.addButton("Stop", QMessageBox.DestructiveRole)
        box.addButton("Keep running", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is stop:
            self.cancel()

    def cancel(self) -> None:
        for j in self.jobs:
            if j.state in ("queued", "running", "paused"):
                j.cancel()
        self.title.setText("<b>Cancelling…</b>")

    def show_problems(self) -> None:
        errs = [e for j in self.jobs for e in j.errors]
        warns = [w for j in self.jobs for w in j.warnings]
        box = QMessageBox(QMessageBox.Warning if errs else QMessageBox.Information, APP_NAME,
                          f"{len(errs)} errors, {len(warns)} warnings. See Details.", QMessageBox.Ok, self)
        box.setDetailedText("\n".join(errs[:300] + warns[:100]))
        box.exec()

    def open_log(self) -> None:
        if not self.log_path or not os.path.exists(self.log_path):
            return
        from . import dialogs
        dialogs.ViewerDialog(self.parent().win if hasattr(self.parent(), "win") else self.parent(),
                             self.log_path, [self.log_path]).exec()

    # ------------------------------------------------------------------ end of run
    def finish(self) -> None:
        """All jobs ended: freeze the display on the result."""
        if self.ended:
            return
        self.refresh()
        self.ended = True
        self._timer.stop()
        cancelled = any(j.state == "cancelled" for j in self.jobs)
        failed = [j for j in self.jobs if j.state == "failed"]
        errs = sum(len(j.errors) for j in self.jobs)
        warns = sum(len(j.warnings) for j in self.jobs)
        ok = not cancelled and not failed and not errs
        self.outcome = "cancelled" if cancelled else ("ok" if ok else "problems")
        took = fmt_duration(self.elapsed())
        stats = {"copied": 0, "deleted": 0, "linked": 0, "bytes": 0}
        for j in self.jobs:
            for k in stats:
                stats[k] += (j.stats or {}).get(k, 0)
        frac = 1.0 if ok else self.fraction()
        top = "COMPLETE" if ok else ("CANCELLED" if cancelled else "FINISHED")
        self.ring.setValue(frac, top, f"IN {took}")
        self.ring.setGear(True, False)
        self.gear.stop()
        self.strip.set_state("", False)
        head = "✓ Synchronization complete" if ok else ("⚠ Synchronization cancelled" if cancelled
                                                        else "⚠ Synchronization finished with problems")
        self.title.setText(f"<b>{head}</b>")
        self.action.setText("")
        for w in (self.from_lbl, self.from_path, self.to_lbl, self.to_path, self.file_bar):
            w.hide()
        self.file_txt.setText("")
        self.stat["speed"].setText(f"{fsutil.human_size(int(stats['bytes'] / max(0.001, self.elapsed())))}/s average"
                                   if stats["bytes"] else "—")
        self.stat["remaining"].setText("—")
        msg = (f"{stats['copied']:,} copied ({fsutil.human_size(stats['bytes'])}), {stats['deleted']:,} deleted, "
               f"{stats['linked']:,} linked · {errs} errors, {warns} warnings · took {took}.")
        if cancelled:
            msg = "Stopped by you. Files already copied stay; nothing is left half-written. " + msg
        for j in failed:
            msg += f"<br>Task failed: {j.errors[-1] if j.errors else 'see the log'}"
        color = "#1d9e75" if ok else "#d33"
        self.summary.setText(f"<span style='color:{color}'>{msg}</span>")
        self.summary.show()
        for b in (self.b_pause, self.b_bg, self.b_cancel):
            b.hide()
        self.b_problems.setVisible(bool(errs or warns))
        self.b_log.setVisible(bool(self.log_path))
        self.b_close.show()
        self.b_close.setDefault(True)
        if ok and self.auto_close.isChecked():
            QTimer.singleShot(1500, self.close)

    def closeEvent(self, ev):
        if not self.ended:
            # closing the window doesn't stop the run: it goes to the background
            self.hide()
            ev.ignore()
            return
        self._timer.stop()
        self.closed.emit()
        super().closeEvent(ev)

    def reject(self) -> None:          # Esc
        if not self.ended:
            self.hide()
        else:
            super().reject()
            self.closed.emit()
