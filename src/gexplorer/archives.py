"""Compress / extract archives (no Qt; unit-testable).

Built in (Python standard library, bundled): .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz,
and single-file .gz/.bz2/.xz.
Needs a system tool, used only when installed: .tar.zst (zstd), .7z/.rar/.iso and others (7-Zip:
`sudo apt install 7zip` — Ubuntu's 7zip 23.01 provides /usr/bin/7z and reads RAR/RAR5).

Extraction safety
  * Everything is extracted into a fresh hidden staging folder inside the destination, then moved into
    place, so a cancelled or failed extraction never leaves half-written files among yours and never
    overwrites an existing file.
  * tar: Python's `data` extraction filter (PEP 706) — refuses absolute paths, "..", links pointing
    outside the destination, device files; drops setuid/setgid and group/other write bits.
  * zip: names are checked before writing (absolute paths and ".." are refused); a symlink entry is
    created only if its target stays inside the destination.
  * 7-Zip formats: the listing is checked first (absolute paths / ".." refused) and links that point
    outside the destination are removed after extraction.
  * Free space is checked as files are written; extraction stops if the disk is about to fill up
    (protects against "zip bombs").
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile

from . import fsutil

CHUNK = 1024 * 1024
MIN_FREE = 256 * 1024 * 1024          # stop extracting when less than this would be left free


class Cancelled(Exception):
    pass


class ArchiveError(Exception):
    pass


class NeedsPassword(ArchiveError):
    pass


# =========================================================================== tools / formats
def tool(name: str) -> str | None:
    if name == "7z":
        for exe in ("7z", "7zz", "7za"):
            p = shutil.which(exe)
            if p:
                return p
        return None
    return shutil.which(name)


# (id, label, extension, needs tool)
FORMATS = [
    ("zip", "ZIP (.zip) — opens everywhere", ".zip", None),
    ("tar.gz", "TAR.GZ (.tar.gz) — Linux standard", ".tar.gz", None),
    ("tar.xz", "TAR.XZ (.tar.xz) — smallest, slower", ".tar.xz", None),
    ("tar.bz2", "TAR.BZ2 (.tar.bz2)", ".tar.bz2", None),
    ("tar.zst", "TAR.ZST (.tar.zst) — fast, needs zstd", ".tar.zst", "zstd"),
    ("7z", "7Z (.7z) — small, optional password, needs 7-Zip", ".7z", "7z"),
    ("tar", "TAR (.tar) — no compression", ".tar", None),
]
LEVELS = {"fast": (1, 1, 1, 1), "normal": (6, 6, 9, 3), "max": (9, 9, 9, 19)}   # zip/gz, xz, bz2, zstd


def available_formats() -> list[tuple]:
    return [f for f in FORMATS if f[3] is None or tool(f[3])]


_SUFFIXES = [  # longest first
    (".tar.gz", "tar.gz"), (".tar.bz2", "tar.bz2"), (".tar.xz", "tar.xz"), (".tar.zst", "tar.zst"),
    (".tgz", "tar.gz"), (".tbz2", "tar.bz2"), (".tbz", "tar.bz2"), (".txz", "tar.xz"), (".tzst", "tar.zst"),
    (".tar", "tar"), (".zip", "zip"), (".jar", "zip"), (".7z", "7z"), (".rar", "7z"), (".iso", "7z"),
    (".cab", "7z"), (".arj", "7z"), (".lzh", "7z"), (".gz", "gz"), (".bz2", "bz2"), (".xz", "xz"),
    (".zst", "zst"),
]


def kind_of(path: str) -> str:
    """Archive kind from the file name ('' = not an archive we handle)."""
    n = path.lower()
    for suf, k in _SUFFIXES:
        if n.endswith(suf):
            return k
    return ""


def is_archive(path: str) -> bool:
    return bool(kind_of(path)) and os.path.isfile(path)


def strip_suffix(name: str) -> str:
    n = name.lower()
    for suf, _k in _SUFFIXES:
        if n.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return os.path.splitext(name)[0] or name


def can_extract(path: str) -> tuple[bool, str]:
    k = kind_of(path)
    if not k:
        return False, "Not a supported archive"
    if k == "7z" and not tool("7z"):
        return False, "Needs 7-Zip: sudo apt install 7zip"
    if k in ("tar.zst", "zst") and not tool("zstd"):
        return False, "Needs zstd: sudo apt install zstd"
    return True, ""


def needs_password(path: str) -> bool:
    """True if the archive is encrypted (zip: ZipCrypto/AES flag; 7z/rar: 7-Zip says so)."""
    k = kind_of(path)
    if k == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                return any(i.flag_bits & 0x1 for i in z.infolist())
        except (OSError, zipfile.BadZipFile):
            return False
    if k == "7z" and tool("7z"):
        r = subprocess.run([tool("7z"), "l", "-slt", "-p", "--", path], capture_output=True, text=True,
                           errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        return "Encrypted = +" in r.stdout or "Wrong password" in (r.stdout + r.stderr) \
            or "Can not open encrypted archive" in (r.stdout + r.stderr)
    return False


# =========================================================================== progress helper
class _Ctx:
    def __init__(self, progress, check):
        self.progress = progress or (lambda done, total, name: None)
        self.check = check or (lambda: None)
        self.done = 0
        self.total = 0

    def add(self, n: int, name: str = "") -> None:
        self.done += n
        self.check()
        self.progress(self.done, self.total, name)


class _CountingReader:
    """Wraps the compressed input so progress = compressed bytes read / archive size."""

    def __init__(self, f, ctx: _Ctx):
        self.f, self.ctx = f, ctx

    def read(self, n=-1):
        b = self.f.read(n)
        if b:
            self.ctx.add(len(b))
        return b

    def __getattr__(self, a):
        return getattr(self.f, a)


# =========================================================================== compress
def _walk(sources: list[str]):
    """(path, arcname) for every entry; arcnames are relative to each source's parent folder."""
    for src in sources:
        base = os.path.dirname(os.path.abspath(src))
        yield src, os.path.relpath(src, base)
        if os.path.isdir(src) and not os.path.islink(src):
            for root, dirs, files in os.walk(src):
                dirs.sort()
                for name in sorted(dirs) + sorted(files):
                    p = os.path.join(root, name)
                    yield p, os.path.relpath(p, base)


