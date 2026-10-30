"""Places sidebar: standard places, bookmarks, mounted devices, trash."""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QStandardPaths, QStorageInfo, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMenu, QStyle, QTreeWidget, QTreeWidgetItem

from .fsutil import display_name, home_trash, human_size
from .icons import themed

PATH_ROLE = Qt.UserRole
KIND_ROLE = Qt.UserRole + 1

_IGNORE_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2",
              "devpts", "securityfs", "pstore", "efivarfs", "bpf", "tracefs", "debugfs", "configfs",
              "fusectl", "hugetlbfs", "mqueue", "autofs", "ramfs", "nsfs", "binfmt_misc",
              "fuse.portal", "fuse.gvfsd-fuse", "fuse.rclone", "fuse.snapfuse", "rpc_pipefs", "nfsd", "zram"}
_IGNORE_PREFIX = ("/snap", "/var/snap", "/boot", "/proc", "/sys", "/dev", "/run/user", "/run/snapd",
                  "/var/lib/docker", "/run/credentials")


class Sidebar(QTreeWidget):
    open_path = Signal(str)
    open_path_new_tab = Signal(str)
    bookmarks_changed = Signal(list)

    def __init__(self, bookmarks: list[str], parent=None, connector=None):
        super().__init__(parent)
        self.connector = connector
        self.setObjectName("Sidebar")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(8)
        self.setFocusPolicy(Qt.NoFocus)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemClicked.connect(self._clicked)
        self.bookmarks = list(bookmarks)
        self._devices_sig: tuple = ()
        self.rebuild()
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll_devices)
        self._timer.start()

    # ------------------------------------------------------------------ build
    def _section(self, title: str) -> QTreeWidgetItem:
        it = QTreeWidgetItem([title])
        f = QFont(self.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        f.setBold(True)
        it.setFont(0, f)
        it.setFlags(Qt.ItemIsEnabled)
        it.setForeground(0, self.palette().placeholderText())
        self.addTopLevelItem(it)
        it.setExpanded(True)
        return it

    def _add(self, parent, label, path, icon, kind="place", tip=None):
        it = QTreeWidgetItem([label])
        it.setIcon(0, icon)
        it.setData(0, PATH_ROLE, path)
        it.setData(0, KIND_ROLE, kind)
        it.setToolTip(0, tip or display_name(path))
        parent.addChild(it)
        return it

    def rebuild(self) -> None:
        self.clear()
        places = self._section("Places")
        home = os.path.expanduser("~")
        self._add(places, "Home", home, themed(["user-home"], QStyle.SP_DirHomeIcon))
        std = [
            (QStandardPaths.DesktopLocation, "Desktop", ["user-desktop"]),
            (QStandardPaths.DocumentsLocation, "Documents", ["folder-documents"]),
            (QStandardPaths.DownloadLocation, "Downloads", ["folder-download"]),
            (QStandardPaths.PicturesLocation, "Pictures", ["folder-pictures"]),
            (QStandardPaths.MusicLocation, "Music", ["folder-music"]),
            (QStandardPaths.MoviesLocation, "Videos", ["folder-videos"]),
        ]
        for loc, label, icon in std:
            p = QStandardPaths.writableLocation(loc)
            if p and p != home and os.path.isdir(p):
                self._add(places, label, p, themed(icon + ["folder"], QStyle.SP_DirIcon))
        self._add(places, "File System", "/", themed(["drive-harddisk", "computer"], QStyle.SP_DriveHDIcon))
        trash_files = os.path.join(home_trash(), "files")
        self._add(places, "Trash", trash_files,
                  themed(["user-trash-full" if self._trash_nonempty() else "user-trash"], QStyle.SP_TrashIcon),
                  kind="trash")

        bm = self._section("Bookmarks")
        if not self.bookmarks:
            hint = QTreeWidgetItem(["Ctrl+D bookmarks a folder"])
            hint.setFlags(Qt.NoItemFlags)
            bm.addChild(hint)
        for p in self.bookmarks:
            label = display_name(os.path.basename(p.rstrip("/")) or p)
            icon = themed(["folder-bookmark", "user-bookmarks", "folder"], QStyle.SP_DirLinkIcon)
            it = self._add(bm, label, p, icon, kind="bookmark")
            if not os.path.isdir(p):
                it.setForeground(0, self.palette().placeholderText())
                it.setToolTip(0, display_name(p) + " (not available)")

        dev = self._section("Devices")
        for root, name, removable, info in self._devices():
            icon = themed(["drive-removable-media", "drive-harddisk"] if removable else ["drive-harddisk"],
                          QStyle.SP_DriveHDIcon)
            tip = f"{display_name(root)} · {info}"
            self._add(dev, name, root, icon, kind="device", tip=tip)
        self._devices_sig = self._signature()
        self._build_network()

    # ------------------------------------------------------------------ network (Connect)
    OBJ_ROLE = Qt.UserRole + 2

    def _build_network(self) -> None:
        if self.connector is None:
            return
        from . import connect as cn
        net = self._section("Network")
        mounts = cn.list_gio_mounts()
        for m in mounts:
            icon = themed(["folder-remote", "network-server", "network-workgroup"], QStyle.SP_DriveNetIcon)
            it = self._add(net, m.name if m.mounted else f"{m.name} (connect)", m.path or m.uri, icon,
                           kind="net", tip=m.uri)
            it.setData(0, self.OBJ_ROLE, m)
            if not m.mounted:
                it.setForeground(0, self.palette().placeholderText())
        mounted_hosts = {m.uri.split("://")[-1].split("/")[0].split("@")[-1].lower() for m in mounts if m.mounted}
        for c in self.connector.saved():
            if c.kind == "mount" and c.host.lower() in mounted_hosts:
                continue
            icon = themed(["network-server", "folder-remote", "utilities-terminal"], QStyle.SP_DriveNetIcon)
            it = self._add(net, c.title, c.display_target(), icon, kind="saved", tip=c.display_target())
            it.setData(0, self.OBJ_ROLE, c)
        a = self._add(net, "Connect to Server…", "connect:", themed(["list-add"], QStyle.SP_FileDialogNewFolder),
                      kind="connect", tip="Ctrl+K")
        a.setForeground(0, self.palette().placeholderText())

    def _signature(self) -> tuple:
        from . import connect as cn
        try:
            gv = tuple(sorted(os.listdir(cn.gvfs_dir())))
        except OSError:
            gv = ()
        rc = tuple(m.path for m in cn.rclone_mounts())
        return tuple(r for r, *_ in self._devices()) + gv + rc

    @staticmethod
    def _trash_nonempty() -> bool:
        try:
            return any(True for _ in os.scandir(os.path.join(home_trash(), "files")))
        except OSError:
            return False

    @staticmethod
    def _devices() -> list[tuple[str, str, bool, str]]:
        out = []
        for v in QStorageInfo.mountedVolumes():
            if not v.isValid() or not v.isReady():
                continue
            root = v.rootPath()
            fs = bytes(v.fileSystemType()).decode("ascii", "replace")
            if fs in _IGNORE_FS or root == "/" or root.startswith(_IGNORE_PREFIX):
                continue
            name = v.displayName() or os.path.basename(root) or root
            if name == root:
                name = os.path.basename(root) or root
            removable = root.startswith(("/media/", "/run/media/", "/mnt/"))
            free = f"{human_size(v.bytesAvailable())} free of {human_size(v.bytesTotal())} ({fs})"
            out.append((root, name, removable, free))
        out.sort(key=lambda t: t[1].casefold())
        return out

    def _poll_devices(self) -> None:
        sig = self._signature()
        if sig != self._devices_sig:
            self.rebuild()

    # ------------------------------------------------------------------ interaction
    def _clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        p = item.data(0, PATH_ROLE)
        kind = item.data(0, KIND_ROLE)
        if kind == "net":
            self.connector.open_mount(item.data(0, self.OBJ_ROLE))
            return
        if kind == "saved":
            self.connector.connect(item.data(0, self.OBJ_ROLE))
            return
        if kind == "connect":
            self.connector.connect_dialog()
            return
        if p:
            if item.data(0, KIND_ROLE) == "trash":
                os.makedirs(p, mode=0o700, exist_ok=True)
            self.open_path.emit(p)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MiddleButton:
            it = self.itemAt(ev.position().toPoint())
            if it and it.data(0, PATH_ROLE) and os.path.isdir(it.data(0, PATH_ROLE)):
                self.open_path_new_tab.emit(it.data(0, PATH_ROLE))
                return
        super().mouseReleaseEvent(ev)

    def add_bookmark(self, path: str) -> bool:
        if path in self.bookmarks:
            return False
        self.bookmarks.append(path)
        self.bookmarks_changed.emit(self.bookmarks)
        self.rebuild()
        return True

    def _menu(self, pos) -> None:
        it = self.itemAt(pos)
        if not it or not it.data(0, PATH_ROLE):
            return
        p, kind = it.data(0, PATH_ROLE), it.data(0, KIND_ROLE)
        m = QMenu(self)
        if kind in ("net", "saved", "connect"):
            obj = it.data(0, self.OBJ_ROLE)
            if kind == "net":
                m.addAction("Open" if obj.mounted else "Connect", lambda: self.connector.open_mount(obj))
                if obj.mounted:
                    m.addAction("Disconnect", lambda: self.connector.disconnect(obj))
            elif kind == "saved":
                m.addAction("Connect", lambda: self.connector.connect(obj))
                m.addAction("Edit…", lambda: self.connector.edit(obj))
                m.addAction("Remove", lambda: self.connector.delete(obj))
            else:
                m.addAction("Connect to Server…", self.connector.connect_dialog)
            m.exec(self.viewport().mapToGlobal(pos))
            return
        m.addAction("Open", lambda: self.open_path.emit(p))
        m.addAction("Open in new tab", lambda: self.open_path_new_tab.emit(p))
        if kind == "bookmark":
            m.addSeparator()
            i = self.bookmarks.index(p)
            if i > 0:
                m.addAction("Move up", lambda: self._move_bm(i, -1))
            if i < len(self.bookmarks) - 1:
                m.addAction("Move down", lambda: self._move_bm(i, 1))
            m.addAction("Remove bookmark", lambda: self._remove_bm(p))
        if kind == "device" and shutil.which("udisksctl"):
            m.addSeparator()
            m.addAction("Unmount", lambda: self.window().unmount(p))
        if kind == "trash":
            m.addSeparator()
            m.addAction("Empty trash…", lambda: self.window().empty_trash())
        m.exec(self.viewport().mapToGlobal(pos))

    def _remove_bm(self, p: str) -> None:
        self.bookmarks = [b for b in self.bookmarks if b != p]
        self.bookmarks_changed.emit(self.bookmarks)
        self.rebuild()

    def _move_bm(self, i: int, d: int) -> None:
        b = self.bookmarks
        b[i], b[i + d] = b[i + d], b[i]
        self.bookmarks_changed.emit(b)
        self.rebuild()
