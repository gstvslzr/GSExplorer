"""Find Duplicates window (Tools > Find Duplicates, Ctrl+Shift+D).

Results use a model/view list (DupModel + a table view): rows are formatted only when drawn, so a result
with hundreds of thousands of files costs no per-row Qt objects. Anything that touches every result
(preparing, sorting, selection rules, updating after an action, export) runs in a background thread
for large results while a turning gear says what is happening — the window never freezes.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from bisect import bisect_right
from itertools import accumulate

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QTableView, QToolButton, QVBoxLayout,
                               QWidget)

from . import APP_NAME, dupes, fsutil, system
from .busygear import BusyGear
from .preview import PreviewPanel
from .ringprogress import RingProgress

PATH = Qt.UserRole + 2
GROUP = Qt.UserRole + 3
F_GROUP = Qt.ItemIsEnabled | Qt.ItemIsSelectable
F_FILE = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
COLS = ["Name", "Location", "Size", "Modified", "Copies", "Wasted"]
C_NAME, C_LOC, C_SIZE, C_DATE, C_COPIES, C_WASTED = range(6)
SORTS = [("Wasted space", C_WASTED, Qt.DescendingOrder), ("Size", C_SIZE, Qt.DescendingOrder),
         ("Copies", C_COPIES, Qt.DescendingOrder), ("Name", C_NAME, Qt.AscendingOrder),
         ("Location", C_LOC, Qt.AscendingOrder), ("Date modified", C_DATE, Qt.DescendingOrder)]
MODES = [("Identical content (recommended)", "content"), ("Same name and size", "name_size"),
         ("Same name only", "name")]
RULES = [("All but the newest in each group", "all_but_newest"),
         ("All but the oldest in each group", "all_but_oldest"),
         ("All but the first in each group (current sort)", "all_but_first"),
         ("All but the shortest path in each group", "all_but_shortest"),
         ("All copies inside a folder…", "in_folder")]
SYNC_LIMIT = 5000          # results with fewer files are processed directly (instant); more -> background + gear


_DEFAULT_SWITCH = sys.getswitchinterval()
_switch_users = 0


def responsive_gil(on: bool) -> None:
    """While background Python work runs, make it hand the GIL back every 0.5 ms (default 5 ms).

    Every Qt event that reaches Python code (painting the gear, event filters, the list model) must take
    the GIL; with the default interval a busy worker makes each of those wait up to 5 ms, so the gear and
    the ring stutter. Measured on a 120,000-file sort: 1 animation tick in 568 ms at 5 ms, 12 ticks
    (longest gap 60 ms) at 0.5 ms, same total time. Restored when no background work is left."""
    global _switch_users
    _switch_users = max(0, _switch_users + (1 if on else -1))
    sys.setswitchinterval(0.0005 if _switch_users else _DEFAULT_SWITCH)


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    return lbl


# =========================================================================== pure helpers (thread-safe)
def order_rows(groups: list[dupes.DupGroup], col: int, desc: bool) -> list[tuple]:
    """Display order: [(group, files-in-display-order)], new lists only (never mutates the groups)."""
    if col == C_NAME:
        fkey = lambda f: os.path.basename(f.path).lower()  # noqa: E731
    elif col == C_LOC:
        fkey = lambda f: os.path.dirname(f.path).lower()  # noqa: E731
    elif col == C_SIZE:
        fkey = lambda f: f.size  # noqa: E731
    elif col == C_DATE:
        fkey = lambda f: f.mtime  # noqa: E731
    else:
        fkey = lambda f: 0  # noqa: E731
    rows = [(g, sorted(g.files, key=lambda f: (fkey(f), f.path), reverse=desc)) for g in groups]

    def gkey(r):
        g, files = r
        first = g.files[0]
        if col == C_NAME:
            v = os.path.basename(first.path).lower()
        elif col == C_LOC:
            v = os.path.dirname(first.path).lower()
        elif col == C_SIZE:
            v = g.size
        elif col == C_DATE:
            v = max(f.mtime for f in files)
        elif col == C_COPIES:
            v = len(files)
        else:
            v = g.wasted
        return (v, first.path)

    rows.sort(key=gkey, reverse=desc)
    return rows


def ordered_groups_of(rows) -> list[dupes.DupGroup]:
    return [dupes.DupGroup(g.gid, g.size, g.key, files, g.mode) for g, files in rows]


def selection_bytes(selected: set[str], by_path: dict) -> int:
    seen, total = set(), 0
    for p in selected:
        gf = by_path.get(p)
        if gf is None:
            continue
        f = gf[1]
        k = (f.dev, f.ino)
        if k not in seen:
            seen.add(k)
            total += f.size
    return total


# =========================================================================== model
INDENT = 22                # file rows are drawn this much to the right of their group header


class DupModel(QAbstractItemModel):
    """Flat list model: each group is a header row followed by its file rows (unless collapsed).

    A flat list needs no tree expansion (QTreeView.expandAll walks every row through Python and took
    ~0.9 s for 40,000 groups). Row -> (group, file) is a binary search over the groups' start rows, so
    there are no per-row objects; text is produced in data() only for rows being painted. Check state
    is `owner.selected` (a set of paths).
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.rows: list[tuple] = []           # [(group, files in display order)]
        self.collapsed: set[int] = set()      # group ids shown without their files
        self._starts: list[int] = []          # first flat row of each group
        self._total = 0
        self._disp: dict[str, str] = {}
        self._bold = None

    def set_rows(self, rows: list[tuple]) -> None:
        self.beginResetModel()
        self.rows = rows
        self._disp = {}
        if self.collapsed:
            self.collapsed &= {g.gid for g, _f in rows}
        self._layout()
        self.endResetModel()

    def _layout(self) -> None:
        col = self.collapsed
        sizes = [1 if g.gid in col else 1 + len(files) for g, files in self.rows] if col else \
            [1 + len(files) for _g, files in self.rows]
        self._starts = list(accumulate(sizes, initial=0))[:-1] if sizes else []
        self._total = sum(sizes)

    def set_collapsed(self, gids: set[int]) -> None:
        self.beginResetModel()
        self.collapsed = set(gids)
        self._layout()
        self.endResetModel()

    # ---------------------------------------------------------------- flat row <-> (group, file)
    def locate(self, row: int) -> tuple[int, int]:
        """(group row, file position) for a flat row; file position -1 = the group header."""
        gi = bisect_right(self._starts, row) - 1
        return gi, row - self._starts[gi] - 1

    def group_index(self, gi: int, col: int = 0) -> QModelIndex:
        return self.createIndex(self._starts[gi], col, 0) if 0 <= gi < len(self.rows) else QModelIndex()

    def file_index(self, gi: int, k: int, col: int = 0) -> QModelIndex:
        if not (0 <= gi < len(self.rows)) or self.rows[gi][0].gid in self.collapsed \
                or not (0 <= k < len(self.rows[gi][1])):
            return QModelIndex()
        return self.createIndex(self._starts[gi] + 1 + k, col, 0)

    def is_group(self, index) -> bool:
        return index.isValid() and self.locate(index.row())[1] < 0

    def group_files(self, index) -> tuple:
        return self.rows[self.locate(index.row())[0]]

    def file_at(self, index):
        if not index.isValid():
            return None
        gi, k = self.locate(index.row())
        return None if k < 0 else self.rows[gi][1][k]

    def path_at(self, index) -> str:
        if not index.isValid():
            return ""
        gi, k = self.locate(index.row())
        return self.rows[gi][1][max(k, 0)].path

    # ---------------------------------------------------------------- structure (flat)
    def index(self, row, col, parent=QModelIndex()):
        if parent.isValid() or row < 0 or row >= self._total or col < 0 or col >= len(COLS):
            return QModelIndex()
        return self.createIndex(row, col, 0)

    def parent(self, index=QModelIndex()):
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self._total

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return F_GROUP if self.is_group(index) else F_FILE

    def _shown_folder(self, folder: str) -> str:
        s = self._disp.get(folder)
        if s is None:
            s = self._disp[folder] = fsutil.display_path(folder)
        return s

    # ---------------------------------------------------------------- data
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        gi, k = self.locate(index.row())
        g, files = self.rows[gi]
        if k < 0:                                         # group header
            if role == Qt.DisplayRole:
                if col == C_NAME:
                    first = os.path.basename(files[0].path)
                    same = all(os.path.basename(f.path) == first for f in files)
                    arrow = "▸ " if g.gid in self.collapsed else "▾ "
                    return arrow + (first if same else first + "  (+ other names)")
                if col == C_LOC:
                    return f"{len({os.path.dirname(f.path) for f in files})} folder(s)"
                if col == C_SIZE:
                    return fsutil.human_size(g.size)
                if col == C_DATE:
                    return _date(max(f.mtime for f in files))
                if col == C_COPIES:
                    return str(len(files))
                if col == C_WASTED:
                    return fsutil.human_size(g.wasted)
            elif role == Qt.FontRole and col == C_NAME:
                if self._bold is None:
                    self._bold = QFont(self.owner.tree.font())
                    self._bold.setBold(True)
                return self._bold
            elif role == Qt.ToolTipRole and col == C_NAME:
                tip = "Click ▾/▸ (or press ←/→) to collapse or expand this group"
                return tip + ("\nMatched by name only — contents were not compared" if g.mode != "content" else "")
            elif role == PATH:
                return files[0].path
            elif role == GROUP:
                return g.gid
            return None
        f = files[k]
        if role == Qt.DisplayRole:
            if col == C_NAME:
                return os.path.basename(f.path)
            if col == C_LOC:
                return self._shown_folder(os.path.dirname(f.path))
            if col == C_SIZE:
                return fsutil.human_size(f.size)
            if col == C_DATE:
                return _date(f.mtime)
            if col == C_COPIES:
                return f"+{len(f.links)} hard link(s)" if f.links else ("hard link" if f.nlink > 1 else "")
            return ""
        if role == Qt.CheckStateRole and col == C_NAME:
            return Qt.Checked if f.path in self.owner.selected else Qt.Unchecked
        if role == Qt.ToolTipRole:
            if col == C_LOC:
                return self._shown_folder(os.path.dirname(f.path))
            if col == C_SIZE:
                return f"{f.size:,} bytes"
            if col == C_COPIES and f.links:
                return "Same file on disk also at:\n" + "\n".join(map(fsutil.display_path, f.links))
            return None
        if role == PATH:
            return f.path
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or not index.isValid() or self.is_group(index):
            return False
        on = value == Qt.Checked or value == 2 or getattr(value, "value", None) == 2
        p = self.path_at(index)
        (self.owner.selected.add if on else self.owner.selected.discard)(p)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.owner._checks_changed()
        return True

    def sort(self, column, order=Qt.AscendingOrder):
        self.owner.request_sort(column, order)


