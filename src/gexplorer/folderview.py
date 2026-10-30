"""One folder tab: breadcrumb + details/icon view + quick filter + status line."""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QStackedWidget, QToolButton, QTreeView, QVBoxLayout,
                               QWidget, QHeaderView, QStyle)

from .dirmodel import DirModel, Entry
from .fsutil import display_path, human_size, is_trash_files_dir
from .icons import themed
from .widgets import Breadcrumb


class _TreeView(QTreeView):
    def __init__(self, owner: "FolderView"):
        super().__init__()
        self.owner = owner

    def startDrag(self, actions):
        # never let Qt remove rows itself after a move-drag; the job queue does it
        super().startDrag(actions)


class FolderView(QWidget):
    path_changed = Signal(str)
    focused = Signal()
    selection_changed = Signal()

    def __init__(self, win, path: str, view_mode: str = "details", show_hidden: bool = False):
        super().__init__()
        self._ready = False
        self.win = win
        self.path = ""
        self.back_stack: list[str] = []
        self.fwd_stack: list[str] = []
        self._pending_select: str | None = None
        self._saved: tuple | None = None

        self.model = DirModel(win.icons, self)
        self.model.show_hidden = show_hidden
        self.model.drop_requested.connect(win.handle_drop)
        self.model.loading_changed.connect(lambda _b: self._update_status())
        self.model.loaded.connect(self._on_loaded)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.crumb = Breadcrumb()
        self.crumb.navigate.connect(self.navigate)
        self.crumb.nav_back.connect(self.go_back)
        self.crumb.nav_forward.connect(self.go_forward)
        self.crumb.nav_up.connect(self.go_up)
        lay.addWidget(self.crumb)

        self.message = QLabel()
        self.message.setObjectName("Danger")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.hide()

        # details view
        self.tree = _TreeView(self)
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setIconSize(QSize(18, 18))
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setMinimumSectionSize(48)
        for c, w in ((1, 78), (2, 132), (3, 128), (4, 92)):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
            hdr.resizeSection(c, w)
        hidden_cols = win.cfg["hidden_columns"] if isinstance(win.cfg["hidden_columns"], list) else [4]
        for c in hidden_cols:
            if 0 < c < 5:
                hdr.setSectionHidden(c, True)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        hdr.setSortIndicatorShown(True)
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._header_menu)

        # icon view shares model + selection model
        self.icons = QListView()
        self.icons.setModel(self.model)
        self.icons.setSelectionModel(self.tree.selectionModel())
        self.icons.setViewMode(QListView.IconMode)
        self.icons.setResizeMode(QListView.Adjust)
        self.icons.setMovement(QListView.Static)
        self.icons.setUniformItemSizes(True)
        self.icons.setWordWrap(True)
        self.icons.setIconSize(QSize(48, 48))
        self.icons.setGridSize(QSize(104, 92))
        self.icons.setSpacing(4)
        self.icons.setTextElideMode(Qt.ElideMiddle)

        for v in (self.tree, self.icons):
            v.setSelectionMode(QAbstractItemView.ExtendedSelection)
            v.setSelectionBehavior(QAbstractItemView.SelectRows)
            v.setEditTriggers(QAbstractItemView.NoEditTriggers)
            v.setDragEnabled(True)
            v.setAcceptDrops(True)
            v.setDropIndicatorShown(True)
            v.setDragDropMode(QAbstractItemView.DragDrop)
            v.setDefaultDropAction(Qt.CopyAction)
            v.setContextMenuPolicy(Qt.CustomContextMenu)
            v.customContextMenuRequested.connect(self._context_menu)
            v.activated.connect(self._activated)
            v.installEventFilter(self)
            v.viewport().installEventFilter(self)
        # Connect AFTER setModel(): the view and selection model clear themselves on
        # modelReset, so our restore must run after theirs.
        self.model.modelAboutToBeReset.connect(self._save_state)
        self.model.modelReset.connect(self._restore_state)
        self.tree.selectionModel().selectionChanged.connect(lambda *_: self._on_sel())
        self.tree.selectionModel().currentChanged.connect(lambda *_: self.selection_changed.emit())

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.icons)
        self.stack.addWidget(self.message)
        lay.addWidget(self.stack, 1)

        # quick filter (Ctrl+S)
        self.filter_bar = QFrame()
        self.filter_bar.setObjectName("FilterBar")
        fl = QHBoxLayout(self.filter_bar)
        fl.setContentsMargins(6, 4, 6, 4)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter: text or *.pattern  (Esc to clear)")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._filter_changed)
        self.filter_edit.installEventFilter(self)
        close = self.filter_close = QToolButton()
        close.setIcon(themed(["window-close"], QStyle.SP_DialogCloseButton))
        close.setAutoRaise(True)
        close.clicked.connect(self.hide_filter)
        fl.addWidget(self.filter_edit, 1)
        fl.addWidget(close)
        self.filter_bar.hide()
        lay.addWidget(self.filter_bar)

        self.status = QLabel()
        self.status.setObjectName("PaneStatus")
        lay.addWidget(self.status)

        self.set_view_mode(view_mode)
        self._ready = True
        self.navigate(path)

    # ------------------------------------------------------------------ state
    @property
    def view(self) -> QAbstractItemView:
        return self.icons if self.view_mode == "icons" else self.tree

    ICON_SIZES = (48, 64, 96, 128, 192, 256)

    def set_view_mode(self, mode: str) -> None:
        self.view_mode = "icons" if mode == "icons" else "details"
        self._apply_icon_size()
        if self.message.isVisible() and self.stack.currentWidget() is self.message:
            return
        self.stack.setCurrentWidget(self.view)

    def _apply_icon_size(self) -> None:
        px = int(self.win.cfg["icon_size"] or 96)
        px = min(self.ICON_SIZES, key=lambda s: abs(s - px))
        self.icons.setIconSize(QSize(px, px))
        self.icons.setGridSize(QSize(max(104, px + 40), px + 48))
        self.model.set_thumbnails(self.win.thumbs, px if self.view_mode == "icons" else 0)

    def zoom(self, step: int) -> None:
        """Ctrl+wheel / View > Zoom: change icon size (and thumbnail size) in icon view."""
        cur = int(self.win.cfg["icon_size"] or 96)
        sizes = self.ICON_SIZES
        i = min(range(len(sizes)), key=lambda k: abs(sizes[k] - cur))
        i = max(0, min(len(sizes) - 1, i + step))
        self.win.cfg["icon_size"] = sizes[i]
        for pane in (self.win.left, self.win.right):
            for v in pane.views():
                v._apply_icon_size()
        self.win.flash(f"Icon size {sizes[i]} px")

    def is_trash(self) -> str | None:
        return is_trash_files_dir(self.path)

    # ------------------------------------------------------------------ navigation
    def navigate(self, path: str, add_history: bool = True, select: str | None = None) -> None:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(path) or (os.path.lexists(path) and not os.path.isdir(path)):
            select = path
            path = os.path.dirname(path)
        orig = path
        while not os.path.isdir(path) and path != "/":
            path = os.path.dirname(path)
        if path != orig:
            self.win.flash(f"“{display_path(orig)}” doesn't exist; opened the nearest folder.")
        if path == self.path and select is None:
            self.model.refresh()
            return
        if add_history and self.path and path != self.path:
            self.back_stack.append(self.path)
            del self.back_stack[:-100]
            self.fwd_stack.clear()
        prev = self.path
        self.path = path
        if select is None and prev and os.path.dirname(prev) == path:
            select = prev            # going up: land on the folder we came from
        self._pending_select = select or ""
        self._saved = None
        self.message.hide()
        self.stack.setCurrentWidget(self.view)
        self.win.thumbs.cancel_pending()
        self.model.set_path(path)
        self.crumb.set_path(path)
        self._update_nav()
        self.path_changed.emit(path)
        self._update_status()

    def go_back(self) -> None:
        if self.back_stack:
            self.fwd_stack.append(self.path)
            self.navigate(self.back_stack.pop(), add_history=False)

    def go_forward(self) -> None:
        if self.fwd_stack:
            self.back_stack.append(self.path)
            self.navigate(self.fwd_stack.pop(), add_history=False)

    def go_up(self) -> None:
        if self.path != "/":
            self.navigate(os.path.dirname(self.path))

    def refresh(self) -> None:
        self.model.refresh()

    def _update_nav(self) -> None:
        self.crumb.set_nav_state(bool(self.back_stack), bool(self.fwd_stack), self.path != "/")

    # ------------------------------------------------------------------ selection
    def selected_entries(self, use_current: bool = True) -> list[Entry]:
        rows = self.tree.selectionModel().selectedRows(0)
        out = [self.model.rows[i.row()] for i in sorted(rows, key=lambda i: i.row())
               if i.row() < len(self.model.rows) and not self.model.rows[i.row()].parent_link]
        if not out and use_current:
            e = self.model.entry(self.view.currentIndex())
            if e is not None and not e.parent_link:
                out = [e]
        return out

    def current_entry(self) -> Entry | None:
        return self.model.entry(self.view.currentIndex())

    def select_path(self, path: str) -> bool:
        r = self.model.row_of(path)
        if r < 0:
            return False
        idx = self.model.index(r, 0)
        sm = self.tree.selectionModel()
        sm.setCurrentIndex(idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self.view.scrollTo(idx, QAbstractItemView.EnsureVisible)
        return True

    def select_all(self) -> None:
        self.view.selectAll()
        if self.model.rows and self.model.rows[0].parent_link:
            self.tree.selectionModel().select(self.model.index(0, 0),
                                              QItemSelectionModel.Deselect | QItemSelectionModel.Rows)

    def invert_selection(self) -> None:
        sm = self.tree.selectionModel()
        n = self.model.rowCount()
        if not n:
            return
        sel = QItemSelection(self.model.index(0, 0), self.model.index(n - 1, self.model.columnCount() - 1))
        sm.select(sel, QItemSelectionModel.Toggle | QItemSelectionModel.Rows)
        if self.model.rows[0].parent_link:
            sm.select(self.model.index(0, 0), QItemSelectionModel.Deselect | QItemSelectionModel.Rows)

    def select_pattern(self, pattern: str, select: bool = True) -> int:
        import fnmatch
        sm = self.tree.selectionModel()
        sel = QItemSelection()
        n = 0
        pat = pattern.casefold()
        for r, e in enumerate(self.model.rows):
            if not e.parent_link and fnmatch.fnmatchcase(e.disp.casefold(), pat):
                sel.select(self.model.index(r, 0), self.model.index(r, 0))
                n += 1
        sm.select(sel, (QItemSelectionModel.Select if select else QItemSelectionModel.Deselect)
                  | QItemSelectionModel.Rows)
        return n

    def _save_state(self) -> None:
        sm = self.tree.selectionModel()
        # _pending_select is not None while a navigation is in flight: nothing to keep.
        if self._pending_select is None and self._saved is None and self.model.rows:
            sel = {self.model.rows[i.row()].path for i in sm.selectedRows(0) if i.row() < len(self.model.rows)}
            cur = self.model.entry(self.view.currentIndex())
            self._saved = (sel, cur.path if cur else None,
                           self.view.verticalScrollBar().value(), self.path)

    def _restore_state(self) -> None:
        saved, self._saved = self._saved, None
        if not self.model.rows:
            self._saved = saved   # still loading; keep for the populated reset
            return
        sm = self.tree.selectionModel()
        if saved and saved[3] == self.path and self._pending_select is None:
            sel_paths, cur, scroll, _ = saved
            pos = {e.path: n for n, e in enumerate(self.model.rows)}
            qs = QItemSelection()
            for p in sel_paths:
                r = pos.get(p)
                if r is not None:
                    qs.select(self.model.index(r, 0), self.model.index(r, 0))
            if cur in pos:
                sm.setCurrentIndex(self.model.index(pos[cur], 0), QItemSelectionModel.NoUpdate)
            elif self.model.rows:
                sm.setCurrentIndex(self.model.index(0, 0), QItemSelectionModel.NoUpdate)
            sm.select(qs, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            QTimer.singleShot(0, lambda s=scroll: self.view.verticalScrollBar().setValue(s))
            return
        target = self._pending_select
        self._pending_select = None
        if target and self.select_path(target):
            return
        idx = self.model.index(0, 0)
        sm.setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
        self.view.scrollToTop()

    def _on_loaded(self) -> None:
        if self.model.error:
            self.message.setText(f"Can't open “{display_path(self.path)}”\n{self.model.error}")
            self.stack.setCurrentWidget(self.message)
        self._update_status()

    def _on_sel(self) -> None:
        self._update_status()
        self.selection_changed.emit()

    # ------------------------------------------------------------------ filter
    def show_filter(self) -> None:
        self.filter_bar.show()
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def hide_filter(self) -> None:
        self.filter_edit.clear()
        self.filter_bar.hide()
        self.view.setFocus()

    def _filter_changed(self, text: str) -> None:
        self.model.set_filter(text)
        self._update_status()

    def set_show_hidden(self, on: bool) -> None:
        self.model.set_show_hidden(on)

    # ------------------------------------------------------------------ status
    def _update_status(self) -> None:
        if self.model.loading and not self.model.rows:
            self.status.setText("Loading…")
            return
        rows = [e for e in self.model.rows if not e.parent_link]
        nd = sum(1 for e in rows if e.is_dir)
        nf = len(rows) - nd
        sel = self.selected_entries(use_current=False)
        parts = [f"{nd} folder{'s' if nd != 1 else ''}, {nf} file{'s' if nf != 1 else ''}"]
        hidden = len(self.model.all) - len(rows) if not self.model.filter_text else 0
        if hidden > 0 and not self.model.show_hidden:
            parts[0] += f" ({hidden} hidden)"
        if self.model.filter_text:
            parts.append(f"filter “{self.model.filter_text}”")
        if sel:
            sz = sum(e.size for e in sel if not e.is_dir and e.size > 0)
            parts.append(f"{len(sel)} selected ({human_size(sz)})")
        try:
            parts.append(f"{human_size(shutil.disk_usage(self.path).free)} free")
        except OSError:
            pass
        if not self.model.watch_ok:
            parts.append("auto-refresh off (Ctrl+R)")
        self.status.setText("  ·  ".join(parts))

    # ------------------------------------------------------------------ activation / keys
    def _activated(self, index: QModelIndex) -> None:
        e = self.model.entry(index)
        if e is None:
            return
        if e.parent_link:
            self.go_up()
        else:
            self.win.open_entry(self, e)

    def _header_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        hdr = self.tree.header()
        for c in range(1, self.model.columnCount()):
            a = m.addAction(self.model.headerData(c, Qt.Horizontal))
            a.setCheckable(True)
            a.setChecked(not hdr.isSectionHidden(c))
            a.toggled.connect(lambda on, c=c: self.win.set_column_visible(c, on))
        m.exec(hdr.mapToGlobal(pos))

    def _context_menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            self.tree.selectionModel().clearSelection()
        elif not self.tree.selectionModel().isRowSelected(idx.row(), QModelIndex()):
            self.tree.selectionModel().setCurrentIndex(
                idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self.win.show_context_menu(self, self.view.viewport().mapToGlobal(pos), idx.isValid())

    def eventFilter(self, obj, ev):
        if not self._ready:
            return False
        t = ev.type()
        if t == ev.Type.FocusIn and obj in (self.tree, self.icons):
            self.focused.emit()
        elif t == ev.Type.MouseButtonPress and obj in (self.tree.viewport(), self.icons.viewport()):
            self.focused.emit()
            if ev.button() == Qt.BackButton:
                self.go_back()
                return True
            if ev.button() == Qt.ForwardButton:
                self.go_forward()
                return True
        elif t == ev.Type.Wheel and obj is self.icons.viewport() and ev.modifiers() & Qt.ControlModifier:
            self.zoom(1 if ev.angleDelta().y() > 0 else -1)
            return True
        elif t == ev.Type.MouseButtonRelease and obj in (self.tree.viewport(), self.icons.viewport()) \
                and ev.button() == Qt.MiddleButton:
            e = self.model.entry(self.view.indexAt(ev.position().toPoint()))
            if e is not None and e.is_dir:
                self.win.open_in_new_tab(e.path)
                return True
        elif t == ev.Type.KeyPress and obj in (self.tree, self.icons):
            k, mods = ev.key(), ev.modifiers()
            if k == Qt.Key_Tab and mods == Qt.NoModifier:
                self.win.switch_pane()
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter) and mods == Qt.NoModifier:
                self._activated(self.view.currentIndex())
                return True
            if k == Qt.Key_Backspace and mods == Qt.NoModifier:
                self.go_up()
                return True
            if k == Qt.Key_Insert and mods == Qt.NoModifier:
                cur = self.view.currentIndex()
                if cur.isValid():
                    e = self.model.entry(cur)
                    if e and not e.parent_link:
                        self.tree.selectionModel().select(cur, QItemSelectionModel.Toggle | QItemSelectionModel.Rows)
                    nxt = self.model.index(min(cur.row() + 1, self.model.rowCount() - 1), 0)
                    self.tree.selectionModel().setCurrentIndex(nxt, QItemSelectionModel.NoUpdate)
                return True
            if k == Qt.Key_Escape and self.model.filter_text:
                self.hide_filter()
                return True
        elif obj is self.filter_edit and t == ev.Type.KeyPress:
            if ev.key() == Qt.Key_Escape:
                self.hide_filter()
                return True
            if ev.key() in (Qt.Key_Down, Qt.Key_Return, Qt.Key_Enter):
                self.view.setFocus()
                if not self.view.currentIndex().isValid() and self.model.rowCount():
                    self.view.setCurrentIndex(self.model.index(0, 0))
                return True
        return super().eventFilter(obj, ev)

    def focus_view(self) -> None:
        self.view.setFocus()

    def apply_chrome_icons(self) -> None:
        ci = self.win.chrome_icon
        self.crumb.btn_back.setIcon(ci("back"))
        self.crumb.btn_fwd.setIcon(ci("forward"))
        self.crumb.btn_up.setIcon(ci("up"))
        self.filter_close.setIcon(ci("close"))
