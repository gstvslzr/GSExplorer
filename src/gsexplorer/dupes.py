# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find Duplicates engine (no Qt; unit-testable).

Content matching is staged so that most files are never read in full:
  1. walk the folders, keep regular files only (no symlinks, pipes, devices)
  2. group by size — a file with a unique size can't have a duplicate
  3. hash the first and last 16 KiB of each same-size file (BLAKE2b)
  4. hash the whole file (BLAKE2b-256) only where step 3 still matches
Hard links (same device + inode) are one file on disk: by default only one path per inode is
listed, because deleting a hard link frees no space.

Actions (trash / delete / move for audit) re-check every file against the scan first and never
remove the last remaining copy of a group.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field

from . import fsutil

PART = 16 * 1024                  # bytes hashed from each end in the quick pass
CHUNK = 1024 * 1024
AUDIT_PREFIX = "GSExplorer-duplicates-"
LEGACY_AUDIT_PREFIX = "GExplorer-duplicates-"   # folders made before 26.8.0
MANIFEST = "manifest.csv"
PSEUDO_FS = ("/proc", "/sys", "/dev", "/run")
MATCH_MODES = ("content", "name_size", "name")


class Cancelled(Exception):
    pass


@dataclass(slots=True)
class DupFile:
    path: str
    size: int
    mtime: float
    dev: int
    ino: int
    nlink: int = 1
    links: list[str] | None = None    # other paths to the same inode (when collapsed); None = none

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass(slots=True)
class DupGroup:
    gid: int
    size: int
    key: str                       # content hash (content mode) or the matched name
    files: list[DupFile]
    mode: str = "content"

    @property
    def copies(self) -> int:
        return len(self.files)

    @property
    def wasted(self) -> int:
        """Space freed by keeping one copy (hard links to the same inode count once)."""
        if self.mode != "content":
            return sum(f.size for f in self.files) - max(f.size for f in self.files)
        return self.size * (len({(f.dev, f.ino) for f in self.files}) - 1)


@dataclass
class Options:
    roots: list[str]
    mode: str = "content"          # content | name_size | name
    min_size: int = 1              # bytes; 0 includes empty files
    max_size: int = 0              # 0 = no limit
    include: str = ""              # ";"-separated globs, e.g. "*.jpg;*.png" (empty = all)
    exclude: str = ""              # ";"-separated folder/file globs to skip, e.g. ".git;node_modules"
    hidden: bool = False
    hardlinks: bool = False        # list every path of a hard-linked file
    skip_trash: bool = True
    ignore_case: bool = True       # name modes only


@dataclass
class Result:
    groups: list[DupGroup]
    files_scanned: int = 0
    bytes_hashed: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cancelled: bool = False


def _globs(s: str) -> list[str]:
    return [g.strip() for g in s.replace(",", ";").split(";") if g.strip()]


def _hash(path: str, part: bool, cancel: threading.Event | None, counter: list[int], buf=None) -> bytes:
    """BLAKE2b-256 digest (32 raw bytes).

    part=True: quick check — files up to 2*PART are hashed WHOLE (so the quick hash is a full hash
    for them); larger files hash the first and last PART bytes.
    part=False: the whole file, read into one reusable buffer (no per-chunk allocations)."""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb", buffering=0) as f:
        size = os.fstat(f.fileno()).st_size
        if part:
            if size <= 2 * PART:
                data = f.read(2 * PART + 1)
                h.update(data)
                counter[0] += len(data)
            else:
                h.update(f.read(PART))
                f.seek(-PART, os.SEEK_END)
                h.update(f.read(PART))
                counter[0] += 2 * PART
            return h.digest()
        mv = memoryview(buf if buf is not None else bytearray(CHUNK))
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            n = f.readinto(mv)
            if not n:
                break
            h.update(mv[:n])
            counter[0] += n
    return h.digest()


def file_hash(path: str) -> str:
    """Full BLAKE2b-256 of a file, hex (used to verify audit copies)."""
    return _hash(path, False, None, [0]).hex()


