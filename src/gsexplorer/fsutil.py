# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem helpers: display names, sizes, unique names, freedesktop Trash.

Paths are Python ``str`` objects. Undecodable bytes in filenames survive as
surrogate escapes (PEP 383), so every os.* call round-trips them exactly.
Only the *display* form is lossy.
"""
from __future__ import annotations

import datetime as _dt
import errno
import os
import stat
from urllib.parse import quote, unquote_to_bytes


# --------------------------------------------------------------------------- display

def display_name(name: str) -> str:
    """Human-readable form of a filename (invalid UTF-8 -> U+FFFD, control chars visible)."""
    try:
        raw = os.fsencode(name)
    except UnicodeError:
        raw = name.encode("utf-8", "surrogateescape")
    text = raw.decode("utf-8", "replace")
    if any(ord(c) < 32 for c in text):
        text = "".join("⏎" if c == "\n" else ("·" if ord(c) < 32 else c) for c in text)
    return text


def display_path(path: str) -> str:
    return display_name(path)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        n /= 1024.0
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
    return f"{n:.0f} PB"


def perm_string(mode: int) -> str:
    return stat.filemode(mode)


# --------------------------------------------------------------------------- names

def unique_name(directory: str, name: str) -> str:
    """Return a non-existing name in *directory* based on *name* ("a (2).txt")."""
    if not os.path.lexists(os.path.join(directory, name)):
        return name
    base, ext = os.path.splitext(name)
    if base == "" or name.startswith(".") and ext == "":
        base, ext = name, ""
    i = 2
    while True:
        cand = f"{base} ({i}){ext}"
        if not os.path.lexists(os.path.join(directory, cand)):
            return cand
        i += 1


def is_inside(child: str, parent: str) -> bool:
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def validate_new_name(name: str) -> str | None:
    """Return an error message, or None if *name* is a valid single path component."""
    if not name or not name.strip():
        return "Enter a name."
    if name in (".", ".."):
        return "That name is reserved."
    if "/" in name or "\0" in name:
        return "Names can't contain “/”."
    if len(os.fsencode(name)) > 255:
        return "That name is too long (255 bytes max)."
    return None


# --------------------------------------------------------------------------- trash
# freedesktop.org Trash specification 1.0
# https://specifications.freedesktop.org/trash-spec/trashspec-latest.html

class TrashError(OSError):
    pass


def _xdg_data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")


def home_trash() -> str:
    return os.path.join(_xdg_data_home(), "Trash")


def _mount_point(path: str) -> str:
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _ensure_dirs(trash_dir: str) -> None:
    for sub in ("", "files", "info"):
        d = os.path.join(trash_dir, sub)
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)


def _topdir_trash(topdir: str) -> tuple[str, bool]:
    """Return (trash_dir, uses_relative_paths) for a non-home mount, creating if needed."""
    uid = os.getuid()
    admin = os.path.join(topdir, ".Trash")
    try:
        st = os.lstat(admin)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and (st.st_mode & stat.S_ISVTX):
            d = os.path.join(admin, str(uid))
            _ensure_dirs(d)
            return d, True
    except OSError:
        pass
    d = os.path.join(topdir, f".Trash-{uid}")
    try:
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            raise TrashError(errno.EPERM, f"Unsafe trash directory: {d}")
    except FileNotFoundError:
        pass
    _ensure_dirs(d)
    return d, True


def trash_dir_for(path: str) -> tuple[str, bool, str]:
    """Pick the trash for *path*. Returns (trash_dir, relative_paths, topdir)."""
    home = home_trash()
    os.makedirs(_xdg_data_home(), exist_ok=True)
    try:
        same_dev = os.lstat(path).st_dev == os.stat(_xdg_data_home()).st_dev
    except OSError as e:
        raise TrashError(e.errno or errno.EIO, str(e))
    if same_dev:
        _ensure_dirs(home)
        return home, False, "/"
    topdir = _mount_point(os.path.dirname(os.path.abspath(path)))
    d, rel = _topdir_trash(topdir)
    return d, rel, topdir


def move_to_trash(path: str) -> str:
    """Move *path* to the appropriate trash. Returns the trashed location."""
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise FileNotFoundError(errno.ENOENT, "No such file", path)
    trash, relative, topdir = trash_dir_for(path)
    files_dir = os.path.join(trash, "files")
    info_dir = os.path.join(trash, "info")
    base = os.path.basename(path.rstrip("/")) or "root"
    # Reserve a unique name by atomically creating the .trashinfo file.
    name = base
    i = 1
    while True:
        info_path = os.path.join(info_dir, name + ".trashinfo")
        try:
            fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            i += 1
            stem, ext = os.path.splitext(base)
            name = f"{stem}.{i}{ext}"
    stored = os.path.relpath(path, topdir) if relative else path
    date = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    info = f"[Trash Info]\nPath={quote(os.fsencode(stored))}\nDeletionDate={date}\n"
    try:
        os.write(fd, info.encode("utf-8"))
    finally:
        os.close(fd)
    dest = os.path.join(files_dir, name)
    try:
        os.rename(path, dest)
    except OSError:
        try:
            os.unlink(info_path)
        except OSError:
            pass
        raise
    return dest


def read_trashinfo(trash_dir: str, name: str) -> tuple[str, str] | None:
    """Return (original_path, deletion_date) for an item in *trash_dir*/files."""
    info_path = os.path.join(trash_dir, "info", name + ".trashinfo")
    try:
        with open(info_path, "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    orig, date = None, ""
    for line in data.splitlines():
        if line.startswith("Path="):
            orig = os.fsdecode(unquote_to_bytes(line[5:].strip()))
        elif line.startswith("DeletionDate="):
            date = line[13:]
    if orig is None:
        return None
    if not orig.startswith("/"):
        topdir = os.path.dirname(os.path.dirname(trash_dir)) if os.path.basename(
            os.path.dirname(trash_dir)) == ".Trash" else os.path.dirname(trash_dir)
        orig = os.path.join(topdir, orig)
    return orig, date


def restore_from_trash(trash_dir: str, name: str) -> str:
    meta = read_trashinfo(trash_dir, name)
    if meta is None:
        raise TrashError(errno.ENOENT, f"No trash info for {name}")
    orig, _ = meta
    parent = os.path.dirname(orig)
    os.makedirs(parent, exist_ok=True)
    dest = orig
    if os.path.lexists(dest):
        dest = os.path.join(parent, unique_name(parent, os.path.basename(orig)))
    os.rename(os.path.join(trash_dir, "files", name), dest)
    try:
        os.unlink(os.path.join(trash_dir, "info", name + ".trashinfo"))
    except OSError:
        pass
    return dest


def is_trash_files_dir(path: str) -> str | None:
    """If *path* is a trash 'files' directory, return the trash root."""
    p = os.path.abspath(path)
    if os.path.basename(p) == "files" and os.path.isdir(os.path.join(os.path.dirname(p), "info")):
        root = os.path.dirname(p)
        b = os.path.basename(root)
        if b == "Trash" or b.startswith(".Trash-") or os.path.basename(os.path.dirname(root)) == ".Trash":
            return root
    return None