class _IndentDelegate(QStyledItemDelegate):
    """Draws file rows' Name cell indented under their group header (the list itself is flat)."""

    def __init__(self, model: DupModel, parent=None):
        super().__init__(parent)
        self.m = model

    def _shift(self, option, index):
        if index.column() == C_NAME and not self.m.is_group(index):
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(INDENT, 0, 0, 0)
        return option

    def paint(self, painter, option, index):
        super().paint(painter, self._shift(option, index), index)

    def editorEvent(self, event, model, option, index):
        return super().editorEvent(event, model, self._shift(option, index), index)


class _View(QTableView):
    """The results list. A table view only lays out the rows on screen (a QTreeView lays out every row
    through the model first — ~1.2 s for 160,000 rows), so it stays instant at any size.
    Rows scroll into view vertically only: Qt's default also scrolls sideways to show a wide cell
    (e.g. a widened Location), which pushed the Name column and its checkboxes off-screen."""

    def header(self):                    # same name as QTreeView's, used throughout
        return self.horizontalHeader()

    def scrollTo(self, index, hint=QAbstractItemView.EnsureVisible):
        h = self.horizontalScrollBar().value()
        super().scrollTo(index, hint)
        self.horizontalScrollBar().setValue(h)


class _Worker(QThread):
    progress = Signal(dict)
    result = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancel = threading.Event()

    def start(self, *a):
        responsive_gil(True)
        self.finished.connect(lambda: responsive_gil(False))
        super().start(*a)

    def run(self):
        try:
            r = self.fn(self)
        except Exception as e:  # noqa: BLE001 — shown to the user, never swallowed
            r = e
        self.result.emit(r)


class ProgressDialog(QDialog):
    """Ring gauge popup. Scan: step 1 is busy (count unknown); step 2 fills 0–40 %, step 3 fills
    40–100 % by bytes read. Actions (trash/delete/move) fill 0–100 % by files done."""
    QUICK_SHARE = 0.40

    def __init__(self, parent, title: str, worker: _Worker):
        super().__init__(parent)
        self.setWindowTitle(f"{title} — {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.worker = worker
        self.t0 = time.monotonic()
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        self.title = QLabel(f"<b>{title}…</b>")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.ring = RingProgress()
        self.ring.setFixedSize(260, 260)
        self.ring.setBusy("0", "STARTING", "")
        lay.addWidget(self.ring, 0, Qt.AlignHCenter)
        self.detail = _muted()
        self.detail.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.detail)
        self.path = _muted()
        self.path.setAlignment(Qt.AlignCenter)
        self.path.setMinimumWidth(400)
        lay.addWidget(self.path)
        row = QHBoxLayout()
        self.elapsed = _muted()
        row.addWidget(self.elapsed, 1)
        b = QPushButton("Cancel")
        b.clicked.connect(self.reject)
        row.addWidget(b)
        lay.addLayout(row)
        worker.progress.connect(self.update_progress)

    def update_progress(self, d: dict) -> None:
        ph, done, total = d.get("phase", ""), d.get("done", 0), d.get("total", 0)
        frac = done / total if total else 0.0
        if ph == "walk":
            self.title.setText("<b>Step 1 of 3 — listing files</b>")
            self.ring.setBusy(f"{d.get('files', 0):,}", "SCANNING", "FILES FOUND")
            self.detail.setText("Counting files…")
        elif ph == "quick":
            self.title.setText("<b>Step 2 of 3 — comparing sizes and file starts/ends</b>")
            self.ring.setValue(frac * self.QUICK_SHARE)
            self.detail.setText(f"{done:,} of {total:,} same-size files checked")
        elif ph == "full":
            self.title.setText("<b>Step 3 of 3 — comparing full contents</b>")
            self.ring.setValue(self.QUICK_SHARE + frac * (1 - self.QUICK_SHARE))
            self.detail.setText(f"{fsutil.human_size(done)} of {fsutil.human_size(total)} read")
        elif ph == "act":
            self.ring.setValue(frac)
            self.detail.setText(f"{done:,} of {total:,} files")
        elif ph == "done":
            self.ring.setValue(1.0)
        self.path.setText(self.fontMetrics().elidedText(fsutil.display_path(d.get("path", "")), Qt.ElideMiddle, 400))
        self.elapsed.setText(f"Elapsed {int(time.monotonic() - self.t0)} s")

    def reject(self) -> None:          # Esc / Cancel / close
        self.detail.setText("Cancelling…")
        self.worker.cancel.set()