def scan(opts: Options, progress=None, cancel: threading.Event | None = None) -> Result:
    """Find duplicate files. *progress(dict)* is called at most ~10×/s from this thread.

    Memory: while walking, each file is one small tuple (folder index, name, size, mtime, dev, inode,
    nlink) in a bucket keyed by size (or name); full paths and DupFile objects are only built for files
    that end up in a duplicate group. Working structures are released stage by stage.
    """
    t0 = time.monotonic()
    res = Result(groups=[])
    inc, exc = _globs(opts.include), _globs(opts.exclude)
    inc_l = [g.lower() for g in inc]
    trash_dirs = {os.path.realpath(fsutil.home_trash())} if opts.skip_trash else set()
    last = [0.0]

    def report(phase: str, done: int, total: int, path: str = "", force: bool = False) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        now = time.monotonic()
        if progress and (force or now - last[0] > 0.1):
            last[0] = now
            progress({"phase": phase, "done": done, "total": total, "path": path, "files": res.files_scanned})

    if opts.mode == "name_size":
        bucket_key = lambda name, size: (name.lower() if opts.ignore_case else name, size)  # noqa: E731
    elif opts.mode == "name":
        bucket_key = lambda name, size: name.lower() if opts.ignore_case else name  # noqa: E731
    else:
        bucket_key = lambda name, size: size  # noqa: E731

    # ---------------------------------------------------------------- 1. walk
    dirs: list[str] = []                       # folder table; entries refer to it by index
    buckets: dict = {}                          # key -> list[(dir_idx, name, size, mtime, dev, ino, nlink)]
    seen_dirs: set[tuple[int, int]] = set()     # (dev, inode) of folders: overlapping roots, bind mounts, loops
    roots: list[str] = []
    for r in opts.roots:
        rp = os.path.realpath(os.path.expanduser(r))
        if rp not in roots:
            roots.append(rp)
    stack = list(reversed(roots))
    try:
        while stack:
            d = stack.pop()
            if d in PSEUDO_FS or any(d.startswith(p + "/") for p in PSEUDO_FS):
                continue
            try:
                dst = os.stat(d)
            except OSError as e:
                res.errors.append(f"{fsutil.display_path(d)}: {e.strerror or e}")
                continue
            if (dst.st_dev, dst.st_ino) in seen_dirs:
                continue
            seen_dirs.add((dst.st_dev, dst.st_ino))
            report("walk", res.files_scanned, 0, d)
            try:
                it = os.scandir(d)
            except OSError as e:
                res.errors.append(f"{fsutil.display_path(d)}: {e.strerror or e}")
                continue
            di = -1
            subdirs = []
            with it:
                for de in it:
                    name = de.name
                    if not opts.hidden and name.startswith("."):
                        continue
                    if exc and any(fnmatch.fnmatch(name, g) for g in exc):
                        continue
                    try:
                        if de.is_dir(follow_symlinks=False):
                            if name.startswith((AUDIT_PREFIX, LEGACY_AUDIT_PREFIX)):
                                continue           # our own audit folders
                            p = de.path
                            if trash_dirs and os.path.realpath(p) in trash_dirs:
                                continue
                            subdirs.append(p)
                            continue
                        if not de.is_file(follow_symlinks=False):
                            continue               # symlinks, FIFOs, sockets, devices
                        st = de.stat(follow_symlinks=False)
                    except OSError as e:
                        res.errors.append(f"{fsutil.display_path(de.path)}: {e.strerror or e}")
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    size = st.st_size
                    if size < opts.min_size or (opts.max_size and size > opts.max_size):
                        continue
                    if inc_l and not any(fnmatch.fnmatch(name.lower(), g) for g in inc_l):
                        continue
                    if di < 0:
                        di = len(dirs)
                        dirs.append(d)
                    k = bucket_key(name, size)
                    e = (di, name, size, st.st_mtime, st.st_dev, st.st_ino, st.st_nlink)
                    b = buckets.get(k)
                    if b is None:
                        buckets[k] = [e]
                    else:
                        b.append(e)
                    res.files_scanned += 1
            stack.extend(sorted(subdirs, reverse=True))

        # ------------------------------------------------------------ candidates
        def path_of(e) -> str:
            return os.path.join(dirs[e[0]], e[1])

        def collapse(entries: list) -> list:
            """Sort by path; with hard links hidden, keep one entry per inode (+ its other paths)."""
            entries.sort(key=lambda e: (dirs[e[0]], e[1]))
            if opts.hardlinks:
                return [(e, None) for e in entries]
            first: dict = {}
            out = []
            for e in entries:
                ik = (e[4], e[5])
                if ik in first:
                    links = first[ik][1]
                    if links is None:
                        links = first[ik][1] = []
                    links.append(path_of(e))
                else:
                    first[ik] = [e, None]
                    out.append(first[ik])
            return out

        def unique_inodes(entries) -> int:
            return len({(e[4], e[5]) for e in entries})

        def make_files(items) -> list[DupFile]:
            return [DupFile(path_of(e), e[2], e[3], e[4], e[5], e[6], links) for e, links in items]

        cands = [b for b in buckets.values() if len(b) > 1 and (opts.hardlinks or unique_inodes(b) > 1)]
        buckets = None                          # free the per-file table of everything else
        gid = 0

        # ------------------------------------------------------------ name modes
        if opts.mode in ("name", "name_size"):
            for b in cands:
                items = collapse(b)
                if len(items) > 1:
                    gid += 1
                    fs = make_files(items)
                    res.groups.append(DupGroup(gid, max(f.size for f in fs), fs[0].name, fs, opts.mode))
            res.seconds = time.monotonic() - t0
            report("done", 1, 1, force=True)
            return res

        # ------------------------------------------------------------ 2+3. quick hash per size bucket
        counter = [0]
        n_quick = sum(len(b) for b in cands)
        done = 0
        stage2: list[list] = []                 # same size + same quick hash, file > 2*PART: needs full hash
        for b in cands:
            items = collapse(b)
            size = items[0][0][2]
            sub: dict[bytes, list] = {}
            for item in items:
                p = path_of(item[0])
                report("quick", done, n_quick, p)
                done += 1
                try:
                    hq = _hash(p, True, cancel, counter)
                except OSError as e:
                    res.errors.append(f"{fsutil.display_path(p)}: {e.strerror or e}")
                    continue
                sub.setdefault(hq, []).append(item)
            for hq, grp in sub.items():
                if len(grp) < 2 or (not opts.hardlinks and len({(e[4], e[5]) for e, _ in grp}) < 2):
                    continue
                if size <= 2 * PART:            # quick hash covered the whole file
                    gid += 1
                    res.groups.append(DupGroup(gid, size, hq.hex(), make_files(grp), "content"))
                else:
                    stage2.append(grp)
        cands = None

        # ------------------------------------------------------------ 4. full hash
        total_bytes = sum(len(grp) * grp[0][0][2] for grp in stage2)
        counter = [0]
        buf = bytearray(CHUNK)                  # one read buffer for every file
        while stage2:
            grp = stage2.pop()                  # release as we go
            size = grp[0][0][2]
            sub = {}
            for item in grp:
                p = path_of(item[0])
                report("full", counter[0], total_bytes, p)
                try:
                    hf = _hash(p, False, cancel, counter, buf)
                except OSError as e:
                    res.errors.append(f"{fsutil.display_path(p)}: {e.strerror or e}")
                    continue
                sub.setdefault(hf, []).append(item)
            for hf, g2 in sub.items():
                if len(g2) > 1 and (opts.hardlinks or len({(e[4], e[5]) for e, _ in g2}) > 1):
                    gid += 1
                    res.groups.append(DupGroup(gid, size, hf.hex(), make_files(g2), "content"))
        res.bytes_hashed = counter[0]
    except Cancelled:
        res.cancelled = True
    res.groups.sort(key=lambda g: g.files[0].path)
    for i, g in enumerate(res.groups, 1):       # stable ids in path order
        g.gid = i
    res.seconds = time.monotonic() - t0
    if progress:
        progress({"phase": "done", "done": 1, "total": 1, "path": "", "files": res.files_scanned})
    return res


