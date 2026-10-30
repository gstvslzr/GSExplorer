# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Thumbnails per the freedesktop.org Thumbnail Managing Standard (shared cache with Nautilus etc.).

Cache:  $XDG_CACHE_HOME/thumbnails/{normal,large,x-large}/<md5(file URI)>.png  (128/256/512 px)
        PNG text keys Thumb::URI + Thumb::MTime (+ Thumb::Size) are written and verified.
Fails:  thumbnails/fail/gsexplorer-plus-<version>/<md5>.png so broken files aren't retried.
Makers: images via Qt (fast, in-process, EXIF-rotated); everything else via the system's
        freedesktop thumbnailers (/usr/share/thumbnailers/*.thumbnailer: evince, totem,
        gdk-pixbuf, ffmpegthumbnailer, ...) with built-in fallbacks pdftoppm (PDF) and ffmpeg
        (video). External thumbnailers parse untrusted files, so they run inside a bubblewrap
        sandbox (read-only filesystem, no network, killed with us) whenever bwrap works.
Remote:  by default only local files are thumbnailed (like GNOME's "local files only").
"""
from __future__ import annotations

import configparser
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from urllib.parse import quote

from PySide6.QtCore import QMimeDatabase, QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader

from . import VERSION

SIZES = {"normal": 128, "large": 256, "x-large": 512}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "fuse.rclone", "fuse.gvfsd-fuse",
              "davfs", "fuse.davfs2", "9p", "afs", "ceph", "glusterfs", "fuse.s3fs", "sshfs"}
_GLIB_PATH_SAFE = "/!$&'()*+,;=:@~"


def cache_root() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "thumbnails")


def file_uri(path: str) -> str:
    """Canonical file URI as GLib builds it (so hashes match other freedesktop apps)."""
    return "file://" + quote(os.fsencode(os.path.abspath(path)), safe=_GLIB_PATH_SAFE)


def flavor_for(px: int) -> str:
    return "normal" if px <= 128 else "large" if px <= 256 else "x-large"


def cache_path(path: str, flavor: str) -> str:
    return os.path.join(cache_root(), flavor, hashlib.md5(file_uri(path).encode()).hexdigest() + ".png")


def fail_path(path: str) -> str:
    return os.path.join(cache_root(), "fail", f"gsexplorer-plus-{VERSION}",
                        hashlib.md5(file_uri(path).encode()).hexdigest() + ".png")


# ----------------------------------------------------------------------------- remote detection

_mounts_cache: tuple[float, list[str]] = (0.0, [])
_mounts_lock = threading.Lock()


def _network_mountpoints() -> list[str]:
    global _mounts_cache
    with _mounts_lock:
        if time.monotonic() - _mounts_cache[0] < 10:
            return _mounts_cache[1]
        pts = []
        try:
            with open("/proc/self/mounts", "rb") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].decode(errors="replace") in NETWORK_FS:
                        pts.append(os.fsdecode(parts[1]).replace("\\040", " "))
        except OSError:
            pass
        _mounts_cache = (time.monotonic(), pts)
        return pts


def is_remote(path: str) -> bool:
    if "/gvfs/" in path or path.startswith("/run/user/") and "/gvfs" in path:
        return True
    return any(path == m or path.startswith(m.rstrip("/") + "/") for m in _network_mountpoints())


# ----------------------------------------------------------------------------- thumbnailers

class _Thumbnailer:
    def __init__(self, exec_: str, mimes: list[str]):
        self.exec, self.mimes = exec_, mimes


def _thumbnailer_dirs() -> list[str]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = [home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [os.path.join(d, "thumbnailers") for d in dirs if d]


def load_thumbnailers() -> dict[str, _Thumbnailer]:
    by_mime: dict[str, _Thumbnailer] = {}
    for d in _thumbnailer_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".thumbnailer"):
                continue
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            try:
                cp.read(os.path.join(d, n), encoding="utf-8")
                e = cp["Thumbnailer Entry"]
            except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
                continue
            try_exec = e.get("TryExec", "")
            if try_exec and not shutil.which(try_exec):
                continue
            t = _Thumbnailer(e.get("Exec", ""), [m for m in e.get("MimeType", "").split(";") if m])
            if not t.exec:
                continue
            for m in t.mimes:
                by_mime.setdefault(m, t)          # first directory wins (user overrides system)
    return by_mime


_bwrap_ok: bool | None = None


def sandbox_available() -> bool:
    """True when bubblewrap can create an unprivileged sandbox here (tested once)."""
    global _bwrap_ok
    if _bwrap_ok is None:
        exe = shutil.which("bwrap")
        _bwrap_ok = False
        if exe:
            try:
                p = subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-all",
                                    "--die-with-parent", "true"], capture_output=True, timeout=5)
                _bwrap_ok = p.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _bwrap_ok = False
    return _bwrap_ok


def _run_sandboxed(argv: list[str], outdir: str, timeout: int = 20) -> bool:
    env = {"PATH": "/usr/bin:/bin", "HOME": outdir, "LANG": "C.UTF-8", "TMPDIR": outdir}
    if sandbox_available():
        # Whole filesystem read-only (inputs may live anywhere, even /tmp), only the private
        # output dir writable, no network/IPC/PID namespaces shared, killed if we exit.
        argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                "--bind", outdir, outdir, "--unshare-all", "--die-with-parent", "--new-session",
                "--setenv", "HOME", outdir, "--setenv", "TMPDIR", outdir, "--"] + argv
    try:
        p = subprocess.run(argv, env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ----------------------------------------------------------------------------- generation

def _read_image_scaled(path: str, px: int) -> QImage | None:
    r = QImageReader(path)
    r.setAutoTransform(True)
    if not r.canRead():
        return None
    sz = r.size()
    if sz.isValid() and (sz.width() > px or sz.height() > px):
        r.setScaledSize(sz.scaled(QSize(px, px), Qt.KeepAspectRatio))
    img = r.read()
    if img.isNull():
        return None
    if img.width() > px or img.height() > px:
        img = img.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img


def _builtin_command(mime: str, src: str, out: str, px: int) -> list[str] | None:
    if mime == "application/pdf" and shutil.which("pdftoppm"):
        return ["pdftoppm", "-png", "-singlefile", "-f", "1", "-l", "1", "-scale-to", str(px), src, out[:-4]]
    if (mime.startswith("video/") or mime in ("application/x-matroska",)) and shutil.which("ffmpeg"):
        return ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", "3", "-i", src, "-frames:v", "1",
                "-vf", f"scale='min({px},iw)':-2", "-y", out]
    return None


def generate(path: str, px: int, mime: str, thumbnailers: dict) -> QImage | None:
    img = _read_image_scaled(path, px) if mime.startswith("image/") else None
    if img is not None:
        return img
    work = os.path.join(os.path.dirname(cache_root()), "gsexplorer-plus", "thumbtmp")
    os.makedirs(work, mode=0o700, exist_ok=True)
    outdir = tempfile.mkdtemp(dir=work)
    out = os.path.join(outdir, "t.png")
    try:
        t = thumbnailers.get(mime)
        cmds = []
        if t is not None:
            argv = []
            for tok in shlex.split(t.exec):
                tok = (tok.replace("%i", path).replace("%u", file_uri(path)).replace("%o", out)
                       .replace("%s", str(px)).replace("%%", "%"))
                argv.append(tok)
            cmds.append(argv)
        b = _builtin_command(mime, path, out, px)
        if b:
            cmds.append(b)
        for argv in cmds:
            if _run_sandboxed(argv, outdir) and os.path.exists(out):
                img = QImage(out)
                if not img.isNull():
                    if img.width() > px or img.height() > px:
                        img = img.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    return img
        return None
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _save_png(img: QImage, dest: str, uri: str, mtime: int, size: int | None) -> None:
    d = os.path.dirname(dest)
    os.makedirs(d, mode=0o700, exist_ok=True)
    img = QImage(img)
    img.setText("Thumb::URI", uri)
    img.setText("Thumb::MTime", str(mtime))
    if size is not None:
        img.setText("Thumb::Size", str(size))
    img.setText("Software", f"GSExplorer+ {VERSION}")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".gx", suffix=".png")
    os.close(fd)
    os.chmod(tmp, 0o600)
    if img.save(tmp, "PNG"):
        os.replace(tmp, dest)
    else:
        os.unlink(tmp)


def load_cached(path: str, flavor: str, mtime: int) -> QImage | None:
    p = cache_path(path, flavor)
    img = QImage(p)
    if img.isNull():
        return None
    if img.text("Thumb::URI") != file_uri(path) or img.text("Thumb::MTime") != str(mtime):
        return None
    return img


def thumbnail(path: str, px: int, mime: str, thumbnailers: dict) -> QImage | None:
    """Full lookup: memory-less cache read, fail check, generate + store. Thread-safe."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.R_OK):
        return None
    if os.path.abspath(path).startswith(cache_root() + os.sep):
        return _read_image_scaled(path, px)      # never thumbnail the thumbnail cache itself
    mtime, flavor = int(st.st_mtime), flavor_for(px)
    img = load_cached(path, flavor, mtime)
    if img is not None:
        return img
    fp = fail_path(path)
    fimg = QImage(fp)
    if not fimg.isNull() and fimg.text("Thumb::MTime") == str(mtime):
        return None
    img = generate(path, SIZES[flavor], mime, thumbnailers)
    uri = file_uri(path)
    try:
        if img is None:
            blank = QImage(1, 1, QImage.Format_ARGB32)
            blank.fill(0)
            _save_png(blank, fp, uri, mtime, st.st_size)
            return None
        if img.width() >= 64 or img.height() >= 64:     # tiny images aren't worth caching
            _save_png(img, cache_path(path, flavor), uri, mtime, st.st_size)
    except OSError:
        pass
    return img


# ----------------------------------------------------------------------------- async manager

class _Signals(QObject):
    done = Signal(str, int, object)        # path, px, QImage|None


class _Job(QRunnable):
    def __init__(self, mgr: "ThumbManager", path: str, px: int, mime: str):
        super().__init__()
        self.mgr, self.path, self.px, self.mime = mgr, path, px, mime
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            img = thumbnail(self.path, self.px, self.mime, self.mgr.thumbnailers)
        except Exception:  # noqa: BLE001 - a bad file must never kill the worker
            img = None
        self.mgr._sig.done.emit(self.path, self.px, img)


class ThumbManager(QObject):
    """Async, memory-cached thumbnails for views and the preview panel."""
    ready = Signal(str)                    # path whose thumbnail is now available

    def __init__(self, mode: str = "local", parent=None):
        super().__init__(parent)
        self.mode = mode                   # never | local | all
        self.db = QMimeDatabase()
        self.thumbnailers = load_thumbnailers()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 2) // 2)))
        self.mem: OrderedDict[tuple, QImage | None] = OrderedDict()
        self.pending: set[tuple] = set()
        self._sig = _Signals()
        self._sig.done.connect(self._done)

    def enabled_for(self, path: str) -> bool:
        if self.mode == "never":
            return False
        if self.mode == "local" and is_remote(path):
            return False
        return True

    def can_thumbnail(self, path: str, mime: str) -> bool:
        return (mime.startswith("image/") or mime in self.thumbnailers
                or _builtin_command(mime, path, "/x.png", 128) is not None)

    def get(self, path: str, mtime: float, px: int) -> QImage | None:
        """Return a cached image now, or None and schedule generation (emits ready())."""
        key = (path, int(mtime), flavor_for(px))
        if key in self.mem:
            self.mem.move_to_end(key)
            return self.mem[key]
        if key in self.pending or not self.enabled_for(path):
            return None
        mime = self.db.mimeTypeForFile(path, QMimeDatabase.MatchExtension).name()
        if not self.can_thumbnail(path, mime):
            self.mem[key] = None
            return None
        self.pending.add(key)
        self.pool.start(_Job(self, path, SIZES[flavor_for(px)], mime))
        return None

    def cancel_pending(self) -> None:
        self.pool.clear()                  # drop queued (not running) jobs, e.g. after navigation
        self.pending.clear()

    def _done(self, path: str, px: int, img) -> None:
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            return
        key = (path, mtime, flavor_for(px))
        self.pending.discard(key)
        self.mem[key] = img
        while len(self.mem) > 1500:
            self.mem.popitem(last=False)
        if img is not None:
            self.ready.emit(path)
