# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dialogs: transfer, conflict, properties, open-with, queue, viewer, preferences, about."""
from __future__ import annotations

import grp
import shutil
import os
import pwd
import stat
import threading
import time

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QImageReader, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from . import APP_NAME, VERSION, fsutil, system
from .fileops import CANCEL, OVERWRITE, RENAME, SKIP, Job


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    lbl.setWordWrap(True)
    return lbl


def _fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


# --------------------------------------------------------------------------- transfer (F5/F6)

class TransferDialog(QDialog):
    def __init__(self, parent, kind: str, sources: list[str], dest: str):
        super().__init__(parent)
        verb = "Copy" if kind == "copy" else "Move"
        self.setWindowTitle(f"{verb} — {APP_NAME}")
        lay = QVBoxLayout(self)
        n = len(sources)
        what = f"“{fsutil.display_name(os.path.basename(sources[0]))}”" if n == 1 else f"{n} items"
        lay.addWidget(QLabel(f"{verb} {what} to:"))
        self.edit = QLineEdit(fsutil.display_path(dest))
        self._orig_dest, self._orig_text = dest, self.edit.text()
        self.edit.setMinimumWidth(520)
        lay.addWidget(self.edit)
        if n == 1:
            lay.addWidget(_muted("Tip: end the path with a new name to copy or move under that name."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(verb)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def destination(self) -> str:
        t = self.edit.text().strip()
        if t == self._orig_text:
            return self._orig_dest          # keep exact bytes if user didn't edit
        return os.path.abspath(os.path.expanduser(t))


# --------------------------------------------------------------------------- compress

class CompressDialog(QDialog):
    """Name, format, level, optional password (7z), and where to save the archive."""

    def __init__(self, parent, sources: list[str], here: str, other: str | None, cfg=None):
        from . import archives
        super().__init__(parent)
        self.setWindowTitle(f"Compress — {APP_NAME}")
        self.sources, self.here, self.other, self.cfg = sources, here, other, cfg
        self.formats = archives.available_formats()
        lay = QVBoxLayout(self)
        n = len(sources)
        what = f"“{fsutil.display_name(os.path.basename(sources[0]))}”" if n == 1 else f"{n} items"
        lay.addWidget(QLabel(f"<b>Compress {what}</b>"))
        form = QFormLayout()
        if n == 1:
            base = os.path.basename(sources[0].rstrip("/"))
            stem = base if os.path.isdir(sources[0]) else (os.path.splitext(base)[0] or base)
        else:
            stem = os.path.basename(os.path.dirname(sources[0])) or "Archive"
        self.name = QLineEdit(fsutil.display_name(stem))
        self.name.setMinimumWidth(360)
        form.addRow("Archive name:", self.name)
        self.fmt = QComboBox()
        for fid, label, ext, _need in self.formats:
            self.fmt.addItem(label, fid)
        last = (cfg["archive_format"] if cfg is not None else "") or "zip"
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(last)))
        form.addRow("Format:", self.fmt)
        missing = [f[1].split(" — ")[0] for f in archives.FORMATS if f not in self.formats]
        if missing:
            form.addRow("", _muted("Also possible after installing a tool: " + ", ".join(missing)
                                   + " (zstd / 7zip packages)"))
        self.level = QComboBox()
        for t, v in (("Fast", "fast"), ("Normal", "normal"), ("Maximum (slower)", "max")):
            self.level.addItem(t, v)
        self.level.setCurrentIndex(1)
        form.addRow("Compression:", self.level)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("optional — 7Z only (file names are encrypted too)")
        form.addRow("Password:", self.pw)
        self.where = QComboBox()
        self.where.addItem(f"This folder — {fsutil.display_path(here)}", here)
        if other and other != here:
            self.where.addItem(f"Other pane — {fsutil.display_path(other)}", other)
        form.addRow("Save in:", self.where)
        lay.addLayout(form)
        self.info = _muted("")
        lay.addWidget(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setText("Create archive")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.fmt, self.where):
            w.currentIndexChanged.connect(self._update)
        self.name.textChanged.connect(self._update)
        self._update()

    def fmt_id(self) -> str:
        return self.fmt.currentData()

    def archive_path(self) -> str:
        ext = next(f[2] for f in self.formats if f[0] == self.fmt_id())
        name = self.name.text().strip()
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
        return os.path.join(self.where.currentData(), name + ext)

    def _update(self) -> None:
        is7 = self.fmt_id() == "7z"
        self.pw.setEnabled(is7)
        if not is7:
            self.pw.clear()
        name = self.name.text().strip()
        bad = fsutil.validate_new_name(name) if name else "Enter a name"
        if bad:
            self.info.setText(bad)
            self.ok.setEnabled(False)
            return
        self.ok.setEnabled(True)
        target = self.archive_path()
        note = ""
        if os.path.lexists(target):
            from . import archives
            note = f" — exists, will be saved as “{os.path.basename(archives.unique_path(target))}”"
        self.info.setText(f"Creates {fsutil.display_path(target)}{note}")

    def _accept(self) -> None:
        if self.cfg is not None:
            self.cfg["archive_format"] = self.fmt_id()
        self.accept()

    def result_options(self) -> dict:
        from . import archives
        return {"archive": archives.unique_path(self.archive_path()), "fmt": self.fmt_id(),
                "level": self.level.currentData(), "password": self.pw.text()}


