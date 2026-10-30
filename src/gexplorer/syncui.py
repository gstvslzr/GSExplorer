"""Sync & Backup window: profiles of folder-pair tasks, compare preview, run, schedule."""
from __future__ import annotations

import copy
import os
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTime, Signal
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QTableView, QTimeEdit, QToolButton, QVBoxLayout,
                               QWidget)

from . import APP_NAME, fsutil, schedule, system
from . import syncengine as se
from .ringprogress import RingProgress
from .syncprogress import SyncRunDialog

UNSAVED = "(unsaved profile)"
FILTER_HELP = ("One pattern per line. <b>*</b> and <b>?</b> wildcards, case-insensitive. "
               "No “/” → matches a name at any depth (<code>*.tmp</code>). "
               "With “/” → path from the folder root (<code>docs/*.pdf</code>). "
               "Trailing “/” → folders only (<code>node_modules/</code>).")


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    lbl.setWordWrap(True)
    return lbl


def _fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


# --------------------------------------------------------------------------- preview model

class PlanModel(QAbstractTableModel):
    COLS = ("Task", "Category", "Path", "Left size", "Left modified", "Action", "Right size", "Right modified",
            "Note")

    def __init__(self, colors: dict, parent=None):
        super().__init__(parent)
        self.plans: list[se.Plan] = []
        self.rows: list[tuple[se.Plan, se.Item]] = []
        self.show: set[str] = {se.LEFT_ONLY, se.RIGHT_ONLY, se.LEFT_NEWER, se.RIGHT_NEWER,
                               se.DIFFERENT, se.TYPE_DIFF, se.CONFLICT}
        self.set_colors(colors)

    def set_colors(self, c: dict) -> None:
        self.cat_color = {
            se.LEFT_ONLY: QColor(c["accent"]), se.LEFT_NEWER: QColor(c["accent"]),
            se.RIGHT_ONLY: QColor("#1d9e75"), se.RIGHT_NEWER: QColor("#1d9e75"),
            se.DIFFERENT: QColor("#ba7517"), se.TYPE_DIFF: QColor("#ba7517"),
            se.CONFLICT: QColor(c["danger"]), se.EQUAL: QColor(c["muted"]),
        }

    def set_plans(self, plans: list[se.Plan]) -> None:
        self.plans = plans
        self.refilter()

    def refilter(self) -> None:
        self.beginResetModel()
        self.rows = [(p, i) for p in self.plans for i in p.items if i.category in self.show]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLS[s]
        return None

    @staticmethod
    def _size(info: se.Info | None, item: se.Item, side_has: bool) -> str:
        if info is None:
            return ""
        if info.kind == "d":
            return f"<DIR> {item.files} files" if side_has and item.files else "<DIR>"
        if info.kind == "l":
            return "link"
        if info.kind == "o":
            return "special"
        return fsutil.human_size(info.size)

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        plan, it = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return plan.task.name
            if c == 1:
                return se.CATEGORY_LABELS[it.category]
            if c == 2:
                return fsutil.display_name(it.rel)
            if c == 3:
                return self._size(it.left, it, True)
            if c == 4:
                return _fmt_time(it.left.mtime) if it.left else ""
            if c == 5:
                lbl = se.ACTION_LABELS[it.action]
                return lbl + ("  (changed)" if it.action != it.default_action else "")
            if c == 6:
                return self._size(it.right, it, True)
            if c == 7:
                return _fmt_time(it.right.mtime) if it.right else ""
            if c == 8:
                return it.note
        elif role == Qt.ForegroundRole and c in (1, 5):
            return self.cat_color.get(it.category if c == 1 else (
                se.LEFT_ONLY if it.action == se.COPY_LR else se.RIGHT_ONLY if it.action == se.COPY_RL
                else se.CONFLICT if it.action in (se.DEL_L, se.DEL_R) else se.EQUAL))
        elif role == Qt.FontRole and c == 5:
            f = QFont()
            f.setBold(it.action not in (se.NONE, se.LINK))
            return f
        elif role == Qt.TextAlignmentRole and c in (3, 6):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.TextAlignmentRole and c == 5:
            return int(Qt.AlignCenter)
        elif role == Qt.ToolTipRole:
            return f"{plan.task.left}/{it.rel}\n{plan.task.right}/{it.rel}" + (f"\n{it.note}" if it.note else "")
        return None

    def item(self, row: int) -> tuple[se.Plan, se.Item]:
        return self.rows[row]

    def changed(self, rows: list[int]) -> None:
        for r in rows:
            self.dataChanged.emit(self.index(r, 0), self.index(r, self.columnCount() - 1))


# --------------------------------------------------------------------------- background compare

class _CompareSignals(QObject):
    progress = Signal(str)
    scan = Signal(object)               # dict: task, index, n, phase, rel, expected
    done = Signal(object, object)       # plans, error str


