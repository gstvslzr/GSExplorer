# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Directory listing model with background scanning, Python-side sort/filter.

Scanning (os.scandir + lstat) runs in a QThreadPool worker; the UI thread only
receives finished batches, so a 100k-entry directory never blocks the window.
Sorting and filtering are done on Python lists (one key per entry, computed
once) instead of QSortFilterProxyModel, which would call back into Python for
every comparison.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import time

from PySide6.QtCore import (QAbstractTableModel, QMimeData, QModelIndex, QObject,
                            QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal,
                            QFileSystemWatcher)
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QApplication

from .fsutil import display_name, human_size, perm_string
from .icons import IconCache

INTERNAL_MIME = "application/x-gsexplorer-plus-paths"
BATCH = 4000
COLUMNS = ("Name", "Size", "Type", "Modified", "Permissions")
_num_re = re.compile(r"(\d+)")


def natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in _num_re.split(s.casefold())]


class Entry:
    __slots__ = ("name", "path", "disp", "is_dir", "is_link", "broken", "size", "mtime",
                 "mode", "uid", "gid", "hidden", "nkey", "ext", "parent_link")

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.disp = display_name(name)
        self.is_dir = False
        self.is_link = False
        self.broken = False
        self.size = 0
        self.mtime = 0.0
        self.mode = 0
        self.uid = 0
        self.gid = 0
        self.hidden = name.startswith(".")
        self.nkey = natural_key(self.disp)
        self.ext = os.path.splitext(self.disp)[1][1:].casefold()
        self.parent_link = False


def make_entry(directory: str, name: str, dirent=None) -> Entry | None:
    path = os.path.join(directory, name)
    e = Entry(name, path)
    try:
        st = dirent.stat(follow_symlinks=False) if dirent is not None else os.lstat(path)
    except OSError:
        return None if dirent is None else e
    e.mode = st.st_mode
    e.size = st.st_size
    e.mtime = st.st_mtime
    e.uid = st.st_uid
    e.gid = st.st_gid
    if stat.S_ISLNK(st.st_mode):
        e.is_link = True
        try:
            tst = os.stat(path)
            e.is_dir = stat.S_ISDIR(tst.st_mode)
            if not e.is_dir:
                e.size = tst.st_size
        except OSError:
            e.broken = True
    else:
        e.is_dir = stat.S_ISDIR(st.st_mode)
    if e.is_dir:
        e.size = -1
    return e


class _ScanSignals(QObject):
    batch = Signal(int, object)
    done = Signal(int, str)


class _Scanner(QRunnable):
    def __init__(self, gen: int, path: str, signals: _ScanSignals):
        super().__init__()
        self.gen, self.path, self.sig = gen, path, signals
        self.cancelled = False
        self.setAutoDelete(True)

    def run(self) -> None:
        out: list[Entry] = []
        err = ""
        try:
            with os.scandir(self.path) as it:
                for d in it:
                    if self.cancelled:
                        return
                    e = make_entry(self.path, d.name, d)
                    if e is not None:
                        out.append(e)
        except OSError as ex:
            err = ex.strerror or str(ex)
        if not self.cancelled:
            # deliver in one shot; the model sorts once and inserts in chunks
            self.sig.batch.emit(self.gen, out)
            self.sig.done.emit(self.gen, err)