def total_size(sources: list[str]) -> int:
    n = 0
    for p, _a in _walk(sources):
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            n += st.st_size
    return n


def unique_path(path: str) -> str:
    if not os.path.lexists(path):
        return path
    d, name = os.path.split(path)
    stem = strip_suffix(name)
    ext = name[len(stem):]
    i = 2
    while True:
        cand = os.path.join(d, f"{stem} ({i}){ext}")
        if not os.path.lexists(cand):
            return cand
        i += 1


def compress(sources: list[str], archive: str, fmt: str, level: str = "normal", password: str = "",
             progress=None, check=None) -> list[str]:
    """Create *archive*. Written to a temporary name first and renamed when complete, so a cancelled or
    failed run never leaves a broken archive behind. Returns warnings (skipped special files, …)."""
    ctx = _Ctx(progress, check)
    ctx.total = total_size(sources)
    warnings: list[str] = []
    lv = LEVELS.get(level, LEVELS["normal"])
    d = os.path.dirname(os.path.abspath(archive))
    fd, tmp = tempfile.mkstemp(prefix=".gexplorer-", suffix=".part", dir=d)
    os.close(fd)
    try:
        if fmt == "zip":
            _zip(sources, tmp, lv[0], ctx, warnings)
        elif fmt in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
            mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[fmt]
            kw = {} if fmt == "tar" else ({"preset": lv[1]} if fmt == "tar.xz" else {"compresslevel": lv[2] if fmt == "tar.bz2" else lv[0]})
            with tarfile.open(tmp, mode, format=tarfile.PAX_FORMAT, **kw) as t:
                _tar_add(t, sources, ctx, warnings)
        elif fmt == "tar.zst":
            _tar_zst(sources, tmp, lv[3], ctx, warnings)
        elif fmt == "7z":
            _seven_zip_create(sources, tmp, level, password, ctx)
        else:
            raise ArchiveError(f"Unknown format {fmt}")
        os.chmod(tmp, 0o666 & ~_umask())             # like any new file (mkstemp creates 0600)
        os.replace(tmp, archive)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return warnings