# --------------------------------------------------------------------------- conflict

class ConflictDialog(QDialog):
    def __init__(self, parent, job: Job, src: str, dst: str):
        super().__init__(parent)
        self.setWindowTitle(f"File already exists — {APP_NAME}")
        self.choice = CANCEL
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>“{fsutil.display_name(os.path.basename(dst))}” already exists in "
                             f"{fsutil.display_path(os.path.dirname(dst))}</b>"))
        grid = QGridLayout()
        for r, (lbl, p) in enumerate((("Existing:", dst), ("New:", src))):
            try:
                st = os.lstat(p)
                info = f"{fsutil.human_size(st.st_size)} · modified {_fmt_time(st.st_mtime)}"
                if stat.S_ISDIR(st.st_mode):
                    info = f"folder · modified {_fmt_time(st.st_mtime)}"
            except OSError:
                info = "(unavailable)"
            grid.addWidget(QLabel(lbl), r, 0)
            grid.addWidget(_muted(info), r, 1)
        lay.addLayout(grid)
        self.all = QCheckBox("Apply to all remaining conflicts in this job")
        lay.addWidget(self.all)
        row = QHBoxLayout()
        for text, ch in (("Overwrite", OVERWRITE), ("Skip", SKIP), ("Keep both", RENAME), ("Cancel job", CANCEL)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, ch=ch: self._pick(ch))
            row.addWidget(b)
            if ch == SKIP:
                b.setDefault(True)
        lay.addLayout(row)

    def _pick(self, ch: str) -> None:
        self.choice = ch
        self.accept()


# --------------------------------------------------------------------------- properties

class _SizeSignals(QObject):
    done = Signal(int, int, int, bool)


