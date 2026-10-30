# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find Files engine (no Qt; unit-testable).

Walks the chosen folders (or, optionally, asks the system file index — plocate/locate — for names),
applies every criterion, and streams hits back in batches so results appear while the search runs.

Content search reads text files line by line (UTF-8, invalid bytes replaced), and — when enabled —
the text of DOCX/XLSX/PPTX/ODF documents (parsed locally) and PDFs (`pdftotext`, poppler-utils, if
installed). Binary files are skipped unless asked for. Nothing leaves the machine.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field

from . import fsutil

PSEUDO_FS = ("/proc", "/sys", "/dev", "/run")
BATCH_SECONDS = 0.1

KINDS = {
    "documents": {"pdf", "doc", "docx", "odt", "rtf", "txt", "md", "xls", "xlsx", "ods", "csv", "ppt", "pptx",
                  "odp", "epub", "tex", "pages", "numbers", "key"},
    "images": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg", "heic", "heif", "avif", "ico",
               "raw", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "psd", "xcf"},
    "audio": {"mp3", "flac", "ogg", "oga", "opus", "wav", "m4a", "aac", "wma", "aiff", "aif", "alac", "mka"},
    "video": {"mp4", "mkv", "avi", "mov", "webm", "wmv", "flv", "m4v", "mpg", "mpeg", "ts", "3gp", "ogv"},
    "archives": {"zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "zst", "tzst", "iso", "cab",
                 "deb", "rpm", "jar", "apk"},
    "code": {"py", "c", "h", "cpp", "hpp", "cc", "cs", "java", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "php",
             "sh", "bash", "zsh", "ps1", "pl", "lua", "swift", "kt", "sql", "html", "htm", "css", "scss", "json",
             "yaml", "yml", "toml", "xml", "ini", "cfg", "conf", "md", "ipynb", "r", "m", "vb", "bat", "cmd"},
}
DOC_EXT = {"docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "odt": "odf", "ods": "odf", "odp": "odf"}


class Cancelled(Exception):
    pass


@dataclass
class Criteria:
    roots: list[str] = field(default_factory=list)
    name: str = ""                 # pattern(s); several separated by ';'
    name_mode: str = "contains"    # contains | wildcard | regex | exact
    name_case: bool = False
    match_path: bool = False       # match the pattern against the full path instead of the name
    exclude: str = ""              # ';'-separated globs: matching files/folders are skipped
    what: str = "both"             # both | files | folders
    kind: str = ""                 # '' or a KINDS key
    text: str = ""
    text_mode: str = "plain"       # plain | regex
    text_case: bool = False
    whole_word: bool = False
    text_invert: bool = False      # files that do NOT contain the text
    in_documents: bool = True      # also look inside DOCX/XLSX/PPTX/ODF/PDF
    binary: bool = False           # also look inside binary files
    max_text_mb: int = 100         # skip content search for larger files
    min_size: int = -1             # bytes; -1 = no limit
    max_size: int = -1
    after: float = 0.0             # modified at/after (epoch); 0 = any
    before: float = 0.0            # modified before (epoch); 0 = any
    subfolders: bool = True
    max_depth: int = 0             # 0 = unlimited (1 = the folder itself only)
    hidden: bool = False
    one_fs: bool = False
    in_archives: bool = False      # match names inside .zip/.tar* archives too
    empty_only: bool = False       # empty files (0 bytes) / empty folders
    use_index: bool = False        # names from plocate/locate instead of walking
    limit: int = 100000
    candidates: list[str] | None = None   # refine: test only these paths (no walking)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("candidates", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(slots=True)
class Hit:
    path: str                      # for archive members: "<archive>::<member>"
    is_dir: bool
    size: int
    mtime: float
    line: int = 0                  # first matching line (content search)
    snippet: str = ""
    count: int = 0                 # matching lines (content search)

    @property
    def name(self) -> str:
        return os.path.basename(self.member or self.path.rstrip("/")) if self.member \
            else os.path.basename(self.path)

    @property
    def archive(self) -> str:
        return self.path.split("::", 1)[0] if "::" in self.path else ""

    @property
    def member(self) -> str:
        return self.path.split("::", 1)[1] if "::" in self.path else ""

    @property
    def folder(self) -> str:
        if self.member:
            inner = os.path.dirname(self.member.rstrip("/"))
            return self.archive + ("/" + inner if inner else "")
        return os.path.dirname(self.path)


@dataclass
class Summary:
    found: int = 0
    folders: int = 0
    files: int = 0
    content_checked: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    cancelled: bool = False
    seconds: float = 0.0
    source: str = "walk"           # walk | index | refine
    notes: list[str] = field(default_factory=list)


# =========================================================================== matchers
def _globs(s: str) -> list[str]:
    return [g.strip() for g in s.replace(",", ";").split(";") if g.strip()]


def name_matcher(c: Criteria):
    """Returns f(name, path) -> bool, or raises re.error for a bad regex."""
    if not c.name.strip():
        return lambda name, path: True
    pats = _globs(c.name) if c.name_mode != "regex" else [c.name]
    fold = (lambda s: s) if c.name_case else (lambda s: s.casefold())
    if c.name_mode == "regex":
        rx = re.compile(c.name, 0 if c.name_case else re.IGNORECASE)
        test = lambda s: rx.search(s) is not None  # noqa: E731
    elif c.name_mode == "wildcard":
        fp = [fold(p) for p in pats]
        test = lambda s: any(fnmatch.fnmatchcase(fold(s), p) for p in fp)  # noqa: E731
    elif c.name_mode == "exact":
        fp = {fold(p) for p in pats}
        test = lambda s: fold(s) in fp  # noqa: E731
    else:
        fp = [fold(p) for p in pats]
        test = lambda s: any(p in fold(s) for p in fp)  # noqa: E731
    if c.match_path:
        return lambda name, path: test(path)
    return lambda name, path: test(name)


def text_regex(c: Criteria):
    if not c.text:
        return None
    pat = c.text if c.text_mode == "regex" else re.escape(c.text)
    if c.whole_word:
        pat = r"(?<!\w)(?:" + pat + r")(?!\w)"
    return re.compile(pat, 0 if c.text_case else re.IGNORECASE)


def kind_ok(c: Criteria, name: str, is_dir: bool) -> bool:
    if not c.kind:
        return True
    if is_dir:
        return False
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if c.kind == "archives" and name.lower().endswith((".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")):
        return True
    return ext in KINDS.get(c.kind, set())


# =========================================================================== content
def _snippet(line: str, m: re.Match) -> str:
    s, e = m.start(), m.end()
    a = max(0, s - 50)
    b = min(len(line), e + 70)
    return ("…" if a else "") + line[a:b].strip("\r\n").replace("\t", " ") + ("…" if b < len(line) else "")


def _lines_of(path: str, c: Criteria, check):
    """Yield text lines to search, or None if the file isn't searchable (binary/too big/unreadable)."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    if c.in_documents and ext in DOC_EXT:
        from .preview import office_text
        try:
            txt = office_text(path, DOC_EXT[ext], limit=20_000_000)
        except Exception:  # noqa: BLE001 - damaged documents are just not searchable
            return None
        return iter(txt.splitlines())
    if c.in_documents and ext == "pdf":
        exe = shutil.which("pdftotext")
        if not exe:
            return None
        try:
            r = subprocess.run([exe, "-q", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return iter(r.stdout.decode("utf-8", "replace").splitlines())
    try:
        f = open(path, "rb")
    except OSError:
        return None
    head = f.read(8192)
    if b"\0" in head and not c.binary:
        f.close()
        return None

    def gen():
        with f:
            f.seek(0)
            buf = b""
            n = 0
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += 1
                if n % 16 == 0:
                    check()
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for p in parts:
                    yield p.decode("utf-8", "replace") if not c.binary else p.decode("latin-1")
            if buf:
                yield buf.decode("utf-8", "replace") if not c.binary else buf.decode("latin-1")
    return gen()


def content_match(path: str, size: int, c: Criteria, rx, check) -> tuple[bool, int, str, int] | None:
    """(matches, first line no, snippet, matching line count) or None when the file can't be searched."""
    if size > c.max_text_mb * 1024 * 1024:
        return None
    lines = _lines_of(path, c, check)
    if lines is None:
        return None
    first, snip, count = 0, "", 0
    for i, line in enumerate(lines, 1):
        m = rx.search(line)
        if m:
            count += 1
            if not first:
                first, snip = i, _snippet(line, m)
                if c.text_invert:
                    break
    found = count > 0
    return (not found if c.text_invert else found), first, snip, count


# =========================================================================== search
def index_tool() -> str | None:
    for exe in ("plocate", "locate"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def search(c: Criteria, emit, progress=None, cancel: threading.Event | None = None) -> Summary:
    """Run the search. emit(list[Hit]) is called in batches; progress(dict) about 10×/s."""
    t0 = time.monotonic()
    s = Summary()
    match_name = name_matcher(c)
    rx = text_regex(c)
    exc = _globs(c.exclude)
    batch: list[Hit] = []
    last = [time.monotonic(), time.monotonic()]       # last emit, last progress

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    def flush(force=False):
        now = time.monotonic()
        if batch and (force or now - last[0] >= BATCH_SECONDS):
            emit(list(batch))
            batch.clear()
            last[0] = now

    def report(where: str, force=False):
        now = time.monotonic()
        if progress and (force or now - last[1] >= 0.1):
            last[1] = now
            progress({"folders": s.folders, "files": s.files, "found": s.found, "path": where,
                      "content": s.content_checked})

    def add(hit: Hit):
        batch.append(hit)
        s.found += 1
        if s.found >= c.limit:
            s.truncated = True
            raise Cancelled()
        flush()

    def test_entry(path: str, name: str, st, is_dir: bool):
        """All criteria except name-walk structure; adds a Hit when everything matches."""
        if c.what == "files" and is_dir or c.what == "folders" and not is_dir:
            return
        if not match_name(name, path):
            return
        if not kind_ok(c, name, is_dir):
            return
        size = 0 if is_dir else st.st_size
        if is_dir and (c.min_size >= 0 or c.max_size >= 0):
            return                                    # a size filter means files
        if not is_dir:
            if c.min_size >= 0 and size < c.min_size:
                return
            if c.max_size >= 0 and size > c.max_size:
                return
        if c.after and st.st_mtime < c.after:
            return
        if c.before and st.st_mtime >= c.before:
            return
        if c.empty_only:
            if is_dir:
                try:
                    with os.scandir(path) as it:
                        if any(True for _ in it):
                            return
                except OSError:
                    return
            elif size:
                return
        line = count = 0
        snip = ""
        if rx is not None:
            if is_dir or not stat.S_ISREG(st.st_mode):
                return                                # only regular files are read (never via a symlink)
            s.content_checked += 1
            r = content_match(path, size, c, rx, check)
            if r is None or not r[0]:
                return
            _ok, line, snip, count = r
        add(Hit(path, is_dir, size, st.st_mtime, line, snip, count))

    def archive_members(path: str):
        low = path.lower()
        try:
            if low.endswith((".zip", ".jar")):
                with zipfile.ZipFile(path) as z:
                    for i in z.infolist():
                        nm = i.filename.rstrip("/")
                        yield nm, i.is_dir(), i.file_size, time.mktime(i.date_time + (0, 0, -1))
            elif low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
                if os.path.getsize(path) > 2 * 1024 ** 3:
                    s.notes.append(f"{fsutil.display_path(path)}: archive over 2 GB, contents not listed")
                    return
                with tarfile.open(path, "r:*") as t:
                    for m in t:
                        check()
                        yield m.name.rstrip("/"), m.isdir(), m.size, float(m.mtime)
        except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as e:
            s.errors.append(f"{fsutil.display_path(path)}: can't list archive ({e})")

    def scan_archive(path: str):
        if rx is not None:
            return                                    # content search doesn't look inside archives
        for nm, is_dir, size, mtime in archive_members(path):
            name = nm.rsplit("/", 1)[-1]
            full = f"{path}::{nm}"
            if c.what == "files" and is_dir or c.what == "folders" and not is_dir:
                continue
            if not match_name(name, full) or not kind_ok(c, name, is_dir):
                continue
            if not is_dir and (c.min_size >= 0 and size < c.min_size or c.max_size >= 0 and size > c.max_size):
                continue
            if c.after and mtime < c.after or c.before and mtime >= c.before:
                continue
            add(Hit(full, is_dir, size, mtime))

    def excluded(name: str) -> bool:
        return bool(exc) and any(fnmatch.fnmatch(name, g) for g in exc)

    try:
        if c.candidates is not None:                  # refine within earlier results
            s.source = "refine"
            for p in c.candidates:
                check()
                if "::" in p:
                    continue
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                s.files += 1
                test_entry(p, os.path.basename(p), st, stat.S_ISDIR(st.st_mode))
                report(p)
        elif c.use_index and index_tool() and c.name.strip():
            s.source = "index"
            _from_index(c, s, test_entry, check, report)
        else:
            _walk(c, s, test_entry, scan_archive, excluded, check, report)
    except Cancelled:
        if not s.truncated:
            s.cancelled = True
    flush(force=True)
    s.seconds = time.monotonic() - t0
    report("", force=True)
    return s


def _walk(c, s, test_entry, scan_archive, excluded, check, report):
    seen: set[tuple[int, int]] = set()
    roots = []
    for r in c.roots:
        rp = os.path.realpath(os.path.expanduser(r))
        if rp not in roots and os.path.isdir(rp):
            roots.append(rp)
    for root in roots:
        try:
            root_dev = os.stat(root).st_dev
        except OSError as e:
            s.errors.append(f"{fsutil.display_path(root)}: {e.strerror or e}")
            continue
        stack = [(root, 1)]
        while stack:
            d, depth = stack.pop()
            check()
            if d in PSEUDO_FS or any(d.startswith(p + "/") for p in PSEUDO_FS):
                continue
            try:
                dst = os.stat(d)
            except OSError as e:
                s.errors.append(f"{fsutil.display_path(d)}: {e.strerror or e}")
                continue
            if (dst.st_dev, dst.st_ino) in seen:
                continue
            seen.add((dst.st_dev, dst.st_ino))
            s.folders += 1
            report(d)
            try:
                it = os.scandir(d)
            except OSError as e:
                s.errors.append(f"{fsutil.display_path(d)}: {e.strerror or e}")
                continue
            subdirs = []
            with it:
                entries = sorted(it, key=lambda e: e.name)
            for de in entries:
                name = de.name
                if not c.hidden and name.startswith("."):
                    continue
                if excluded(name):
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError as e:
                    s.errors.append(f"{fsutil.display_path(de.path)}: {e.strerror or e}")
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if not is_dir and not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    continue                          # pipes, sockets, devices: never touched
                s.files += 0 if is_dir else 1
                test_entry(de.path, name, st, is_dir)
                if is_dir:
                    if c.subfolders and (not c.max_depth or depth < c.max_depth) \
                            and not (c.one_fs and st.st_dev != root_dev):
                        subdirs.append(de.path)
                elif c.in_archives and stat.S_ISREG(st.st_mode):
                    scan_archive(de.path)
            stack.extend((p, depth + 1) for p in reversed(subdirs))


def _from_index(c, s, test_entry, check, report):
    """Names from plocate/locate (fast, whole disk). The index is updated periodically (Ubuntu: daily),
    so files created since then are missing and deleted ones are dropped after a check."""
    exe = index_tool()
    base = [exe, "-0"] + ([] if c.name_case else ["-i"])
    if c.name_mode == "regex":
        queries = [base + ["--regex", c.name]]
    elif c.name_mode == "wildcard":                    # plocate ANDs several patterns -> one query each
        queries = [base + ["--", p if p.startswith("*") else "*" + p] for p in _globs(c.name)]
    else:
        queries = [base + ["--", p] for p in _globs(c.name)]
    out: list[bytes] = []
    seen_paths: set[bytes] = set()
    for args in queries:
        check()
        try:
            r = subprocess.run(args, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            s.errors.append(f"index search failed: {e}")
            return
        if r.returncode not in (0, 1):                 # 1 = nothing found
            s.errors.append(r.stderr.decode("utf-8", "replace").strip() or "index search failed")
            return
        for raw in r.stdout.split(b"\0"):
            if raw and raw not in seen_paths:
                seen_paths.add(raw)
                out.append(raw)
    roots = [os.path.realpath(os.path.expanduser(x)).rstrip("/") + "/" for x in c.roots] or ["/"]
    exc = _globs(c.exclude)
    s.notes.append("Names came from the system file index; files created since it was last updated "
                   "(usually daily) are not listed.")
    for raw in out:
        check()
        p = os.fsdecode(raw)
        if roots != ["/"] and not any((p + "/").startswith(rt) for rt in roots):
            continue
        rel_parts = p.split("/")
        if not c.hidden and any(x.startswith(".") for x in rel_parts if x):
            continue
        if exc and any(fnmatch.fnmatch(x, g) for x in rel_parts if x for g in exc):
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue                                  # in the index but gone from disk
        s.files += 1
        test_entry(p, os.path.basename(p), st, stat.S_ISDIR(st.st_mode))
        report(p)


def export_csv(hits: list[Hit], path: str) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "folder", "type", "size_bytes", "modified", "line", "match", "matching_lines"])
        for h in hits:
            w.writerow([h.name, h.folder, "folder" if h.is_dir else "file", "" if h.is_dir else h.size,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h.mtime)), h.line or "", h.snippet,
                        h.count or ""])