def _zip(sources, out, level, ctx, warnings):
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level, allowZip64=True) as z:
        for p, arc in _walk(sources):
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                zi = zipfile.ZipInfo.from_file(p, arc + "/")
                z.writestr(zi, b"")
            elif stat.S_ISLNK(st.st_mode):           # stored as a link (Info-ZIP convention)
                zi = zipfile.ZipInfo(arc, time.localtime(st.st_mtime)[:6])
                zi.create_system = 3
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, os.readlink(p))
            elif stat.S_ISREG(st.st_mode):
                zi = zipfile.ZipInfo.from_file(p, arc)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(p, "rb") as src, z.open(zi, "w", force_zip64=st.st_size > 2 ** 31) as dst:
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        dst.write(b)
                        ctx.add(len(b), arc)
            else:
                warnings.append(f"{fsutil.display_path(p)}: special file skipped")


def _tar_add(t: tarfile.TarFile, sources, ctx, warnings):
    for p, arc in _walk(sources):
        ti = t.gettarinfo(p, arc)
        if ti is None or not (ti.isreg() or ti.isdir() or ti.issym() or ti.islnk()):
            warnings.append(f"{fsutil.display_path(p)}: special file skipped")
            continue
        if ti.isreg():
            with open(p, "rb") as f:
                t.addfile(ti, _CountingReader(f, ctx))
        else:
            t.addfile(ti)
        ctx.check()


def _tar_zst(sources, out, level, ctx, warnings):
    with open(out, "wb") as fo:
        pr = subprocess.Popen([tool("zstd"), f"-{level}", "-q", "-c", "-T0"], stdin=subprocess.PIPE, stdout=fo)
        try:
            with tarfile.open(fileobj=pr.stdin, mode="w|", format=tarfile.PAX_FORMAT) as t:
                _tar_add(t, sources, ctx, warnings)
        except BaseException:
            pr.kill()
            pr.wait()
            raise
        pr.stdin.close()
        if pr.wait() != 0:
            raise ArchiveError("zstd failed")


def _seven_zip_create(sources, out, level, password, ctx):
    os.unlink(out)                                    # 7z wants to create the file itself
    mx = {"fast": "-mx=1", "normal": "-mx=5", "max": "-mx=9"}[level]
    args = [tool("7z"), "a", "-t7z", mx, "-snl", "-bsp1", "-bso0", "-y"]   # -snl: keep symlinks as links
    if password:
        args += [f"-p{password}", "-mhe=on"]          # also encrypt the file names
    base = os.path.dirname(os.path.abspath(sources[0]))
    args += ["--", out] + [os.path.relpath(s, base) for s in sources]
    _run_7z(args, base, ctx)


def _run_7z(args, cwd, ctx):
    pr = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL)
    buf = b""
    try:
        while True:
            ch = pr.stdout.read(64)
            if not ch:
                break
            buf = (buf + ch)[-256:]
            ctx.check()
            for part in reversed(buf.replace(b"\b", b"\r").split(b"\r")):
                s = part.strip()
                if s.endswith(b"%") or b"% " in s:
                    try:
                        pct = int(s.split(b"%")[0].split()[-1])
                        ctx.total = 100
                        ctx.done = pct
                        ctx.progress(pct, 100, "")
                    except (ValueError, IndexError):
                        pass
                    break
    except BaseException:
        pr.kill()
        pr.wait()
        raise
    err = pr.stderr.read().decode("utf-8", "replace")
    if pr.wait() == 0:
        ctx.total, ctx.done = 100, 100
        ctx.progress(100, 100, "")
        return
    if True:
        if "Wrong password" in err:
            raise NeedsPassword("Wrong password")
        raise ArchiveError(err.strip().splitlines()[-1] if err.strip() else f"7-Zip exited with {pr.returncode}")