class PropertiesDialog(QDialog):
    def __init__(self, parent, entries: list, icon_cache):
        super().__init__(parent)
        self.entries = entries
        self.setWindowTitle(f"Properties — {APP_NAME}")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        gen = QWidget()
        form = QFormLayout(gen)
        tabs.addTab(gen, "General")
        self._cancel = threading.Event()
        self._sig = _SizeSignals()
        self._sig.done.connect(self._size_done)
        self.size_lbl = QLabel("Calculating…")

        if len(entries) == 1:
            e = entries[0]
            head = QHBoxLayout()
            ic = QLabel()
            ic.setPixmap(icon_cache.for_entry(e).pixmap(48, 48))
            head.addWidget(ic)
            nm = QLabel(f"<b>{fsutil.display_name(e.name)}</b>")
            nm.setTextInteractionFlags(Qt.TextSelectableByMouse)
            nm.setWordWrap(True)
            head.addWidget(nm, 1)
            form.addRow(head)
            form.addRow("Type:", QLabel(icon_cache.type_label(e) + (f"  ({icon_cache.mime_name(e.path)})"
                                                                     if not e.is_dir else "")))
            loc = QLabel(fsutil.display_path(os.path.dirname(e.path)))
            loc.setTextInteractionFlags(Qt.TextSelectableByMouse)
            loc.setWordWrap(True)
            form.addRow("Location:", loc)
            if e.is_link:
                try:
                    tgt = fsutil.display_path(os.readlink(e.path))
                except OSError:
                    tgt = "(unreadable)"
                form.addRow("Link target:", QLabel(tgt + ("  — broken" if e.broken else "")))
            form.addRow("Size:", self.size_lbl)
            try:
                st = os.lstat(e.path)
                form.addRow("Modified:", QLabel(_fmt_time(st.st_mtime)))
                form.addRow("Accessed:", QLabel(_fmt_time(st.st_atime)))
                form.addRow("Changed:", QLabel(_fmt_time(st.st_ctime)))
                form.addRow("Inode:", _muted(f"{st.st_ino} on device {st.st_dev}"))
            except OSError as ex:
                form.addRow("Error:", QLabel(ex.strerror or str(ex)))
                st = None
            if st is not None:
                tabs.addTab(self._perm_tab(e, st), "Permissions")
        else:
            form.addRow(QLabel(f"<b>{len(entries)} items</b>"))
            form.addRow("Location:", QLabel(fsutil.display_path(os.path.dirname(entries[0].path))))
            form.addRow("Total size:", self.size_lbl)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        threading.Thread(target=self._compute_size, daemon=True).start()

    # size of files / trees, off the UI thread
    def _compute_size(self) -> None:
        total = files = dirs = 0
        for e in self.entries:
            try:
                st = os.lstat(e.path)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                dirs += 1
                for root, ds, fs in os.walk(e.path, followlinks=False):
                    if self._cancel.is_set():
                        return
                    dirs += len(ds)
                    for f in fs:
                        try:
                            total += os.lstat(os.path.join(root, f)).st_size
                            files += 1
                        except OSError:
                            pass
            else:
                total += st.st_size
                files += 1
        if not self._cancel.is_set():
            self._sig.done.emit(total, files, dirs, True)

    def _size_done(self, total: int, files: int, dirs: int, _ok: bool) -> None:
        txt = f"{fsutil.human_size(total)} ({total:,} bytes)"
        if dirs:
            txt += f" — {files:,} files, {dirs:,} folders"
        self.size_lbl.setText(txt)

    def done(self, r):
        self._cancel.set()
        super().done(r)

    def _perm_tab(self, e, st) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._st = st
        own = QFormLayout()
        self.owner = QComboBox()
        self.group = QComboBox()
        users = sorted({p.pw_name for p in pwd.getpwall() if p.pw_uid == 0 or p.pw_uid >= 1000}
                       | {self._uname(st.st_uid)})
        groups = sorted({g.gr_name for g in grp.getgrall()} | {self._gname(st.st_gid)})
        self.owner.addItems(users)
        self.group.addItems(groups)
        self.owner.setCurrentText(self._uname(st.st_uid))
        self.group.setCurrentText(self._gname(st.st_gid))
        own.addRow("Owner:", self.owner)
        own.addRow("Group:", self.group)
        lay.addLayout(own)
        box = QGroupBox("Access")
        g = QGridLayout(box)
        self.bits: dict[int, QCheckBox] = {}
        for c, h in enumerate(("Read", "Write", "Execute"), 1):
            g.addWidget(_muted(h), 0, c)
        rows = (("Owner", (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR)),
                ("Group", (stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP)),
                ("Others", (stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH)))
        for r, (name, bits) in enumerate(rows, 1):
            g.addWidget(QLabel(name), r, 0)
            for c, b in enumerate(bits, 1):
                cb = QCheckBox()
                cb.setChecked(bool(st.st_mode & b))
                g.addWidget(cb, r, c)
                self.bits[b] = cb
        lay.addWidget(box)
        self.octal = _muted("")
        lay.addWidget(self.octal)
        for cb in self.bits.values():
            cb.toggled.connect(self._upd_octal)
        self._upd_octal()
        if e.is_link:
            lay.addWidget(_muted("Permissions of symbolic links are not used on Linux; "
                                 "changes apply to the link target."))
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(lambda: self._apply(e.path))
        lay.addWidget(apply_btn, 0, Qt.AlignRight)
        lay.addStretch(1)
        return w

    @staticmethod
    def _uname(uid: int) -> str:
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    @staticmethod
    def _gname(gid: int) -> str:
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return str(gid)

    def _mode(self) -> int:
        m = stat.S_IMODE(self._st.st_mode) & ~0o777
        for b, cb in self.bits.items():
            if cb.isChecked():
                m |= b
        return m

    def _upd_octal(self) -> None:
        m = self._mode()
        self.octal.setText(f"Mode {m:04o}  ·  {stat.filemode(stat.S_IFMT(self._st.st_mode) | m)}")

    def _apply(self, path: str) -> None:
        mode = self._mode()
        new_owner, new_group = self.owner.currentText(), self.group.currentText()
        need_chown = (new_owner != self._uname(self._st.st_uid) or new_group != self._gname(self._st.st_gid))
        errors = []
        if mode != stat.S_IMODE(self._st.st_mode):
            try:
                os.chmod(path, mode)
            except PermissionError:
                errors.append(("chmod", [f"{mode:04o}", "--", path]))
            except OSError as ex:
                QMessageBox.warning(self, APP_NAME, f"Couldn't change permissions: {ex.strerror}")
                return
        if need_chown:
            try:
                uid = pwd.getpwnam(new_owner).pw_uid
                gid = grp.getgrnam(new_group).gr_gid
                os.chown(path, uid, gid, follow_symlinks=False)
            except (PermissionError, KeyError):
                errors.append(("chown", ["-h", f"{new_owner}:{new_group}", "--", path]))
            except OSError as ex:
                QMessageBox.warning(self, APP_NAME, f"Couldn't change owner: {ex.strerror}")
                return
        if errors:
            if not system.pkexec_available():
                QMessageBox.warning(self, APP_NAME, "Permission denied, and pkexec isn't installed "
                                                    "to request administrator rights.")
                return
            r = QMessageBox.question(self, APP_NAME, "You don't own this item. Use administrator rights "
                                                     "(pkexec) to apply the change?")
            if r != QMessageBox.Yes:
                return
            for cmd, args in errors:
                ok, msg = system.pkexec([cmd] + args)
                if not ok:
                    QMessageBox.warning(self, APP_NAME, f"{cmd} failed: {msg}")
                    return
        try:
            self._st = os.lstat(path)
        except OSError:
            pass
        self._upd_octal()