class ScanProgressDialog(QDialog):
    """Modal popup while Compare scans folders.

    The bar is exact when this folder pair was scanned before (the previous item count is
    the estimate); on a first scan it shows activity (busy bar) plus a live item count.
    """
    cancelled = Signal()

    def __init__(self, parent, profile: str, n_tasks: int):
        super().__init__(parent)
        self.setWindowTitle(f"Comparing — {profile} — {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.n_tasks = max(1, n_tasks)
        self.t0 = time.monotonic()
        lay = QVBoxLayout(self)
        self.title = QLabel("<b>Scanning…</b>")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.ring = RingProgress()
        self.ring.setFixedSize(240, 240)
        self.ring.setBusy("0", "SCANNING", "ITEMS")
        lay.addWidget(self.ring, 0, Qt.AlignHCenter)
        self.detail = _muted("")
        lay.addWidget(self.detail)
        self.path = _muted("")
        self.path.setWordWrap(False)
        self.path.setMinimumWidth(480)
        lay.addWidget(self.path)
        row = QHBoxLayout()
        self.elapsed = _muted("")
        row.addWidget(self.elapsed, 1)
        b = QPushButton("Cancel")
        b.clicked.connect(self._cancel)
        row.addWidget(b)
        lay.addLayout(row)
        parent._sig.scan.connect(self.update_progress)

    def update_progress(self, d: dict) -> None:
        i, n, exp = d["index"], d["n"], d["expected"]
        phase = {"left": "left / source", "right": "right / target", "compare": "comparing"}.get(d["phase"], "")
        self.title.setText(f"<b>Task {i + 1} of {self.n_tasks}: {d['task']}</b>")
        if exp > 0:
            frac = min(0.99, n / exp)
            self.ring.setValue((i + frac) / self.n_tasks)
            self.detail.setText(f"Scanning {phase}: {n:,} of ~{exp:,} items")
        else:
            self.ring.setBusy(f"{n:,}", "SCANNING", "ITEMS")   # size unknown on first scan of this pair
            self.detail.setText(f"Scanning {phase}: {n:,} items  (first scan of this pair)")
        rel = fsutil.display_name(d.get("rel") or "")
        self.path.setText(self.fontMetrics().elidedText(rel, Qt.ElideMiddle, 480))
        self.elapsed.setText(f"Elapsed {int(time.monotonic() - self.t0)} s")

    def _cancel(self) -> None:
        self.detail.setText("Cancelling…")
        self.cancelled.emit()

    def finish(self) -> None:
        try:
            self.parent()._sig.scan.disconnect(self.update_progress)
        except (RuntimeError, TypeError):
            pass
        self.ring.setValue(1.0)
        self.accept()
        self.deleteLater()

    def reject(self) -> None:              # Esc / window close = cancel
        self._cancel()


# --------------------------------------------------------------------------- path row

class PathRow(QWidget):
    changed = Signal()

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.textChanged.connect(lambda _t: self.changed.emit())
        browse = QToolButton()
        browse.setText("…")
        browse.setToolTip("Browse")
        browse.clicked.connect(self._browse)
        pick = QToolButton()
        pick.setText("Pane")
        pick.setToolTip("Use the folder open in a pane")
        pick.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(pick)
        m.addAction("Left pane folder", lambda: self.edit.setText(self.win.left.current().path))
        m.addAction("Right pane folder", lambda: self.edit.setText(self.win.right.current().path))
        pick.setMenu(m)
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)
        lay.addWidget(pick)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose folder", self.edit.text() or os.path.expanduser("~"))
        if d:
            self.edit.setText(d)

    def text(self) -> str:
        t = self.edit.text().strip()
        return os.path.abspath(os.path.expanduser(t)) if t else ""

    def setText(self, t: str) -> None:
        self.edit.setText(fsutil.display_path(t))


# --------------------------------------------------------------------------- main window