# =========================================================================== selection rules
def select_rule(groups: list[DupGroup], rule: str, folder: str = "") -> set[str]:
    """Paths to select. Always leaves at least one file per group unselected.

    rule: all_but_newest | all_but_oldest | all_but_first | all_but_shortest | in_folder
    ("first" = first in the list as currently ordered by the caller.)
    """
    out: set[str] = set()
    for g in groups:
        fs = g.files
        if rule == "in_folder":
            inside = [f for f in fs if fsutil.is_inside(f.path, folder)]
            if len(inside) == len(fs):
                inside = inside[1:]                    # keep one
            out.update(f.path for f in inside)
            continue
        if rule == "all_but_newest":
            keep = max(fs, key=lambda f: f.mtime)
        elif rule == "all_but_oldest":
            keep = min(fs, key=lambda f: f.mtime)
        elif rule == "all_but_shortest":
            keep = min(fs, key=lambda f: (len(f.path), f.path))
        else:
            keep = fs[0]
        out.update(f.path for f in fs if f is not keep)
    return out


def groups_fully_selected(groups: list[DupGroup], selected: set[str]) -> list[DupGroup]:
    return [g for g in groups if all(f.path in selected for f in g.files)]


# =========================================================================== actions
@dataclass
class ActionResult:
    done: list[str] = field(default_factory=list)          # original paths handled
    skipped: list[str] = field(default_factory=list)       # messages
    errors: list[str] = field(default_factory=list)
    manifest: str = ""                                      # audit manifest path (move)
    audit_dir: str = ""


def _unchanged(f: DupFile) -> str:
    """'' if the file still matches the scan, else a reason."""
    try:
        st = os.lstat(f.path)
    except OSError as e:
        return e.strerror or "missing"
    if not stat.S_ISREG(st.st_mode):
        return "no longer a regular file"
    if st.st_size != f.size or abs(st.st_mtime - f.mtime) > 1e-6:
        return "changed since the scan"
    return ""