# --------------------------------------------------------------------------- open with

class OpenWithDialog(QDialog):
    def __init__(self, parent, mime: str, apps_all: list):
        super().__init__(parent)
        self.setWindowTitle(f"Open with — {APP_NAME}")
        self.resize(460, 480)
        self.apps_all = apps_all
        self.mime = mime
        self.selected = None
        lay = QVBoxLayout(self)
        lay.addWidget(_muted(f"Type: {mime}"))
        self.list = QListWidget()
        self.list.setIconSize(QSize(24, 24))
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        lay.addWidget(self.list, 1)
        self.show_all = QCheckBox("Show all applications")
        self.show_all.toggled.connect(self._fill)
        lay.addWidget(self.show_all)
        lay.addWidget(QLabel("Or run a custom command (%f = file):"))
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("gimp %f")
        lay.addWidget(self.custom)
        bb = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()

    def _fill(self) -> None:
        from PySide6.QtGui import QIcon
        self.list.clear()
        apps = self.apps_all if self.show_all.isChecked() else system.apps_for_mime(self.mime, self.apps_all)
        if not apps and not self.show_all.isChecked():
            self.show_all.setChecked(True)
            return
        for a in apps:
            it = QListWidgetItem(QIcon.fromTheme(a.icon) if a.icon and not a.icon.startswith("/")
                                 else (QIcon(a.icon) if a.icon else QIcon()), a.name)
            it.setData(Qt.UserRole, a)
            it.setToolTip(a.exec)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def result_app(self):
        if self.custom.text().strip():
            return ("custom", self.custom.text().strip())
        it = self.list.currentItem()
        return ("app", it.data(Qt.UserRole)) if it else None


# --------------------------------------------------------------------------- queue