# =========================================================================== extract
def _safe_rel(name: str) -> str | None:
    """Normalised relative path inside the destination, or None if unsafe (absolute, '..')."""
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return os.path.join(*parts)


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _space_guard(dest: str, more: int) -> None:
    if shutil.disk_usage(dest).free - more < MIN_FREE:
        raise ArchiveError("Stopped: the disk is almost full (the archive expands to more than the free space)")


def extract(archive: str, dest: str, mode: str = "smart", password: str = "", progress=None,
            check=None) -> tuple[str, list[str]]:
    """Extract *archive* into *dest*.

    mode 'smart'  — one top-level item: put it in dest; several: a folder named after the archive.
    mode 'folder' — always a folder named after the archive.
    Returns (path created, warnings). Never overwrites: name clashes get " (2)" etc."""
    ctx = _Ctx(progress, check)
    warnings: list[str] = []
    os.makedirs(dest, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".gexplorer-extract-", dir=dest)
    try:
        k = kind_of(archive)
        ok, why = can_extract(archive)
        if not ok:
            raise ArchiveError(why)
        if k == "zip":
            _x_zip(archive, stage, password, ctx, warnings)
        elif k.startswith("tar"):
            _x_tar(archive, stage, k, ctx, warnings)
        elif k in ("gz", "bz2", "xz", "zst"):
            _x_single(archive, stage, k, ctx)
        elif k == "7z":
            _x_7z(archive, stage, password, ctx, warnings)
        else:
            raise ArchiveError("Not a supported archive")
        top = sorted(os.listdir(stage))
        if not top:
            raise ArchiveError("The archive is empty")
        name = strip_suffix(os.path.basename(archive))
        if mode == "smart" and len(top) == 1:
            target = unique_path(os.path.join(dest, top[0]))
            os.rename(os.path.join(stage, top[0]), target)
            os.rmdir(stage)
        else:
            target = unique_path(os.path.join(dest, name))
            os.chmod(stage, 0o755 & ~_umask())
            os.rename(stage, target)
        return target, warnings
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _umask() -> int:
    m = os.umask(0)
    os.umask(m)
    return m


