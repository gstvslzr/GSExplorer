"""Find Files window (toolbar 🔍, Ctrl+F, Alt+F7, or right-click a folder ▸ Search in…)."""
from __future__ import annotations

import os
import re
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QDate, QDateTime, QModelIndex, QThread, Qt, QTime, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog,
                               QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter, QStyledItemDelegate, QTableView,
                               QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, fsutil, system, uiicons
from . import search as se
from .busygear import GearSpinner
from .dirmodel import Entry
from .dupesui import responsive_gil
from .preview import PreviewPanel

COLS = ["Name", "Folder", "Size", "Modified", "Match"]
C_NAME, C_FOLDER, C_SIZE, C_DATE, C_MATCH = range(5)
NAME_MODES = [("Name contains", "contains"), ("Wildcards  (*.pdf;*.docx)", "wildcard"),
              ("Regular expression", "regex"), ("Exact name", "exact")]
KIND_CHOICES = [("Any kind", ""), ("Documents", "documents"), ("Images", "images"), ("Audio", "audio"),
                ("Video", "video"), ("Archives", "archives"), ("Code & text", "code")]
WHAT_CHOICES = [("Files and folders", "both"), ("Files only", "files"), ("Folders only", "folders")]
DATE_CHOICES = [("Any time", ""), ("Today", "today"), ("Last 7 days", "7"), ("Last 30 days", "30"),
                ("Last 12 months", "365"), ("This year", "year"), ("Between…", "between")]