class JobRow(QFrame):
    def __init__(self, job: Job, parent=None):
        super().__init__(parent)
        self.job = job
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.title = QLabel(job.title)
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(job.cancel)
        top.addWidget(self.pause_btn)
        top.addWidget(self.cancel_btn)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.detail = _muted("")
        lay.addWidget(self.detail)
        self.update_from_job()

    def _toggle_pause(self) -> None:
        if self.job.state == "paused":
            self.job.resume()
        else:
            self.job.pause()
        self.update_from_job()

    def update_from_job(self) -> None:
        j = self.job
        self.bar.setValue(int(j.fraction * 1000))
        active = j.state in ("queued", "running", "paused")
        self.pause_btn.setVisible(j.state in ("running", "paused"))
        self.pause_btn.setText("Resume" if j.state == "paused" else "Pause")
        self.cancel_btn.setVisible(active)
        parts = [j.state.capitalize()]
        if j.total_bytes:
            parts.append(f"{fsutil.human_size(j.done_bytes)} of {fsutil.human_size(j.total_bytes)}")
            if j.state == "running" and j.started:
                el = max(0.001, time.time() - j.started)
                parts.append(f"{fsutil.human_size(int(j.done_bytes / el))}/s")
        elif j.total_items:
            parts.append(f"{min(j.done_items, j.total_items):,} of {j.total_items:,} items")
        if j.current and active:
            parts.append(fsutil.display_name(os.path.basename(j.current)))
        if j.errors:
            parts.append(f"{len(j.errors)} problem{'s' if len(j.errors) != 1 else ''}")
        self.detail.setText("  ·  ".join(parts))
        if j.errors and not active:
            self.detail.setToolTip("\n".join(j.errors[:50]))


class JobsDialog(QDialog):
    def __init__(self, parent, manager):
        super().__init__(parent)
        self.setWindowTitle(f"File operations — {APP_NAME}")
        self.resize(560, 380)
        self.manager = manager
        self.rows: dict[int, JobRow] = {}
        lay = QVBoxLayout(self)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.inner_lay = QVBoxLayout(self.inner)
        self.inner_lay.addStretch(1)
        self.area.setWidget(self.inner)
        lay.addWidget(self.area, 1)
        self.empty = _muted("No file operations.")
        lay.addWidget(self.empty)
        row = QHBoxLayout()
        clr = QPushButton("Clear finished")
        clr.clicked.connect(self._clear)
        row.addWidget(clr)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        lay.addLayout(row)
        manager.job_added.connect(self._add)
        manager.job_updated.connect(self._upd)
        for j in manager.jobs:
            self._add(j)
        self._check_empty()

    def _add(self, job: Job) -> None:
        r = JobRow(job)
        self.rows[job.id] = r
        self.inner_lay.insertWidget(self.inner_lay.count() - 1, r)
        self._check_empty()

    def _upd(self, job: Job) -> None:
        r = self.rows.get(job.id)
        if r and self.isVisible():
            r.update_from_job()
        elif r:
            r.update_from_job()

    def _clear(self) -> None:
        self.manager.clear_finished()
        keep = {j.id for j in self.manager.jobs}
        for jid in list(self.rows):
            if jid not in keep:
                self.rows.pop(jid).deleteLater()
        self._check_empty()

    def _check_empty(self) -> None:
        self.empty.setVisible(not self.rows)


# --------------------------------------------------------------------------- F3 viewer