def _keeper(g: DupGroup, selected: set[str]) -> DupFile | None:
    for f in g.files:
        if f.path not in selected and not _unchanged(f):
            return f
    return None


def apply(groups: list[DupGroup], selected: set[str], action: str, audit_root: str = "",
          progress=None, cancel: threading.Event | None = None) -> ActionResult:
    """action: trash | delete | move.  Only touches files in *selected*.

    Safety: each file must be unchanged since the scan, and its group must still have an
    unselected, unchanged copy (the keeper) — otherwise it is skipped.
    """
    res = ActionResult()
    todo = [(g, f) for g in groups for f in g.files if f.path in selected]
    writer = mf = None
    if action == "move":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        res.audit_dir = os.path.join(audit_root, AUDIT_PREFIX + stamp)
        os.makedirs(res.audit_dir, exist_ok=False)
        res.manifest = os.path.join(res.audit_dir, MANIFEST)
        mf = open(res.manifest, "w", newline="", encoding="utf-8", errors="surrogateescape")
        writer = csv.writer(mf)
        writer.writerow(["moved_at", "original_path", "audit_path", "size_bytes", "modified",
                         "match", "group", "kept_copy"])
    try:
        for i, (g, f) in enumerate(todo):
            if cancel is not None and cancel.is_set():
                res.skipped.append("Cancelled")
                break
            if progress:
                progress(i, len(todo), f.path)
            why = _unchanged(f)
            if why:
                res.skipped.append(f"{fsutil.display_path(f.path)}: {why}")
                continue
            keep = _keeper(g, selected)
            if keep is None:
                res.skipped.append(f"{fsutil.display_path(f.path)}: no other copy would remain")
                continue
            try:
                if action == "trash":
                    fsutil.move_to_trash(f.path)
                elif action == "delete":
                    os.unlink(f.path)
                elif action == "move":
                    dest = os.path.join(res.audit_dir, f.path.lstrip("/"))
                    _move_verified(f.path, dest, g.key if g.mode == "content" else "")
                    writer.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), os.fsdecode(f.path), dest, f.size,
                                     time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.mtime)),
                                     ("blake2b-256:" + g.key) if g.mode == "content" else g.mode,
                                     g.gid, keep.path])
                    mf.flush()
                else:
                    raise ValueError(action)
                res.done.append(f.path)
            except OSError as e:
                res.errors.append(f"{fsutil.display_path(f.path)}: {e.strerror or e}")
    finally:
        if mf:
            mf.close()
    if progress:
        progress(len(todo), len(todo), "")
    return res


def _move_verified(src: str, dest: str, digest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        raise FileExistsError(17, "audit destination exists", dest)
    try:
        os.rename(src, dest)                        # same file system: atomic, keeps everything
        return
    except OSError as e:
        if e.errno != 18:                           # EXDEV: different file system -> copy
            raise
    shutil.copy2(src, dest)
    ok = os.path.getsize(dest) == os.path.getsize(src) and (not digest or file_hash(dest) == digest)
    if not ok:
        os.unlink(dest)
        raise OSError(5, "copy verification failed; original kept", src)
    os.unlink(src)


def undo_audit(manifest: str, progress=None) -> ActionResult:
    """Move files from an audit folder back to where they were (per the manifest)."""
    res = ActionResult(manifest=manifest, audit_dir=os.path.dirname(manifest))
    with open(manifest, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        rows = list(csv.DictReader(fh))
    for i, r in enumerate(rows):
        if progress:
            progress(i, len(rows), r["original_path"])
        src, orig = r["audit_path"], r["original_path"]
        if not fsutil.is_inside(src, res.audit_dir):
            res.errors.append(f"{src}: outside the audit folder; ignored")
            continue
        if not os.path.lexists(src):
            res.skipped.append(f"{fsutil.display_path(orig)}: no longer in the audit folder")
            continue
        if os.path.lexists(orig):
            res.skipped.append(f"{fsutil.display_path(orig)}: a file already exists there")
            continue
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            _move_verified(src, orig, "")
            res.done.append(orig)
        except OSError as e:
            res.errors.append(f"{fsutil.display_path(orig)}: {e.strerror or e}")
    return res


def export_csv(groups: list[DupGroup], path: str, selected: set[str] | None = None) -> None:
    with open(path, "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["group", "copies", "match", "name", "folder", "size_bytes", "modified", "selected"])
        for g in groups:
            for f in g.files:
                w.writerow([g.gid, g.copies, ("blake2b-256:" + g.key) if g.mode == "content" else g.mode,
                            f.name, f.folder, f.size,
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.mtime)),
                            "yes" if selected and f.path in selected else ""])