UNITS = [("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)]
HISTORY_MAX = 20


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    return lbl


class SearchWorker(QThread):
    hits = Signal(list)
    progress = Signal(dict)
    done = Signal(object)

    def __init__(self, crit: se.Criteria):
        super().__init__()
        self.crit = crit
        self.cancel = threading.Event()

    def start(self, *a):
        responsive_gil(True)                       # keep the window and the gear smooth while searching
        self.finished.connect(lambda: responsive_gil(False))
        super().start(*a)

    def run(self):
        try:
            r = se.search(self.crit, self.hits.emit, self.progress.emit, self.cancel)
        except Exception as e:  # noqa: BLE001 - reported to the user
            r = e
        self.done.emit(r)


# =========================================================================== results model
class ResultModel(QAbstractTableModel):
    """All hits in a flat list; `rows` are indices into it after filtering + sorting (done in Python,
    which is far faster than a proxy model calling back into Python for every comparison)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.all: list[se.Hit] = []
        self.rows: list[int] = []
        self.filter = ""
        self.sort_col, self.sort_desc = -1, False
        self._icons: dict = {}
        self._disp: dict[str, str] = {}

    # ---------------------------------------------------------------- updates
    def clear(self) -> None:
        self.beginResetModel()
        self.all, self.rows, self._disp = [], [], {}
        self.endResetModel()

    def append(self, hits: list[se.Hit]) -> None:
        start = len(self.all)
        self.all.extend(hits)
        new = [i for i in range(start, len(self.all)) if self._passes(self.all[i])]
        if not new:
            return
        if self.sort_col >= 0:                      # keep the chosen order while results arrive
            self.beginResetModel()
            self.rows.extend(new)
            self._sort()
            self.endResetModel()
        else:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(new) - 1)
            self.rows.extend(new)
            self.endInsertRows()

    def remove_paths(self, paths: set[str]) -> None:
        self.beginResetModel()
        self.all = [h for h in self.all if h.path not in paths]
        self._rebuild()
        self.endResetModel()

    def set_filter(self, text: str) -> None:
        self.beginResetModel()
        self.filter = text.casefold().strip()
        self._rebuild()
        self.endResetModel()

    def _passes(self, h: se.Hit) -> bool:
        return not self.filter or self.filter in h.name.casefold() or self.filter in h.folder.casefold() \
            or self.filter in h.snippet.casefold()

    def _rebuild(self) -> None:
        self.rows = [i for i, h in enumerate(self.all) if self._passes(h)]
        if self.sort_col >= 0:
            self._sort()

    def _sort(self) -> None:
        a, col = self.all, self.sort_col
        keys = {C_NAME: lambda i: a[i].name.casefold(), C_FOLDER: lambda i: (a[i].folder.casefold(), a[i].name.casefold()),
                C_SIZE: lambda i: (not a[i].is_dir, a[i].size), C_DATE: lambda i: a[i].mtime,
                C_MATCH: lambda i: (a[i].count, a[i].snippet.casefold())}
        self.rows.sort(key=keys[col], reverse=self.sort_desc)

    def sort(self, column, order=Qt.AscendingOrder):
        self.beginResetModel()
        self.sort_col, self.sort_desc = column, order == Qt.DescendingOrder
        if column < 0:                              # no sort column: the order results were found in
            self.rows.sort()
        else:
            self._sort()
        self.endResetModel()

    def hit(self, row: int) -> se.Hit:
        return self.all[self.rows[row]]

    # ---------------------------------------------------------------- Qt
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def _folder(self, f: str) -> str:
        s = self._disp.get(f)
        if s is None:
            s = self._disp[f] = fsutil.display_path(f)
        return s

    def _icon(self, h: se.Hit):
        ext = "" if h.is_dir else os.path.splitext(h.name)[1][1:].casefold()
        key = (h.is_dir, ext)
        ic = self._icons.get(key)
        if ic is None:
            e = Entry(h.name, h.path)
            e.is_dir = h.is_dir
            ic = self._icons[key] = self.win.icons.for_entry(e)
        return ic

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        h = self.all[self.rows[index.row()]]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == C_NAME:
                return fsutil.display_name(h.name)
            if col == C_FOLDER:
                return self._folder(h.folder) + ("  (in archive)" if h.member else "")
            if col == C_SIZE:
                return "" if h.is_dir else fsutil.human_size(h.size)
            if col == C_DATE:
                return _date(h.mtime)
            if col == C_MATCH:
                if not h.line:
                    return ""
                more = f"  (+{h.count - 1} more)" if h.count > 1 else ""
                return f"{h.line}: {h.snippet}{more}"
        elif role == Qt.DecorationRole and col == C_NAME:
            return self._icon(h)
        elif role == Qt.ToolTipRole:
            if col in (C_NAME, C_FOLDER):
                return fsutil.display_path(h.path.replace("::", " ▸ "))
            if col == C_MATCH and h.line:
                return f"Line {h.line}: {h.snippet}"
            if col == C_SIZE and not h.is_dir:
                return f"{h.size:,} bytes"
        return None


class _RightElide(QStyledItemDelegate):
    """Matching text is cut at the end (its start is what matters); paths keep the view's middle elide."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideRight


class _View(QTableView):
    def header(self):
        return self.horizontalHeader()


# =========================================================================== window
class SearchWindow(QMainWindow):
    def __init__(self, win):
        super().__init__(None)
        self.win = win
        self.cfg = win.cfg
        self.setWindowTitle(f"Find Files — {APP_NAME}")
        self.setWindowIcon(win.windowIcon())
        self.resize(1400, 860)
        self.worker: SearchWorker | None = None
        self._refining = False
        self._t0 = 0.0
        self._last_summary = None
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ------------------------------------------------------------ criteria
        top = QHBoxLayout()
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        self.f_name = QComboBox()
        self.f_name.setEditable(True)
        self.f_name.setInsertPolicy(QComboBox.NoInsert)
        self.f_name.lineEdit().setPlaceholderText("part of a name — or *.pdf;*.docx with Wildcards — empty = everything")
        self.f_name.setMinimumWidth(420)
        self.f_mode = QComboBox()
        for t, _ in NAME_MODES:
            self.f_mode.addItem(t)
        self.f_ncase = QCheckBox("Match case")
        self.f_path = QCheckBox("Whole path")
        self.f_path.setToolTip("Match the pattern against the full path (folders included), not just the name")
        g.addWidget(QLabel("Find:"), 0, 0)
        g.addWidget(self.f_name, 0, 1)
        g.addWidget(self.f_mode, 0, 2)
        g.addWidget(self.f_ncase, 0, 3)
        g.addWidget(self.f_path, 0, 4)

        self.f_where = QLineEdit()
        self.f_where.setPlaceholderText("folder(s) — several separated by ;")
        bw = QPushButton("Browse…")
        bw.clicked.connect(self._browse)
        bp = QPushButton("Pane folders")
        bp.setToolTip("Search the folders shown in both panes")
        bp.clicked.connect(self._pane_folders)
        self.f_sub = QCheckBox("Subfolders")
        self.f_sub.setChecked(True)
        self.f_depth = QSpinBox()
        self.f_depth.setRange(0, 999)
        self.f_depth.setSpecialValueText("all levels")
        self.f_depth.setPrefix("")
        self.f_depth.setSuffix(" levels")
        self.f_depth.setToolTip("How deep to look: 1 = only this folder, 2 = one level of subfolders, …")
        self.f_sub.toggled.connect(self.f_depth.setEnabled)
        wl = QHBoxLayout()
        wl.addWidget(self.f_where, 1)
        wl.addWidget(bw)
        wl.addWidget(bp)
        g.addWidget(QLabel("Look in:"), 1, 0)
        g.addLayout(wl, 1, 1, 1, 2)
        g.addWidget(self.f_sub, 1, 3)
        g.addWidget(self.f_depth, 1, 4)

        self.f_text = QLineEdit()
        self.f_text.setPlaceholderText("text inside the files (optional)")
        self.f_tmode = QComboBox()
        self.f_tmode.addItems(["Text", "Regular expression"])
        self.f_tcase = QCheckBox("Match case")
        self.f_word = QCheckBox("Whole word")
        self.f_not = QCheckBox("NOT containing")
        tl = QHBoxLayout()
        tl.addWidget(self.f_text, 1)
        tl.addWidget(self.f_tmode)
        g.addWidget(QLabel("Containing:"), 2, 0)
        g.addLayout(tl, 2, 1, 1, 2)
        g.addWidget(self.f_tcase, 2, 3)
        wl2 = QHBoxLayout()
        wl2.addWidget(self.f_word)
        wl2.addWidget(self.f_not)
        g.addLayout(wl2, 2, 4)
        g.setColumnStretch(1, 1)
        top.addLayout(g, 1)

        btns = QVBoxLayout()
        self.b_search = QPushButton("Search")
        self.b_search.setIcon(uiicons.icon("search", "#ffffff", "#bfc7d1"))   # white glass on the accent button
        self.b_search.setDefault(True)
        f = self.b_search.font()
        f.setBold(True)
        self.b_search.setFont(f)
        self.b_search.setMinimumWidth(150)
        self.b_search.clicked.connect(self.start_search)
        self.b_stop = QPushButton("Stop")
        self.b_stop.clicked.connect(self.stop_search)
        self.b_reset = QPushButton("Clear criteria")
        self.b_reset.clicked.connect(self.reset_criteria)
        for b in (self.b_search, self.b_stop, self.b_reset):
            btns.addWidget(b)
        top.addLayout(btns)
        root.addLayout(top)

        # ------------------------------------------------------------ more options
        mrow = QHBoxLayout()
        self.b_more = QToolButton()
        self.b_more.setCheckable(True)
        self.b_more.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.b_more.setText("▸ More options")
        self.b_more.toggled.connect(self._toggle_more)
        mrow.addWidget(self.b_more)
        self.more_summary = _muted()
        mrow.addWidget(self.more_summary, 1)
        mrow.addWidget(QLabel("Saved searches:"))
        self.saved = QComboBox()
        self.saved.setMinimumWidth(200)
        self.saved.activated.connect(self._load_saved)
        mrow.addWidget(self.saved)
        bs = QToolButton()
        bs.setText("Save…")
        bs.clicked.connect(self._save_search)
        bd = QToolButton()
        bd.setText("Delete")
        bd.clicked.connect(self._delete_saved)
        mrow.addWidget(bs)
        mrow.addWidget(bd)
        root.addLayout(mrow)

        self.more = QWidget()
        mg = QGridLayout(self.more)
        mg.setContentsMargins(18, 0, 0, 4)
        self.f_what = QComboBox()
        for t, _ in WHAT_CHOICES:
            self.f_what.addItem(t)
        self.f_kind = QComboBox()
        for t, _ in KIND_CHOICES:
            self.f_kind.addItem(t)
        mg.addWidget(QLabel("Type:"), 0, 0)
        tk = QHBoxLayout()
        tk.addWidget(self.f_what)
        tk.addWidget(self.f_kind)
        tk.addStretch(1)
        mg.addLayout(tk, 0, 1)

        self.f_min, self.f_minu = QSpinBox(), QComboBox()
        self.f_max, self.f_maxu = QSpinBox(), QComboBox()
        for sp, un in ((self.f_min, self.f_minu), (self.f_max, self.f_maxu)):
            sp.setRange(0, 999_999)
            sp.setSpecialValueText("any")
            for t, _ in UNITS:
                un.addItem(t)
            un.setCurrentIndex(1)
        sz = QHBoxLayout()
        for w in (QLabel("at least"), self.f_min, self.f_minu, QLabel("  at most"), self.f_max, self.f_maxu):
            sz.addWidget(w)
        sz.addStretch(1)
        mg.addWidget(QLabel("Size:"), 1, 0)
        mg.addLayout(sz, 1, 1)

        self.f_date = QComboBox()
        for t, _ in DATE_CHOICES:
            self.f_date.addItem(t)
        self.f_from, self.f_to = QDateEdit(), QDateEdit()
        for d in (self.f_from, self.f_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
            d.setDate(QDate.currentDate())
            d.setEnabled(False)
        self.f_from.setDate(QDate.currentDate().addMonths(-1))
        self.f_date.currentIndexChanged.connect(
            lambda i: [d.setEnabled(DATE_CHOICES[i][1] == "between") for d in (self.f_from, self.f_to)])
        dl = QHBoxLayout()
        for w in (self.f_date, QLabel("from"), self.f_from, QLabel("to"), self.f_to):
            dl.addWidget(w)
        dl.addStretch(1)
        mg.addWidget(QLabel("Modified:"), 2, 0)
        mg.addLayout(dl, 2, 1)

        self.f_exclude = QLineEdit()
        self.f_exclude.setPlaceholderText("names to skip, e.g. node_modules;.git;*.tmp;Cache")
        mg.addWidget(QLabel("Skip:"), 3, 0)
        mg.addWidget(self.f_exclude, 3, 1)

        self.f_hidden = QCheckBox("Hidden files and folders")
        self.f_onefs = QCheckBox("Stay on this drive")
        self.f_onefs.setToolTip("Don't descend into other mounted drives or network shares")
        self.f_arch = QCheckBox("Look inside .zip / .tar archives (names)")
        self.f_docs = QCheckBox("Text inside documents (DOCX, XLSX, PPTX, ODF, PDF)")
        self.f_docs.setChecked(True)
        self.f_bin = QCheckBox("Text inside binary files")
        self.f_empty = QCheckBox("Only empty files / folders")
        self.f_index = QCheckBox("Use the system file index (fast, names only)")
        tool = se.index_tool()
        self.f_index.setEnabled(bool(tool))
        self.f_index.setToolTip(f"Uses {os.path.basename(tool)} — instant on whole disks; the index is updated "
                                "periodically (usually daily), so very new files may be missing. Only for name "
                                "searches." if tool else "Install plocate to enable: sudo apt install plocate")
        self.f_limit = QSpinBox()
        self.f_limit.setRange(100, 1_000_000)
        self.f_limit.setSingleStep(10_000)
        self.f_limit.setValue(100_000)
        self.f_limit.setPrefix("stop after ")
        self.f_limit.setSuffix(" results")
        opts = QGridLayout()
        for i, w in enumerate((self.f_hidden, self.f_onefs, self.f_arch, self.f_docs, self.f_bin, self.f_empty,
                               self.f_index, self.f_limit)):
            opts.addWidget(w, i // 4, i % 4)
        mg.addWidget(QLabel("Options:"), 4, 0)
        mg.addLayout(opts, 4, 1)
        self.more.setVisible(False)
        root.addWidget(self.more)

        # ------------------------------------------------------------ results + preview
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        fr = QHBoxLayout()
        self.r_filter = QLineEdit()
        self.r_filter.setPlaceholderText("Filter these results…")
        self.r_filter.setClearButtonEnabled(True)
        self.r_filter.textChanged.connect(self._filter_changed)
        fr.addWidget(self.r_filter, 1)
        self.r_count = _muted()
        fr.addWidget(self.r_count)
        ll.addLayout(fr)
        self.model = ResultModel(win)
        self.view = _View()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.setAlternatingRowColors(True)
        self.view.setTextElideMode(Qt.ElideMiddle)
        self.view.setItemDelegateForColumn(C_MATCH, _RightElide(self.view))
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        vh = self.view.verticalHeader()
        vh.hide()
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(self.view.fontMetrics().height() + 8)
        hv = self.view.horizontalHeader()
        hv.setSectionResizeMode(QHeaderView.Interactive)
        hv.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hv.setStretchLastSection(True)
        for c, wdt in ((C_NAME, 260), (C_FOLDER, 330), (C_SIZE, 80), (C_DATE, 135)):
            self.view.setColumnWidth(c, wdt)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda i: self.open_hits([self.model.hit(i.row())]))
        self.view.selectionModel().currentRowChanged.connect(self._current_changed)
        self.view.installEventFilter(self)
        ll.addWidget(self.view, 1)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.match = QLabel()
        self.match.setWordWrap(True)
        self.match.setTextFormat(Qt.RichText)
        self.match.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(self.match)
        self.preview = PreviewPanel(win, closable=False)
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([1000, 400])
        root.addWidget(split, 1)

        # ------------------------------------------------------------ status + result actions
        bottom = QHBoxLayout()
        self.gear = GearSpinner(self)
        bottom.addWidget(self.gear)
        self.status = QLabel()
        bottom.addWidget(self.status, 1)
        self.b_refine = QPushButton("Search within results")
        self.b_refine.setToolTip("Apply the current criteria to these results only (no new folder walk)")
        self.b_refine.clicked.connect(lambda: self.start_search(refine=True))
        self.b_export = QPushButton("Export CSV…")
        self.b_export.clicked.connect(self.export)
        self.b_copy = QPushButton("Copy paths")
        self.b_copy.clicked.connect(lambda: self.copy_paths(self.all_or_selected()))
        for b in (self.b_refine, self.b_export, self.b_copy):
            bottom.addWidget(b)
        root.addLayout(bottom)

        for w in (self.f_text, self.f_exclude, self.f_where):
            w.returnPressed.connect(self.start_search)
        self.f_name.lineEdit().returnPressed.connect(self.start_search)
        QShortcut(QKeySequence("Esc"), self, self._escape)
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: self.r_filter.setFocus())
        for w in (self.f_what, self.f_kind, self.f_date, self.f_min, self.f_max, self.f_exclude, self.f_hidden,
                  self.f_onefs, self.f_arch, self.f_empty, self.f_index, self.f_bin):
            sig = getattr(w, "currentIndexChanged", None) or getattr(w, "valueChanged", None) \
                or getattr(w, "toggled", None) or getattr(w, "textChanged", None)
            sig.connect(self._update_more_summary)
        self._load_history()
        self._load_state(self.cfg["search_last"] or {})
        self._refresh_buttons()

    # ================================================================== criteria <-> UI
    def criteria(self) -> se.Criteria:
        roots = [os.path.abspath(os.path.expanduser(p.strip())) for p in self.f_where.text().split(";") if p.strip()]
        dmode = DATE_CHOICES[self.f_date.currentIndex()][1]
        now = time.time()
        after = before = 0.0
        if dmode == "today":
            after = QDateTime(QDate.currentDate(), QTime(0, 0)).toSecsSinceEpoch()
        elif dmode in ("7", "30", "365"):
            after = now - int(dmode) * 86400
        elif dmode == "year":
            after = QDateTime(QDate(QDate.currentDate().year(), 1, 1), QTime(0, 0)).toSecsSinceEpoch()
        elif dmode == "between":
            after = QDateTime(self.f_from.date(), QTime(0, 0)).toSecsSinceEpoch()
            before = QDateTime(self.f_to.date().addDays(1), QTime(0, 0)).toSecsSinceEpoch()
        unit = lambda cb: UNITS[cb.currentIndex()][1]  # noqa: E731
        return se.Criteria(
            roots=roots, name=self.f_name.currentText().strip(), name_mode=NAME_MODES[self.f_mode.currentIndex()][1],
            name_case=self.f_ncase.isChecked(), match_path=self.f_path.isChecked(), exclude=self.f_exclude.text(),
            what=WHAT_CHOICES[self.f_what.currentIndex()][1], kind=KIND_CHOICES[self.f_kind.currentIndex()][1],
            text=self.f_text.text(), text_mode="regex" if self.f_tmode.currentIndex() == 1 else "plain",
            text_case=self.f_tcase.isChecked(), whole_word=self.f_word.isChecked(), text_invert=self.f_not.isChecked(),
            in_documents=self.f_docs.isChecked(), binary=self.f_bin.isChecked(),
            min_size=self.f_min.value() * unit(self.f_minu) if self.f_min.value() else -1,
            max_size=self.f_max.value() * unit(self.f_maxu) if self.f_max.value() else -1,
            after=float(after), before=float(before), subfolders=self.f_sub.isChecked(),
            max_depth=self.f_depth.value() if self.f_sub.isChecked() else 1, hidden=self.f_hidden.isChecked(),
            one_fs=self.f_onefs.isChecked(), in_archives=self.f_arch.isChecked(), empty_only=self.f_empty.isChecked(),
            use_index=self.f_index.isChecked() and self.f_index.isEnabled(), limit=self.f_limit.value())

    def _state(self) -> dict:
        """Everything on screen (for saved searches / last search), including the date choice."""
        d = self.criteria().to_dict()
        d.update(date_choice=DATE_CHOICES[self.f_date.currentIndex()][1],
                 date_from=self.f_from.date().toString("yyyy-MM-dd"), date_to=self.f_to.date().toString("yyyy-MM-dd"),
                 min_value=self.f_min.value(), min_unit=self.f_minu.currentIndex(),
                 max_value=self.f_max.value(), max_unit=self.f_maxu.currentIndex(), more=self.b_more.isChecked())
        return d

    def _load_state(self, d: dict) -> None:
        def idx(choices, val):
            return next((i for i, (_, v) in enumerate(choices) if v == val), 0)
        if "roots" in d and d["roots"]:
            self.f_where.setText(";".join(fsutil.display_path(r) for r in d["roots"]))
        self.f_name.setEditText(d.get("name", ""))
        self.f_mode.setCurrentIndex(idx(NAME_MODES, d.get("name_mode", "contains")))
        self.f_ncase.setChecked(bool(d.get("name_case")))
        self.f_path.setChecked(bool(d.get("match_path")))
        self.f_exclude.setText(d.get("exclude", ""))
        self.f_what.setCurrentIndex(idx(WHAT_CHOICES, d.get("what", "both")))
        self.f_kind.setCurrentIndex(idx(KIND_CHOICES, d.get("kind", "")))
        self.f_text.setText(d.get("text", ""))
        self.f_tmode.setCurrentIndex(1 if d.get("text_mode") == "regex" else 0)
        self.f_tcase.setChecked(bool(d.get("text_case")))
        self.f_word.setChecked(bool(d.get("whole_word")))
        self.f_not.setChecked(bool(d.get("text_invert")))
        self.f_docs.setChecked(d.get("in_documents", True) is not False)
        self.f_bin.setChecked(bool(d.get("binary")))
        self.f_sub.setChecked(d.get("subfolders", True) is not False)
        depth = int(d.get("max_depth", 0) or 0)
        self.f_depth.setValue(depth if d.get("subfolders", True) is not False else 0)
        self.f_hidden.setChecked(bool(d.get("hidden")))
        self.f_onefs.setChecked(bool(d.get("one_fs")))
        self.f_arch.setChecked(bool(d.get("in_archives")))
        self.f_empty.setChecked(bool(d.get("empty_only")))
        self.f_index.setChecked(bool(d.get("use_index")) and self.f_index.isEnabled())
        self.f_limit.setValue(int(d.get("limit", 100_000) or 100_000))
        self.f_date.setCurrentIndex(idx(DATE_CHOICES, d.get("date_choice", "")))
        for w, k in ((self.f_from, "date_from"), (self.f_to, "date_to")):
            if d.get(k):
                w.setDate(QDate.fromString(d[k], "yyyy-MM-dd"))
        self.f_min.setValue(int(d.get("min_value", 0) or 0))
        self.f_minu.setCurrentIndex(int(d.get("min_unit", 1)))
        self.f_max.setValue(int(d.get("max_value", 0) or 0))
        self.f_maxu.setCurrentIndex(int(d.get("max_unit", 1)))
        if d.get("more"):
            self.b_more.setChecked(True)
        self._update_more_summary()

    def reset_criteria(self) -> None:
        where = self.f_where.text()
        self._load_state({})
        self.f_where.setText(where)

    def set_folders(self, folders: list[str]) -> None:
        self.f_where.setText(";".join(fsutil.display_path(f) for f in folders))

    def _browse(self) -> None:
        cur = self.f_where.text().split(";")[0].strip() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Look in", cur)
        if d:
            self.f_where.setText(fsutil.display_path(d))

    def _pane_folders(self) -> None:
        ps = []
        for pane in (self.win.left, self.win.right):
            try:
                v = pane.current()
                if v and v.path and not v.is_trash() and v.path not in ps:
                    ps.append(v.path)
            except (AttributeError, RuntimeError):
                pass
        self.set_folders(ps)

    def _toggle_more(self, on: bool) -> None:
        self.more.setVisible(on)
        self.b_more.setText(("▾" if on else "▸") + " More options")
        self._update_more_summary()

    def _update_more_summary(self, *_):
        """When the options are folded away, say which ones are active so nothing is hidden."""
        if self.b_more.isChecked():
            self.more_summary.setText("")
            return
        bits = []
        if self.f_what.currentIndex():
            bits.append(WHAT_CHOICES[self.f_what.currentIndex()][0].lower())
        if self.f_kind.currentIndex():
            bits.append(KIND_CHOICES[self.f_kind.currentIndex()][0].lower())
        if self.f_min.value() or self.f_max.value():
            bits.append("size filter")
        if self.f_date.currentIndex():
            bits.append(DATE_CHOICES[self.f_date.currentIndex()][0].lower())
        if self.f_exclude.text().strip():
            bits.append("skipping " + self.f_exclude.text().strip())
        for cb, label in ((self.f_hidden, "hidden"), (self.f_onefs, "this drive only"), (self.f_arch, "inside archives"),
                          (self.f_empty, "empty only"), (self.f_index, "system index"), (self.f_bin, "binary files")):
            if cb.isChecked():
                bits.append(label)
        self.more_summary.setText(("Active: " + ", ".join(bits)) if bits else "")

    # ================================================================== history / saved searches
    def _load_history(self) -> None:
        self.f_name.clear()
        for h in self.cfg["search_history"] or []:
            self.f_name.addItem(h)
        self.f_name.setEditText("")
        self.saved.clear()
        self.saved.addItem("—")
        for n in sorted((self.cfg["search_saved"] or {}).keys(), key=str.casefold):
            self.saved.addItem(n)

    def _remember(self) -> None:
        name = self.f_name.currentText().strip()
        hist = [h for h in (self.cfg["search_history"] or []) if h != name]
        if name:
            hist.insert(0, name)
        self.cfg["search_history"] = hist[:HISTORY_MAX]
        self.cfg["search_last"] = self._state()
        try:
            self.cfg.save()
        except OSError:
            pass
        text = self.f_name.currentText()
        self.f_name.blockSignals(True)
        self.f_name.clear()
        for h in self.cfg["search_history"]:
            self.f_name.addItem(h)
        self.f_name.setEditText(text)
        self.f_name.blockSignals(False)

    def _save_search(self) -> None:
        name, ok = QInputDialog.getText(self, f"Save search — {APP_NAME}", "Name for this search:",
                                        text=self.saved.currentText() if self.saved.currentIndex() > 0 else "")
        if not ok or not name.strip():
            return
        saved = dict(self.cfg["search_saved"] or {})
        saved[name.strip()] = self._state()
        self.cfg["search_saved"] = saved
        try:
            self.cfg.save()
        except OSError:
            pass
        self._load_history()
        self.saved.setCurrentText(name.strip())

    def _load_saved(self, i: int) -> None:
        if i <= 0:
            return
        d = (self.cfg["search_saved"] or {}).get(self.saved.itemText(i))
        if d:
            self._load_state(d)

    def _delete_saved(self) -> None:
        n = self.saved.currentText()
        saved = dict(self.cfg["search_saved"] or {})
        if self.saved.currentIndex() > 0 and n in saved:
            del saved[n]
            self.cfg["search_saved"] = saved
            self._load_history()

    # ================================================================== run
    def start_search(self, refine: bool = False) -> None:
        if self.worker is not None:
            return
        c = self.criteria()
        if refine:
            if not self.model.all:
                return
            c.candidates = [h.path for h in self.model.all if not h.member]
        elif not c.roots:
            QMessageBox.information(self, APP_NAME, "Choose a folder to look in.")
            return
        else:
            missing = [r for r in c.roots if not os.path.isdir(r)]
            if missing:
                QMessageBox.warning(self, APP_NAME, "Folder not found:\n" + "\n".join(map(fsutil.display_path, missing)))
                return
        try:
            se.name_matcher(c)
            se.text_regex(c)
        except re.error as e:
            QMessageBox.warning(self, APP_NAME, f"The regular expression isn't valid: {e}")
            return
        self._remember()
        self.model.clear()
        self.preview.show_entry(None)
        self.match.setText("")
        self.worker = w = SearchWorker(c)
        w.hits.connect(self._got_hits)
        w.progress.connect(self._progress)
        w.done.connect(self._done)
        self._t0 = time.monotonic()
        self._refining = refine
        self.gear.start()
        self.status.setText("Refining results…" if refine else "Searching…")
        self._refresh_buttons()
        w.start()

    def stop_search(self) -> None:
        if self.worker is not None:
            self.worker.cancel.set()
            self.status.setText("Stopping…")

    def _escape(self) -> None:
        if self.worker is not None:
            self.stop_search()
        else:
            self.close()

    def _got_hits(self, hits: list) -> None:
        first = self.model.rowCount() == 0
        self.model.append(hits)
        if first and self.model.rowCount():
            self.view.selectRow(0)
        self._update_count()

    def _progress(self, d: dict) -> None:
        if self.worker is None:
            return
        where = fsutil.display_path(d.get("path", ""))
        extra = f" · {d['content']:,} files read" if d.get("content") else ""
        self.status.setText(f"Searching… {d.get('folders', 0):,} folders, {d.get('files', 0):,} files checked{extra}"
                            f" · {d.get('found', 0):,} found — " + self.fontMetrics().elidedText(where, Qt.ElideMiddle, 420))

    def _done(self, r) -> None:
        w, self.worker = self.worker, None
        if w is not None:
            w.wait(2000)
        self.gear.stop()
        self._refresh_buttons()
        if isinstance(r, Exception):
            self.status.setText("")
            QMessageBox.critical(self, APP_NAME, f"The search failed: {r}")
            return
        self._last_summary = r
        how = {"index": " (system index)", "refine": " (within previous results)"}.get(r.source, "")
        msg = f"{r.found:,} found in {r.seconds:.1f} s{how} · {r.folders:,} folders, {r.files:,} files checked"
        if r.content_checked:
            msg += f", {r.content_checked:,} read"
        if r.truncated:
            msg = f"<b>Stopped at {r.found:,} results</b> (limit in More options) — " + msg
        elif r.cancelled:
            msg = "<b>Stopped</b> — " + msg
        if r.errors:
            msg += f" · {len(r.errors)} not readable"
        self.status.setText(msg)
        self.status.setToolTip("\n".join((r.notes or []) + r.errors[:60]))
        self._update_count()

    def _refresh_buttons(self) -> None:
        busy = self.worker is not None
        self.b_search.setEnabled(not busy)
        self.b_stop.setEnabled(busy)
        has = bool(self.model.all)
        for b in (self.b_refine, self.b_export, self.b_copy):
            b.setEnabled(has and not busy)

    def _update_count(self) -> None:
        n, t = self.model.rowCount(), len(self.model.all)
        self.r_count.setText(f"{n:,} shown of {t:,}" if n != t else (f"{t:,} result{'s' if t != 1 else ''}" if t else ""))
        self._refresh_buttons()

    def _filter_changed(self, text: str) -> None:
        self.model.set_filter(text)
        self._update_count()

    # ================================================================== selection / preview
    def selected_hits(self) -> list[se.Hit]:
        return [self.model.hit(i.row()) for i in self.view.selectionModel().selectedRows(0)]

    def all_or_selected(self) -> list[se.Hit]:
        sel = self.selected_hits()
        return sel if len(sel) > 1 else [self.model.hit(r) for r in range(self.model.rowCount())]

    def _current_changed(self, cur, _prev) -> None:
        if not cur.isValid():
            self.preview.show_entry(None)
            self.match.setText("")
            return
        h = self.model.hit(cur.row())
        self.preview.show_entry(h.archive or h.path)
        if h.member:
            self.match.setText(f"Inside archive <b>{fsutil.display_name(os.path.basename(h.archive))}</b>: "
                               f"{fsutil.display_name(h.member)}")
        elif h.line:
            esc = (h.snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            crit = self.worker.crit if self.worker else None
            rx = None
            try:
                rx = se.text_regex(crit or self.criteria())
            except re.error:
                pass
            if rx is not None:
                m = rx.search(h.snippet)
                if m:
                    a, b = m.span()
                    pre = h.snippet[:a].replace("&", "&amp;").replace("<", "&lt;")
                    hit = h.snippet[a:b].replace("&", "&amp;").replace("<", "&lt;")
                    post = h.snippet[b:].replace("&", "&amp;").replace("<", "&lt;")
                    esc = f"{pre}<span style='background:#f2d24b;color:#111'>{hit}</span>{post}"
            more = f" — {h.count} matching lines" if h.count > 1 else ""
            self.match.setText(f"<b>Line {h.line}</b>{more}<br><tt>{esc}</tt>")
        else:
            self.match.setText("")

    # ================================================================== actions
    def open_hits(self, hits: list[se.Hit]) -> None:
        for h in hits[:20]:
            target = h.archive or h.path
            if h.is_dir and not h.member:
                self.show_in_pane(h)
                return
            try:
                if self.win.cfg["media_open"] == "builtin":
                    from .mediaplayer import media_kind
                    if media_kind(target):
                        self.win.play_media(target)
                        continue
                system.open_default(target)
            except OSError as e:
                QMessageBox.warning(self, APP_NAME, f"Couldn't open: {e}")

    def view_hits(self, h: se.Hit) -> None:
        files = [x.path for x in (self.model.hit(r) for r in range(self.model.rowCount())) if not x.is_dir and not x.member]
        if h.is_dir or h.member or h.path not in files:
            return
        from .dialogs import ViewerDialog
        ViewerDialog(self, h.path, files, on_change=self.select_path).exec()

    def play_media(self, path: str, **kw) -> None:          # for the F3 viewer's Play button
        self.win.play_media(path, **kw)

    def select_path(self, path: str) -> None:
        for r in range(self.model.rowCount()):
            if self.model.hit(r).path == path:
                self.view.selectRow(r)
                self.view.scrollTo(self.model.index(r, 0))
                return

    def show_in_pane(self, h: se.Hit) -> None:
        target = h.archive or h.path
        self.win.active_view().navigate(os.path.dirname(target), select=target)
        self.win.raise_()
        self.win.activateWindow()

    def open_folder_tab(self, h: se.Hit) -> None:
        target = h.archive or h.path
        folder = target if h.is_dir and not h.member else os.path.dirname(target)
        self.win.active_pane().add_tab(folder)
        self.win.raise_()
        self.win.activateWindow()

    @staticmethod
    def copy_paths(hits: list[se.Hit]) -> None:
        QApplication.clipboard().setText("\n".join(fsutil.display_path(h.path.replace("::", "/")) for h in hits))

    def _real_paths(self, hits) -> list[str]:
        return [h.path for h in hits if not h.member]

    def transfer(self, kind: str, hits) -> None:
        paths = self._real_paths(hits)
        if not paths:
            return
        dest = self.win.other_pane().current().path
        if QMessageBox.question(self, APP_NAME, f"{'Copy' if kind == 'copy' else 'Move'} {len(paths):,} item(s) to\n"
                                f"{fsutil.display_path(dest)}?") != QMessageBox.Yes:
            return
        self.win.jobs.submit(kind, paths, dest)
        if kind == "move":
            self.model.remove_paths(set(paths))
            self._update_count()

    def trash(self, hits) -> None:
        paths = self._real_paths(hits)
        if not paths:
            return
        if QMessageBox.question(self, APP_NAME, f"Move {len(paths):,} item(s) to the trash?") != QMessageBox.Yes:
            return
        self.win.jobs.submit("trash", paths)
        self.model.remove_paths(set(paths))
        self._update_count()

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export results", os.path.join(
            os.path.expanduser("~"), f"search-{time.strftime('%Y%m%d-%H%M')}.csv"), "CSV (*.csv)")
        if path:
            try:
                se.export_csv([self.model.hit(r) for r in range(self.model.rowCount())], path)
            except OSError as e:
                QMessageBox.warning(self, APP_NAME, f"Couldn't save: {e.strerror or e}")

    def _context_menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        hits = self.selected_hits() or [self.model.hit(idx.row())]
        h = self.model.hit(idx.row())
        if h not in hits:
            hits = [h]
        m = QMenu(self)
        n = len(hits)
        real = self._real_paths(hits)
        m.addAction("Open" + (f" ({n})" if n > 1 else ""), lambda: self.open_hits(hits))
        if not h.is_dir and not h.member:
            m.addAction("View (F3)", lambda: self.view_hits(h))
        m.addAction("Show in GSExplorer+" + (" (the archive)" if h.member else ""), lambda: self.show_in_pane(h))
        m.addAction("Open folder in a new tab", lambda: self.open_folder_tab(h))
        folder = (h.archive and os.path.dirname(h.archive)) or (h.path if h.is_dir else os.path.dirname(h.path))
        m.addAction("Open terminal in the folder", lambda: self.win.open_terminal(folder))
        m.addSeparator()
        m.addAction("Copy path" + ("s" if n > 1 else ""), lambda: self.copy_paths(hits))
        if real:
            m.addAction("Copy to other pane…", lambda: self.transfer("copy", hits))
            m.addAction("Move to other pane…", lambda: self.transfer("move", hits))
            m.addAction("Compress…", lambda: self.win.compress(real))
            m.addSeparator()
            m.addAction("Move to trash" + (f" ({len(real)})" if len(real) > 1 else ""), lambda: self.trash(hits))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, ev):
        if obj is self.view and ev.type() == ev.Type.ShortcutOverride and (
                ev.key() in (Qt.Key_Delete, Qt.Key_F3, Qt.Key_Return, Qt.Key_Enter) or ev.matches(QKeySequence.Copy)):
            ev.accept()                                 # these keys belong to the results list, never to
            return True                                 # a shortcut of another window
        if obj is self.view and ev.type() == ev.Type.KeyPress:
            k = ev.key()
            cur = self.view.currentIndex()
            if k in (Qt.Key_Return, Qt.Key_Enter) and cur.isValid():
                self.open_hits(self.selected_hits() or [self.model.hit(cur.row())])
                return True
            if k == Qt.Key_F3 and cur.isValid():
                self.view_hits(self.model.hit(cur.row()))
                return True
            if k == Qt.Key_Delete and self.selected_hits():
                self.trash(self.selected_hits())
                return True
            if ev.matches(QKeySequence.Copy):
                self.copy_paths(self.selected_hits())
                return True
        return super().eventFilter(obj, ev)

    def closeEvent(self, ev):
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(5000)
        try:
            self.cfg["search_last"] = self._state()
            self.cfg.save()
        except OSError:
            pass
        self.preview.show_entry(None)
        self.preview.media.stop()
        super().closeEvent(ev)