class ViewerDialog(QDialog):
    """F3 viewer: images (fit to window), text, hex for binaries.

    ← / → (also P / N, Backspace / Space) step to the previous / next file in the pane's current
    order, folders skipped, wrapping at the ends. The pane's selection follows, so closing the
    viewer leaves you on the last file you looked at. Up/Down/PgUp/PgDn scroll text.
    """
    TEXT_LIMIT = 4 * 1024 * 1024
    HEX_LIMIT = 64 * 1024
    IMG_MAX = 2400                       # decode big photos at most this size (memory/speed)

    def __init__(self, parent, path: str, files: list[str] | None = None, on_change=None):
        super().__init__(parent)
        self.files = [f for f in (files or [path])]
        if path not in self.files:
            self.files.insert(0, path)
        self.index = self.files.index(path)
        self.on_change = on_change
        self.resize(960, 680)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.b_prev = QPushButton("◀")
        self.b_prev.setToolTip("Previous file (← or P)")
        self.b_next = QPushButton("▶")
        self.b_next.setToolTip("Next file (→ or N)")
        for b in (self.b_prev, self.b_next):
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoDefault(False)
            b.setFixedWidth(44)
        self.b_prev.clicked.connect(self.prev)
        self.b_next.clicked.connect(self.next)
        self.name = QLabel()
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        self.name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.counter = _muted("")
        self.counter.setWordWrap(False)
        bar.addWidget(self.b_prev)
        bar.addWidget(self.name, 1)
        bar.addWidget(self.counter)
        bar.addWidget(self.b_next)
        lay.addLayout(bar)
        self.info = _muted("")
        lay.addWidget(self.info)
        self.body = QVBoxLayout()
        lay.addLayout(self.body, 1)
        self._content: QWidget | None = None
        self.load(self.index)

    # ------------------------------------------------------------------ navigation
    def prev(self) -> None:
        if len(self.files) > 1:
            self.load((self.index - 1) % len(self.files))

    def next(self) -> None:
        if len(self.files) > 1:
            self.load((self.index + 1) % len(self.files))

    def current(self) -> str:
        return self.files[self.index]

    def load(self, i: int) -> None:
        self.index = i
        path = self.files[i]
        base = fsutil.display_name(os.path.basename(path))
        self.setWindowTitle(f"{base} — {APP_NAME} viewer")
        self.name.setText(base)
        n = len(self.files)
        self.counter.setText(f"{i + 1} of {n}")
        self.b_prev.setEnabled(n > 1)
        self.b_next.setEnabled(n > 1)
        if self._content is not None:
            self.body.removeWidget(self._content)
            self._content.deleteLater()
        self._content = self._build(path)
        self._content.installEventFilter(self)
        for w in self._content.findChildren(QWidget):
            w.installEventFilter(self)
        self.body.addWidget(self._content, 1)
        self.setFocus()
        if self.on_change is not None:
            self.on_change(path)

    # ------------------------------------------------------------------ content
    def _message(self, text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        return lbl

    def _build(self, path: str) -> QWidget:
        self.info.setText("")
        try:
            st = os.stat(path)
            size = st.st_size
        except OSError as e:
            return self._message(f"Can't open: {e.strerror}")
        if not stat.S_ISREG(st.st_mode):
            # FIFOs, sockets and devices would block or stream forever
            return self._message("This is a special file (pipe, socket or device) and can't be previewed.")
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        if reader.canRead():
            sz = reader.size()
            if sz.isValid() and (sz.width() > self.IMG_MAX or sz.height() > self.IMG_MAX):
                reader.setScaledSize(sz.scaled(QSize(self.IMG_MAX, self.IMG_MAX), Qt.KeepAspectRatio))
            img = reader.read()
            if not img.isNull():
                from .preview import _ScaledImage
                view = _ScaledImage()
                view.set_image(img)
                dims = f"{sz.width()}×{sz.height()}" if sz.isValid() else f"{img.width()}×{img.height()}"
                self.info.setText(f"Image · {dims} · {fsutil.human_size(size)} · scaled to fit the window")
                return view
        from .mediaplayer import media_kind
        kind = media_kind(path)
        if kind:
            w = QWidget()
            v = QVBoxLayout(w)
            v.addStretch(1)
            v.addWidget(self._message(f"{'Video' if kind == 'video' else 'Audio'} file · {fsutil.human_size(size)}"))
            b = QPushButton("Play in GSExplorer+ Player")
            b.setAutoDefault(False)
            b.clicked.connect(lambda: (self.parent().play_media(path), self.close()))
            v.addWidget(b, 0, Qt.AlignCenter)
            v.addStretch(1)
            self.info.setText("Media · Enter plays it")
            return w
        try:
            with open(path, "rb") as f:
                data = f.read(self.TEXT_LIMIT)
        except OSError as e:
            return self._message(f"Can't open: {e.strerror}")
        ed = QPlainTextEdit()
        ed.setReadOnly(True)
        ed.setLineWrapMode(QPlainTextEdit.NoWrap)
        ed.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        ed.setFocusPolicy(Qt.NoFocus)          # keys stay with the viewer (arrows navigate files)
        binary = b"\0" in data[:8192]
        if binary:
            chunk = data[:self.HEX_LIMIT]
            lines = []
            for off in range(0, len(chunk), 16):
                row = chunk[off:off + 16]
                hx = " ".join(f"{b:02x}" for b in row)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                lines.append(f"{off:08x}  {hx:<47}  {asc}")
            ed.setPlainText("\n".join(lines))
            self.info.setText(f"Binary · hex view of first {fsutil.human_size(len(chunk))} of "
                              f"{fsutil.human_size(size)}")
        else:
            ed.setPlainText(data.decode("utf-8", "replace"))
            note = f"Text · {fsutil.human_size(size)}"
            if size > self.TEXT_LIMIT:
                note += f" · showing first {fsutil.human_size(self.TEXT_LIMIT)}"
            self.info.setText(note)
        return ed

    # ------------------------------------------------------------------ keys
    def _scroll(self, key) -> bool:
        ed = self._content if isinstance(self._content, QPlainTextEdit) else None
        if ed is None:
            return False
        sb = ed.verticalScrollBar()
        step = {Qt.Key_Up: -sb.singleStep(), Qt.Key_Down: sb.singleStep(),
                Qt.Key_PageUp: -sb.pageStep(), Qt.Key_PageDown: sb.pageStep()}.get(key)
        if step is not None:
            sb.setValue(sb.value() + step)
            return True
        if key == Qt.Key_Home:
            sb.setValue(0)
            return True
        if key == Qt.Key_End:
            sb.setValue(sb.maximum())
            return True
        return False

    def _handle_key(self, ev) -> bool:
        k, mods = ev.key(), ev.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier):
            return False
        if k in (Qt.Key_Left, Qt.Key_P, Qt.Key_Backspace):
            self.prev()
            return True
        if k in (Qt.Key_Right, Qt.Key_N, Qt.Key_Space):
            self.next()
            return True
        if k in (Qt.Key_F3, Qt.Key_Escape, Qt.Key_Q):
            self.close()
            return True
        if k in (Qt.Key_Return, Qt.Key_Enter):
            from .mediaplayer import media_kind
            if media_kind(self.current()):
                self.parent().play_media(self.current())
                self.close()
                return True
        return self._scroll(k)

    def keyPressEvent(self, ev):
        if not self._handle_key(ev):
            super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.KeyPress and self._handle_key(ev):
            return True
        return super().eventFilter(obj, ev)