class SyncWindow(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"Sync & Backup — {APP_NAME}")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1180, 780)
        self.profile = se.Profile(UNSAVED, [])
        self.dirty = False
        self._loading = False
        self._cancel = threading.Event()
        self._sig = _CompareSignals()
        self._sig.progress.connect(lambda s: self.status.setText(s))
        self._sig.done.connect(self._compare_done)
        self._jobs: dict[int, object] = {}
        self._run_dlg: SyncRunDialog | None = None
        win.jobs.job_updated.connect(self._job_updated)
        win.jobs.job_finished.connect(self._job_finished)

        root = QVBoxLayout(self)
        # ------------------------------------------------ profile bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Profile:"))
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(220)
        self.profile_box.activated.connect(self._profile_selected)
        bar.addWidget(self.profile_box)
        for text, slot in (("New", self.new_profile), ("Save", self.save), ("Save as…", self.save_as),
                           ("Delete", self.delete_profile), ("Schedule…", self.schedule),
                           ("Logs", self.open_logs)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            bar.addWidget(b)
            setattr(self, "btn_" + text.lower().replace("…", "").replace(" ", "_"), b)
        bar.addStretch(1)
        self.sched_lbl = _muted("")
        bar.addWidget(self.sched_lbl)
        root.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        root.addWidget(split, 1)

        # ------------------------------------------------ tasks + editor
        top = QSplitter(Qt.Horizontal)
        left_box = QWidget()
        lb = QVBoxLayout(left_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.addWidget(QLabel("<b>Tasks</b> (checked tasks run, in order)"))
        self.task_list = QListWidget()
        self.task_list.currentRowChanged.connect(self._task_selected)
        self.task_list.itemChanged.connect(self._task_checked)
        lb.addWidget(self.task_list, 1)
        tb = QHBoxLayout()
        for text, slot, tip in (("+", self.add_task, "Add task"), ("−", self.remove_task, "Remove task"),
                                ("↑", lambda: self.move_task(-1), "Move up"),
                                ("↓", lambda: self.move_task(1), "Move down"),
                                ("Duplicate", self.duplicate_task, "Duplicate task")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            tb.addWidget(b)
        tb.addStretch(1)
        lb.addLayout(tb)
        top.addWidget(left_box)

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.f_name = QLineEdit()
        self.f_mode = QComboBox()
        for m in se.MODES:
            self.f_mode.addItem(se.MODE_LABELS[m], m)
        self.mode_help = _muted("")
        self.f_left = PathRow(win)
        self.f_right = PathRow(win)
        self.lbl_left, self.lbl_right = QLabel("Source:"), QLabel("Target:")
        self.f_compare = QComboBox()
        for c in se.COMPARES:
            self.f_compare.addItem(se.COMPARE_LABELS[c], c)
        self.f_tol = QSpinBox()
        self.f_tol.setRange(0, 3600)
        self.f_tol.setSuffix(" s tolerance")
        cmp_row = QHBoxLayout()
        cmp_row.addWidget(self.f_compare, 1)
        cmp_row.addWidget(self.f_tol)
        self.f_ver = QComboBox()
        for v in se.VERSIONING:
            self.f_ver.addItem(se.VERSIONING_LABELS[v], v)
        self.f_ver_dir = PathRow(win)
        self.f_ver_days = QSpinBox()
        self.f_ver_days.setRange(0, 3650)
        self.f_ver_days.setSpecialValueText("keep forever")
        self.f_ver_days.setSuffix(" days")
        ver_row = QHBoxLayout()
        ver_row.addWidget(self.f_ver_dir, 1)
        ver_row.addWidget(QLabel("Keep:"))
        ver_row.addWidget(self.f_ver_days)
        self.ver_row_w = QWidget()
        self.ver_row_w.setLayout(ver_row)
        self.f_keep = QSpinBox()
        self.f_keep.setRange(1, 999)
        self.f_keep.setSuffix(" snapshots")
        self.f_empty = QCheckBox("Allow empty source (lets a mirror delete everything on the target)")
        self.f_shift = QCheckBox("Ignore exact 1-hour time differences (daylight-saving changes; "
                                 "always on for FAT/exFAT drives)")
        self.f_inc = QPlainTextEdit()
        self.f_exc = QPlainTextEdit()
        for e in (self.f_inc, self.f_exc):
            e.setFixedHeight(64)
        filt = QHBoxLayout()
        iv, ev = QVBoxLayout(), QVBoxLayout()
        iv.addWidget(_muted("Include"))
        iv.addWidget(self.f_inc)
        ev.addWidget(_muted("Exclude"))
        ev.addWidget(self.f_exc)
        filt.addLayout(iv)
        filt.addLayout(ev)
        self.f_pre = QLineEdit()
        self.f_pre.setPlaceholderText("optional shell command run before the task (non-zero exit aborts)")
        self.f_post = QLineEdit()
        self.f_post.setPlaceholderText("optional shell command run after the task ($GX_ERRORS = error count)")

        form.addRow("Name:", self.f_name)
        form.addRow("Mode:", self.f_mode)
        form.addRow("", self.mode_help)
        form.addRow(self.lbl_left, self.f_left)
        form.addRow(self.lbl_right, self.f_right)
        self.row_compare = QWidget()
        self.row_compare.setLayout(cmp_row)
        form.addRow("Compare by:", self.row_compare)
        form.addRow("", self.f_shift)
        form.addRow("Old versions:", self.f_ver)
        form.addRow("Versions folder:", self.ver_row_w)
        form.addRow("Keep:", self.f_keep)
        form.addRow("", self.f_empty)
        filt_w = QWidget()
        filt_w.setLayout(filt)
        form.addRow("Filters:", filt_w)
        form.addRow("", _muted(FILTER_HELP))
        form.addRow("Before:", self.f_pre)
        form.addRow("After:", self.f_post)
        self._form = form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.editor)
        scroll.setFrameShape(QFrame.NoFrame)
        self.empty_page = QWidget()
        el = QVBoxLayout(self.empty_page)
        el.addStretch(1)
        msg = QLabel("<b>No task yet.</b><br>A task is one pair of folders: what to back up (Source) "
                     "and where to (Target).")
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        el.addWidget(msg)
        add_btn = QPushButton("Add a task")
        add_btn.setAutoDefault(False)
        add_btn.clicked.connect(self.add_task)
        el.addWidget(add_btn, 0, Qt.AlignCenter)
        el.addStretch(1)
        from PySide6.QtWidgets import QStackedWidget
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(scroll)
        self.editor_stack.addWidget(self.empty_page)
        self._editor_scroll = scroll
        top.addWidget(self.editor_stack)
        top.setStretchFactor(1, 1)
        top.setSizes([260, 900])
        split.addWidget(top)

        for w in (self.f_name, self.f_pre, self.f_post):
            w.textChanged.connect(self._editor_changed)
        for w in (self.f_inc, self.f_exc):
            w.textChanged.connect(self._editor_changed)
        for w in (self.f_mode, self.f_compare, self.f_ver):
            w.currentIndexChanged.connect(self._editor_changed)
        for w in (self.f_tol, self.f_ver_days, self.f_keep):
            w.valueChanged.connect(self._editor_changed)
        self.f_empty.toggled.connect(self._editor_changed)
        self.f_shift.toggled.connect(self._editor_changed)
        for w in (self.f_left, self.f_right, self.f_ver_dir):
            w.changed.connect(self._editor_changed)

        # ------------------------------------------------ preview
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.btn_compare = QPushButton("Compare")
        self.btn_compare.setToolTip("Scan both sides and preview what will happen (nothing is changed)")
        self.btn_compare.clicked.connect(self.compare)
        self.btn_compare.setAutoDefault(False)
        self.btn_sync = QPushButton("Synchronize")
        self.btn_sync.setDefault(True)
        self.btn_sync.setToolTip("Compares first if needed, shows what will happen, then asks before changing anything")
        self._sync_after_compare = False
        self._result_html = ""
        self.btn_sync.clicked.connect(self.synchronize)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._cancel_compare)
        actions.addWidget(self.btn_compare)
        actions.addWidget(self.btn_sync)
        actions.addWidget(self.btn_cancel)
        actions.addSpacing(12)
        self.cat_btns: dict[str, QToolButton] = {}
        for cat in (se.LEFT_ONLY, se.LEFT_NEWER, se.RIGHT_ONLY, se.RIGHT_NEWER, se.DIFFERENT, se.CONFLICT,
                    se.EQUAL):
            b = QToolButton()
            b.setCheckable(True)
            b.setChecked(cat != se.EQUAL)
            b.setText(se.CATEGORY_LABELS[cat])
            b.toggled.connect(lambda on, c=cat: self._toggle_cat(c, on))
            actions.addWidget(b)
            self.cat_btns[cat] = b
        actions.addStretch(1)
        bl.addLayout(actions)
        self.summary = QLabel("Set Source and Target, then press Synchronize — it shows what will change "
                              "and asks before touching anything. Compare only previews.")
        self.summary.setWordWrap(True)
        bl.addWidget(self.summary)
        self.model = PlanModel(self._colors())
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        for c, wdt in ((0, 110), (1, 95), (3, 90), (4, 125), (5, 105), (6, 90), (7, 125), (8, 220)):
            hh.resizeSection(c, wdt)
        self.table.verticalHeader().setDefaultSectionSize(24)
        bl.addWidget(self.table, 1)
        st = QHBoxLayout()
        self.status = _muted("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.btn_progress = QPushButton("Show progress")
        self.btn_progress.setToolTip("Open the synchronization progress window")
        self.btn_progress.clicked.connect(self.show_run_progress)
        self.btn_progress.hide()
        st.addWidget(self.status, 1)
        st.addWidget(self.bar)
        st.addWidget(self.btn_progress)
        bl.addLayout(st)
        split.addWidget(bottom)
        split.setSizes([360, 420])

        self._fill_profiles()
        last = win.cfg["sync_last_profile"]
        if last and last in se.list_profiles():
            self.load(last)
        else:
            self._show_profile()
            self.add_task()                      # a usable first task, prefilled from the panes
            self.dirty = False
            self._update_title()

    # ================================================================ helpers
    def _colors(self) -> dict:
        from . import themes
        return themes.colors_for(self.win.cfg["theme"], self.win.themes)

    def current_task(self) -> se.Task | None:
        r = self.task_list.currentRow()
        return self.profile.tasks[r] if 0 <= r < len(self.profile.tasks) else None

    def _fill_profiles(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        names = se.list_profiles()
        if self.profile.name == UNSAVED or self.profile.name not in names:
            self.profile_box.addItem(self.profile.name if self.profile.name != UNSAVED else UNSAVED)
        for n in names:
            self.profile_box.addItem(n)
        self.profile_box.setCurrentText(self.profile.name)
        self.profile_box.blockSignals(False)
        self._update_schedule_label()

    def _show_profile(self) -> None:
        self._loading = True
        self.task_list.clear()
        for t in self.profile.tasks:
            it = QListWidgetItem(t.label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if t.enabled else Qt.Unchecked)
            self.task_list.addItem(it)
        self._loading = False
        if self.profile.tasks:
            self.task_list.setCurrentRow(0)
        self._task_selected(self.task_list.currentRow())
        self.model.set_plans([])
        self._refresh_buttons()
        self._update_title()

    def _update_title(self) -> None:
        star = " *" if self.dirty else ""
        self.setWindowTitle(f"Sync & Backup — {self.profile.name}{star} — {APP_NAME}")

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()
        self._refresh_buttons()
        if self.model.plans:
            self.model.set_plans([])
            self._refresh_buttons()
            self.summary.setText("Settings changed — press Compare to preview again (Synchronize also compares first).")

    # ================================================================ profiles
    def _confirm_discard(self) -> bool:
        if not self.dirty or not self.profile.tasks:
            return True
        r = QMessageBox.question(self, APP_NAME, f"Save changes to “{self.profile.name}”?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save()
        return True

    def _profile_selected(self, _i: int) -> None:
        name = self.profile_box.currentText()
        if name == self.profile.name:
            return
        if not self._confirm_discard():
            self.profile_box.setCurrentText(self.profile.name)
            return
        if name == UNSAVED:
            return
        self.load(name)

    def load(self, name: str) -> None:
        try:
            self.profile = se.load_profile(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't load profile: {e}")
            return
        self.dirty = False
        self.win.cfg["sync_last_profile"] = name
        self._fill_profiles()
        self._show_profile()

    def new_profile(self) -> None:
        if not self._confirm_discard():
            return
        self.profile = se.Profile(UNSAVED, [])
        self.dirty = False
        self._fill_profiles()
        self._show_profile()
        self.add_task()

    def _pane_pair(self) -> tuple[str, str]:
        """Source = active pane, Target = other pane — unless they're the same/nested folder."""
        src = self.win.active_view().path
        dst = self.win.other_pane().current().path if self.win.right.isVisible() else ""
        if not dst or se._inside(src, dst) or se._inside(dst, src):
            dst = ""
        return src, dst

    def start_with_panes(self, left: str, right: str) -> None:
        """FreeFileSync-style quick start: compare the two pane folders."""
        if not self._confirm_discard():
            return
        clash = (not right) or se._inside(left, right) or se._inside(right, left)
        self.profile = se.Profile(UNSAVED, [se.Task(name="Panes", mode="mirror", left=left,
                                                    right="" if clash else right)])
        self.dirty = True
        self._fill_profiles()
        self._show_profile()
        if clash:
            QMessageBox.information(
                self, APP_NAME,
                "Both panes show the same folder (or one is inside the other), so there's nothing to "
                "compare yet.\n\nChoose the Target folder below, or close this window, open the source "
                "folder in one pane and the backup folder in the other, and press Ctrl+Shift+S again.")
            self.f_right.edit.setFocus()

    def save(self) -> bool:
        if self.profile.name == UNSAVED:
            return self.save_as()
        try:
            se.save_profile(self.profile)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't save: {e}")
            return False
        self.dirty = False
        self.win.cfg["sync_last_profile"] = self.profile.name
        self.win.cfg.save()
        self._fill_profiles()
        self._update_title()
        return True

    def save_as(self) -> bool:
        while True:
            name, ok = QInputDialog.getText(self, "Save profile", "Profile name:",
                                            text="" if self.profile.name == UNSAVED else self.profile.name)
            if not ok:
                return False
            err = se.valid_profile_name(name)
            if err:
                QMessageBox.warning(self, APP_NAME, err)
                continue
            if name in se.list_profiles() and name != self.profile.name:
                if QMessageBox.question(self, APP_NAME, f"Replace profile “{name}”?") != QMessageBox.Yes:
                    continue
            self.profile.name = name
            return self.save()

    def delete_profile(self) -> None:
        name = self.profile.name
        if name == UNSAVED:
            return
        if QMessageBox.question(self, APP_NAME, f"Delete profile “{name}”? Its schedule is removed too; "
                                                "your files and backups are not touched.") != QMessageBox.Yes:
            return
        try:
            se.delete_profile(name)
        except OSError:
            pass
        if schedule.status(name)["installed"]:
            schedule.remove(name)
        self.profile = se.Profile(UNSAVED, [])
        self.dirty = False
        self._fill_profiles()
        self._show_profile()

    # ================================================================ tasks
    def add_task(self) -> None:
        n = len(self.profile.tasks) + 1
        left, right = self._pane_pair()
        t = se.Task(name=f"Task {n}", left=left, right=right)
        self.profile.tasks.append(t)
        it = QListWidgetItem(t.label)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        self.task_list.addItem(it)
        self.task_list.setCurrentRow(self.task_list.count() - 1)
        self._mark_dirty()

    def duplicate_task(self) -> None:
        t = self.current_task()
        if t is None:
            return
        c = copy.deepcopy(t)
        c.name = t.name + " copy"
        self.profile.tasks.append(c)
        self._show_profile()
        self.task_list.setCurrentRow(len(self.profile.tasks) - 1)
        self._mark_dirty()

    def remove_task(self) -> None:
        r = self.task_list.currentRow()
        if r < 0:
            return
        del self.profile.tasks[r]
        self._show_profile()
        self.task_list.setCurrentRow(min(r, len(self.profile.tasks) - 1))
        self._mark_dirty()

    def move_task(self, d: int) -> None:
        r = self.task_list.currentRow()
        j = r + d
        if r < 0 or not 0 <= j < len(self.profile.tasks):
            return
        ts = self.profile.tasks
        ts[r], ts[j] = ts[j], ts[r]
        self._show_profile()
        self.task_list.setCurrentRow(j)
        self._mark_dirty()

    def _task_checked(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        r = self.task_list.row(item)
        if 0 <= r < len(self.profile.tasks):
            on = item.checkState() == Qt.Checked
            if self.profile.tasks[r].enabled != on:
                self.profile.tasks[r].enabled = on
                self._mark_dirty()

    def _task_selected(self, _r: int) -> None:
        t = self.current_task()
        self.editor_stack.setCurrentWidget(self._editor_scroll if t is not None else self.empty_page)
        self._refresh_buttons()
        if t is None:
            return
        self._loading = True
        self.f_name.setText(t.name)
        self.f_mode.setCurrentIndex(se.MODES.index(t.mode))
        self.f_left.setText(t.left)
        self.f_right.setText(t.right)
        self.f_compare.setCurrentIndex(se.COMPARES.index(t.compare))
        self.f_tol.setValue(t.tolerance)
        self.f_ver.setCurrentIndex(se.VERSIONING.index(t.versioning))
        self.f_ver_dir.setText(t.versions_dir)
        self.f_ver_days.setValue(t.versions_keep_days)
        self.f_keep.setValue(t.snapshots_keep)
        self.f_empty.setChecked(t.allow_empty_source)
        self.f_shift.setChecked(t.ignore_hour_shift)
        self.f_inc.setPlainText("\n".join(t.include))
        self.f_exc.setPlainText("\n".join(t.exclude))
        self.f_pre.setText(t.pre_cmd)
        self.f_post.setText(t.post_cmd)
        self._loading = False
        self._update_visibility()

    def _update_visibility(self) -> None:
        t = self.current_task()
        if t is None:
            return
        mode = t.mode
        help_ = {
            "mirror": "Make the target an exact copy of the source. Extra files on the target are removed "
                      "(and versioned per “Old versions”).",
            "update": "Copy new and newer files to the target. Never deletes anything on the target.",
            "twoway": "Propagate creates, changes and deletions both ways. Files changed on both sides are "
                      "flagged as conflicts for you to decide. The first run only merges.",
            "snapshot": "Create a dated, browsable copy of the source inside the target on every run. Unchanged "
                        "files are hard links to the previous snapshot, so each snapshot only costs the space "
                        "of what changed.",
        }[mode]
        self.mode_help.setText(help_)
        two = mode == "twoway"
        self.lbl_left.setText("Left:" if two else "Source:")
        self.lbl_right.setText("Right:" if two else ("Backup folder:" if mode == "snapshot" else "Target:"))
        snap = mode == "snapshot"
        f = self._form
        f.setRowVisible(self.row_compare, not snap)
        f.setRowVisible(self.f_ver, not snap)
        f.setRowVisible(self.ver_row_w, not snap and t.versioning == "folder")
        f.setRowVisible(self.f_keep, snap)
        f.setRowVisible(self.f_empty, mode == "mirror")
        f.setRowVisible(self.f_shift, t.compare == "time_size")

    def _editor_changed(self, *_):
        if self._loading:
            return
        t = self.current_task()
        if t is None:
            return
        t.name = self.f_name.text().strip() or "Task"
        t.mode = self.f_mode.currentData()
        t.left = self.f_left.text()
        t.right = self.f_right.text()
        t.compare = self.f_compare.currentData()
        t.tolerance = self.f_tol.value()
        t.versioning = self.f_ver.currentData()
        t.versions_dir = self.f_ver_dir.text()
        t.versions_keep_days = self.f_ver_days.value()
        t.snapshots_keep = self.f_keep.value()
        t.allow_empty_source = self.f_empty.isChecked()
        t.ignore_hour_shift = self.f_shift.isChecked()
        t.include = [l.strip() for l in self.f_inc.toPlainText().splitlines() if l.strip()] or ["*"]
        t.exclude = [l.strip() for l in self.f_exc.toPlainText().splitlines() if l.strip()]
        t.pre_cmd = self.f_pre.text().strip()
        t.post_cmd = self.f_post.text().strip()
        it = self.task_list.currentItem()
        if it:
            self._loading = True
            it.setText(t.label)
            self._loading = False
        self._update_visibility()
        self._mark_dirty()

    # ================================================================ compare
    def compare(self) -> None:
        tasks = [t for t in self.profile.tasks if t.enabled]
        if not tasks:
            self._sync_after_compare = False
            QMessageBox.information(self, APP_NAME, "There's no task to run. Add a task (+) or tick an "
                                                    "existing one in the Tasks list.")
            return
        if not getattr(self, "_keep_result", False):
            self._result_html = ""
        self._keep_result = False
        self._cancel.clear()
        self._comparing = True
        self._refresh_buttons()
        self.status.setText("Scanning…")
        snapshot = [copy.deepcopy(t) for t in tasks]
        expected = [se.expected_items(t) for t in snapshot]
        self._scan_dlg = ScanProgressDialog(self, self.profile.name, len(snapshot))
        self._scan_dlg.cancelled.connect(self._cancel_compare)
        self._scan_dlg.show()

        def checkpoint():
            if self._cancel.is_set():
                raise se.Cancelled()

        def work():
            plans, err = [], ""
            try:
                for i, t in enumerate(snapshot):
                    self._sig.scan.emit({"task": t.name, "index": i, "n": 0, "phase": "left", "rel": "",
                                         "expected": expected[i]})
                    plans.append(se.compare(
                        t, checkpoint,
                        lambda n, phase, rel, t=t, i=i: self._sig.scan.emit(
                            {"task": t.name, "index": i, "n": n, "phase": phase, "rel": rel,
                             "expected": expected[i]})))
            except se.Cancelled:
                err = "Compare cancelled."
            except Exception as e:  # noqa: BLE001
                err = f"Compare failed: {e!r}"
            self._sig.done.emit(plans, err)

        threading.Thread(target=work, daemon=True).start()

    def _cancel_compare(self) -> None:
        self._cancel.set()

    def _compare_done(self, plans: list, err: str) -> None:
        self._comparing = False
        self._refresh_buttons()
        self.btn_cancel.hide()
        if getattr(self, "_scan_dlg", None) is not None:
            self._scan_dlg.finish()
            self._scan_dlg = None
        then_sync, self._sync_after_compare = self._sync_after_compare, False
        if not err:
            log = getattr(self, "_last_log", "")
            self.status.setText(f"Last run log: {log}" if self._result_html and log else "")
        if err:
            self.status.setText(err)
            self._refresh_buttons()
            return
        self.model.set_colors(self._colors())
        self.model.set_plans(plans)
        self._update_summary()
        self.table.resizeColumnToContents(1)
        blocked = [p for p in plans if p.errors and not p.left_scan]
        if blocked:
            QMessageBox.warning(self, APP_NAME, "Can't run:\n\n" + "\n".join(
                f"• {p.task.name}: {'; '.join(p.errors)}" for p in blocked))
            return
        if then_sync:
            self.synchronize()

    def _update_summary(self) -> None:
        plans = self.model.plans
        lines, can_run = [], False
        for p in plans:
            t = p.task
            if p.errors and not p.left_scan:
                lines.append(f"<b>{t.name}</b>: <span style='color:#d33'>{'; '.join(p.errors)}</span>")
                continue
            s = p.summary()
            n = sum(s[k] for k in ("copy_lr", "copy_rl", "del_l", "del_r"))
            can_run |= n > 0 or t.mode == "snapshot"
            parts = []
            if t.mode == "snapshot":
                links = sum(1 for i in p.items if i.action == se.LINK)
                parts.append(f"new snapshot: {s['copy_lr']} files copied ({fsutil.human_size(s['bytes'])}), "
                             f"{links} unchanged files hard-linked")
            else:
                if s["copy_lr"]:
                    parts.append(f"copy {s['copy_lr']} →")
                if s["copy_rl"]:
                    parts.append(f"copy {s['copy_rl']} ←")
                if s["del_l"] or s["del_r"]:
                    parts.append(f"{s['del_l'] + s['del_r']} to delete ({se.VERSIONING_LABELS[t.versioning].lower()})")
                parts.append(fsutil.human_size(s["bytes"]) + " to copy")
                if not n:
                    parts = ["already in sync"]
            if s["conflicts"]:
                parts.append(f"<span style='color:#d33'>{s['conflicts']} conflicts (right-click to resolve)</span>")
            if p.first_twoway:
                parts.append("first two-way run: merge only, no deletions")
            warn = "".join(f"<br><span style='color:#b8860b'>⚠ {w}</span>" for w in p.warnings + p.errors[:3])
            lines.append(f"<b>{t.name}</b>: " + ", ".join(parts) + warn)
        text = "<br>".join(lines) or "Nothing to compare."
        if self._result_html:
            text = self._result_html + "<br>" + text
        self.summary.setText(text)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        if not hasattr(self, "btn_sync"):
            return
        has_tasks = any(t.enabled for t in self.profile.tasks)
        busy = bool(self._jobs) or getattr(self, "_comparing", False)
        self.btn_compare.setEnabled(has_tasks and not busy)
        self.btn_sync.setEnabled(has_tasks and not busy)

    def _toggle_cat(self, cat: str, on: bool) -> None:
        cats = {cat} | ({se.TYPE_DIFF} if cat == se.DIFFERENT else set())
        for c in cats:
            (self.model.show.add if on else self.model.show.discard)(c)
        self.model.refilter()

    def _row_menu(self, pos) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows:
            idx = self.table.indexAt(pos)
            if not idx.isValid():
                return
            rows = [idx.row()]
        items = [self.model.item(r) for r in rows]
        m = QMenu(self)

        def setter(action):
            def apply():
                for plan, it in items:
                    if self._allowed(plan, it, action):
                        it.action = action
                self.model.changed(rows)
                self._update_summary()
            return apply

        for action, label in ((se.COPY_LR, "Copy left → right"), (se.COPY_RL, "Copy right → left"),
                              (se.DEL_L, "Delete on left"), (se.DEL_R, "Delete on right"),
                              (se.NONE, "Do nothing")):
            a = m.addAction(label, setter(action))
            a.setEnabled(any(self._allowed(p, i, action) for p, i in items))
        m.addAction("Reset to default", lambda: ([setattr(i, "action", i.default_action) for _, i in items],
                                                 self.model.changed(rows), self._update_summary()))
        m.addSeparator()
        plan, it = items[0]
        for side, root, info in (("left", plan.task.left, it.left), ("right", plan.task.right, it.right)):
            if info is not None:
                path = os.path.join(root, it.rel)
                m.addAction(f"Show on {side} in pane",
                            lambda p=path: self.win.active_view().navigate(p))
        m.exec(self.table.viewport().mapToGlobal(pos))

    @staticmethod
    def _allowed(plan: se.Plan, it: se.Item, action: str) -> bool:
        if plan.task.mode == "snapshot":
            return action in (se.NONE, it.default_action)
        if action == se.COPY_LR or action == se.DEL_L:
            return it.left is not None
        if action == se.COPY_RL or action == se.DEL_R:
            return it.right is not None
        return True

    # ================================================================ run
    def synchronize(self) -> None:
        if not self.model.plans:
            # No fresh preview (never compared, or settings changed): compare, then come back here.
            self._sync_after_compare = True
            self.compare()
            return
        plans = [p for p in self.model.plans if not (p.errors and not p.left_scan)]
        if not plans:
            return
        desc = []
        for p in plans:
            s = p.summary()
            if p.task.mode == "snapshot":
                desc.append(f"• {p.task.name}: new snapshot in {fsutil.display_path(p.task.right)}")
                continue
            n = sum(s[k] for k in ("copy_lr", "copy_rl", "del_l", "del_r"))
            if not n:
                continue
            d = f"• {p.task.name}: copy {s['copy_lr'] + s['copy_rl']} items ({fsutil.human_size(s['bytes'])})"
            if s["del_l"] + s["del_r"]:
                d += f", delete {s['del_l'] + s['del_r']} ({se.VERSIONING_LABELS[p.task.versioning].lower()})"
            desc.append(d)
        if not desc:
            QMessageBox.information(self, APP_NAME, "Nothing to do: the folders are already in sync.")
            return
        box = QMessageBox(QMessageBox.Question, APP_NAME, "Start synchronization?\n\n" + "\n".join(desc),
                          QMessageBox.Cancel, self)
        go = box.addButton("Synchronize", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is not go:
            return
        log = se.RunLog(self.profile.name if self.profile.name != UNSAVED else "unsaved")
        log.write(f"Profile “{self.profile.name}” started from GExplorer")
        self.bar.show()
        self.bar.setValue(0)
        runnable = [p for p in plans if p.task.mode == "snapshot" or p.actionable()]
        self._run_totals = {"copied": 0, "deleted": 0, "linked": 0, "errors": [], "warnings": [],
                            "cancelled": False, "log": log.path}
        for i, p in enumerate(runnable):
            # several tasks share one log file; the last job closes it
            job = self.win.jobs.submit_sync(p, log if i == len(runnable) - 1 else _SharedLog(log))
            self._jobs[job.id] = job
        self._refresh_buttons()
        self.status.setText(f"Running {len(self._jobs)} task(s)… (also visible in the operations queue)")
        if self._run_dlg is not None:
            self._run_dlg.deleteLater()
        self._run_dlg = SyncRunDialog(self, self.profile.name, list(self._jobs.values()), self.win.cfg, log.path,
                                      self.win.jobs)
        self._run_dlg.show()
        self.btn_progress.show()

    def show_run_progress(self) -> None:
        if self._run_dlg is not None:
            self._run_dlg.show()
            self._run_dlg.raise_()
            self._run_dlg.activateWindow()

    def _job_updated(self, job) -> None:
        if job.id in self._jobs:
            frac = self._run_dlg.fraction() if self._run_dlg is not None else job.fraction
            self.bar.setValue(int(frac * 1000))
            op = (job.sync or {}).get("op") or {}
            arrow = {"lr": "→ ", "rl": "← ", "delete-left": "✕ left: ", "delete-right": "✕ right: ",
                     "snapshot": "→ "}.get(op.get("dir", ""), "")
            self.status.setText(f"Synchronizing — {int(frac * 100)}%  {arrow}"
                                f"{fsutil.display_path(job.current)}")

    def _job_finished(self, job) -> None:
        if job.id not in self._jobs:
            return
        self._jobs.pop(job.id)
        tot = self._run_totals
        for k in ("copied", "deleted", "linked"):
            tot[k] += (job.stats or {}).get(k, 0)
        tot["errors"] += job.errors
        tot["warnings"] += job.warnings
        tot["cancelled"] |= job.state == "cancelled"
        if self._jobs:
            return
        self.bar.hide()
        self.btn_progress.hide()
        dlg = self._run_dlg
        shown = dlg is not None and dlg.isVisible()
        if dlg is not None:
            dlg.finish()
        counts = (f"{tot['copied']} copied, {tot['deleted']} deleted, {tot['linked']} linked, "
                  f"{len(tot['errors'])} errors, {len(tot['warnings'])} warnings")
        msg = counts + "."
        if tot["cancelled"]:
            msg = "Cancelled. Files already copied stay; nothing is left half-written. " + msg
        self._last_log = tot["log"]
        self.status.setText(f"Last run log: {tot['log']}")
        ok = not tot["errors"] and not tot["cancelled"]
        color = "#1d9e75" if ok else "#d33"
        head = "✓ Done" if ok else ("⚠ Cancelled" if tot["cancelled"] else "⚠ Finished with problems")
        self._result_html = f"<span style='color:{color}'><b>{head}:</b> {counts}.</span>"
        self.win.flash(("Sync finished: " if ok else "Sync finished with problems: ") + msg, 8000)
        if (tot["errors"] or tot["warnings"]) and not shown:     # the progress window shows them otherwise
            box = QMessageBox(QMessageBox.Warning if tot["errors"] else QMessageBox.Information, APP_NAME, msg,
                              QMessageBox.Ok, self)
            box.setDetailedText("\n".join(tot["errors"][:300] + tot["warnings"][:100]))
            box.exec()
        self._keep_result = True
        self.compare()       # show what's left (should only be conflicts / equal)

    # ================================================================ logs / schedule
    def open_logs(self) -> None:
        os.makedirs(se.logs_dir(), exist_ok=True)
        self.win.open_in_new_tab(se.logs_dir())

    def _update_schedule_label(self) -> None:
        if self.profile.name == UNSAVED:
            self.sched_lbl.setText("")
            return
        st = schedule.status(self.profile.name)
        self.sched_lbl.setText(f"Scheduled: {st['calendar']}" if st["installed"] else "Not scheduled")

    def schedule(self) -> None:
        if self.profile.name == UNSAVED or self.dirty:
            QMessageBox.information(self, APP_NAME, "Save the profile first; the schedule runs the saved version.")
            if not self.save():
                return
        ScheduleDialog(self, self.profile.name).exec()
        self._update_schedule_label()


class _SharedLog:
    """Lets several jobs write to one RunLog; only the last job closes it."""

    def __init__(self, log: se.RunLog):
        self._log, self.path = log, log.path

    def write(self, msg: str) -> None:
        self._log.write(msg)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- schedule dialog

class ScheduleDialog(QDialog):
    DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def __init__(self, parent, profile: str):
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"Schedule “{profile}” — {APP_NAME}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        if not schedule.available():
            lay.addWidget(QLabel("systemd isn't available, so GExplorer can't schedule runs here.\n"
                                 f"Use cron instead:  gexplorer --sync \"{profile}\" --quiet"))
            bb = QDialogButtonBox(QDialogButtonBox.Close)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)
            return
        st = schedule.status(profile)
        form = QFormLayout()
        self.freq = QComboBox()
        self.freq.addItems(list(schedule.PRESETS) + ["Custom (systemd OnCalendar)"])
        self.time = QTimeEdit(QTime(2, 30))
        self.time.setDisplayFormat("HH:mm")
        self.dow = QComboBox()
        self.dow.addItems(self.DAYS)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("e.g. Mon..Fri *-*-* 18:00:00")
        self.persist = QCheckBox("Run missed jobs at next login/boot")
        self.persist.setChecked(st.get("persistent", True))
        form.addRow("Frequency:", self.freq)
        form.addRow("Time:", self.time)
        form.addRow("Day:", self.dow)
        form.addRow("Expression:", self.custom)
        form.addRow("", self.persist)
        lay.addLayout(form)
        self.preview = _muted("")
        lay.addWidget(self.preview)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        linger = schedule.linger_enabled()
        self.linger_btn = QPushButton("Also run when I'm logged out (enable linger)")
        self.linger_btn.setVisible(linger is False)
        self.linger_btn.clicked.connect(self._linger)
        lay.addWidget(self.linger_btn)
        lay.addWidget(_muted("Runs headless: gexplorer --sync \"" + profile + "\" --quiet. "
                             "Results go to the sync logs; errors raise a desktop notification."))
        row = QHBoxLayout()
        self.b_install = QPushButton("Save schedule")
        self.b_install.setDefault(True)
        self.b_remove = QPushButton("Remove schedule")
        self.b_run = QPushButton("Run now in background")
        close = QPushButton("Close")
        for b in (self.b_install, self.b_remove, self.b_run):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.b_install.clicked.connect(self._install)
        self.b_remove.clicked.connect(self._remove)
        self.b_run.clicked.connect(self._run)
        close.clicked.connect(self.accept)
        for w in (self.freq, self.dow):
            w.currentIndexChanged.connect(self._refresh)
        self.time.timeChanged.connect(self._refresh)
        self.custom.textChanged.connect(self._refresh)
        if st["installed"] and st["calendar"]:
            self.freq.setCurrentIndex(self.freq.count() - 1)
            self.custom.setText(st["calendar"])
        else:
            self.freq.setCurrentText("Daily")
        self._refresh()
        self._show_state()

    def expression(self) -> str:
        f = self.freq.currentText()
        if f not in schedule.PRESETS:
            return self.custom.text().strip()
        t = self.time.time()
        return schedule.PRESETS[f].format(hh=f"{t.hour():02d}", mm=f"{t.minute():02d}", dow=self.dow.currentText())

    def _refresh(self, *_):
        f = self.freq.currentText()
        custom = f not in schedule.PRESETS
        self.time.setEnabled(not custom and f != "Hourly")
        self.dow.setEnabled(f == "Weekly")
        self.custom.setEnabled(custom)
        expr = self.expression()
        if not expr:
            self.preview.setText("")
            return
        ok, info = schedule.validate_calendar(expr)
        self.preview.setText(f"OnCalendar={expr}  ·  " + (f"next run: {info}" if ok else f"invalid: {info}"))
        self.b_install.setEnabled(ok)

    def _show_state(self) -> None:
        st = schedule.status(self.profile)
        if st["installed"]:
            self.state.setText(f"<b>Scheduled</b> ({st['calendar']}). State: {st['active'] or 'unknown'}. "
                               f"Next: {st['next'] or 'n/a'}. Last: {st['last'] or 'never'}.")
        else:
            self.state.setText("Not scheduled.")
        self.b_remove.setEnabled(st["installed"])
        self.b_run.setEnabled(st["installed"])

    def _install(self) -> None:
        ok, msg = schedule.install(self.profile, self.expression(), self.persist.isChecked())
        (QMessageBox.information if ok else QMessageBox.warning)(self, APP_NAME, msg)
        self._show_state()

    def _remove(self) -> None:
        ok, msg = schedule.remove(self.profile)
        self._show_state()

    def _run(self) -> None:
        ok, msg = schedule.run_now(self.profile)
        (QMessageBox.information if ok else QMessageBox.warning)(self, APP_NAME, msg)

    def _linger(self) -> None:
        ok, msg = schedule.enable_linger()
        (QMessageBox.information if ok else QMessageBox.warning)(self, APP_NAME, msg)
        self.linger_btn.setVisible(not ok)
