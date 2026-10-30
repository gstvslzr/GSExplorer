"""Icon theme discovery and per-type icon/label caching (freedesktop icon themes)."""
from __future__ import annotations

import os
import shutil
import subprocess

from PySide6.QtCore import QMimeDatabase
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle

_ICON_DIRS = [os.path.expanduser("~/.local/share/icons"), os.path.expanduser("~/.icons"),
              "/usr/share/icons", "/usr/local/share/icons"]


def _gsettings_icon_theme() -> str:
    exe = shutil.which("gsettings")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "get", "org.gnome.desktop.interface", "icon-theme"],
                             capture_output=True, text=True, timeout=1.5)
        return out.stdout.strip().strip("'\"")
    except (OSError, subprocess.SubprocessError):
        return ""


def setup_icon_theme() -> str:
    """Make sure Qt resolves freedesktop icons even without a Qt platform theme."""
    paths = [p for p in _ICON_DIRS if os.path.isdir(p)]
    QIcon.setThemeSearchPaths(list(dict.fromkeys(paths + QIcon.themeSearchPaths())))
    def installed(name: str) -> bool:
        return bool(name) and any(os.path.isfile(os.path.join(d, name, "index.theme")) for d in paths)

    # The desktop's own choice (GNOME/Ubuntu: gsettings) wins, so GExplorer matches the system.
    desktop = _gsettings_icon_theme()
    if installed(desktop):
        QIcon.setThemeName(desktop)
        QIcon.setFallbackThemeName("Adwaita" if desktop != "Adwaita" else "hicolor")
        return desktop
    current = QIcon.themeName()
    if current and current != "hicolor" and not QIcon.fromTheme("folder").isNull():
        return current
    candidates = ["Yaru", "Papirus", "Adwaita", "breeze", "elementary", "Humanity"]
    for name in candidates:
        if installed(name):
            QIcon.setThemeName(name)
            QIcon.setFallbackThemeName("Adwaita" if name != "Adwaita" else "hicolor")
            return name
    return current or ""


def themed(names: str | list[str], fallback: QStyle.StandardPixmap | None = None) -> QIcon:
    for n in ([names] if isinstance(names, str) else names):
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    if fallback is not None:
        return QApplication.style().standardIcon(fallback)
    return QIcon()


class IconCache:
    def __init__(self) -> None:
        self.db = QMimeDatabase()
        self._by_ext: dict[str, QIcon] = {}
        self._label_by_ext: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._by_ext.clear()
        self.folder = themed(["folder"], QStyle.SP_DirIcon)
        self.folder_link = themed(["folder-link", "inode-symlink", "folder"], QStyle.SP_DirLinkIcon)
        self.file = themed(["text-x-generic", "application-octet-stream"], QStyle.SP_FileIcon)
        self.up = themed(["go-up", "go-parent-folder"], QStyle.SP_FileDialogToParent)
        self.broken = themed(["emblem-unreadable", "dialog-warning"], QStyle.SP_MessageBoxWarning)
        self.exec = themed(["application-x-executable", "application-x-sharedlib"], QStyle.SP_FileIcon)

    def _mime_for_ext(self, ext: str):
        return self.db.mimeTypeForFile("x." + ext, QMimeDatabase.MatchExtension) if ext else None

    def for_entry(self, e) -> QIcon:
        if e.parent_link:
            return self.up
        if e.broken:
            return self.broken
        if e.is_dir:
            return self.folder_link if e.is_link else self.folder
        ext = e.ext
        ic = self._by_ext.get(ext)
        if ic is None:
            mt = self._mime_for_ext(ext)
            ic = QIcon()
            if mt is not None and mt.isValid() and not mt.isDefault():
                ic = QIcon.fromTheme(mt.iconName())
                if ic.isNull():
                    ic = QIcon.fromTheme(mt.genericIconName())
            if ic.isNull():
                ic = self.file
            self._by_ext[ext] = ic
        if not ext and e.mode & 0o111:
            return self.exec
        return ic

    def type_label(self, e) -> str:
        if e.broken:
            return "Broken link"
        if e.is_dir:
            return "Link to folder" if e.is_link else "Folder"
        lbl = self._label_by_ext.get(e.ext)
        if lbl is None:
            mt = self._mime_for_ext(e.ext)
            lbl = mt.comment() if mt is not None and mt.isValid() and not mt.isDefault() else (
                (e.ext.upper() + " file") if e.ext else "File")
            self._label_by_ext[e.ext] = lbl
        if e.is_link:
            return "Link · " + lbl
        return lbl

    def mime_name(self, path: str) -> str:
        import stat as _stat
        try:
            if not _stat.S_ISREG(os.stat(path).st_mode):
                # never sniff content of FIFOs/devices: reading could block forever
                return self.db.mimeTypeForFile(path, QMimeDatabase.MatchExtension).name()
            return self.db.mimeTypeForFile(path).name()
        except Exception:  # noqa: BLE001 - Qt may throw on odd paths
            return "application/octet-stream"
