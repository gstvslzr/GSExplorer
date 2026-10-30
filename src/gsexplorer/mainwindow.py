# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window: sidebar + two tabbed panes + command line + F-key bar."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from PySide6.QtCore import QMimeData, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter, QStyle,
                               QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, RDNN_ID, VERSION, dialogs, fsutil, system, themes, uiicons
from .config import Config, themes_dir
from .dirmodel import DirModel, Entry
from .fileops import JobManager
from .folderview import FolderView
from .icons import IconCache, themed
from .preview import PreviewPanel
from .connectui import ConnectController
from .sidebar import Sidebar
from .thumbs import ThumbManager
from .widgets import FKeyBar

GNOME_COPIED = "x-special/gnome-copied-files"


class Pane(QFrame):
    """A tabbed pane; each tab is a FolderView."""

    def __init__(self, win: "MainWindow", side: str):
        super().__init__()
        self.win, self.side = win, side
        self.setObjectName("PaneFrame")
        self.setProperty("active", False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)
        newtab = self.newtab_btn = QToolButton()
        newtab.setToolTip("New tab (Ctrl+T)")
        newtab.setAutoRaise(True)
        newtab.setFocusPolicy(Qt.NoFocus)
        newtab.clicked.connect(lambda: self.add_tab(self.current().path if self.current() else "~"))
        self.tabs.setCornerWidget(newtab, Qt.TopRightCorner)
        lay.addWidget(self.tabs)

    def current(self) -> FolderView | None:
        return self.tabs.currentWidget()

    def views(self) -> list[FolderView]:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def add_tab(self, path: str, activate: bool = True) -> FolderView:
        cfg = self.win.cfg
        v = FolderView(self.win, path, cfg["view_mode"], cfg["show_hidden"])
        v.path_changed.connect(lambda _p, v=v: self._update_title(v))
        v.focused.connect(lambda: self.win.set_active(self))
        v.selection_changed.connect(self.win.update_actions)
        if hasattr(self.win, "theme_btn"):
            v.apply_chrome_icons()
        i = self.tabs.addTab(v, "")
        self.tabs.setTabsClosable(self.tabs.count() > 1)
        self._update_title(v)
        if activate:
            self.tabs.setCurrentIndex(i)
        return v

    def close_tab(self, i: int) -> None:
        if self.tabs.count() <= 1:
            return
        w = self.tabs.widget(i)
        self.tabs.removeTab(i)
        w.deleteLater()
        self.tabs.setTabsClosable(self.tabs.count() > 1)

    def _update_title(self, v: FolderView) -> None:
        i = self.tabs.indexOf(v)
        if i < 0:
            return
        name = os.path.basename(v.path.rstrip("/")) or "/"
        if v.path == os.path.expanduser("~"):
            name = "Home"
        elif v.is_trash():
            name = "Trash"
        self.tabs.setTabText(i, fsutil.display_name(name))
        self.tabs.setTabToolTip(i, fsutil.display_path(v.path))
        if v is self.current():
            self.win.update_title()

    def _tab_changed(self, _i: int) -> None:
        self.win.update_title()
        self.win.update_actions()

    def _tab_menu(self, pos) -> None:
        bar = self.tabs.tabBar()
        i = bar.tabAt(pos)
        if i < 0:
            return
        m = QMenu(self)
        m.addAction("Duplicate tab", lambda: self.add_tab(self.tabs.widget(i).path))
        m.addAction("Open in other pane", lambda: self.win.other_pane(self).add_tab(self.tabs.widget(i).path))
        m.addSeparator()
        a = m.addAction("Close tab", lambda: self.close_tab(i))
        a.setEnabled(self.tabs.count() > 1)
        b = m.addAction("Close other tabs", lambda: self._close_others(i))
        b.setEnabled(self.tabs.count() > 1)
        m.exec(bar.mapToGlobal(pos))

    def _close_others(self, keep: int) -> None:
        w = self.tabs.widget(keep)
        for j in reversed(range(self.tabs.count())):
            if self.tabs.widget(j) is not w:
                self.close_tab(j)

    def set_active_look(self, on: bool) -> None:
        self.setProperty("active", on)
        self.style().unpolish(self)
        self.style().polish(self)


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, start_path: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.icons = IconCache()
        self.jobs = JobManager(self)
        self.jobs.conflict.connect(self._on_conflict)
        self.jobs.job_finished.connect(self._on_job_finished)
        self.jobs.job_updated.connect(lambda _j: self._update_queue_button())
        self.jobs.job_added.connect(lambda _j: self._update_queue_button())
        self.jobs.dirs_changed.connect(self._refresh_dirs)
        self._jobs_dlg: dialogs.JobsDialog | None = None
        self._apps_cache = None
        self.themes = themes.load_themes()
        self.thumbs = ThumbManager(self.cfg["thumbnails"], self)
        self.setWindowIcon(_app_icon())

        self._build_actions()
        self._build_ui()
        self._build_menus()
        self._restore_session(start_path)
        self.retint()
        self.update_title()
        self.update_actions()
        self._sys_scheme_hook()

    # ================================================================== chrome icons
    def chrome_icon(self, name: str) -> QIcon:
        c = themes.colors_for(self.cfg["theme"], self.themes)
        key = (name, c["text"], c["muted"])
        cache = self.__dict__.setdefault("_icon_cache", {})
        if key not in cache:
            cache[key] = uiicons.icon(name, c["text"], c["border"])
        return cache[key]

    def retint(self) -> None:
        """Re-render toolbar/path-bar icons and tab close buttons for the current theme."""
        chrome = {self.a_back: "back", self.a_fwd: "forward", self.a_up: "up", self.a_home: "home",
                  self.a_refresh: "refresh", self.a_newtab: "newtab", self.a_mkdir: "newfolder",
                  self.a_filter: "filter", self.a_search: "search", self.a_details: "details", self.a_icons: "grid",
                  self.a_hidden: "eye", self.a_dual: "dual", self.a_sidebar: "sidebar", self.a_queue: "queue",
                  self.a_sync_panes: "sync", self.a_sync: "sync",
                  self.a_preview: "preview", self.a_connect: "connect"}
        for a, n in chrome.items():
            a.setIcon(self.chrome_icon(n))
        self.theme_btn.setIcon(self.chrome_icon("theme"))
        self.icons.up = self.chrome_icon("up")
        for pane in (self.left, self.right):
            pane.newtab_btn.setIcon(self.chrome_icon("plus"))
            for v in pane.views():
                v.apply_chrome_icons()
        c = themes.colors_for(self.cfg["theme"], self.themes)
        close_svg = uiicons.write_svg("close", c["muted"])
        app = QApplication.instance()
        app.setStyleSheet(app.styleSheet() +
                          f"\nQTabBar::close-button {{ image: url({close_svg}); subcontrol-position: right;"
                          f" border-radius: 3px; padding: 1px; }}"
                          f"\nQTabBar::close-button:hover {{ background: {c['hover']}; }}\n")

    # ================================================================== UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.connector = ConnectController(self)
        self.sidebar = Sidebar(self.cfg["bookmarks"], connector=self.connector)
        self.connector.changed.connect(self.sidebar.rebuild)
        self.sidebar.open_path.connect(lambda p: self.active_view().navigate(p))
        self.sidebar.open_path_new_tab.connect(self.open_in_new_tab)
        self.sidebar.bookmarks_changed.connect(self._save_bookmarks)

        self.left = Pane(self, "left")
        self.right = Pane(self, "right")
        self.pane_split = QSplitter(Qt.Horizontal)
        self.pane_split.setChildrenCollapsible(False)
        self.pane_split.addWidget(self.left)
        self.pane_split.addWidget(self.right)
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.setChildrenCollapsible(False)
        self.main_split.addWidget(self.sidebar)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(6, 6, 6, 6)
        hl.addWidget(self.pane_split)
        self.main_split.addWidget(holder)
        self.preview = PreviewPanel(self)
        self.main_split.addWidget(self.preview)
        self.preview.setVisible(bool(self.cfg["preview_panel"]))
        self.main_split.setStretchFactor(1, 1)
        root.addWidget(self.main_split, 1)

        # command line + queue indicator
        cmd = QFrame()
        cmd.setObjectName("CmdBar")
        cl = QHBoxLayout(cmd)
        cl.setContentsMargins(8, 3, 8, 3)
        self.cmd_prompt = QLabel()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Command — Enter runs in this folder · Shift+Enter runs in a terminal · cd <dir>")
        self.cmd.installEventFilter(self)
        self.queue_btn = QPushButton("Queue: idle")
        self.queue_btn.setObjectName("QueueButton")
        self.queue_btn.setFocusPolicy(Qt.NoFocus)
        self.queue_btn.setCursor(Qt.PointingHandCursor)
        self.queue_btn.clicked.connect(self.show_jobs)
        cl.addWidget(self.cmd_prompt)
        cl.addWidget(self.cmd, 1)
        cl.addWidget(self.queue_btn)
        root.addWidget(cmd)

        self.fkeys = FKeyBar([("F2 Rename", self.a_rename), ("F3 View", self.a_view), ("F4 Edit", self.a_edit),
                              ("F5 Copy", self.a_copy_to), ("F6 Move", self.a_move_to),
                              ("F7 New folder", self.a_mkdir), ("F8 Delete", self.a_trash),
                              ("Alt+F4 Exit", self.a_quit)])
        root.addWidget(self.fkeys)
        self.setCentralWidget(central)

        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_home, self.a_refresh):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.a_newtab, self.a_mkdir, self.a_filter, self.a_search):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy())
        tb.addWidget(spacer)
        for a in (self.a_details, self.a_icons, self.a_hidden, self.a_dual, self.a_sidebar, self.a_preview):
            tb.addAction(a)
        theme_btn = self.theme_btn = QToolButton()
        theme_btn.setToolTip("Theme")
        theme_btn.setPopupMode(QToolButton.InstantPopup)
        self.theme_menu = QMenu("&Theme", self)
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        theme_btn.setMenu(self.theme_menu)
        tb.addWidget(theme_btn)
        tb.addAction(self.a_connect)
        tb.addAction(self.a_sync_panes)
        tb.addAction(self.a_queue)
        self.addToolBar(tb)
        self.statusBar().hide()

        self.sidebar.setVisible(self.cfg["show_sidebar"])
        self.right.setVisible(self.cfg["dual_pane"])

    def _act(self, text, slot, shortcut=None, icon=None, sp=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if icon or sp is not None:
            a.setIcon(themed(icon or [], sp))
        if shortcut:
            seqs = shortcut if isinstance(shortcut, list) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in seqs])
            a.setShortcutContext(Qt.WindowShortcut)
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
        elif shortcut:
            first = shortcut[0] if isinstance(shortcut, list) else shortcut
            a.setToolTip(f"{text.replace('&', '').replace('…', '')} ({first})")
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self) -> None:
        A = self._act
        self.a_back = A("&Back", lambda: self.active_view().go_back(), "Alt+Left", ["go-previous"], QStyle.SP_ArrowBack)
        self.a_fwd = A("&Forward", lambda: self.active_view().go_forward(), "Alt+Right", ["go-next"], QStyle.SP_ArrowForward)
        self.a_up = A("&Up", lambda: self.active_view().go_up(), "Alt+Up", ["go-up"], QStyle.SP_ArrowUp)
        self.a_home = A("&Home", lambda: self.active_view().navigate("~"), "Alt+Home", ["go-home", "user-home"],
                        QStyle.SP_DirHomeIcon)
        self.a_location = A("&Location…", lambda: self.active_view().crumb.start_edit(), ["Ctrl+L", "Alt+D"])
        self.a_refresh = A("&Refresh", lambda: self.active_view().refresh(), "Ctrl+R", ["view-refresh"],
                           QStyle.SP_BrowserReload)
        self.a_newtab = A("New &tab", lambda: self.active_pane().add_tab(self.active_view().path), "Ctrl+T",
                          ["tab-new"], QStyle.SP_FileDialogNewFolder)
        self.a_closetab = A("&Close tab", self._close_tab, "Ctrl+W")
        self.a_nexttab = A("Next tab", lambda: self._cycle_tab(1), "Ctrl+Tab")
        self.a_prevtab = A("Previous tab", lambda: self._cycle_tab(-1), "Ctrl+Shift+Tab")
        self.a_rename = A("&Rename…", self.rename, ["F2", "Shift+F6"])
        self.a_view = A("&View", self.view_file, "F3")
        self.a_edit = A("&Edit", self.edit_file, "F4")
        self.a_copy_to = A("&Copy to other pane…", lambda: self.transfer("copy"), "F5")
        self.a_move_to = A("&Move to other pane…", lambda: self.transfer("move"), "F6")
        self.a_mkdir = A("New &folder…", self.make_dir, ["F7", "Ctrl+Shift+N"], ["folder-new"],
                         QStyle.SP_FileDialogNewFolder)
        self.a_mkfile = A("New fi&le…", self.make_file, "Shift+F4", ["document-new"], QStyle.SP_FileIcon)
        self.a_trash = A("Move to &trash", self.trash, ["F8", "Delete"], ["user-trash"], QStyle.SP_TrashIcon)
        self.a_delete = A("&Delete permanently…", self.delete_permanently, ["Shift+Delete", "Shift+F8"],
                          ["edit-delete"])
        self.a_cut = A("Cu&t", lambda: self.clipboard_put(cut=True), "Ctrl+X", ["edit-cut"])
        self.a_copy = A("&Copy", lambda: self.clipboard_put(cut=False), "Ctrl+C", ["edit-copy"])
        self.a_paste = A("&Paste", self.paste, "Ctrl+V", ["edit-paste"])
        self.a_copypath = A("Copy &path", self.copy_path, "Ctrl+Shift+C")
        self.a_selall = A("Select &all", lambda: self.active_view().select_all(), "Ctrl+A")
        self.a_selnone = A("Deselect all", lambda: self.active_view().tree.selectionModel().clearSelection(),
                           "Ctrl+Shift+A")
        self.a_invert = A("&Invert selection", lambda: self.active_view().invert_selection(), "Ctrl+I")
        self.a_selpat = A("Select by pattern…", lambda: self.select_pattern(True), "Ctrl++")
        self.a_deselpat = A("Deselect by pattern…", lambda: self.select_pattern(False), "Ctrl+-")
        self.a_props = A("P&roperties", self.properties, "Alt+Return", ["document-properties"])
        self.a_openwith = A("Open &with…", self.open_with, None)
        self.a_terminal = A("Open &terminal here", lambda: self.open_terminal(), "F12", ["utilities-terminal"],
                            tip="Open a terminal in the folder shown in the active pane (F12)")
        self.a_search = A("&Find files…", lambda: self.open_search(), ["Ctrl+F", "Alt+F7"], ["edit-find", "system-search"],
                          tip="Find files and folders — by name, text, size, date, type (Ctrl+F)")
        self.a_compress = A("&Compress…", lambda: self.compress(), "Alt+F5", ["package-x-generic"],
                            tip="Pack the selected items into an archive (Alt+F5)")
        self.a_extract_here = A("E&xtract here", lambda: self.extract("here"))
        self.a_extract_to = A("Extract &to…", lambda: self.extract("to"), "Alt+F9",
                              tip="Unpack the selected archive(s) into a folder you choose (Alt+F9)")
        self.a_dual = A("&Dual pane", self.toggle_dual, "F9", ["view-split-left-right", "view-dual"],
                        QStyle.SP_TitleBarNormalButton, checkable=True)
        self.a_dual.setChecked(self.cfg["dual_pane"])
        self.a_sidebar = A("&Sidebar", self.toggle_sidebar, "Ctrl+B", ["view-sidetree", "sidebar-show"],
                           QStyle.SP_FileDialogListView, checkable=True)
        self.a_sidebar.setChecked(self.cfg["show_sidebar"])
        self.a_preview = A("&Preview panel", self.toggle_preview, "Alt+P", checkable=True,
                           tip="Preview panel (Alt+P)")
        self.a_preview.setChecked(bool(self.cfg["preview_panel"]))
        self.a_zoom_in = A("&Larger icons", lambda: self.active_view().zoom(1), "Ctrl+Alt+=")
        self.a_zoom_out = A("&Smaller icons", lambda: self.active_view().zoom(-1), "Ctrl+Alt+-")
        self.a_hidden = A("&Hidden files", self.toggle_hidden, "Ctrl+H", ["view-hidden", "view-visible"],
                          QStyle.SP_FileDialogContentsView, checkable=True)
        self.a_hidden.setChecked(self.cfg["show_hidden"])
        grp = QActionGroup(self)
        self.a_details = A("&Details view", lambda: self.set_view_mode("details"), "Ctrl+1",
                           ["view-list-details", "view-list"], QStyle.SP_FileDialogDetailedView, checkable=True)
        self.a_icons = A("&Icon view", lambda: self.set_view_mode("icons"), "Ctrl+2",
                         ["view-list-icons", "view-grid"], QStyle.SP_FileDialogListView, checkable=True)
        grp.addAction(self.a_details)
        grp.addAction(self.a_icons)
        (self.a_icons if self.cfg["view_mode"] == "icons" else self.a_details).setChecked(True)
        self.a_filter = A("Quick &filter", lambda: self.active_view().show_filter(), "Ctrl+S",
                          ["view-filter", "edit-find"], QStyle.SP_FileDialogContentsView)
        self.a_swap = A("S&wap panes", self.swap_panes, "Ctrl+U")
        self.a_same = A("Same folder in &both panes", self.same_folder, "Ctrl+=")
        self.a_bookmark = A("&Bookmark this folder", self.bookmark_current, "Ctrl+D", ["bookmark-new"])
        self.a_queue = A("File &operations", self.show_jobs, "Ctrl+J", ["view-process-tree", "system-run"],
                         QStyle.SP_BrowserReload)
        self.a_prefs = A("&Preferences…", self.preferences, "Ctrl+,", ["preferences-system"])
        self.a_themes_dir = A("Open themes folder", self.open_themes_dir)
        self.a_keys = A("&Keyboard shortcuts", lambda: dialogs.ShortcutsDialog(self).exec(), "Ctrl+F1")
        self.a_about = A("&About GSExplorer+", self.about)
        self.a_quit = A("&Quit", self.close, "Ctrl+Q", ["application-exit"])
        self.a_restore = A("&Restore from trash", self.restore_trash)
        self.a_empty_trash = A("&Empty trash…", self.empty_trash)
        self.a_cmdfocus = A("Command line", lambda: self.cmd.setFocus(), "Ctrl+E")
        self.a_player = A("Media &player", lambda: (self.player().show(), self.player().raise_()), "Ctrl+Shift+P")
        self.a_sync = A("&Sync && Backup…", self.open_sync, "Ctrl+Shift+B")
        self.a_dupes = A("Find &Duplicates…", lambda: self.open_dupes(), "Ctrl+Shift+D", ["edit-find"],
                         tip="Find duplicate files (Ctrl+Shift+D)")
        self.a_connect = A("Connect to &Server…", lambda: self.connector.connect_dialog(), "Ctrl+K",
                           tip="Connect to a server: SMB, SFTP, FTP, WebDAV, NFS, SSH, RDP, VNC (Ctrl+K)")
        self.a_sync_panes = A("&Compare && sync panes…", self.sync_panes, "Ctrl+Shift+S",
                              tip="Compare & sync the two pane folders (Ctrl+Shift+S)")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.a_newtab, self.a_mkdir, self.a_mkfile, None, self.a_openwith, self.a_terminal, None,
                  self.a_compress, self.a_extract_here, self.a_extract_to, None,
                  self.a_props, None, self.a_empty_trash, None, self.a_closetab, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Edit")
        for a in (self.a_cut, self.a_copy, self.a_paste, self.a_copypath, None, self.a_rename,
                  self.a_copy_to, self.a_move_to, self.a_trash, self.a_delete, None, self.a_selall,
                  self.a_selnone, self.a_invert, self.a_selpat, self.a_deselpat, None, self.a_search, None,
                  self.a_prefs):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&View")
        for a in (self.a_dual, self.a_sidebar, self.a_preview, self.a_hidden, None, self.a_zoom_in,
                  self.a_zoom_out, None, self.a_details, self.a_icons, None,
                  self.a_filter, self.a_refresh, None, self.a_swap, self.a_same):
            m.addSeparator() if a is None else m.addAction(a)
        m.addSeparator()
        m.addMenu(self.theme_menu)
        m = mb.addMenu("&Go")
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_home, self.a_location, None, self.a_bookmark):
            m.addSeparator() if a is None else m.addAction(a)
        self.bm_menu = m.addMenu("Bookmarks")
        self.bm_menu.aboutToShow.connect(self._fill_bookmarks)
        self.connect_menu = mb.addMenu("&Connect")
        self.connect_menu.aboutToShow.connect(lambda: self.connector.fill_menu(self.connect_menu))
        self.connector.fill_menu(self.connect_menu)
        m = mb.addMenu("&Tools")
        for a in (self.a_search, self.a_sync_panes, self.a_sync, self.a_dupes, self.a_player, None, self.a_queue, self.a_terminal, self.a_cmdfocus, None,
                  self.a_themes_dir):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Help")
        m.addAction(self.a_keys)
        m.addAction(self.a_about)

    # ================================================================== panes & state
    def active_pane(self) -> Pane:
        return self._active if getattr(self, "_active", None) else self.left

    def other_pane(self, pane: Pane | None = None) -> Pane:
        pane = pane or self.active_pane()
        return self.right if pane is self.left else self.left

    def active_view(self) -> FolderView:
        return self.active_pane().current()

    def set_active(self, pane: Pane) -> None:
        if getattr(self, "_active", None) is pane:
            return
        self._active = pane
        self.left.set_active_look(pane is self.left)
        self.right.set_active_look(pane is self.right and self.right.isVisible())
        self.update_title()
        self.update_actions()

    def switch_pane(self) -> None:
        if not self.right.isVisible():
            return
        target = self.other_pane()
        self.set_active(target)
        target.current().focus_view()

    def update_title(self) -> None:
        v = self.active_view() if hasattr(self, "left") and self.left.tabs.count() else None
        p = fsutil.display_path(v.path) if v else ""
        self.setWindowTitle(f"{p} — {APP_NAME} {VERSION}" if p else f"{APP_NAME} {VERSION}")
        if hasattr(self, "cmd_prompt") and v:
            short = p.replace(os.path.expanduser("~"), "~", 1) if p.startswith(os.path.expanduser("~")) else p
            self.cmd_prompt.setText(f"{short} $")

    def update_actions(self) -> None:
        v = self.active_view() if hasattr(self, "left") and self.left.tabs.count() else None
        if v is None:
            return
        sel = v.selected_entries()
        has = bool(sel)
        in_trash = bool(v.is_trash())
        for a in (self.a_rename, self.a_copy_to, self.a_move_to, self.a_trash, self.a_delete, self.a_cut,
                  self.a_copy, self.a_copypath, self.a_props):
            a.setEnabled(has)
        self.a_trash.setEnabled(has and not in_trash)
        self.a_view.setEnabled(has and not sel[0].is_dir)
        self.a_edit.setEnabled(has and not sel[0].is_dir)
        self.a_openwith.setEnabled(has and not sel[0].is_dir)
        self.a_restore.setEnabled(has and in_trash)
        if self.preview.isVisible():
            cur = v.current_entry()
            self.preview.show_entry(cur.path if cur is not None and not cur.parent_link else v.path)

    def _cycle_tab(self, d: int) -> None:
        t = self.active_pane().tabs
        if t.count() > 1:
            t.setCurrentIndex((t.currentIndex() + d) % t.count())
            t.currentWidget().focus_view()

    def _close_tab(self) -> None:
        p = self.active_pane()
        p.close_tab(p.tabs.currentIndex())

    def open_in_new_tab(self, path: str) -> None:
        v = self.active_pane().add_tab(path)
        v.focus_view()

    def flash(self, msg: str, ms: int = 4000) -> None:
        v = self.active_view() if hasattr(self, "left") and self.left.tabs.count() else None
        if v is not None:
            v.status.setText(msg)
            QTimer.singleShot(ms, v._update_status)

    def _refresh_dirs(self, dirs: list) -> None:
        # safety net for folders whose inotify watch failed
        for pane in (self.left, self.right):
            for v in pane.views():
                if v.path in dirs and not v.model.watch_ok:
                    v.refresh()
        if any("Trash" in d for d in dirs):
            self.sidebar.rebuild()

    # ================================================================== view toggles
    def toggle_dual(self) -> None:
        on = self.a_dual.isChecked()
        self.right.setVisible(on)
        self.cfg["dual_pane"] = on
        if not on and self.active_pane() is self.right:
            self.set_active(self.left)
        self.left.set_active_look(self.active_pane() is self.left)
        self.right.set_active_look(on and self.active_pane() is self.right)

    def toggle_sidebar(self) -> None:
        self.sidebar.setVisible(self.a_sidebar.isChecked())
        self.cfg["show_sidebar"] = self.a_sidebar.isChecked()

    def toggle_preview(self) -> None:
        on = self.a_preview.isChecked()
        self.cfg["preview_panel"] = on
        self.preview.setVisible(on)
        if on:
            sizes = self.main_split.sizes()
            if len(sizes) == 3 and sizes[2] < 200:
                self.main_split.setSizes([sizes[0], max(400, sizes[1] - 340), 340])
            self.update_actions()

    def toggle_hidden(self) -> None:
        on = self.a_hidden.isChecked()
        self.cfg["show_hidden"] = on
        for pane in (self.left, self.right):
            for v in pane.views():
                v.set_show_hidden(on)

    def set_view_mode(self, mode: str) -> None:
        self.cfg["view_mode"] = mode
        v = self.active_view()
        v.set_view_mode(mode)
        v.focus_view()

    def set_column_visible(self, col: int, on: bool) -> None:
        hidden = set(self.cfg["hidden_columns"] or [])
        (hidden.discard if on else hidden.add)(col)
        self.cfg["hidden_columns"] = sorted(hidden)
        for pane in (self.left, self.right):
            for v in pane.views():
                v.tree.header().setSectionHidden(col, not on)

    def swap_panes(self) -> None:
        a, b = self.left.current(), self.right.current()
        pa, pb = a.path, b.path
        a.navigate(pb)
        b.navigate(pa)

    def same_folder(self) -> None:
        self.other_pane().current().navigate(self.active_view().path)

    # ================================================================== themes
    def _fill_theme_menu(self) -> None:
        self.theme_menu.clear()
        self.themes = themes.load_themes()
        grp = QActionGroup(self.theme_menu)
        cur = self.cfg["theme"]
        for name in [themes.SYSTEM] + list(self.themes):
            label = "Follow system (light/dark)" if name == themes.SYSTEM else name
            a = self.theme_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(name == cur)
            a.triggered.connect(lambda _=False, n=name: self.apply_theme(n))
            grp.addAction(a)
            if name == themes.SYSTEM:
                self.theme_menu.addSeparator()
        self.theme_menu.addSeparator()
        self.theme_menu.addAction(self.a_themes_dir)

    def apply_theme(self, name: str) -> None:
        self.cfg["theme"] = name
        themes.apply(QApplication.instance(), name, self.themes)
        self.icons.reload()
        self.retint()
        for pane in (self.left, self.right):
            pane.set_active_look(pane is self.active_pane() and pane.isVisible())
            for v in pane.views():
                v.view.viewport().update()
        self.sidebar.rebuild()
        self.cfg.save()

    def _sys_scheme_hook(self) -> None:
        try:
            QApplication.styleHints().colorSchemeChanged.connect(
                lambda *_: self.apply_theme(themes.SYSTEM) if self.cfg["theme"] == themes.SYSTEM else None)
        except AttributeError:
            pass

    def open_themes_dir(self) -> None:
        themes.ensure_user_themes()
        v = self.active_pane().add_tab(themes_dir())
        v.focus_view()

    # ================================================================== opening
    def open_entry(self, view: FolderView, e: Entry) -> None:
        if e.is_dir:
            view.navigate(e.path)
            return
        if e.broken:
            self.flash("That link is broken; its target doesn't exist.")
            return
        from .mediaplayer import media_kind
        if media_kind(e.path) and self.cfg["media_open"] != "default":
            if self.cfg["media_open"] == "vlc" and shutil.which("vlc"):
                system._spawn([shutil.which("vlc"), "--", e.path], cwd=os.path.dirname(e.path))
            else:
                self.play_media(e.path)
            return
        try:
            system.open_default(e.path)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))

    def player(self):
        from .mediaplayer import PlayerWindow
        if getattr(self, "_player", None) is None:
            self._player = PlayerWindow(self)
        return self._player

    def play_media(self, path: str, playlist: list | None = None, start_at: int = 0) -> None:
        """Open GSExplorer+ Player. Selected media files become the playlist; otherwise the folder."""
        if playlist is None:
            from .mediaplayer import media_kind
            sel = [x.path for x in self.active_view().selected_entries() if not x.is_dir and media_kind(x.path)]
            playlist = sel if len(sel) > 1 and path in sel else None
        self.preview.media.stop()
        self.player().open_media(path, playlist, start_at=start_at)

    def play_folder(self, folder: str) -> None:
        from .mediaplayer import folder_playlist
        items = [p for p in folder_playlist(os.path.join(folder, "\0")) if not p.endswith("\0")]
        if not items:
            self.flash("No video or audio files in this folder.")
            return
        self.player().open_media(items[0], items)

    def view_file(self) -> None:
        sel = self.active_view().selected_entries()
        if sel and sel[0].is_dir:
            return self.properties()
        if sel:
            v = self.active_view()
            files = [e.path for e in v.model.rows if not e.is_dir and not e.parent_link]
            dlg = dialogs.ViewerDialog(self, sel[0].path, files, on_change=v.select_path)
            dlg.exec()

    def edit_file(self) -> None:
        sel = self.active_view().selected_entries()
        if sel and not sel[0].is_dir:
            try:
                system.open_editor(sel[0].path, self.cfg["editor"])
            except OSError as ex:
                QMessageBox.warning(self, APP_NAME, f"Couldn't start editor: {ex}")

    def open_with(self) -> None:
        sel = [e for e in self.active_view().selected_entries() if not e.is_dir]
        if not sel:
            return
        if self._apps_cache is None:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._apps_cache = system.list_apps()
            finally:
                QApplication.restoreOverrideCursor()
        dlg = dialogs.OpenWithDialog(self, self.icons.mime_name(sel[0].path), self._apps_cache)
        if dlg.exec():
            r = dlg.result_app()
            if not r:
                return
            paths = [e.path for e in sel]
            try:
                if r[0] == "custom":
                    system.launch_command_line(r[1], paths)
                else:
                    system.launch_app(r[1], paths, self.cfg["terminal"])
            except (OSError, ValueError) as ex:
                QMessageBox.warning(self, APP_NAME, f"Couldn't start the application: {ex}")

    def open_terminal(self, folder: str | None = None) -> None:
        """Terminal in *folder*, default: the folder shown in the active pane (not the trash's
        internal folder — that falls back to the home folder)."""
        v = self.active_view()
        if folder is None:
            folder = v.path if not v.is_trash() else os.path.expanduser("~")
        try:
            system.open_terminal(folder, self.cfg["terminal"])
            self.flash(f"Terminal opened in {fsutil.display_path(folder)}")
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))

    # ================================================================== archives
    def compress(self, paths: list[str] | None = None) -> None:
        v = self.active_view()
        paths = paths or self._sel_paths()
        if not paths or v.is_trash():
            self.flash("Select the files or folders to compress.")
            return
        other = self.other_pane().current().path if self.cfg["dual_pane"] else None
        dlg = dialogs.CompressDialog(self, paths, v.path, other, self.cfg)
        if not dlg.exec():
            return
        o = dlg.result_options()
        if not os.access(os.path.dirname(o["archive"]), os.W_OK):
            QMessageBox.warning(self, APP_NAME, f"Can't write to {fsutil.display_path(os.path.dirname(o['archive']))}.")
            return
        self.jobs.submit("compress", paths, os.path.dirname(o["archive"]), payload=o)

    def _ask_password(self, archive: str, again: bool = False) -> str | None:
        name = fsutil.display_name(os.path.basename(archive))
        pw, ok = QInputDialog.getText(self, f"Password — {APP_NAME}",
                                      ("Wrong password. Try again for " if again else "Password for ") + f"“{name}”:",
                                      QLineEdit.Password)
        return pw if ok and pw else None

    def extract(self, how: str, paths: list[str] | None = None) -> None:
        """how: 'here' (smart: one item -> here, several -> folder named after the archive),
        'folder' (always a folder named after the archive), 'to' (choose a destination)."""
        from . import archives
        v = self.active_view()
        paths = [p for p in (paths or self._sel_paths()) if archives.is_archive(p)]
        if not paths:
            self.flash("Select an archive (.zip, .tar.gz, .7z, …) to extract.")
            return
        problems = [(p, archives.can_extract(p)[1]) for p in paths if not archives.can_extract(p)[0]]
        if problems:
            QMessageBox.information(self, APP_NAME, "Can't extract:\n" + "\n".join(
                f"• {fsutil.display_name(os.path.basename(p))}: {why}" for p, why in problems))
            paths = [p for p in paths if archives.can_extract(p)[0]]
            if not paths:
                return
        if how == "to":
            start = self.other_pane().current().path if self.cfg["dual_pane"] else v.path
            dest = QFileDialog.getExistingDirectory(self, "Extract to…", start)
            if not dest:
                return
        else:
            dest = v.path
        if not os.access(dest, os.W_OK):
            QMessageBox.warning(self, APP_NAME, f"Can't write to {fsutil.display_path(dest)}.")
            return
        password = ""
        if len(paths) == 1 and archives.needs_password(paths[0]):
            password = self._ask_password(paths[0])
            if password is None:
                return
        self.jobs.submit("extract", paths, dest, payload={"mode": "folder" if how == "folder" else "smart",
                                                          "password": password})

    # ================================================================== file operations
    def _sel_paths(self) -> list[str]:
        return [e.path for e in self.active_view().selected_entries()]

    def transfer(self, kind: str) -> None:
        paths = self._sel_paths()
        if not paths:
            return
        dest = self.other_pane().current().path
        dlg = dialogs.TransferDialog(self, kind, paths, dest)
        if not dlg.exec():
            return
        target = dlg.destination()
        dest_name = ""
        if not os.path.isdir(target):
            parent = os.path.dirname(target)
            if len(paths) == 1 and os.path.isdir(parent):
                dest_name = os.path.basename(target)
                err = fsutil.validate_new_name(dest_name)
                if err:
                    QMessageBox.warning(self, APP_NAME, err)
                    return
                target = parent
            else:
                QMessageBox.warning(self, APP_NAME, f"“{fsutil.display_path(target)}” is not a folder.")
                return
        self.jobs.submit(kind, paths, target, dest_name)

    def handle_drop(self, paths: list, target: str, action) -> None:
        paths = [p for p in paths if p and os.path.abspath(p) != os.path.abspath(target)]
        if not paths:
            return
        if all(os.path.dirname(p) == target for p in paths):
            return   # dropped onto its own folder
        kind = "move" if action == Qt.MoveAction else "copy"
        self.jobs.submit(kind, paths, target)

    def make_dir(self) -> None:
        v = self.active_view()
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:", text="New folder")
        if not ok:
            return
        err = fsutil.validate_new_name(name)
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        p = os.path.join(v.path, name)
        try:
            os.mkdir(p)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Couldn't create folder: {ex.strerror}")
            return
        v._pending_select = p
        v.refresh()

    def make_file(self) -> None:
        v = self.active_view()
        name, ok = QInputDialog.getText(self, "New file", "File name:", text="New file.txt")
        if not ok:
            return
        err = fsutil.validate_new_name(name)
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        p = os.path.join(v.path, name)
        try:
            with open(p, "x"):
                pass
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Couldn't create file: {ex.strerror}")
            return
        v._pending_select = p
        v.refresh()

    def rename(self) -> None:
        v = self.active_view()
        e = v.current_entry()
        sel = v.selected_entries()
        if e is None or e.parent_link:
            e = sel[0] if sel else None
        if e is None:
            return
        dlg = QInputDialog(self)
        dlg.setWindowTitle("Rename")
        dlg.setLabelText(f"New name for “{e.disp}”:")
        dlg.setTextValue(e.disp)
        le = dlg.findChild(QLineEdit)
        if le is not None and not e.is_dir:
            stem = os.path.splitext(e.disp)[0]
            QTimer.singleShot(0, lambda: le.setSelection(0, len(stem) if stem else len(e.disp)))
        if not dlg.exec():
            return
        new = dlg.textValue()
        if new == e.disp:
            return
        err = fsutil.validate_new_name(new)
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        dst = os.path.join(os.path.dirname(e.path), new)
        if os.path.lexists(dst) and os.path.normcase(dst) != os.path.normcase(e.path):
            QMessageBox.warning(self, APP_NAME, f"“{new}” already exists.")
            return
        try:
            os.rename(e.path, dst)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Couldn't rename: {ex.strerror}")
            return
        v._pending_select = dst
        v.refresh()

    def trash(self) -> None:
        v = self.active_view()
        if v.is_trash():
            return self.delete_permanently()
        paths = self._sel_paths()
        if not paths:
            return
        if self.cfg["confirm_trash"]:
            r = QMessageBox.question(self, APP_NAME, f"Move {self._describe(paths)} to the trash?")
            if r != QMessageBox.Yes:
                return
        self.jobs.submit("trash", paths)

    def delete_permanently(self) -> None:
        paths = self._sel_paths()
        if not paths:
            return
        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          f"Permanently delete {self._describe(paths)}?\n\nThis can't be undone.",
                          QMessageBox.Cancel, self)
        btn = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not btn:
            return
        trash_root = self.active_view().is_trash()
        self.jobs.submit("delete", paths)
        if trash_root:
            for p in paths:
                try:
                    os.unlink(os.path.join(trash_root, "info", os.path.basename(p) + ".trashinfo"))
                except OSError:
                    pass

    @staticmethod
    def _describe(paths: list[str]) -> str:
        if len(paths) == 1:
            return f"“{fsutil.display_name(os.path.basename(paths[0]))}”"
        return f"{len(paths)} items"

    def restore_trash(self) -> None:
        v = self.active_view()
        root = v.is_trash()
        if not root:
            return
        errs = []
        for e in v.selected_entries():
            try:
                fsutil.restore_from_trash(root, e.name)
            except OSError as ex:
                errs.append(f"{e.disp}: {ex.strerror or ex}")
        if errs:
            QMessageBox.warning(self, APP_NAME, "Couldn't restore:\n" + "\n".join(errs[:20]))
        v.refresh()
        self.sidebar.rebuild()

    def empty_trash(self) -> None:
        root = fsutil.home_trash()
        files = os.path.join(root, "files")
        try:
            items = [os.path.join(files, n) for n in os.listdir(files)]
        except OSError:
            items = []
        if not items:
            self.flash("The trash is already empty.")
            return
        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          f"Permanently delete all {len(items)} items in the trash?\n\nThis can't be undone.",
                          QMessageBox.Cancel, self)
        btn = box.addButton("Empty trash", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is not btn:
            return
        self.jobs.submit("delete", items)
        info = os.path.join(root, "info")
        try:
            for n in os.listdir(info):
                if n.endswith(".trashinfo"):
                    os.unlink(os.path.join(info, n))
        except OSError:
            pass

    def unmount(self, mountpoint: str) -> None:
        exe = shutil.which("udisksctl")
        if not exe:
            return
        dev = None
        try:
            with open("/proc/self/mounts", "rb") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) > 1 and os.fsdecode(parts[1]).replace("\\040", " ") == mountpoint:
                        dev = os.fsdecode(parts[0])
        except OSError:
            pass
        if not dev or not dev.startswith("/dev/"):
            QMessageBox.warning(self, APP_NAME, "Couldn't find the device for this mount.")
            return
        for pane in (self.left, self.right):
            for v in pane.views():
                if fsutil.is_inside(v.path, mountpoint):
                    v.navigate("~")
        p = subprocess.run([exe, "unmount", "-b", dev], capture_output=True, text=True)
        if p.returncode != 0:
            QMessageBox.warning(self, APP_NAME, f"Unmount failed:\n{(p.stderr or p.stdout).strip()}")
        self.sidebar.rebuild()

    # ================================================================== clipboard
    def clipboard_put(self, cut: bool) -> None:
        paths = self._sel_paths()
        if not paths:
            return
        md = QMimeData()
        urls = [QUrl.fromLocalFile(p) for p in paths]
        md.setUrls(urls)
        md.setData(GNOME_COPIED, ("cut" if cut else "copy").encode() + b"\n" +
                   b"\n".join(bytes(u.toEncoded()) for u in urls))
        import json
        md.setData("application/x-gsexplorer-plus-paths", json.dumps(paths, ensure_ascii=True).encode())
        md.setData("application/x-gsexplorer-plus-op", b"cut" if cut else b"copy")
        md.setText("\n".join(fsutil.display_path(p) for p in paths))
        QApplication.clipboard().setMimeData(md)
        self.flash(f"{'Cut' if cut else 'Copied'} {self._describe(paths)}.")

    def paste(self, dest: str | None = None) -> None:
        md = QApplication.clipboard().mimeData()
        if md is None:
            return
        paths = DirModel.paths_from_mime(md)
        cut = False
        if md.hasFormat("application/x-gsexplorer-plus-op"):
            cut = bytes(md.data("application/x-gsexplorer-plus-op")) == b"cut"
        elif md.hasFormat(GNOME_COPIED):
            cut = bytes(md.data(GNOME_COPIED)).startswith(b"cut")
        if not paths:
            return
        dest = dest or self.active_view().path
        self.jobs.submit("move" if cut else "copy", paths, dest)
        if cut:
            QApplication.clipboard().clear()

    def copy_path(self) -> None:
        paths = self._sel_paths()
        if paths:
            QApplication.clipboard().setText("\n".join(fsutil.display_path(p) for p in paths))

    def select_pattern(self, select: bool) -> None:
        pat, ok = QInputDialog.getText(self, "Select" if select else "Deselect",
                                       "Pattern (e.g. *.pdf):", text="*")
        if ok and pat:
            n = self.active_view().select_pattern(pat, select)
            self.flash(f"{'Selected' if select else 'Deselected'} {n} item{'s' if n != 1 else ''}.")

    # ================================================================== jobs
    def _on_conflict(self, job, src: str, dst: str) -> None:
        dlg = dialogs.ConflictDialog(self, job, src, dst)
        dlg.exec()
        job.answer(dlg.choice, dlg.all.isChecked())

    def open_sync(self):
        from .syncui import SyncWindow
        if getattr(self, "_sync_win", None) is None:
            self._sync_win = SyncWindow(self)
        self._sync_win.show()
        self._sync_win.raise_()
        self._sync_win.activateWindow()
        return self._sync_win

    def open_search(self, folders: list[str] | None = None):
        """Find Files window. Without *folders* it looks in the active pane's folder."""
        from .searchui import SearchWindow
        w = getattr(self, "_search_win", None)
        if w is None:
            w = self._search_win = SearchWindow(self)
        if folders is None:
            v = self.active_view()
            folders = [v.path] if not v.is_trash() else [os.path.expanduser("~")]
        w.set_folders(folders)
        w.show()
        w.raise_()
        w.activateWindow()
        w.f_name.setFocus()
        w.f_name.lineEdit().selectAll()
        return w

    def open_dupes(self, folders: list[str] | None = None):
        """Find Duplicates window. Without *folders* it starts from the active pane's folder."""
        from .dupesui import DuplicatesWindow
        w = getattr(self, "_dupes_win", None)
        if w is None:
            w = self._dupes_win = DuplicatesWindow(self)
        if folders is None:
            v = self.active_view()
            folders = [] if w.folder_list() or v.is_trash() else [v.path]
        if folders:
            w.set_folders(folders)
        w.show()
        w.raise_()
        w.activateWindow()
        w.b_scan.setFocus()                  # Enter starts the search
        return w

    def sync_panes(self) -> None:
        """Compare & sync the active pane (source) with the other pane (target)."""
        src, dst = self.active_view().path, self.other_pane().current().path
        w = self.open_sync()
        w.start_with_panes(src, dst)

    def _on_job_finished(self, job) -> None:
        self._update_queue_button()
        if job.kind == "trash":
            self.sidebar.rebuild()
        if job.kind == "sync":
            return       # the Sync & Backup window reports its own results
        if job.kind == "extract" and job.state == "failed" and job.stats.get("needs_password"):
            arc = job.stats["needs_password"]
            pw = self._ask_password(arc, again=bool((job.payload or {}).get("password")))
            if pw:
                rest = job.sources[job.sources.index(arc):]
                self.jobs.submit("extract", rest, job.dest, payload={**(job.payload or {}), "password": pw})
            return
        if job.kind in ("compress", "extract") and job.state == "done":
            created = job.stats.get("created") or []
            v = self.active_view()
            if created and os.path.dirname(created[0]) == v.path:
                v._pending_select = created[0]
                v.refresh()
            if created:
                self.flash(("Created " if job.kind == "compress" else "Extracted to ")
                           + ", ".join(fsutil.display_name(os.path.basename(c)) for c in created[:3]))
            if job.warnings and not job.errors:
                box = QMessageBox(QMessageBox.Information, APP_NAME,
                                  f"{job.title}\n\nFinished. {len(job.warnings)} item(s) were skipped for safety "
                                  "or because they can't be stored (see details).", QMessageBox.Ok, self)
                box.setDetailedText("\n".join(job.warnings[:200]))
                box.exec()
        if job.trash_failed:
            n = len(job.trash_failed)
            box = QMessageBox(QMessageBox.Warning, APP_NAME,
                              f"{n} item{'s' if n != 1 else ''} couldn't be moved to the trash "
                              "(for example, the drive has no trash folder or is read-only).\n\n"
                              "Delete permanently instead? This can't be undone.",
                              QMessageBox.Cancel, self)
            btn = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
            box.setDetailedText("\n".join(job.errors[:100]))
            box.exec()
            if box.clickedButton() is btn:
                self.jobs.submit("delete", job.trash_failed)
            return
        if job.state == "failed" or job.errors:
            box = QMessageBox(QMessageBox.Warning, APP_NAME,
                              f"{job.title}\n\n{'Failed' if job.state == 'failed' else 'Finished with problems'}: "
                              f"{len(job.errors)} issue{'s' if len(job.errors) != 1 else ''}.", QMessageBox.Ok, self)
            box.setDetailedText("\n".join(job.errors[:200]))
            box.exec()

    def _update_queue_button(self) -> None:
        act = self.jobs.active()
        if not act:
            self.queue_btn.setText("Queue: idle")
            busy = False
        else:
            j = next((x for x in act if x.state in ("running", "paused")), act[0])
            more = f" · +{len(act) - 1} queued" if len(act) > 1 else ""
            verb = {"copy": "Copying", "move": "Moving", "delete": "Deleting", "trash": "Trashing",
                    "sync": "Syncing", "compress": "Compressing", "extract": "Extracting"}.get(j.kind, "Working")
            state = " (paused)" if j.state == "paused" else ""
            self.queue_btn.setText(f"{verb} {int(j.fraction * 100)}%{state}{more}")
            busy = True
        if self.queue_btn.property("busy") != busy:
            self.queue_btn.setProperty("busy", busy)
            self.queue_btn.style().unpolish(self.queue_btn)
            self.queue_btn.style().polish(self.queue_btn)

    def show_jobs(self) -> None:
        if self._jobs_dlg is None:
            self._jobs_dlg = dialogs.JobsDialog(self, self.jobs)
        self._jobs_dlg.show()
        self._jobs_dlg.raise_()

    # ================================================================== misc
    def properties(self) -> None:
        v = self.active_view()
        sel = v.selected_entries()
        if not sel:
            parent = os.path.dirname(v.path) or "/"
            from .dirmodel import make_entry
            e = make_entry(parent, os.path.basename(v.path) or "/") if v.path != "/" else None
            if e is None:
                return
            sel = [e]
        dialogs.PropertiesDialog(self, sel, self.icons).exec()
        v.refresh()

    def bookmark_current(self) -> None:
        p = self.active_view().path
        if self.sidebar.add_bookmark(p):
            self.flash(f"Bookmarked {fsutil.display_path(p)}.")
        else:
            self.flash("Already bookmarked.")

    def _save_bookmarks(self, bms: list) -> None:
        self.cfg["bookmarks"] = list(bms)
        self.cfg.save()

    def _fill_bookmarks(self) -> None:
        self.bm_menu.clear()
        for p in self.cfg["bookmarks"]:
            self.bm_menu.addAction(fsutil.display_path(p), lambda p=p: self.active_view().navigate(p))
        if not self.cfg["bookmarks"]:
            a = self.bm_menu.addAction("No bookmarks yet (Ctrl+D)")
            a.setEnabled(False)

    def preferences(self) -> None:
        if dialogs.PreferencesDialog(self, self.cfg).exec():
            self.thumbs.mode = self.cfg["thumbnails"]
            self.thumbs.mem.clear()
            for pane in (self.left, self.right):
                for v in pane.views():
                    v._apply_icon_size()
                    v.model.set_thumbnails(self.thumbs, 0)
                    v._apply_icon_size()

    def about(self) -> None:
        from .about import AboutDialog
        AboutDialog(self).exec()

    def show_context_menu(self, view: FolderView, gpos, on_item: bool) -> None:
        self.set_active(self.left if view in self.left.views() else self.right)
        self.update_actions()
        m = QMenu(self)
        sel = view.selected_entries() if on_item else []
        in_trash = bool(view.is_trash())
        if sel:
            if in_trash:
                m.addAction(self.a_restore)
                m.addAction(self.a_delete)
                m.addSeparator()
                m.addAction(self.a_empty_trash)
            else:
                e = sel[0]
                if e.is_dir:
                    m.addAction("Open", lambda: view.navigate(e.path))
                    m.addAction("Open in new tab", lambda: self.open_in_new_tab(e.path))
                    m.addAction("Open in other pane", lambda: self.other_pane().current().navigate(e.path))
                else:
                    from .mediaplayer import media_kind
                    if media_kind(e.path):
                        m.addAction("Play in GSExplorer+ Player", lambda: self.play_media(e.path))
                        if shutil.which("vlc"):
                            m.addAction("Play in VLC", lambda: system._spawn(
                                [shutil.which("vlc"), "--", e.path], cwd=os.path.dirname(e.path)))
                        m.addAction("Open with default app", lambda: system.open_default(e.path))
                    else:
                        m.addAction("Open", lambda: self.open_entry(view, e))
                    m.addAction(self.a_openwith)
                    m.addAction(self.a_view)
                    m.addAction(self.a_edit)
                m.addSeparator()
                for a in (self.a_cut, self.a_copy, self.a_copypath):
                    m.addAction(a)
                if e.is_dir and len(sel) == 1:
                    m.addAction("Paste into folder", lambda: self._paste_into(e.path))
                m.addSeparator()
                for a in (self.a_rename, self.a_copy_to, self.a_move_to, self.a_trash, self.a_delete):
                    m.addAction(a)
                m.addSeparator()
                m.addSeparator()
                m.addAction(self.a_compress)
                self._add_extract_entries(m, sel)
                if e.is_dir and len(sel) == 1:
                    m.addAction(f"Open terminal in “{fsutil.display_name(e.name)}”", lambda: self.open_terminal(e.path))
                    m.addAction(self.chrome_icon("search"), f"Search in “{fsutil.display_name(e.name)}”…",
                                lambda: self.open_search([e.path]))
                if e.is_dir and len(sel) == 1:
                    m.addAction("Bookmark folder", lambda: self.sidebar.add_bookmark(e.path))
                if not in_trash and all(x.is_dir for x in sel):
                    m.addAction("Find duplicates in " + ("this folder" if len(sel) == 1 else "these folders"),
                                lambda: self.open_dupes([x.path for x in sel]))
                m.addAction(self.a_props)
        else:
            if in_trash:
                m.addAction(self.a_empty_trash)
                m.addSeparator()
            else:
                m.addAction(self.a_mkdir)
                m.addAction(self.a_mkfile)
                m.addAction(self.a_paste)
                m.addSeparator()
                m.addAction("Play all media in this folder", lambda: self.play_folder(view.path))
                m.addAction("Find duplicates in this folder", lambda: self.open_dupes([view.path]))
                m.addAction(self.chrome_icon("search"), "Search in this folder…", lambda: self.open_search([view.path]))
                m.addAction(self.a_terminal)
            m.addAction(self.a_hidden)
            m.addAction(self.a_refresh)
            m.addSeparator()
            m.addAction(self.a_bookmark)
            m.addAction(self.a_props)
        m.exec(gpos)

    def _add_extract_entries(self, m, sel) -> None:
        from . import archives
        arcs = [x.path for x in sel if not x.is_dir and archives.kind_of(x.name)]
        if not arcs or len(arcs) != len(sel):
            return
        ok = all(archives.can_extract(p)[0] for p in arcs)
        why = "" if ok else next(archives.can_extract(p)[1] for p in arcs if not archives.can_extract(p)[0])
        acts = [m.addAction("Extract here", lambda: self.extract("here", arcs))]
        if len(arcs) == 1:
            folder = archives.strip_suffix(os.path.basename(arcs[0]))
            acts.append(m.addAction(f"Extract to “{fsutil.display_name(folder)}/”", lambda: self.extract("folder", arcs)))
        else:
            acts.append(m.addAction("Extract each to its own folder", lambda: self.extract("folder", arcs)))
        acts.append(m.addAction("Extract to…", lambda: self.extract("to", arcs)))
        for a in acts:
            a.setEnabled(ok)
            if not ok:
                a.setText(a.text() + f"  ({why})")

    def _paste_into(self, path: str) -> None:
        self.paste(dest=path)

    # ================================================================== command line
    def eventFilter(self, obj, ev):
        if obj is self.cmd and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._run_cmd(terminal=bool(ev.modifiers() & Qt.ShiftModifier))
                return True
            if ev.key() == Qt.Key_Escape:
                self.cmd.clear()
                self.active_view().focus_view()
                return True
        return super().eventFilter(obj, ev)

    def _run_cmd(self, terminal: bool) -> None:
        text = self.cmd.text().strip()
        if not text:
            return
        v = self.active_view()
        if text == "cd" or text.startswith("cd "):
            arg = text[2:].strip() or "~"
            try:
                arg = shlex.split(arg)[0]
            except (ValueError, IndexError):
                pass
            v.navigate(os.path.join(v.path, os.path.expanduser(arg)))
            self.cmd.clear()
            v.focus_view()
            return
        try:
            if terminal:
                system.run_in_terminal(text, v.path, self.cfg["terminal"])
            else:
                system.run_detached_shell(text, v.path)
            self.cmd.clear()
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))

    # ================================================================== session
    def _restore_session(self, start_path: str | None) -> None:
        s = self.cfg["session"] or {}
        home = os.path.expanduser("~")
        for pane, key in ((self.left, "left"), (self.right, "right")):
            paths = [p for p in (s.get(key) or []) if isinstance(p, str)][:20]
            if not paths:
                paths = [home]
            for p in paths:
                pane.add_tab(p, activate=False)
            idx = s.get(key + "_index", 0)
            pane.tabs.setCurrentIndex(idx if isinstance(idx, int) and 0 <= idx < pane.tabs.count() else 0)
        self._active = None
        self.set_active(self.right if s.get("active") == "right" and self.cfg["dual_pane"] else self.left)
        if start_path:
            # Reuse a restored tab already showing that folder instead of piling up tabs.
            target = start_path if os.path.isdir(start_path) else os.path.dirname(start_path)
            existing = [i for i, v in enumerate(self.left.views()) if v.path == target]
            if existing:
                self.left.tabs.setCurrentIndex(existing[0])
                if target != start_path:
                    self.left.current().navigate(start_path)
            else:
                self.left.add_tab(start_path)
            self.set_active(self.left)
        w = self.cfg["window"]
        self.resize(int(w.get("w", 1280)), int(w.get("h", 800)))
        sb = int(w.get("sidebar_w", 190))
        pw = int(w.get("preview_w", 340))
        QTimer.singleShot(0, lambda: self.main_split.setSizes([sb, max(400, self.width() - sb - pw), pw]))
        if w.get("split"):
            QTimer.singleShot(0, lambda: self.pane_split.setSizes([int(x) for x in w["split"]]))
        QTimer.singleShot(0, lambda: self.active_view().focus_view())

    def closeEvent(self, ev):
        active = self.jobs.active()
        if active:
            r = QMessageBox.question(self, APP_NAME, f"{len(active)} file operation(s) still running. "
                                                     "Cancel them and quit?")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
            self.jobs.cancel_all()
        self.save_session()
        super().closeEvent(ev)

    def save_session(self) -> None:
        """Persist tabs, layout and window size. Also called on SIGTERM/logout."""
        if getattr(self, "_session_saved", False):
            return
        self._session_saved = True
        self.cfg["session"] = {
            "left": [v.path for v in self.left.views()],
            "right": [v.path for v in self.right.views()],
            "left_index": self.left.tabs.currentIndex(),
            "right_index": self.right.tabs.currentIndex(),
            "active": "right" if self.active_pane() is self.right else "left",
        }
        self.cfg["window"] = {"w": self.width(), "h": self.height(), "split": self.pane_split.sizes(),
                              "sidebar_w": self.main_split.sizes()[0] if self.sidebar.isVisible() else 190,
                              "preview_w": self.main_split.sizes()[2] if self.preview.isVisible() else 340}
        self.cfg.save()


def _app_icon() -> QIcon:
    ic = QIcon.fromTheme(RDNN_ID)
    if ic.isNull():
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "gsexplorer-plus.svg")
        if os.path.exists(here):
            ic = QIcon(here)
    return ic