class DuplicatesWindow(QMainWindow):
    def __init__(self, win):
        super().__init__(None)
        self.win = win
        self.cfg = win.cfg
        self.setWindowTitle(f"Find Duplicates — {APP_NAME}")
        self.setWindowIcon(win.windowIcon())
        self.resize(1440, 880)
        self.groups: list[dupes.DupGroup] = []
        self.by_path: dict[str, tuple[dupes.DupGroup, dupes.DupFile]] = {}
        self.selected: set[str] = set()
        self._group_row: dict[int, int] = {}
        self._n_files = 0
        self._wasted = 0
        self._jobs: list[_Worker] = []                         # background tasks (kept alive while running)
        self._sel_ver = 0                                      # bumped on every check change
        self._sel_bytes = (-1, 0)                              # (version, bytes) of the last size calculation
        self._sel_job = None
        self._rows_order = None                                # (col, order) the shown rows are sorted by
        self._sort_running = None                              # (col, order) of a sort in progress
        self.mode = "content"
        self._worker = None
        self._filling = False

        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        # ------------------------------------------------------------ search options
        box = QGroupBox("Search")
        bl = QHBoxLayout(box)
        left = QVBoxLayout()
        left.addWidget(_muted("Folders to search (sub-folders included):"))
        self.folders = QListWidget()
        self.folders.setMaximumHeight(92)
        self.folders.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.folders.setTextElideMode(Qt.ElideMiddle)
        self.folders.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.addWidget(self.folders)
        fr = QHBoxLayout()
        for text, fn in (("Add folder…", self.add_folder), ("Add pane folders", self.add_pane_folders),
                         ("Remove", self.remove_folders)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            fr.addWidget(b)
        fr.addStretch(1)
        left.addLayout(fr)
        bl.addLayout(left, 3)

        form = QFormLayout()
        self.f_mode = QComboBox()
        for t, _ in MODES:
            self.f_mode.addItem(t)
        self.f_mode.setToolTip("Identical content compares the bytes (BLAKE2b-256 hash after size and quick checks).\n"
                               "Name modes do not look inside files: matches may have different content.")
        form.addRow("Match by:", self.f_mode)
        sz = QHBoxLayout()
        self.f_min = QSpinBox()
        self.f_min.setRange(0, 100_000_000)
        self.f_min.setSuffix(" KB")
        self.f_min.setToolTip("Ignore files smaller than this (0 = any size)")
        self.f_max = QSpinBox()
        self.f_max.setRange(0, 100_000_000)
        self.f_max.setSuffix(" MB")
        self.f_max.setSpecialValueText("no limit")
        sz.addWidget(QLabel("min"))
        sz.addWidget(self.f_min)
        sz.addWidget(QLabel("max"))
        sz.addWidget(self.f_max)
        form.addRow("File size:", sz)
        self.f_inc = QLineEdit()
        self.f_inc.setPlaceholderText("all files — or e.g. *.jpg;*.png;*.mp4")
        form.addRow("Only names:", self.f_inc)
        self.f_exc = QLineEdit()
        self.f_exc.setPlaceholderText("e.g. node_modules;*.tmp;Cache")
        form.addRow("Skip names:", self.f_exc)
        bl.addLayout(form, 3)

        right = QVBoxLayout()
        self.f_hidden = QCheckBox("Include hidden files and folders")
        self.f_empty = QCheckBox("Include empty files")
        self.f_links = QCheckBox("List hard links (they use no extra space)")
        self.f_trash = QCheckBox("Skip the trash")
        self.f_trash.setChecked(True)
        self.f_auto = QCheckBox("After searching, check all but the first copy")
        self.f_auto.setToolTip("Ticks every copy except the top one of each group (current sort order).\n"
                               "Nothing is removed until you press Move to Trash, Move to folder or Delete.")
        for w in (self.f_hidden, self.f_empty, self.f_links, self.f_trash, self.f_auto):
            right.addWidget(w)
        right.addStretch(1)
        self.b_scan = QPushButton("Find duplicates")
        self.b_scan.setDefault(True)
        f = self.b_scan.font()
        f.setBold(True)
        self.b_scan.setFont(f)
        self.b_scan.clicked.connect(self.scan)
        right.addWidget(self.b_scan)
        bl.addLayout(right, 2)
        root.addWidget(box)

        # ------------------------------------------------------------ results + compare
        split = QSplitter(Qt.Horizontal)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        tb = QHBoxLayout()
        self.b_select = QToolButton()
        self.b_select.setText("Select ▾")
        self.b_select.setPopupMode(QToolButton.InstantPopup)
        sm = QMenu(self.b_select)
        for t, rule in RULES:
            sm.addAction(t, lambda r=rule: self.apply_rule(r))
        sm.addSeparator()
        sm.addAction("Clear selection", self.clear_selection)
        self.b_select.setMenu(sm)
        self.b_all_but_first = QPushButton("☑ Check all but first")
        self.b_all_but_first.setToolTip("Tick every copy except the first one in each group\n"
                                        "(first = top of the group in the current sort order)")
        self.b_all_but_first.clicked.connect(lambda: self.apply_rule("all_but_first"))
        tb.addWidget(self.b_all_but_first)
        tb.addWidget(self.b_select)
        tb.addSpacing(12)
        tb.addWidget(QLabel("Sort by:"))
        self.sort = QComboBox()
        for t, _, _ in SORTS:
            self.sort.addItem(t)
        self.sort.currentIndexChanged.connect(self._sort_from_combo)
        tb.addWidget(self.sort)
        self.b_order = QToolButton()
        self.b_order.setToolTip("Reverse order")
        self.b_order.clicked.connect(self._flip_order)
        tb.addWidget(self.b_order)
        tb.addStretch(1)
        for t, fn in (("Expand all", lambda: self.collapse_all(False)), ("Collapse all", lambda: self.collapse_all(True))):
            b = QToolButton()
            b.setText(t)
            b.clicked.connect(fn)
            tb.addWidget(b)
        rl.addLayout(tb)

        self.model = DupModel(self)
        self.tree = _View()
        self.tree.setModel(self.model)
        self.tree.setItemDelegate(_IndentDelegate(self.model, self.tree))
        self.tree.setShowGrid(False)
        self.tree.setWordWrap(False)
        self.tree.setCornerButtonEnabled(False)
        vh = self.tree.verticalHeader()
        vh.hide()
        vh.setSectionResizeMode(QHeaderView.Fixed)             # uniform rows: no per-row measuring
        vh.setDefaultSectionSize(self.tree.fontMetrics().height() + 8)
        self.tree.setAlternatingRowColors(True)
        self.tree.setTextElideMode(Qt.ElideMiddle)           # keep both ends of long paths visible
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)   # Shift+click / Ctrl+click
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.selectionModel().currentChanged.connect(self._current_changed)
        self.tree.doubleClicked.connect(self.view_item)
        hv = self.tree.header()
        hv.setSectionsClickable(True)
        hv.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)   # titles left-aligned, like the main panes
        hv.setSectionResizeMode(QHeaderView.Interactive)      # every column can be dragged wider/narrower
        hv.setStretchLastSection(False)
        hv.setSectionsMovable(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._loc_sized = False                                # Location fills the free width once, then is yours
        self._fitting = False
        hv.sectionResized.connect(self._section_resized)
        self.tree.setColumnWidth(C_NAME, 220)
        self.tree.setColumnWidth(C_LOC, 240)
        for c, wdt in ((C_SIZE, 86), (C_DATE, 140), (C_COPIES, 62), (C_WASTED, 86)):
            self.tree.setColumnWidth(c, wdt)
        rl.addWidget(self.tree, 1)
        self.empty = _muted("Choose folders and press Find duplicates.")
        self.empty.setAlignment(Qt.AlignCenter)
        rl.addWidget(self.empty)
        split.addWidget(res)

        cmp_w = QWidget()
        cl = QVBoxLayout(cmp_w)
        cl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Compare</b>"))
        head.addStretch(1)
        head.addWidget(QLabel("with:"))
        self.cmp_with = QComboBox()
        self.cmp_with.setMinimumWidth(220)
        self.cmp_with.currentIndexChanged.connect(self._compare_changed)
        head.addWidget(self.cmp_with)
        cl.addLayout(head)
        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        self.verdict.setTextFormat(Qt.RichText)
        cl.addWidget(self.verdict)
        pv = QSplitter(Qt.Horizontal)
        self.prev_a = PreviewPanel(win, closable=False)
        self.prev_b = PreviewPanel(win, closable=False)
        for p in (self.prev_a, self.prev_b):
            p.setMinimumWidth(220)
            pv.addWidget(p)
        cl.addWidget(pv, 1)
        split.addWidget(cmp_w)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([820, 600])
        root.addWidget(split, 1)

        # ------------------------------------------------------------ actions
        bottom = QHBoxLayout()
        self.status = QLabel()
        bottom.addWidget(self.status, 1)
        self.b_export = QPushButton("Export list…")
        self.b_export.setToolTip("Save every group to a CSV file (for review or records)")
        self.b_export.clicked.connect(self.export)
        self.b_undo = QPushButton("Undo an audit move…")
        self.b_undo.setToolTip("Put files back from an audit folder, using its manifest.csv")
        self.b_undo.clicked.connect(self.undo_audit)
        self.b_move = QPushButton("Move to folder (audit)…")
        self.b_move.setToolTip("Move the selected copies into a dated audit folder, keeping their full original\n"
                               "paths, with a manifest.csv listing what moved, where from, and which copy was kept")
        self.b_move.clicked.connect(lambda: self.act("move"))
        self.b_trash = QPushButton("Move to Trash")
        self.b_trash.clicked.connect(lambda: self.act("trash"))
        self.b_delete = QPushButton("Delete permanently…")
        self.b_delete.clicked.connect(lambda: self.act("delete"))
        for b in (self.b_export, self.b_undo, self.b_move, self.b_trash, self.b_delete):
            bottom.addWidget(b)
        root.addLayout(bottom)
        self.tree.installEventFilter(self)          # F3 / Delete / Shift+Delete / Space on the list
        self.tree.viewport().installEventFilter(self)   # checkbox clicks on a multi-row selection
        QShortcut(QKeySequence("Esc"), self, self.close)
        QShortcut(QKeySequence("F5"), self, self.scan)
        self.b_scan.setToolTip("Find duplicates (Enter, F5)")
        for le in (self.f_inc, self.f_exc):
            le.returnPressed.connect(self.scan)

        self._load_options()
        self._sort_col, self._sort_order = C_WASTED, Qt.DescendingOrder
        hv.setSortIndicator(self._sort_col, self._sort_order)
        self.tree.setSortingEnabled(True)                      # header clicks -> DupModel.sort -> request_sort
        self.gear = BusyGear(central)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(60)
        self._status_timer.timeout.connect(self._refresh_state)
        self._update_order_button()
        self._refresh_state()

    # ================================================================== options
    def _load_options(self) -> None:
        c = self.cfg
        self.f_mode.setCurrentIndex(next((i for i, (_, m) in enumerate(MODES) if m == c["dupes_mode"]), 0))
        self.f_min.setValue(int(c["dupes_min_kb"] or 0))
        self.f_max.setValue(int(c["dupes_max_mb"] or 0))
        self.f_inc.setText(c["dupes_include"] or "")
        self.f_exc.setText(c["dupes_exclude"] or "")
        self.f_hidden.setChecked(bool(c["dupes_hidden"]))
        self.f_empty.setChecked(bool(c["dupes_empty"]))
        self.f_links.setChecked(bool(c["dupes_hardlinks"]))
        self.f_trash.setChecked(c["dupes_skip_trash"] is not False)
        self.f_auto.setChecked(c["dupes_autocheck"] is not False)

    def _save_options(self) -> None:
        c = self.cfg
        c["dupes_mode"] = MODES[self.f_mode.currentIndex()][1]
        c["dupes_min_kb"] = self.f_min.value()
        c["dupes_max_mb"] = self.f_max.value()
        c["dupes_include"] = self.f_inc.text().strip()
        c["dupes_exclude"] = self.f_exc.text().strip()
        c["dupes_hidden"] = self.f_hidden.isChecked()
        c["dupes_empty"] = self.f_empty.isChecked()
        c["dupes_hardlinks"] = self.f_links.isChecked()
        c["dupes_skip_trash"] = self.f_trash.isChecked()
        c["dupes_autocheck"] = self.f_auto.isChecked()
        try:
            c.save()
        except OSError:
            pass

    def set_folders(self, folders: list[str]) -> None:
        if [os.path.abspath(f) for f in folders] != self.folder_list() and self.groups:
            self.selected.clear()
            self.set_results([])                             # results were for other folders
            self.status.setToolTip("")
            if hasattr(self, "_last_scan"):
                del self._last_scan
            self._refresh_state()
        self.folders.clear()
        for f in folders:
            self._add(f)

    def _add(self, folder: str) -> None:
        folder = os.path.abspath(folder)
        if folder and os.path.isdir(folder) and folder not in self.folder_list():
            self.folders.addItem(fsutil.display_path(folder))
            it = self.folders.item(self.folders.count() - 1)
            it.setData(PATH, folder)
            it.setToolTip(fsutil.display_path(folder))

    def folder_list(self) -> list[str]:
        return [self.folders.item(i).data(PATH) for i in range(self.folders.count())]

    def add_folder(self) -> None:
        start = (self.folder_list() or [os.path.expanduser("~")])[-1]
        d = QFileDialog.getExistingDirectory(self, "Add a folder to search", start)
        if d:
            self._add(d)

    def add_pane_folders(self) -> None:
        for p in (self.win.left, self.win.right):
            try:
                v = p.current()
                if v and v.path and not v.is_trash():
                    self._add(v.path)
            except (AttributeError, RuntimeError):
                pass

    def remove_folders(self) -> None:
        for it in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(it))

    def options(self) -> dupes.Options:
        return dupes.Options(
            roots=self.folder_list(), mode=MODES[self.f_mode.currentIndex()][1],
            min_size=max(0 if self.f_empty.isChecked() else 1, self.f_min.value() * 1024),
            max_size=self.f_max.value() * 1024 * 1024, include=self.f_inc.text(), exclude=self.f_exc.text(),
            hidden=self.f_hidden.isChecked(), hardlinks=self.f_links.isChecked(), skip_trash=self.f_trash.isChecked())

    # ================================================================== background work
    def _run(self, text: str, fn, done, heavy: bool, detail: str = "") -> None:
        """Run fn() and pass its result to done(). Heavy work goes to a thread while the gear turns."""
        if not heavy:
            done(fn())
            return
        self._set_busy(True, text, detail)
        w = _Worker(lambda _wk: fn())
        self._jobs.append(w)

        def finished(r, w=w):
            if w in self._jobs:
                self._jobs.remove(w)
            self._set_busy(False)
            if isinstance(r, Exception):
                QMessageBox.critical(self, APP_NAME, f"{text.rstrip('…')} failed:\n{r}")
                return
            done(r)

        w.result.connect(finished)
        w.start()

    def busy(self) -> bool:
        return self.gear.busy()

    def _set_busy(self, on: bool, text: str = "", detail: str = "") -> None:
        if on:
            self.gear.start(text, detail)
        else:
            self.gear.stop()
        for b in (self.b_scan, self.b_select, self.b_all_but_first, self.sort, self.b_order, self.b_export,
                  self.b_undo, self.b_move, self.b_trash, self.b_delete):
            b.setEnabled(not on)
        if not on:
            self._refresh_state()

    def _heavy(self, n: int | None = None) -> bool:
        return (self._n_files if n is None else n) >= SYNC_LIMIT

    # ================================================================== scan
    def scan(self) -> None:
        if not self.folder_list():
            QMessageBox.information(self, APP_NAME, "Add at least one folder to search.")
            return
        if self.busy():
            return
        self._save_options()
        opts = self.options()
        w = _Worker(lambda wk: dupes.scan(opts, wk.progress.emit, wk.cancel))
        dlg = ProgressDialog(self, "Finding duplicates", w)
        w.result.connect(lambda r: self._scan_done(r, opts, dlg))
        self._worker = w
        w.start()
        dlg.show()

    def _scan_done(self, r, opts: dupes.Options, dlg: ProgressDialog) -> None:
        dlg.accept()
        dlg.deleteLater()
        self._worker = None
        if isinstance(r, Exception):
            QMessageBox.critical(self, APP_NAME, f"The search failed:\n{r}")
            return
        self.mode = opts.mode
        msg = (f"Searched {r.files_scanned:,} files in {r.seconds:.1f} s"
               + (f"; read {fsutil.human_size(r.bytes_hashed)} to compare contents" if r.bytes_hashed else ""))
        if r.cancelled:
            msg = "Search cancelled — results are incomplete. " + msg
        self._last_scan = msg
        if r.errors:
            self._last_scan += f"; {len(r.errors)} unreadable"
            self.status.setToolTip("\n".join(r.errors[:50]))
        self.set_results(r.groups, autocheck=self.f_auto.isChecked(), clear_selection=True)

    def set_results(self, groups: list[dupes.DupGroup], autocheck: bool = False, clear_selection: bool = False,
                    after=None) -> None:
        """Show *groups*: order them, index them, optionally tick all but the first — in the background
        (with the gear) when the result is large."""
        col, desc = self._sort_col, self._sort_order == Qt.DescendingOrder
        keep = set() if clear_selection else set(self.selected)
        n = sum(len(g.files) for g in groups)

        def prepare():
            rows = order_rows(groups, col, desc)
            by_path = {f.path: (g, f) for g in groups for f in g.files}
            sel = {f.path for _g, files in rows for f in files[1:]} if autocheck else (keep & by_path.keys())
            stats = (n, sum(g.wasted for g in groups))
            return rows, by_path, sel, stats

        def done(r):
            rows, by_path, sel, stats = r
            self._rows_order = (col, Qt.DescendingOrder if desc else Qt.AscendingOrder)
            self.groups = groups
            self.by_path = by_path
            self.selected = sel
            self._n_files, self._wasted = stats
            self._apply_rows(rows)
            if after:
                after()

        self._run(f"Preparing {n:,} results…", prepare, done, self._heavy(n),
                  "Sorting, indexing" + (" and checking all but the first copy" if autocheck else ""))

    def _apply_rows(self, rows: list[tuple]) -> None:
        self._sel_ver += 1
        cur = self.current_path()
        self.model.set_rows(rows)
        self._group_row = {g.gid: i for i, (g, _f) in enumerate(rows)}
        self._fit_location()
        if cur and cur in self.by_path:
            self.select_path(cur)
        elif rows:
            self.tree.setCurrentIndex(self.model.file_index(0, 0))
        else:
            self._show_compare(QModelIndex())
        self._refresh_state()

    # ================================================================== sorting
    def request_sort(self, col: int, order) -> None:
        """Header click / Sort-by box / ↓↑: reorder in the background for large results."""
        self._sort_col, self._sort_order = col, order
        i = next((k for k, (_, c, _) in enumerate(SORTS) if c == col), -1)
        if i >= 0 and i != self.sort.currentIndex():
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(i)
            self.sort.blockSignals(False)
        self._update_order_button()
        hv = self.tree.header()
        if hv.sortIndicatorSection() != col or hv.sortIndicatorOrder() != order:
            hv.blockSignals(True)
            hv.setSortIndicator(col, order)
            hv.blockSignals(False)
        key = (col, order)
        if not self.groups or key == self._rows_order or key == self._sort_running:
            return                     # already shown this way / already being sorted (Qt asks twice per click)
        groups, desc = self.groups, order == Qt.DescendingOrder
        self._sort_running = key

        def done(rows):
            self._sort_running = None
            self._rows_order = key
            self._apply_rows(rows)

        self._run(f"Sorting {self._n_files:,} files…", lambda: order_rows(groups, col, desc), done, self._heavy())

    def _sort_from_combo(self, i: int) -> None:
        _, col, order = SORTS[i]
        self.request_sort(col, order)

    def _flip_order(self) -> None:
        o = Qt.AscendingOrder if self._sort_order == Qt.DescendingOrder else Qt.DescendingOrder
        self.request_sort(self._sort_col, o)

    def _update_order_button(self) -> None:
        self.b_order.setText("↓" if self._sort_order == Qt.DescendingOrder else "↑")

    def ordered_groups(self) -> list[dupes.DupGroup]:
        """Groups and their files in the order shown (used by 'All but the first')."""
        return ordered_groups_of(self.model.rows)

    # ================================================================== selection (checkboxes)
    def _checks_changed(self) -> None:
        self._sel_ver += 1
        self._status_timer.start()                            # coalesce many toggles into one status update
        if self.tree.currentIndex().isValid():
            self._compare_changed()

    def selected_file_items(self) -> list:
        """Selected (highlighted) file rows, as column-0 indexes."""
        return [i for i in self.tree.selectionModel().selectedRows(0) if not self.model.is_group(i)]

    def check_rows(self, indexes, state=None) -> None:
        """Tick/untick several rows at once. state None = toggle: tick all unless all are ticked."""
        paths = [self.model.path_at(i) for i in indexes if i.isValid() and not self.model.is_group(i)]
        if not paths:
            return
        if state is None:
            state = not all(p in self.selected for p in paths)
        (self.selected.update if state else self.selected.difference_update)(paths)
        self._set_checks()

    def _set_checks(self) -> None:
        self._sel_ver += 1
        self.tree.viewport().update()                         # check states are read live from self.selected
        self._show_compare(self.tree.currentIndex())
        self._refresh_state()

    def apply_rule(self, rule: str) -> None:
        folder = ""
        if rule == "in_folder":
            cp = self.current_path()
            start = os.path.dirname(cp) if cp else ""
            folder = QFileDialog.getExistingDirectory(self, "Select the copies inside this folder", start)
            if not folder:
                return
        rows = self.model.rows

        def done(sel):
            self.selected = sel
            self._set_checks()

        self._run("Checking copies…", lambda: dupes.select_rule(ordered_groups_of(rows), rule, folder), done,
                  self._heavy())

    def clear_selection(self) -> None:
        self.selected = set()
        self._set_checks()

    def index_of(self, path: str) -> QModelIndex:
        gf = self.by_path.get(path)
        if gf is None:
            return QModelIndex()
        r = self._group_row.get(gf[0].gid)
        if r is None:
            return QModelIndex()
        files = self.model.rows[r][1]
        for k, f in enumerate(files):
            if f.path == path:
                if gf[0].gid in self.model.collapsed:               # reveal it
                    self.model.set_collapsed(self.model.collapsed - {gf[0].gid})
                return self.model.file_index(r, k)
        return QModelIndex()

    def select_path(self, path: str) -> None:
        idx = self.index_of(path)
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)

    def current_path(self) -> str:
        idx = self.tree.currentIndex()
        return self.model.path_at(idx) if idx.isValid() and not self.model.is_group(idx) else ""

    # ================================================================== compare
    def _current_changed(self, cur, _prev) -> None:
        self._show_compare(cur)

    def _show_compare(self, idx) -> None:
        self.cmp_with.blockSignals(True)
        self.cmp_with.clear()
        if idx is None or not idx.isValid() or not self.model.rows:
            self.cmp_with.blockSignals(False)
            self._a = ""
            self.prev_a.show_entry(None)
            self.prev_b.show_entry(None)
            self.verdict.setText("")
            return
        _g, files = self.model.group_files(idx)
        a = self.model.path_at(idx)
        others = [f.path for f in files if f.path != a]
        others.sort(key=lambda p: p in self.selected)          # prefer a copy that will be kept
        for p in others:
            self.cmp_with.addItem(f"{os.path.basename(p)}  —  {fsutil.display_path(os.path.dirname(p))}", p)
        self.cmp_with.blockSignals(False)
        self._a = a
        self.prev_a.show_entry(a)
        self._compare_changed()

    def _compare_changed(self, *_):
        b = self.cmp_with.currentData()
        a = getattr(self, "_a", "")
        self.prev_b.show_entry(b)
        if not a or not b or a not in self.by_path or b not in self.by_path:
            self.verdict.setText("")
            return
        g, fa = self.by_path[a]
        fb = self.by_path[b][1]
        na, nb = os.path.basename(fa.path), os.path.basename(fb.path)
        lines = []
        if g.mode == "content":
            lines.append(f"<b style='color:#3fa36b'>✔ Identical content</b> — {fsutil.human_size(g.size)}, "
                         f"BLAKE2b-256 {g.key[:16]}…")
        else:
            same = fa.size == fb.size
            lines.append("<b style='color:#c9852b'>⚠ Matched by name only — contents not compared</b>"
                         + ("" if same else f" (sizes differ: {fsutil.human_size(fa.size)} vs {fsutil.human_size(fb.size)})"))
        if (fa.dev, fa.ino) == (fb.dev, fb.ino):
            lines.append("Same file on disk (hard link): removing one frees no space.")
        lines.append("Name: " + ("same" if na == nb else f"<b>{na}</b> vs <b>{nb}</b>"))
        if abs(fa.mtime - fb.mtime) < 1:
            lines.append("Modified: same time")
        else:
            newer = "left" if fa.mtime > fb.mtime else "right"
            lines.append(f"Modified: {_date(fa.mtime)} vs {_date(fb.mtime)} — <b>{newer} is newer</b>")
        sel = [t for t, p in (("left", a), ("right", b)) if p in self.selected]
        if sel:
            lines.append("Selected for removal: " + " and ".join(sel))
        self.verdict.setText("<br>".join(lines))

    # ================================================================== status
    def _refresh_state(self) -> None:
        has = bool(self.groups)
        self.tree.setVisible(has)
        if has and not self._loc_sized:
            QTimer.singleShot(0, self._fit_location)          # after the list is laid out
        self.empty.setVisible(not has)
        if not has:
            self.empty.setText(getattr(self, "_last_scan", "") + ("\n\nNo duplicates found." if hasattr(self, "_last_scan")
                                                                   else "Choose folders and press Find duplicates."))
        txt = (f"{len(self.groups):,} groups · {self._n_files:,} files · {fsutil.human_size(self._wasted)} reclaimable"
               if has else "")
        if self.selected:
            txt += f"  ·  <b>{len(self.selected):,} selected ({self._selected_size_text()})</b>"
        if has and hasattr(self, "_last_scan"):
            txt += f"<br><span style='opacity:.7'>{self._last_scan}</span>"
        self.status.setText(txt)
        if self.busy():
            return
        for b in (self.b_move, self.b_trash, self.b_delete):
            b.setEnabled(bool(self.selected))
        self.b_export.setEnabled(has)
        self.b_select.setEnabled(has)
        self.b_all_but_first.setEnabled(has)

    def _selected_size_text(self) -> str:
        """Size of the checked files. Small selections: computed now. Large ones: in a thread (the
        status line says 'calculating…' for a moment) so ticking boxes never stalls the window."""
        ver = self._sel_ver
        if self._sel_bytes[0] == ver:
            return fsutil.human_size(self._sel_bytes[1])
        if len(self.selected) < 20000:
            self._sel_bytes = (ver, selection_bytes(self.selected, self.by_path))
            return fsutil.human_size(self._sel_bytes[1])
        if self._sel_job is None:
            sel, bp = set(self.selected), self.by_path
            w = _Worker(lambda _wk: (ver, selection_bytes(sel, bp)))

            def got(r):
                self._sel_job = None
                if not isinstance(r, Exception):
                    self._sel_bytes = r
                self._refresh_state()                      # shows the size, or starts a newer calculation

            w.result.connect(got)
            self._sel_job = w
            self._jobs.append(w)
            w.finished.connect(lambda w=w: w in self._jobs and self._jobs.remove(w))
            w.start()
        return "calculating…"

    # ================================================================== actions
    def act(self, action: str) -> None:
        if not self.selected or self.busy():
            return
        full = dupes.groups_fully_selected(self.groups, self.selected)
        if full:
            box = QMessageBox(QMessageBox.Warning, APP_NAME,
                              f"In {len(full)} group(s) every copy is selected.\n\n"
                              "GExplorer always keeps at least one copy. Keep the newest copy in those groups "
                              "and continue?", QMessageBox.Cancel, self)
            ok = box.addButton("Keep newest and continue", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is not ok:
                return
            for g in full:
                self.selected.discard(max(g.files, key=lambda f: f.mtime).path)
            self._set_checks()
        n = len(self.selected)
        what = f"{n:,} selected file{'s' if n != 1 else ''}"
        warn = ("\n\nThese were matched by NAME only — their contents were not compared and may differ."
                if self.mode != "content" else "")
        audit_root = ""
        if action == "delete":
            box = QMessageBox(QMessageBox.Warning, APP_NAME,
                              f"Permanently delete {what}?\n\nThis can't be undone.{warn}", QMessageBox.Cancel, self)
            btn = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
            box.setDefaultButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is not btn:
                return
        elif action == "trash":
            size = fsutil.human_size(selection_bytes(self.selected, self.by_path))
            if QMessageBox.question(self, APP_NAME, f"Move {what} ({size}) to the trash?{warn}") != QMessageBox.Yes:
                return
        elif action == "move":
            start = self.cfg["dupes_audit_dir"] or os.path.expanduser("~")
            audit_root = QFileDialog.getExistingDirectory(self, "Move selected duplicates into… (an audit folder)", start)
            if not audit_root:
                return
            self.cfg["dupes_audit_dir"] = audit_root
            self._save_options()
        groups, selected = self.groups, set(self.selected)

        def run(wk):
            return dupes.apply(groups, selected, action, audit_root,
                               lambda i, t, p: wk.progress.emit({"phase": "act", "done": i, "total": t, "path": p}),
                               wk.cancel)

        title = {"trash": "Moving to Trash", "delete": "Deleting", "move": "Moving to audit folder"}[action]
        w = _Worker(run)
        dlg = ProgressDialog(self, title, w)
        w.result.connect(lambda r: self._act_done(r, action, dlg))
        self._worker = w
        w.start()
        dlg.show()

    def _act_done(self, r, action: str, dlg) -> None:
        dlg.accept()
        dlg.deleteLater()
        self._worker = None
        if isinstance(r, Exception):
            QMessageBox.critical(self, APP_NAME, f"Stopped: {r}")
            return
        gone = set(r.done)
        freed = sum(self.by_path[p][1].size for p in gone if p in self.by_path)
        old, links = self.groups, self.f_links.isChecked()

        def remaining():                                      # new objects only: the list keeps drawing the old ones
            out = []
            for g in old:
                files = [f for f in g.files if f.path not in gone]
                if len(files) > 1 and (g.mode != "content" or links or len({(f.dev, f.ino) for f in files}) > 1):
                    out.append(dupes.DupGroup(g.gid, g.size, g.key, files, g.mode))
            return out

        def show_summary():
            verb = {"trash": "Moved to Trash", "delete": "Deleted", "move": "Moved to the audit folder"}[action]
            msg = f"{verb}: {len(gone):,} file(s), {fsutil.human_size(freed)}."
            if r.skipped or r.errors:
                msg += f"\n\n{len(r.skipped)} skipped, {len(r.errors)} failed (see details)."
            box = QMessageBox(QMessageBox.Information if not r.errors else QMessageBox.Warning, APP_NAME, msg,
                              QMessageBox.Ok, self)
            if r.skipped or r.errors:
                box.setDetailedText("\n".join(r.errors + r.skipped))
            open_btn = None
            if action == "move" and r.audit_dir:
                box.setInformativeText(f"Audit folder: {fsutil.display_path(r.audit_dir)}\n"
                                       f"Manifest: {dupes.MANIFEST} (original path, new path, hash, kept copy)")
                open_btn = box.addButton("Show audit folder", QMessageBox.ActionRole)
            self._last_box = box
            box.exec()
            if open_btn is not None and box.clickedButton() is open_btn:
                self.win.active_view().navigate(r.audit_dir)
                self.win.raise_()
                self.win.activateWindow()
            for p in (self.win.left, self.win.right):
                try:
                    p.current().refresh()
                except (AttributeError, RuntimeError):
                    pass

        self.selected -= gone
        self._run("Updating the list…", remaining, lambda gs: self.set_results(gs, after=show_summary),
                  self._heavy())

    def export(self) -> None:
        if self.busy():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export duplicate list",
                                              os.path.join(os.path.expanduser("~"),
                                                           f"duplicates-{time.strftime('%Y%m%d-%H%M')}.csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        groups, sel = self.ordered_groups(), set(self.selected)

        def write():
            try:
                dupes.export_csv(groups, path, sel)
                return None
            except OSError as e:
                return e

        def done(err):
            if err is not None:
                QMessageBox.warning(self, APP_NAME, f"Couldn't save: {err.strerror or err}")

        self._run(f"Exporting {self._n_files:,} files…", write, done, self._heavy())

    def undo_audit(self) -> None:
        start = self.cfg["dupes_audit_dir"] or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Choose the audit folder's manifest.csv", start,
                                              "Audit manifest (manifest.csv)")
        if not path:
            return
        try:
            r = dupes.undo_audit(path)
        except (OSError, KeyError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, f"Not a GExplorer audit manifest: {e}")
            return
        box = QMessageBox(QMessageBox.Information, APP_NAME,
                          f"Put back {len(r.done):,} file(s) to their original folders."
                          + (f"\n{len(r.skipped)} skipped, {len(r.errors)} failed." if r.skipped or r.errors else ""),
                          QMessageBox.Ok, self)
        if r.skipped or r.errors:
            box.setDetailedText("\n".join(r.errors + r.skipped))
        box.exec()

    # ================================================================== context menu
    def view_item(self, idx) -> None:
        if idx is None or not idx.isValid():
            return
        _g, files = self.model.group_files(idx)
        paths = [f.path for f in files]
        start = self.model.path_at(idx)
        from .dialogs import ViewerDialog
        ViewerDialog(self, start, paths, on_change=self.select_path).exec()

    def play_media(self, path: str, **kw) -> None:      # used by the F3 viewer's Play button
        self.win.play_media(path, **kw)

    def show_in_pane(self, path: str) -> None:
        self.win.active_view().navigate(os.path.dirname(path), select=path)
        self.win.raise_()
        self.win.activateWindow()

    def _context_menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid() or self.busy():
            return
        idx = idx.siblingAtColumn(0)
        m = QMenu(self)
        rows = self.selected_file_items()
        if len(rows) > 1 and idx in rows:
            m.addAction(f"Check the {len(rows)} selected rows", lambda: self.check_rows(rows, True))
            m.addAction(f"Uncheck the {len(rows)} selected rows", lambda: self.check_rows(rows, False))
            m.addSeparator()
        if not self.model.is_group(idx):
            p = self.model.path_at(idx)
            checked = p in self.selected
            m.addAction("Unselect" if checked else "Select for removal",
                        lambda: self.check_rows([idx], not checked))
            m.addAction("Keep only this copy (select the others)", lambda: self._keep_only(idx))
            m.addSeparator()
            m.addAction("Open", lambda: system.open_default(p))
            m.addAction("View the copies (F3)", lambda: self.view_item(idx))
            m.addAction("Show in GExplorer", lambda: self.show_in_pane(p))
            m.addAction("Copy path", lambda: self._copy(p))
        else:
            m.addAction("View the copies (F3)", lambda: self.view_item(idx))
            gi = self.model.locate(idx.row())[0]
            collapsed = self.model.rows[gi][0].gid in self.model.collapsed
            m.addAction("Expand group" if collapsed else "Collapse group", lambda: self.toggle_group(gi))
            m.addAction("Keep only the first copy", lambda: self._keep_first(gi))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _keep_first(self, gi: int) -> None:
        files = self.model.rows[gi][1]
        self.selected.difference_update([files[0].path])
        self.selected.update(f.path for f in files[1:])
        self._set_checks()

    def toggle_group(self, gi: int) -> None:
        gid = self.model.rows[gi][0].gid
        self.model.set_collapsed(self.model.collapsed ^ {gid})
        self.tree.setCurrentIndex(self.model.group_index(gi))

    def collapse_all(self, on: bool) -> None:
        cur = self.current_path()
        self.model.set_collapsed({g.gid for g, _f in self.model.rows} if on else set())
        if not on and cur:
            self.select_path(cur)

    def _keep_only(self, idx) -> None:
        _g, files = self.model.group_files(idx)
        keep = self.model.path_at(idx)
        for f in files:
            (self.selected.discard if f.path == keep else self.selected.add)(f.path)
        self._set_checks()

    @staticmethod
    def _copy(p: str) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(fsutil.display_path(p))

    def eventFilter(self, obj, ev):
        if self.busy():
            return super().eventFilter(obj, ev)
        if obj is self.tree and ev.type() == ev.Type.KeyPress:
            k, mods = ev.key(), ev.modifiers()
            if k == Qt.Key_F3:
                self.view_item(self.tree.currentIndex())
                return True
            if k == Qt.Key_Delete:
                self.act("delete" if mods & Qt.ShiftModifier else "trash")
                return True
            cur = self.tree.currentIndex()
            if k in (Qt.Key_Left, Qt.Key_Right) and cur.isValid() and not mods:
                gi, fk = self.model.locate(cur.row())
                gid = self.model.rows[gi][0].gid
                if k == Qt.Key_Left and (fk >= 0 or gid not in self.model.collapsed):
                    self.model.set_collapsed(self.model.collapsed | {gid})
                    self.tree.setCurrentIndex(self.model.group_index(gi))
                    return True
                if k == Qt.Key_Right and fk < 0 and gid in self.model.collapsed:
                    self.toggle_group(gi)
                    return True
            if k == Qt.Key_Space and not mods & (Qt.ControlModifier | Qt.ShiftModifier):
                rows = self.selected_file_items()
                if not rows and self.tree.currentIndex().isValid():
                    rows = [self.tree.currentIndex().siblingAtColumn(0)]
                self.check_rows(rows)
                return True
        if obj is self.tree.viewport() and ev.type() == ev.Type.MouseButtonPress \
                and ev.button() == Qt.LeftButton and not ev.modifiers():
            pos = ev.position().toPoint()
            idx = self.tree.indexAt(pos)
            if idx.isValid() and idx.column() == C_NAME and self.model.is_group(idx) \
                    and pos.x() < self.tree.visualRect(idx).left() + INDENT:
                self.toggle_group(self.model.locate(idx.row())[0])   # click on ▾/▸
                return True
            rows = self.selected_file_items()
            if idx.isValid() and idx.column() == C_NAME and idx in rows and len(rows) > 1 \
                    and self._on_checkbox(idx, pos):
                self.check_rows(rows, self.model.path_at(idx) not in self.selected)
                return True                            # keep the multi-row selection
        return super().eventFilter(obj, ev)

    def _on_checkbox(self, idx, pos) -> bool:
        opt = QStyleOptionViewItem()
        opt.initFrom(self.tree)
        opt.rect = self.tree.visualRect(idx).adjusted(INDENT, 0, 0, 0)   # file rows are drawn indented
        opt.features |= QStyleOptionViewItem.HasCheckIndicator
        opt.text = str(self.model.data(idx) or "")
        box = self.tree.style().subElementRect(QStyle.SE_ItemViewItemCheckIndicator, opt, self.tree)
        return box.adjusted(-2, -2, 2, 2).contains(pos)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_location()

    def _section_resized(self, col: int, _old: int, _new: int) -> None:
        if col == C_LOC and not self._fitting:
            self._loc_sized = True                             # the user set it: never auto-fit again

    def _fit_location(self) -> None:
        """Give Location the free width once (first show); after that the user's width is kept."""
        if self._loc_sized or not self.tree.isVisible():
            return
        vw = self.tree.viewport().width()
        others = sum(self.tree.columnWidth(c) for c in range(len(COLS)) if c != C_LOC)
        if vw > 200:
            self._fitting = True
            self.tree.setColumnWidth(C_LOC, max(160, vw - others - 2))
            self._fitting = False
            self._loc_sized = True

    def closeEvent(self, ev):
        if self._worker is not None:
            self._worker.cancel.set()
            self._worker.wait(5000)
        for w in list(self._jobs):
            w.wait(10000)
        self.prev_a.show_entry(None)
        self.prev_b.show_entry(None)
        self.prev_a.media.stop()
        self.prev_b.media.stop()
        super().closeEvent(ev)