# --------------------------------------------------------------------------- preferences

class PreferencesDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"Preferences — {APP_NAME}")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.editor = QLineEdit(cfg["editor"])
        self.editor.setPlaceholderText("Default application (xdg-open)")
        self.terminal = QLineEdit(cfg["terminal"])
        self.terminal.setPlaceholderText("Auto-detect (x-terminal-emulator, gnome-terminal, …)")
        self.confirm = QCheckBox("Ask before moving items to trash")
        self.confirm.setChecked(bool(cfg["confirm_trash"]))
        form.addRow("F4 editor command:", self.editor)
        form.addRow("Terminal command:", self.terminal)
        form.addRow("", self.confirm)
        self.thumbs = QComboBox()
        for key, label in (("local", "Local files only (recommended)"), ("all", "All files, including network"),
                           ("never", "Never")):
            self.thumbs.addItem(label, key)
        self.thumbs.setCurrentIndex(max(0, self.thumbs.findData(cfg["thumbnails"])))
        self.player = QComboBox()
        self.player.addItem("Built-in (preview panel)", "builtin")
        self.player.addItem("VLC media player", "vlc")
        self.player.setCurrentIndex(max(0, self.player.findData(cfg["media_player"])))
        if not shutil.which("vlc"):
            self.player.setItemText(1, "VLC media player (not installed)")
        self.autoplay = QCheckBox("Start playing media when selected in the preview panel")
        self.autoplay.setChecked(bool(cfg["preview_autoplay"]))
        form.addRow("Thumbnails:", self.thumbs)
        self.media_open = QComboBox()
        self.media_open.addItem("GSExplorer+ Player (built in)", "builtin")
        self.media_open.addItem("Default system app", "default")
        self.media_open.addItem("VLC" + ("" if shutil.which("vlc") else " (not installed)"), "vlc")
        self.media_open.setCurrentIndex(max(0, self.media_open.findData(cfg["media_open"])))
        form.addRow("Open video/audio with:", self.media_open)
        form.addRow("Preview panel plays with:", self.player)
        form.addRow("", self.autoplay)
        from . import thumbs as _t
        form.addRow("", _muted("Thumbnailer sandbox (bubblewrap): " +
                               ("active — no network, read-only filesystem" if _t.sandbox_available()
                                else "unavailable — external thumbnailers run unsandboxed")))
        lay.addLayout(form)
        lay.addWidget(_muted("Commands are run directly (no shell). The file path is appended."))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _save(self) -> None:
        self.cfg["editor"] = self.editor.text().strip()
        self.cfg["terminal"] = self.terminal.text().strip()
        self.cfg["confirm_trash"] = self.confirm.isChecked()
        self.cfg["thumbnails"] = self.thumbs.currentData()
        self.cfg["media_player"] = self.player.currentData()
        self.cfg["media_open"] = self.media_open.currentData()
        self.cfg["preview_autoplay"] = self.autoplay.isChecked()
        self.cfg.save()
        self.accept()