class DirModel(QAbstractTableModel):
    loading_changed = Signal(bool)
    load_error = Signal(str)
    loaded = Signal()
    drop_requested = Signal(list, str, object)   # paths, target_dir, Qt.DropAction

    def __init__(self, icons: IconCache, parent=None):
        super().__init__(parent)
        self.icons = icons
        self.path = ""
        self.all: list[Entry] = []
        self.rows: list[Entry] = []
        self.show_hidden = False
        self.filter_text = ""
        self.sort_col = 0
        self.sort_order = Qt.AscendingOrder
        self._gen = 0
        self._scanner: _Scanner | None = None
        self._sig = _ScanSignals()
        self._sig.batch.connect(self._on_batch)
        self._sig.done.connect(self._on_done)
        self.loading = False
        self.error = ""
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self.refresh)
        self.watch_ok = True
        self.thumbs = None              # ThumbManager, set by the view
        self.thumb_px = 0
        self._thumb_icons: dict = {}
        self._index: dict = {}

    # ------------------------------------------------------------------ loading
    def set_path(self, path: str) -> None:
        if self.path and self.path in self.watcher.directories():
            self.watcher.removePath(self.path)
        self.path = path
        self._start_scan(clear=True)
        self.watch_ok = bool(self.watcher.addPath(path))  # False if inotify limit hit

    def refresh(self) -> None:
        if self.path:
            self._start_scan(clear=False)

    def _start_scan(self, clear: bool) -> None:
        if self._scanner is not None:
            self._scanner.cancelled = True
        self._gen += 1
        if clear:
            self.beginResetModel()
            self.all, self.rows, self._index = [], [], {}
            self.endResetModel()
        self.error = ""
        self.loading = True
        self.loading_changed.emit(True)
        self._scanner = _Scanner(self._gen, self.path, self._sig)
        QThreadPool.globalInstance().start(self._scanner)

    def _on_batch(self, gen: int, entries: list) -> None:
        if gen != self._gen:
            return
        self.all = entries
        self._rebuild()

    def _on_done(self, gen: int, err: str) -> None:
        if gen != self._gen:
            return
        self._scanner = None
        self.loading = False
        self.error = err
        self.loading_changed.emit(False)
        if err:
            self.load_error.emit(err)
        self.loaded.emit()

    def _on_dir_changed(self, _p: str) -> None:
        self._debounce.start()

    # ------------------------------------------------------------------ sort / filter
    def _parent_entry(self) -> Entry | None:
        if not self.path or self.path == "/":
            return None
        e = Entry("..", os.path.dirname(self.path.rstrip("/")) or "/")
        e.is_dir = True
        e.size = -1
        e.parent_link = True
        e.disp = ".."
        return e

    def _key(self):
        c = self.sort_col
        if c == 1:
            return lambda e: (e.size, e.nkey)
        if c == 2:
            return lambda e: (e.ext, e.nkey)
        if c == 3:
            return lambda e: (e.mtime, e.nkey)
        if c == 4:
            return lambda e: (e.mode, e.nkey)
        return lambda e: e.nkey

    def _rebuild(self) -> None:
        items = self.all
        if not self.show_hidden:
            items = [e for e in items if not e.hidden]
        ft = self.filter_text.strip()
        if ft:
            if any(ch in ft for ch in "*?["):
                pat = ft.casefold()
                items = [e for e in items if fnmatch.fnmatchcase(e.disp.casefold(), pat)]
            else:
                f = ft.casefold()
                items = [e for e in items if f in e.disp.casefold()]
        key = self._key()
        rev = self.sort_order == Qt.DescendingOrder
        dirs = sorted((e for e in items if e.is_dir), key=key, reverse=rev)
        files = sorted((e for e in items if not e.is_dir), key=key, reverse=rev)
        if self.sort_col == 1:  # size sort: dirs keep name order
            dirs.sort(key=lambda e: e.nkey, reverse=rev)
        rows = dirs + files
        p = self._parent_entry()
        if p is not None:
            rows.insert(0, p)
        # Row count may change, so this is a reset; the view saves/restores
        # selection, current item and scroll position around it.
        self.beginResetModel()
        self.rows = rows
        self._index = {e.path: n for n, e in enumerate(rows)}
        self._thumb_icons.clear()
        self.endResetModel()

    def row_of(self, path: str) -> int:
        return self._index.get(path, -1)

    # ------------------------------------------------------------------ thumbnails
    def set_thumbnails(self, manager, px: int) -> None:
        """px > 0 shows thumbnails at that size (icon view); 0 turns them off."""
        if self.thumbs is not manager and manager is not None:
            self.thumbs = manager
            manager.ready.connect(self._thumb_ready)
        if px != self.thumb_px:
            self.thumb_px = px
            self._thumb_icons.clear()
            if self.rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0),
                                      [Qt.DecorationRole])

    def _thumb_ready(self, path: str) -> None:
        r = self._index.get(path)
        if r is not None and self.thumb_px:
            self._thumb_icons.pop(path, None)
            i = self.index(r, 0)
            self.dataChanged.emit(i, i, [Qt.DecorationRole])

    def _thumb_icon(self, e: Entry):
        ic = self._thumb_icons.get(e.path)
        if ic is not None:
            return ic
        img = self.thumbs.get(e.path, e.mtime, self.thumb_px)
        if img is None:
            return None
        from PySide6.QtGui import QIcon, QPixmap
        ic = QIcon(QPixmap.fromImage(img))
        self._thumb_icons[e.path] = ic
        return ic

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        self.sort_col, self.sort_order = column, order
        self._rebuild()

    def set_show_hidden(self, on: bool) -> None:
        self.show_hidden = on
        self._rebuild()

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._rebuild()

    # ------------------------------------------------------------------ Qt model API
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole and section == 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def entry(self, index: QModelIndex) -> Entry | None:
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        return self.rows[index.row()]

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self.rows[index.row()]
        c = index.column()
        if role == Qt.DisplayRole or role == Qt.EditRole:
            if c == 0:
                return e.disp
            if c == 1:
                if e.parent_link:
                    return ""
                return "<DIR>" if e.is_dir else human_size(e.size)
            if c == 2:
                if e.parent_link:
                    return ""
                return self.icons.type_label(e)
            if c == 3:
                return "" if e.parent_link else time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime))
            if c == 4:
                return "" if e.parent_link else perm_string(e.mode)
        elif role == Qt.DecorationRole and c == 0:
            if self.thumb_px and self.thumbs is not None and not e.is_dir and not e.broken \
                    and not e.parent_link:
                ic = self._thumb_icon(e)
                if ic is not None:
                    return ic
            return self.icons.for_entry(e)
        elif role == Qt.TextAlignmentRole and c == 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole and (e.hidden or e.broken) and not e.parent_link:
            return QApplication.palette().color(QPalette.PlaceholderText)
        elif role == Qt.ToolTipRole and c == 0 and not e.parent_link:
            tip = e.disp
            if e.is_link:
                try:
                    tip += "  →  " + display_name(os.readlink(e.path))
                except OSError:
                    pass
                if e.broken:
                    tip += "  (broken link)"
            return tip
        elif role == Qt.UserRole:
            return e
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        e = self.rows[index.row()]
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if not e.parent_link:
            f |= Qt.ItemIsDragEnabled
        if e.is_dir:
            f |= Qt.ItemIsDropEnabled
        return f

    # ------------------------------------------------------------------ drag & drop
    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def supportedDragActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def mimeTypes(self):
        return [INTERNAL_MIME, "text/uri-list"]

    def mimeData(self, indexes):
        paths = []
        seen = set()
        for i in indexes:
            e = self.entry(i)
            if e and not e.parent_link and e.path not in seen:
                seen.add(e.path)
                paths.append(e.path)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        md.setData(INTERNAL_MIME, json.dumps(paths, ensure_ascii=True).encode("ascii"))
        return md

    @staticmethod
    def paths_from_mime(md: QMimeData) -> list[str]:
        if md.hasFormat(INTERNAL_MIME):
            try:
                return [str(p) for p in json.loads(bytes(md.data(INTERNAL_MIME)).decode("ascii"))]
            except ValueError:
                pass
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()]

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(INTERNAL_MIME) or data.hasUrls()

    def dropMimeData(self, data, action, row, column, parent):
        paths = self.paths_from_mime(data)
        if not paths:
            return False
        target = self.path
        e = self.entry(parent)
        if e is not None and e.is_dir:
            target = e.path
        # Don't let Qt remove source rows itself; the job queue handles the move.
        self.drop_requested.emit(paths, target, action)
        return False