def _x_zip(archive, stage, password, ctx, warnings):
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"Not a valid zip file ({e})") from e
    with z:
        infos = z.infolist()
        if any(i.flag_bits & 0x1 for i in infos) and not password:
            raise NeedsPassword("This zip file is encrypted")
        if password:
            z.setpassword(password.encode())
        ctx.total = sum(i.file_size for i in infos)
        _space_guard(stage, ctx.total)
        links = []
        for i in infos:
            rel = _safe_rel(i.filename)
            if rel is None:
                warnings.append(f"{i.filename}: unsafe path (absolute or '..'), skipped")
                continue
            out = os.path.join(stage, rel)
            mode = i.external_attr >> 16
            if i.is_dir():
                os.makedirs(out, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if stat.S_ISLNK(mode):
                links.append((out, z.read(i).decode("utf-8", "replace")))
                continue
            try:
                with z.open(i) as src, open(out, "wb") as dst:
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        dst.write(b)
                        ctx.add(len(b), rel)
            except RuntimeError as e:                 # wrong/missing password
                raise NeedsPassword("Wrong password") from e
            except NotImplementedError as e:          # e.g. AES-encrypted zip
                raise ArchiveError(f"{e} — install 7-Zip (sudo apt install 7zip) to open this zip") from e
            if mode & 0o777:
                os.chmod(out, mode & 0o755 | 0o600)   # keep exec bits, never setuid/sticky/world-write
            t = time.mktime(i.date_time + (0, 0, -1))
            os.utime(out, (t, t))
        for out, target in links:
            if _inside(os.path.join(os.path.dirname(out), target), stage) and not os.path.isabs(target):
                os.symlink(target, out)
            else:
                warnings.append(f"{os.path.relpath(out, stage)}: link points outside the folder, skipped")


def _x_tar(archive, stage, k, ctx, warnings):
    ctx.total = os.path.getsize(archive)              # progress = compressed bytes read
    with open(archive, "rb") as raw:
        rd = _CountingReader(raw, ctx)
        if k == "tar.zst":
            pr = subprocess.Popen([tool("zstd"), "-d", "-c", "-q"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            import threading

            def feed():
                try:
                    while True:
                        b = rd.read(CHUNK)
                        if not b:
                            break
                        pr.stdin.write(b)
                except (BrokenPipeError, OSError, Cancelled):
                    pass
                finally:
                    try:
                        pr.stdin.close()
                    except OSError:
                        pass

            th = threading.Thread(target=feed, daemon=True)
            th.start()
            src, mode = pr.stdout, "r|"
        else:
            src, mode = rd, "r|*"
            pr = None
        try:
            with tarfile.open(fileobj=src, mode=mode) as t:
                for m in t:
                    ctx.check()
                    try:
                        if m.isfile():
                            _space_guard(stage, m.size)
                        t.extract(m, stage, filter="data")
                    except tarfile.FilterError as e:
                        warnings.append(f"{m.name}: {e} — skipped")
        except tarfile.ReadError as e:
            raise ArchiveError(f"Not a valid archive ({e})") from e
        finally:
            if pr is not None:
                pr.kill()
                pr.wait()


def _x_single(archive, stage, k, ctx):
    name = strip_suffix(os.path.basename(archive)) or "file"
    ctx.total = os.path.getsize(archive)
    out = os.path.join(stage, name)
    with open(archive, "rb") as raw:
        rd = _CountingReader(raw, ctx)
        if k == "zst":
            with open(out, "wb") as fo:
                pr = subprocess.Popen([tool("zstd"), "-d", "-c", "-q"], stdin=subprocess.PIPE, stdout=fo)
                try:
                    while True:
                        b = rd.read(CHUNK)
                        if not b:
                            break
                        pr.stdin.write(b)
                    pr.stdin.close()
                    if pr.wait() != 0:
                        raise ArchiveError("zstd could not decompress this file")
                except BaseException:
                    pr.kill()
                    raise
            return
        opener = {"gz": gzip.GzipFile, "bz2": bz2.BZ2File, "xz": lzma.LZMAFile}[k]
        try:
            with opener(fileobj=rd) as src, open(out, "wb") as dst:
                while True:
                    b = src.read(CHUNK)
                    if not b:
                        break
                    _space_guard(stage, len(b))
                    dst.write(b)
        except (OSError, EOFError, lzma.LZMAError) as e:
            raise ArchiveError(f"Not a valid {k} file ({e})") from e


def _x_7z(archive, stage, password, ctx, warnings):
    exe = tool("7z")
    pw = ["-p" + password] if password else ["-p"]  # "-p" alone: never prompt (fails if encrypted)
    r = subprocess.run([exe, "l", "-slt", "-ba"] + pw + ["--", archive], capture_output=True, text=True,
                       errors="replace", stdin=subprocess.DEVNULL, timeout=600)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        if "Wrong password" in out or "encrypted" in out.lower():
            raise NeedsPassword("This archive is encrypted" if not password else "Wrong password")
        raise ArchiveError(out.strip().splitlines()[-1] if out.strip() else "7-Zip could not read the archive")
    total = 0
    for line in r.stdout.splitlines():
        if line.startswith("Path = "):
            p = line[7:]
            if _safe_rel(p) is None:
                raise ArchiveError(f"Refused: the archive contains an unsafe path ({p})")
        elif line.startswith("Size = "):
            try:
                total += int(line[7:])
            except ValueError:
                pass
    _space_guard(stage, total)
    _run_7z([exe, "x", "-y", "-bsp1", "-bso0"] + pw + ["-o" + stage, "--", archive], stage, ctx)
    for root, dirs, files in os.walk(stage):         # remove links that lead outside
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p) and not _inside(p, stage):
                os.unlink(p)
                warnings.append(f"{os.path.relpath(p, stage)}: link points outside the folder, removed")