# --------------------------------------------------------------------------- help

SHORTCUTS = [
    ("Navigation", [("Enter", "Open file / enter folder"), ("Backspace, Alt+Up", "Parent folder"),
                    ("Alt+Left / Alt+Right", "Back / forward"), ("Alt+Home", "Home folder"),
                    ("Ctrl+L", "Type a location"), ("Tab", "Switch pane"),
                    ("Ctrl+T / Ctrl+W", "New tab / close tab"), ("Ctrl+Tab", "Next tab"),
                    ("Type letters", "Jump to matching name")]),
    ("Files", [("F2, Shift+F6", "Rename"), ("F3", "View (in viewer: ←/→ previous/next file)"), ("F4", "Edit"), ("F5", "Copy to other pane"),
               ("F6", "Move to other pane"), ("F7", "New folder"), ("Shift+F4", "New file"),
               ("F8, Delete", "Move to trash"), ("Shift+Delete, Shift+F8", "Delete permanently"),
               ("Ctrl+C / Ctrl+X / Ctrl+V", "Copy / cut / paste"), ("Alt+Enter", "Properties")]),
    ("Selection", [("Insert", "Toggle selection and move down"), ("Ctrl+A", "Select all"),
                   ("Ctrl+Shift+A", "Deselect all"), ("Ctrl+I", "Invert selection"),
                   ("Ctrl++ / Ctrl+-", "Select / deselect by pattern")]),
    ("View", [("F9", "Toggle dual pane"), ("Ctrl+H", "Hidden files"), ("Ctrl+S", "Quick filter"),
              ("Ctrl+1 / Ctrl+2", "Details / icons"), ("Ctrl+R", "Refresh"), ("Ctrl+U", "Swap panes"),
              ("Ctrl+=", "Same folder in both panes"), ("Ctrl+J", "File operations queue"),
              ("Ctrl+D", "Bookmark current folder"), ("Ctrl+B", "Sidebar"), ("F12", "Terminal in the active pane's folder"), ("Ctrl+F, Alt+F7", "Find files"), ("Alt+F5", "Compress selection"), ("Alt+F9", "Extract archive to…"),
              ("Ctrl+E", "Command line"), ("Ctrl+Shift+S", "Compare and sync panes"),
              ("Ctrl+Shift+B", "Sync and Backup profiles"), ("Alt+P", "Preview panel"),
              ("Ctrl+wheel, Ctrl+Alt+=/-", "Icon / thumbnail size"), ("Ctrl+K", "Connect to server"), ("Ctrl+Shift+P", "Media player"), ("Ctrl+Shift+D", "Find duplicates"),
              ("Ctrl+Q", "Quit")]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"Keyboard shortcuts — {APP_NAME}")
        lay = QHBoxLayout(self)
        for title, items in SHORTCUTS:
            box = QGroupBox(title)
            f = QFormLayout(box)
            for k, d in items:
                kl = QLabel(f"<b>{k}</b>")
                f.addRow(kl, QLabel(d))
            lay.addWidget(box)


def about_text() -> str:
    """Short plain summary (kept for callers/tests); the full dialog is gsexplorer.about.AboutDialog."""
    from . import AUTHOR, COPYRIGHT, LICENSE_SPDX
    return (f"<h3>{APP_NAME} {VERSION}</h3><p>Created by {AUTHOR}<br>{COPYRIGHT}<br>"
            f"License: {LICENSE_SPDX}</p>")
