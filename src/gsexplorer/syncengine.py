# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""GSExplorer+ Sync & Backup engine (no Qt dependency; also used by `gsexplorer-plus --sync`).

Modes (per task / folder pair):
  mirror    left -> right; right becomes an exact copy (extra files on right are removed)
  update    left -> right; new/newer files copied, nothing ever deleted on right
  twoway    changes flow both ways; a per-pair state database detects deletions and
            conflicts (file changed on both sides, or changed on one and deleted on other)
  snapshot  luckyBackup-style dated snapshots of left inside right; unchanged files are
            hard links to the previous snapshot (rsync --link-dest semantics), keep N

Safety rules:
  * Overwritten / deleted files are versioned (trash, versions folder, or permanent).
  * New content is written to a temp file and atomically renamed into place.
  * Symlinks are copied as links and never followed.
  * Pre-flight blocks: missing source, target whose parent is missing (unmounted
    drive), nested pairs, and a mirror whose source is empty while the target is not.
  * A per-pair lock prevents a scheduled run and a GUI run touching the same pair.
"""
from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import fsutil

# ----------------------------------------------------------------------------- constants
MODES = ("mirror", "update", "twoway", "snapshot")
MODE_LABELS = {"mirror": "Mirror →", "update": "Update →", "twoway": "Two-way ↔", "snapshot": "Snapshot backup"}
COMPARES = ("time_size", "content", "size")
COMPARE_LABELS = {"time_size": "Time and size", "content": "File content", "size": "File size"}
VERSIONING = ("trash", "folder", "none")
VERSIONING_LABELS = {"trash": "Move to trash", "folder": "Move to versions folder",
                     "none": "Delete permanently"}

# comparison categories
EQUAL, LEFT_ONLY, RIGHT_ONLY = "equal", "left_only", "right_only"
LEFT_NEWER, RIGHT_NEWER, DIFFERENT = "left_newer", "right_newer", "different"
TYPE_DIFF, CONFLICT = "type_diff", "conflict"
CATEGORY_LABELS = {EQUAL: "Equal", LEFT_ONLY: "Left only", RIGHT_ONLY: "Right only",
                   LEFT_NEWER: "Left newer", RIGHT_NEWER: "Right newer", DIFFERENT: "Different",
                   TYPE_DIFF: "Type differs", CONFLICT: "Conflict"}

# actions
NONE, COPY_LR, COPY_RL, DEL_L, DEL_R, LINK = "none", "copy_lr", "copy_rl", "del_l", "del_r", "link"
ACTION_LABELS = {NONE: "—", COPY_LR: "→", COPY_RL: "←", DEL_L: "Delete left", DEL_R: "Delete right",
                 LINK: "Link (unchanged)"}

DEFAULT_EXCLUDES = [".Trash-*/", ".Trash/", "lost+found/", "*.gxpart", "*.gxsync-tmp",
                    ".gexplorer-versions/", ".gsexplorer-plus-versions/"]
SNAP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,60}$")

# exit codes (same meaning as FreeFileSync batch jobs)
EXIT_OK, EXIT_WARN, EXIT_ERROR, EXIT_CANCELLED = 0, 1, 2, 3


class Cancelled(Exception):
    pass


class PreflightError(Exception):
    pass


# ----------------------------------------------------------------------------- paths

def _data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")


def _config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def profiles_dir() -> str:
    return os.path.join(_config_home(), "gsexplorer-plus", "sync")


def state_dir() -> str:
    return os.path.join(_data_home(), "gsexplorer-plus", "sync-state")


def logs_dir() -> str:
    return os.path.join(_data_home(), "gsexplorer-plus", "sync-logs")


# ----------------------------------------------------------------------------- model

@dataclass
class Task:
    name: str = "Task"
    enabled: bool = True
    mode: str = "mirror"
    left: str = ""
    right: str = ""
    compare: str = "time_size"
    tolerance: int = 2                      # seconds (FAT/exFAT store 2-second times)
    include: list = field(default_factory=lambda: ["*"])
    exclude: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    versioning: str = "trash"
    versions_dir: str = ""
    versions_keep_days: int = 0             # 0 = keep forever
    snapshots_keep: int = 10
    allow_empty_source: bool = False
    ignore_hour_shift: bool = False         # also auto-on when either side is a FAT/exFAT drive
    pre_cmd: str = ""
    post_cmd: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        t = cls()
        for k, v in (d or {}).items():
            if hasattr(t, k):
                setattr(t, k, v)
        t.mode = t.mode if t.mode in MODES else "mirror"
        t.compare = t.compare if t.compare in COMPARES else "time_size"
        t.versioning = t.versioning if t.versioning in VERSIONING else "trash"
        t.include = [str(x) for x in (t.include or ["*"]) if str(x).strip()] or ["*"]
        t.exclude = [str(x) for x in (t.exclude or []) if str(x).strip()]
        t.tolerance = max(0, int(t.tolerance or 0))
        t.snapshots_keep = max(1, int(t.snapshots_keep or 1))
        t.versions_keep_days = max(0, int(t.versions_keep_days or 0))
        return t

    @property
    def label(self) -> str:
        return f"{self.name} ({MODE_LABELS[self.mode]})"


@dataclass
class Profile:
    name: str = "Default"
    tasks: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "version": 1, "tasks": [asdict(t) for t in self.tasks]},
                          indent=2, ensure_ascii=True)

    @classmethod
    def from_json(cls, text: str) -> "Profile":
        d = json.loads(text)
        return cls(str(d.get("name") or "Profile"), [Task.from_dict(t) for t in d.get("tasks") or []])


def valid_profile_name(name: str) -> str | None:
    if not NAME_RE.match(name or ""):
        return "Use 1–60 letters, digits, spaces, dots, dashes or underscores."
    return None


def list_profiles() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(profiles_dir()) if f.endswith(".json"))
    except OSError:
        return []


def load_profile(name: str) -> Profile:
    with open(os.path.join(profiles_dir(), name + ".json"), "r", encoding="utf-8") as f:
        p = Profile.from_json(f.read())
    p.name = name
    return p


def save_profile(p: Profile) -> str:
    err = valid_profile_name(p.name)
    if err:
        raise ValueError(err)
    d = profiles_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, p.name + ".json")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".p.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(p.to_json())
    os.replace(tmp, path)
    return path


def delete_profile(name: str) -> None:
    os.unlink(os.path.join(profiles_dir(), name + ".json"))


# ----------------------------------------------------------------------------- filters

class Filter:
    """Include/exclude rules.

    * A pattern without "/" matches a name at any depth:      *.tmp   node_modules/
    * A pattern containing "/" matches the path from the root: build/out   docs/*.pdf
    * A trailing "/" restricts the pattern to folders.
    * ``*`` and ``?`` wildcards; matching is case-insensitive.
    * Excluding a folder excludes everything inside it.
    """

    def __init__(self, include: list[str], exclude: list[str]):
        self.inc = [self._prep(p) for p in include if p.strip()] or [self._prep("*")]
        self.exc = [self._prep(p) for p in exclude if p.strip()]
        self.include_all = any(p == ("*", False, False) for p in self.inc)

    @staticmethod
    def _prep(p: str):
        p = p.strip().replace("\\", "/")
        dir_only = p.endswith("/")
        p = p.strip("/")
        return (p.casefold(), "/" in p, dir_only)

    @staticmethod
    def _match(rule, rel: str, name: str, is_dir: bool) -> bool:
        pat, anchored, dir_only = rule
        if dir_only and not is_dir:
            return False
        return fnmatch.fnmatchcase(rel if anchored else name, pat)

    def excluded(self, rel: str, is_dir: bool) -> bool:
        r = rel.casefold()
        name = r.rsplit("/", 1)[-1]
        return any(self._match(x, r, name, is_dir) for x in self.exc)

    def included(self, rel: str, is_dir: bool, parent_included: bool) -> bool:
        if parent_included or self.include_all:
            return True
        r = rel.casefold()
        name = r.rsplit("/", 1)[-1]
        return any(self._match(x, r, name, is_dir) for x in self.inc)


# ----------------------------------------------------------------------------- scanning

@dataclass
class Info:
    kind: str            # "f" file, "d" dir, "l" symlink, "o" other (fifo/socket/device)
    size: int = 0
    mtime: float = 0.0
    link: str = ""
    mode: int = 0


def scan(root: str, flt: Filter, checkpoint: Callable[[], None] | None = None,
         progress: Callable[[int, str], None] | None = None,
         extra_excludes: list[str] | None = None) -> tuple[dict[str, Info], list[str]]:
    """Walk *root* without following symlinks. Returns ({rel: Info}, errors)."""
    out = ScanMap()
    errors: list[str] = []
    skip_abs = {os.path.abspath(p) for p in (extra_excludes or []) if p}
    stack: list[tuple[str, str, bool]] = [(root, "", False)]
    n = 0
    while stack:
        d, rel_d, parent_inc = stack.pop()
        if checkpoint:
            checkpoint()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as e:
            errors.append(f"{fsutil.display_path(d)}: {e.strerror or e}")
            continue
        for de in entries:
            rel = f"{rel_d}/{de.name}" if rel_d else de.name
            if os.path.join(d, de.name) in skip_abs:
                continue
            try:
                st = de.stat(follow_symlinks=False)
            except OSError as e:
                errors.append(f"{fsutil.display_path(os.path.join(d, de.name))}: {e.strerror or e}")
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if flt.excluded(rel, is_dir):
                continue
            inc = flt.included(rel, is_dir, parent_inc)
            if is_dir:
                if inc:
                    out[rel] = Info("d", 0, st.st_mtime, "", st.st_mode)
                stack.append((os.path.join(d, de.name), rel, inc))
            elif inc:
                if stat.S_ISLNK(st.st_mode):
                    try:
                        link = os.readlink(os.path.join(d, de.name))
                    except OSError:
                        link = ""
                    out[rel] = Info("l", 0, st.st_mtime, link, st.st_mode)
                elif stat.S_ISREG(st.st_mode):
                    out[rel] = Info("f", st.st_size, st.st_mtime, "", st.st_mode)
                else:
                    out[rel] = Info("o", 0, st.st_mtime, "", st.st_mode)
            n += 1
            if progress and n % 250 == 0:
                progress(n, rel)
    if not flt.include_all:
        # make sure parents of included files exist as dir entries
        for rel in list(out):
            p = rel.rpartition("/")[0]
            while p and p not in out:
                try:
                    st = os.lstat(os.path.join(root, p))
                    out[p] = Info("d", 0, st.st_mtime, "", st.st_mode)
                except OSError:
                    break
                p = p.rpartition("/")[0]
    return out, errors


def _files_equal(a: str, b: str, checkpoint) -> bool:
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                if checkpoint:
                    checkpoint()
                x = fa.read(1 << 20)
                y = fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- compare

@dataclass
class Item:
    rel: str
    left: Info | None
    right: Info | None
    category: str
    action: str
    default_action: str = ""
    note: str = ""
    files: int = 0           # for one-sided folders: number of files inside
    bytes: int = 0           # bytes that the action will transfer

    def __post_init__(self):
        if not self.default_action:
            self.default_action = self.action


@dataclass
class Plan:
    task: Task
    items: list = field(default_factory=list)
    left_scan: dict = field(default_factory=dict)
    right_scan: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    first_twoway: bool = False

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for it in self.items:
            c[it.category] = c.get(it.category, 0) + 1
        return c

    def actionable(self) -> list[Item]:
        return [i for i in self.items if i.action not in (NONE, LINK)]

    def summary(self) -> dict:
        s = {"copy_lr": 0, "copy_rl": 0, "del_l": 0, "del_r": 0, "bytes": 0, "conflicts": 0}
        for i in self.items:
            if i.action in s:
                s[i.action] += max(1, i.files) if i.action in (COPY_LR, COPY_RL) else 1
            if i.action in (COPY_LR, COPY_RL):
                s["bytes"] += i.bytes
            if i.category == CONFLICT and i.action == NONE:
                s["conflicts"] += 1
        return s


def _sortkey(rel: str):
    return rel.split("/")


class ScanMap(dict):
    """rel -> Info, with O(subtree) descendant lookup (children are contiguous when
    keys are sorted by path components)."""
    _keys: list | None = None
    _pos: dict | None = None

    def subtree(self, rel: str) -> list[tuple[str, Info]]:
        if self._keys is None or len(self._keys) != len(self):
            self._keys = sorted(self, key=_sortkey)
            self._pos = {k: i for i, k in enumerate(self._keys)}
        i = self._pos.get(rel)
        if i is None:
            return []
        pre, out, keys = rel + "/", [], self._keys
        j = i + 1
        while j < len(keys) and keys[j].startswith(pre):
            out.append((keys[j], self[keys[j]]))
            j += 1
        return out


def _subtree(scan_: dict, rel: str) -> list[tuple[str, Info]]:
    if isinstance(scan_, ScanMap):
        return scan_.subtree(rel)
    pre = rel + "/"
    return [(k, v) for k, v in scan_.items() if k.startswith(pre)]


def _pair_key(task: Task) -> str:
    raw = (os.path.abspath(task.left) + "\0" + os.path.abspath(task.right)).encode("utf-8", "surrogateescape")
    return hashlib.sha1(raw).hexdigest()


def db_path(task: Task) -> str:
    return os.path.join(state_dir(), _pair_key(task) + ".json")


def load_db(task: Task) -> dict | None:
    try:
        with open(db_path(task), "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("left") == task.left and d.get("right") == task.right:
            return d.get("items") or {}
    except (OSError, ValueError):
        pass
    return None


def save_db(task: Task, items: dict) -> None:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir(), prefix=".db.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"left": task.left, "right": task.right, "saved": time.time(), "items": items},
                  f, ensure_ascii=True)
    os.replace(tmp, db_path(task))


def _counts_path() -> str:
    return os.path.join(state_dir(), "scan-counts.json")


def expected_items(task: Task) -> int:
    """Item count of this pair's previous scan (0 if never scanned) — drives the progress bar."""
    try:
        with open(_counts_path(), "r", encoding="utf-8") as f:
            return int(json.load(f).get(_pair_key(task) + task.mode, 0))
    except (OSError, ValueError, TypeError):
        return 0


def _save_count(task: Task, n: int) -> None:
    try:
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        try:
            with open(_counts_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d[_pair_key(task) + task.mode] = n
        fd, tmp = tempfile.mkstemp(dir=state_dir(), prefix=".cnt.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _counts_path())
    except OSError:
        pass


FAT_FS = {"vfat", "msdos", "fat", "exfat", "fuse.exfat", "fuse.fusefat", "fuse.exfat-fuse"}


def _mount_fstype(path: str) -> str:
    """Filesystem type of the mount containing *path* (longest matching mount point)."""
    path = os.path.abspath(path)
    best, fstype = "", ""
    try:
        with open("/proc/self/mounts", "rb") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = os.fsdecode(parts[1]).replace("\\040", " ")
                if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, fstype = mp, parts[2].decode(errors="replace")
    except OSError:
        pass
    return fstype


def fat_family(path: str) -> bool:
    """True when *path* is on FAT/exFAT. Those store local time (or 2-second steps), so a
    daylight-saving change can make every file look exactly one hour different."""
    return _mount_fstype(path) in FAT_FS


def _shift_ok(task: Task) -> bool:
    v = getattr(task, "_shift_cache", None)
    if v is None:
        v = bool(task.ignore_hour_shift) or fat_family(task.left) or fat_family(task.right)
        try:
            object.__setattr__(task, "_shift_cache", v)
        except (AttributeError, TypeError):
            pass
    return v


def _time_eq(task: Task, a: float, b: float) -> bool:
    d = abs(a - b)
    if d <= task.tolerance:
        return True
    return _shift_ok(task) and abs(d - 3600) <= task.tolerance


def _same(task: Task, a: Info, b: Info, pa: str, pb: str, checkpoint) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "d":
        return True
    if a.kind == "l":
        return a.link == b.link
    if a.kind == "o":
        return True
    if a.size != b.size:
        return False
    if task.compare == "size":
        return True
    if task.compare == "content":
        return _files_equal(pa, pb, checkpoint)
    return _time_eq(task, a.mtime, b.mtime)


def _changed_since(info: Info, e, task: Task) -> bool:
    if e is None:
        return True
    kind, size, mtime = e[0], e[1], e[2]
    if info.kind != kind:
        return True
    if info.kind == "d":
        return False
    return info.size != size or not _time_eq(task, info.mtime, mtime)


def compare(task: Task, checkpoint=None, progress=None) -> Plan:
    plan = Plan(task)
    flt = Filter(task.include, task.exclude)
    extra = [task.versions_dir] if task.versioning == "folder" and task.versions_dir else []
    plan.warnings, plan.errors = preflight(task)
    if plan.errors:
        return plan
    # progress(n, rel) reports a cumulative item count across both sides
    p_left = (lambda n, rel: progress(n, "left", rel)) if progress else None
    left, e1 = scan(task.left, flt, checkpoint, p_left, extra)
    base = len(left)
    p_right = (lambda n, rel: progress(base + n, "right", rel)) if progress else None
    if task.mode == "snapshot":
        latest = latest_snapshot(task.right)
        right, e2 = (scan(latest, Filter(["*"], []), checkpoint, p_right) if latest else (ScanMap(), []))
        if latest:
            plan.warnings.append(f"Compared with latest snapshot {os.path.basename(latest)}")
    else:
        right, e2 = (scan(task.right, flt, checkpoint, p_right, extra)
                     if os.path.isdir(task.right) else (ScanMap(), []))
    plan.left_scan, plan.right_scan = left, right
    plan.errors += e1 + e2
    _save_count(task, len(left) + len(right))
    if progress:
        progress(len(left) + len(right), "compare", "")

    db = load_db(task) if task.mode == "twoway" else None
    plan.first_twoway = task.mode == "twoway" and db is None
    covered: str | None = None
    for rel in sorted(set(left) | set(right), key=_sortkey):
        if checkpoint:
            checkpoint()
        if covered is not None and rel.startswith(covered + "/"):
            continue
        covered = None
        L, R = left.get(rel), right.get(rel)
        pl, pr = os.path.join(task.left, rel), os.path.join(task.right, rel)
        if task.mode == "snapshot":
            item = _snapshot_item(task, rel, L, R, pl, (os.path.join(latest, rel) if R else ""), checkpoint)
        elif L is not None and R is not None:
            item = _both_item(task, rel, L, R, pl, pr, db, checkpoint)
        else:
            item = _one_side_item(task, rel, L, R, left, right, db)
            if (L or R).kind == "d":
                covered = rel
        if item is not None:
            plan.items.append(item)
    return plan


def _cat_both(task: Task, L: Info, R: Info, pl: str, pr: str, checkpoint) -> str:
    if L.kind != R.kind:
        return TYPE_DIFF
    if _same(task, L, R, pl, pr, checkpoint):
        return EQUAL
    if L.kind == "f" and not _time_eq(task, L.mtime, R.mtime):
        return LEFT_NEWER if L.mtime > R.mtime else RIGHT_NEWER
    return DIFFERENT


def _both_item(task, rel, L, R, pl, pr, db, checkpoint) -> Item | None:
    cat = _cat_both(task, L, R, pl, pr, checkpoint)
    if cat == EQUAL:
        return Item(rel, L, R, EQUAL, NONE) if L.kind != "d" else None
    size_l = L.size if L.kind == "f" else 0
    size_r = R.size if R.kind == "f" else 0
    if task.mode == "mirror":
        note = "target is newer; mirror replaces it (old version kept per versioning)" if cat == RIGHT_NEWER else ""
        return Item(rel, L, R, cat, COPY_LR, bytes=size_l, note=note)
    if task.mode == "update":
        if cat in (LEFT_NEWER, DIFFERENT):
            return Item(rel, L, R, cat, COPY_LR, bytes=size_l)
        if cat == RIGHT_NEWER:
            return Item(rel, L, R, cat, NONE, note="right is newer; Update never overwrites newer files")
        return Item(rel, L, R, CONFLICT, NONE, note="file on one side, folder on the other")
    # two-way
    if cat == TYPE_DIFF:
        return Item(rel, L, R, CONFLICT, NONE, note="file on one side, folder on the other")
    if db is None:
        if cat == LEFT_NEWER:
            return Item(rel, L, R, cat, COPY_LR, bytes=size_l)
        if cat == RIGHT_NEWER:
            return Item(rel, L, R, cat, COPY_RL, bytes=size_r)
        return Item(rel, L, R, CONFLICT, NONE, note="same time, different content")
    e = db.get(rel)
    lc, rc = _changed_since(L, e, task), _changed_since(R, e, task)
    if e is None:
        return Item(rel, L, R, CONFLICT, NONE, note="created on both sides with different content")
    if lc and not rc:
        return Item(rel, L, R, cat, COPY_LR, bytes=size_l, note="changed on left")
    if rc and not lc:
        return Item(rel, L, R, cat, COPY_RL, bytes=size_r, note="changed on right")
    return Item(rel, L, R, CONFLICT, NONE, note="changed on both sides")


def _one_side_item(task, rel, L, R, left, right, db) -> Item | None:
    info = L or R
    src_scan = left if L else right
    files, total = 0, 0
    if info.kind == "d":
        for _k, v in _subtree(src_scan, rel):
            if v.kind == "f":
                files += 1
                total += v.size
    elif info.kind == "f":
        files, total = 1, info.size
    if L is not None:
        cat = LEFT_ONLY
        if task.mode in ("mirror", "update"):
            return Item(rel, L, None, cat, COPY_LR, files=files, bytes=total)
        # two-way
        e = db.get(rel) if db else None
        if e is None:
            return Item(rel, L, None, cat, COPY_LR, files=files, bytes=total, note="new on left")
        changed = _changed_since(L, e, task) or (
            info.kind == "d" and any(_changed_since(v, db.get(k), task) for k, v in _subtree(left, rel)))
        if changed:
            return Item(rel, L, None, CONFLICT, NONE, files=files, bytes=total,
                        note="changed on left, deleted on right")
        return Item(rel, L, None, cat, DEL_L, files=files, bytes=0, note="deleted on right")
    cat = RIGHT_ONLY
    if task.mode == "mirror":
        return Item(rel, None, R, cat, DEL_R, files=files, bytes=0)
    if task.mode == "update":
        return Item(rel, None, R, cat, NONE, files=files, note="Update never deletes")
    e = db.get(rel) if db else None
    if e is None:
        return Item(rel, None, R, cat, COPY_RL, files=files, bytes=total, note="new on right")
    changed = _changed_since(R, e, task) or (
        info.kind == "d" and any(_changed_since(v, db.get(k), task) for k, v in _subtree(right, rel)))
    if changed:
        return Item(rel, None, R, CONFLICT, NONE, files=files, bytes=total, note="changed on right, deleted on left")
    return Item(rel, None, R, cat, DEL_R, files=files, bytes=0, note="deleted on left")


def _snapshot_item(task, rel, L, R, pl, pr, checkpoint) -> Item | None:
    if L is None:
        return Item(rel, None, R, RIGHT_ONLY, NONE, note="not in source; kept only in older snapshots")
    if L.kind == "d":
        return None
    if R is not None and L.kind == "f" and R.kind == "f" and L.size == R.size \
            and _time_eq(task, L.mtime, R.mtime):
        return Item(rel, L, R, EQUAL, LINK, note="hard link to previous snapshot")
    cat = LEFT_ONLY if R is None else (TYPE_DIFF if L.kind != R.kind else LEFT_NEWER)
    return Item(rel, L, R, cat, COPY_LR, files=1 if L.kind == "f" else 0, bytes=L.size if L.kind == "f" else 0)


# ----------------------------------------------------------------------------- preflight

def _inside(a: str, b: str) -> bool:
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b.rstrip("/") + "/")


def preflight(task: Task) -> tuple[list[str], list[str]]:
    """Return (warnings, errors). Errors block the run."""
    warn: list[str] = []
    err: list[str] = []
    L, R = task.left.strip(), task.right.strip()
    if not L or not R:
        err.append("Both folders must be set.")
        return warn, err
    if not os.path.isabs(L) or not os.path.isabs(R):
        err.append("Use absolute folder paths.")
        return warn, err
    if not os.path.isdir(L):
        err.append(f"Source folder not found: {fsutil.display_path(L)} (drive not mounted?)")
    if task.mode == "twoway" and not os.path.isdir(R):
        err.append(f"Folder not found: {fsutil.display_path(R)} (drive not mounted?)")
    elif not os.path.isdir(R):
        parent = os.path.dirname(R.rstrip("/"))
        if os.path.isdir(parent):
            warn.append(f"Target folder will be created: {fsutil.display_path(R)}")
        else:
            err.append(f"Target's parent folder not found: {fsutil.display_path(parent)} (drive not mounted?)")
    if os.path.isdir(L) and (os.path.isdir(R) or os.path.isdir(os.path.dirname(R))):
        if _inside(R, L) or _inside(L, R):
            err.append("The two folders are the same or one is inside the other.")
    if task.versioning == "folder":
        if not task.versions_dir:
            err.append("Choose a versions folder (or another versioning option).")
        elif not os.path.isabs(task.versions_dir):
            err.append("The versions folder must be an absolute path.")
    if task.mode == "mirror" and not task.allow_empty_source and os.path.isdir(L) and os.path.isdir(R):
        try:
            l_empty = not any(True for _ in os.scandir(L))
            r_empty = not any(True for _ in os.scandir(R))
        except OSError:
            l_empty = r_empty = False
        if l_empty and not r_empty:
            err.append("Source is empty but target is not: mirroring would delete everything on the target. "
                       "If that's intended, enable “Allow empty source”.")
    if task.mode == "snapshot" and os.path.isdir(R) and not _hardlinks_ok(R):
        warn.append("Target filesystem doesn't support hard links: every snapshot will be a full copy.")
    return warn, err


def _hardlinks_ok(d: str) -> bool:
    try:
        fd, a = tempfile.mkstemp(dir=d, prefix=".gx-linktest-")
        os.close(fd)
        b = a + "-l"
        try:
            os.link(a, b)
            os.unlink(b)
            return True
        except OSError:
            return False
        finally:
            os.unlink(a)
    except OSError:
        return True   # can't test (read-only?) -> don't warn here


# ----------------------------------------------------------------------------- execution

class Context:
    """Callbacks the executor uses. GUI and CLI provide their own."""

    def __init__(self, checkpoint=None, progress=None, log=None, on_op=None):
        self.checkpoint = checkpoint or (lambda: None)
        self._progress = progress or (lambda *_: None)
        self._log = log or (lambda _m: None)
        self._on_op = on_op
        # what the executor is doing right now (for progress displays):
        # "lr" | "rl" | "delete-left" | "delete-right" | "snapshot" | "" (setup / finishing)
        self.direction = ""
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.stats = {"copied": 0, "bytes": 0, "deleted": 0, "dirs": 0, "linked": 0, "skipped": 0}

    def progress(self, nbytes: int, current: str) -> None:
        self._progress(nbytes, current)

    def log(self, msg: str) -> None:
        self._log(msg)

    def op(self, kind: str, src: str, dst: str = "", size: int = 0) -> None:
        """Announce the operation that starts now. *kind*: copy, symlink, mkdir, delete, hardlink,
        scan, hook, prune, finish. Bytes then arrive through progress(). A display callback that
        fails never stops the sync."""
        if self._on_op is not None:
            try:
                self._on_op(kind, src, dst, size, self.direction)
            except Exception:  # noqa: BLE001
                pass

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self._log("ERROR " + msg)


class _Lock:
    def __init__(self, task: Task):
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        self.path = os.path.join(state_dir(), _pair_key(task) + ".lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise PreflightError("Another sync of this folder pair is already running.")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


class Versioner:
    def __init__(self, task: Task, ctx: Context):
        self.task, self.ctx = task, ctx
        self.stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")

    def retire(self, path: str, root: str) -> None:
        """Move an about-to-be-overwritten/deleted item out of the way per versioning policy."""
        if not os.path.lexists(path):
            return
        v = self.task.versioning
        if v == "trash":
            fsutil.move_to_trash(path)
        elif v == "folder":
            rel = os.path.relpath(path, root)
            base, ext = os.path.splitext(os.path.basename(rel))
            if os.path.isdir(path) and not os.path.islink(path):
                base, ext = os.path.basename(rel), ""
            dest_dir = os.path.join(self.task.versions_dir, os.path.dirname(rel))
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, f"{base} {self.stamp}{ext}")
            dest = os.path.join(dest_dir, fsutil.unique_name(dest_dir, os.path.basename(dest)))
            shutil.move(path, dest)
        else:
            _remove(path)

    def prune(self) -> None:
        days = self.task.versions_keep_days
        if self.task.versioning != "folder" or days <= 0 or not os.path.isdir(self.task.versions_dir):
            return
        cutoff = time.time() - days * 86400
        pat = re.compile(r" (\d{4}-\d{2}-\d{2}_\d{6})")
        for root, dirs, files in os.walk(self.task.versions_dir, topdown=False):
            for n in files + dirs:
                m = pat.search(n)
                if not m:
                    continue
                try:
                    t = _dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S").timestamp()
                except ValueError:
                    continue
                if t < cutoff:
                    try:
                        _remove(os.path.join(root, n))
                    except OSError as e:
                        self.ctx.error(f"prune {n}: {e.strerror or e}")


def _remove(path: str) -> None:
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                p = os.path.join(root, d)
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
        os.rmdir(path)
    else:
        os.unlink(path)


def _copy_meta(src_st: os.stat_result, dst: str) -> None:
    """Copy times, then permissions, as separate best-effort steps. FAT, exFAT and many FUSE
    or network filesystems reject chmod; that must never cost us the modification time."""
    try:
        os.utime(dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
    except OSError:
        pass
    try:
        os.chmod(dst, stat.S_IMODE(src_st.st_mode))
    except OSError:
        pass


def _ensure_mtime(src_st: os.stat_result, dst: str, ctx: Context) -> None:
    """Some drivers reset mtime on rename (seen with FUSE FAT; also possible on SMB/NTFS-3g).
    Re-apply it after the rename and verify, so the next Compare doesn't recopy everything."""
    try:
        if abs(os.stat(dst).st_mtime - src_st.st_mtime) <= 1:
            return
        os.utime(dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
        if abs(os.stat(dst).st_mtime - src_st.st_mtime) > 2:
            raise OSError(errno.ENOTSUP, "mtime not kept")
    except OSError:
        if not getattr(ctx, "_mtime_warned", False):
            ctx._mtime_warned = True
            ctx.warnings.append("The target doesn't keep file modification times, so the next Compare may "
                                "list these files again. Set “Compare by” to File size or File content "
                                "for this task.")


def _copy_file(src: str, dst: str, ctx: Context, versioner: Versioner | None, dst_root: str) -> None:
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, fsutil.unique_name(d, "." + os.path.basename(dst) + ".gxsync-tmp"))
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            ctx.op("copy", src, dst, os.fstat(fi.fileno()).st_size)
            while True:
                ctx.checkpoint()
                buf = fi.read(1 << 20)
                if not buf:
                    break
                fo.write(buf)
                ctx.progress(len(buf), src)
        src_st = os.stat(src)
        _copy_meta(src_st, tmp)
        if os.path.lexists(dst):
            if versioner is not None and (os.path.isdir(dst) and not os.path.islink(dst)
                                          or versioner.task.versioning != "none"):
                versioner.retire(dst, dst_root)
            elif os.path.isdir(dst) and not os.path.islink(dst):
                _remove(dst)
        os.replace(tmp, dst)
        _ensure_mtime(src_st, dst, ctx)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_entry(src: str, dst: str, info: Info, ctx: Context, versioner, dst_root: str) -> None:
    if info.kind == "f":
        _copy_file(src, dst, ctx, versioner, dst_root)
        ctx.stats["copied"] += 1
        ctx.stats["bytes"] += info.size
    elif info.kind == "l":
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ctx.op("symlink", src, dst)
        if os.path.lexists(dst):
            versioner.retire(dst, dst_root) if versioner else _remove(dst)
        os.symlink(os.readlink(src), dst)
        ctx.stats["copied"] += 1
    elif info.kind == "d":
        if os.path.lexists(dst) and not os.path.isdir(dst):
            versioner.retire(dst, dst_root) if versioner else _remove(dst)
        if not os.path.isdir(dst):
            ctx.op("mkdir", src, dst)
            os.makedirs(dst, exist_ok=True)
            ctx.stats["dirs"] += 1
    else:
        ctx.warnings.append(f"Skipped special file {fsutil.display_path(src)}")
        ctx.stats["skipped"] += 1


def _copy_tree(task: Task, src_root: str, dst_root: str, rel: str, scan_: dict, ctx: Context, versioner) -> None:
    info = scan_.get(rel)
    entries = [(rel, info)] + sorted(_subtree(scan_, rel), key=lambda kv: _sortkey(kv[0]))
    dirs = []
    for r, inf in entries:
        ctx.checkpoint()
        try:
            _copy_entry(os.path.join(src_root, r), os.path.join(dst_root, r), inf, ctx, versioner, dst_root)
            if inf.kind == "d":
                dirs.append(r)
        except OSError as e:
            if e.errno == errno.ENOSPC:
                raise
            ctx.error(f"{fsutil.display_path(os.path.join(src_root, r))}: {e.strerror or e}")
    for r in reversed(dirs):
        try:
            _copy_meta(os.stat(os.path.join(src_root, r)), os.path.join(dst_root, r))
        except OSError:
            pass


def execute(plan: Plan, ctx: Context) -> None:
    task = plan.task
    if task.mode == "snapshot":
        return execute_snapshot(task, ctx)
    with _Lock(task):
        if not os.path.isdir(task.right):
            os.makedirs(task.right, exist_ok=True)
        versioner = Versioner(task, ctx)
        _run_hook(task.pre_cmd, task, ctx, "pre")
        items = plan.items
        dels = [i for i in items if i.action in (DEL_L, DEL_R)]
        copies = [i for i in items if i.action in (COPY_LR, COPY_RL)]
        for it in dels:
            ctx.checkpoint()
            root = task.left if it.action == DEL_L else task.right
            p = os.path.join(root, it.rel)
            ctx.direction = "delete-left" if it.action == DEL_L else "delete-right"
            ctx.op("delete", p, task.versioning)
            try:
                versioner.retire(p, root)
                ctx.stats["deleted"] += 1
                ctx.log(f"{ACTION_LABELS[it.action]}: {it.rel}")
            except OSError as e:
                ctx.error(f"{fsutil.display_path(p)}: {e.strerror or e}")
        for it in sorted(copies, key=lambda i: _sortkey(i.rel)):
            ctx.checkpoint()
            lr = it.action == COPY_LR
            src_root, dst_root = (task.left, task.right) if lr else (task.right, task.left)
            scan_ = plan.left_scan if lr else plan.right_scan
            info = it.left if lr else it.right
            src, dst = os.path.join(src_root, it.rel), os.path.join(dst_root, it.rel)
            ctx.direction = "lr" if lr else "rl"
            try:
                # re-validate: the source must still be what we compared
                st = os.lstat(src)
                if info.kind == "f" and (st.st_size != info.size or abs(st.st_mtime - info.mtime) > 0.001):
                    ctx.warnings.append(f"Changed since compare, skipped: {it.rel}")
                    ctx.stats["skipped"] += 1
                    continue
                if info.kind == "d":
                    _copy_tree(task, src_root, dst_root, it.rel, scan_, ctx, versioner)
                else:
                    _copy_entry(src, dst, info, ctx, versioner, dst_root)
                ctx.log(f"{ACTION_LABELS[it.action]} {it.rel}")
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    ctx.error(f"Disk full while writing {fsutil.display_path(dst)}; stopped.")
                    break
                ctx.error(f"{fsutil.display_path(src)}: {e.strerror or e}")
        ctx.direction = ""
        ctx.op("finish", task.right)
        versioner.prune()
        if task.mode == "twoway":
            _refresh_db(task, ctx)
        _run_hook(task.post_cmd, task, ctx, "post")


def _refresh_db(task: Task, ctx: Context) -> None:
    flt = Filter(task.include, task.exclude)
    extra = [task.versions_dir] if task.versioning == "folder" and task.versions_dir else []
    left, _ = scan(task.left, flt, ctx.checkpoint, None, extra)
    right, _ = scan(task.right, flt, ctx.checkpoint, None, extra)
    items = {}
    for rel, L in left.items():
        R = right.get(rel)
        if R is not None and _same(task, L, R, os.path.join(task.left, rel), os.path.join(task.right, rel), None):
            items[rel] = [L.kind, L.size, max(L.mtime, R.mtime) if L.kind == "d" else L.mtime]
    save_db(task, items)


def _run_hook(cmd: str, task: Task, ctx: Context, which: str) -> None:
    if not cmd.strip():
        return
    ctx.log(f"Running {which}-task command")
    ctx.op("hook", cmd, which)
    env = dict(os.environ, GX_LEFT=task.left, GX_RIGHT=task.right, GX_TASK=task.name,
               GX_ERRORS=str(len(ctx.errors)))
    p = subprocess.run(["/bin/sh", "-c", cmd], cwd=task.left if os.path.isdir(task.left) else None,
                       env=env, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        msg = f"{which}-task command failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[:300]}"
        if which == "pre":
            raise PreflightError(msg)
        ctx.error(msg)


# ----------------------------------------------------------------------------- snapshots

def list_snapshots(root: str) -> list[str]:
    try:
        return sorted(os.path.join(root, n) for n in os.listdir(root)
                      if SNAP_RE.match(n) and os.path.isdir(os.path.join(root, n))
                      and not os.path.islink(os.path.join(root, n)))
    except OSError:
        return []


def latest_snapshot(root: str) -> str | None:
    s = list_snapshots(root)
    return s[-1] if s else None


def execute_snapshot(task: Task, ctx: Context) -> str:
    with _Lock(task):
        os.makedirs(task.right, exist_ok=True)
        _run_hook(task.pre_cmd, task, ctx, "pre")
        # remove leftovers of interrupted runs (never counted as snapshots)
        for n in os.listdir(task.right):
            if n.endswith(".partial") and SNAP_RE.match(n[:-8]):
                ctx.log(f"Removing incomplete snapshot {n}")
                _remove(os.path.join(task.right, n))
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        while os.path.exists(os.path.join(task.right, stamp)):
            time.sleep(1)
            stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        partial = os.path.join(task.right, stamp + ".partial")
        final = os.path.join(task.right, stamp)
        prev = latest_snapshot(task.right)
        flt = Filter(task.include, task.exclude)
        ctx.direction = "snapshot"
        ctx.op("scan", task.left, final)
        src, errs = scan(task.left, flt, ctx.checkpoint)
        for e in errs:
            ctx.error(e)
        prev_scan = scan(prev, Filter(["*"], []), ctx.checkpoint)[0] if prev else {}
        os.mkdir(partial)
        link_ok = True
        dirs = [""]
        for rel in sorted(src, key=_sortkey):
            ctx.checkpoint()
            info = src[rel]
            s, d = os.path.join(task.left, rel), os.path.join(partial, rel)
            try:
                if info.kind == "d":
                    os.makedirs(d, exist_ok=True)
                    dirs.append(rel)
                    continue
                os.makedirs(os.path.dirname(d), exist_ok=True)
                p = prev_scan.get(rel)
                if (info.kind == "f" and p is not None and p.kind == "f" and p.size == info.size
                        and _time_eq(task, p.mtime, info.mtime) and link_ok):
                    try:
                        os.link(os.path.join(prev, rel), d)
                        ctx.op("hardlink", s, d, info.size)    # announced once done: a failed link falls back to a copy
                        ctx.stats["linked"] += 1
                        continue
                    except OSError as e:
                        if e.errno in (errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EMLINK, errno.EOPNOTSUPP):
                            link_ok = False
                            ctx.warnings.append("Hard links unavailable on target; falling back to full copies.")
                        else:
                            raise
                if info.kind == "f":
                    _copy_file(s, d, ctx, None, partial)
                    ctx.stats["copied"] += 1
                    ctx.stats["bytes"] += info.size
                elif info.kind == "l":
                    ctx.op("symlink", s, d)
                    os.symlink(info.link, d)
                    ctx.stats["copied"] += 1
                else:
                    ctx.stats["skipped"] += 1
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    ctx.error("Disk full; snapshot left incomplete and will be removed next run.")
                    raise
                ctx.error(f"{fsutil.display_path(s)}: {e.strerror or e}")
        ctx.direction = ""
        ctx.op("finish", final)
        for rel in reversed(dirs):
            try:
                _copy_meta(os.stat(os.path.join(task.left, rel) if rel else task.left),
                           os.path.join(partial, rel) if rel else partial)
            except OSError:
                pass
        os.rename(partial, final)
        # "latest" convenience symlink, replaced atomically
        tmp_link = os.path.join(task.right, ".latest.tmp")
        try:
            if os.path.lexists(tmp_link):
                os.unlink(tmp_link)
            os.symlink(stamp, tmp_link)
            os.replace(tmp_link, os.path.join(task.right, "latest"))
        except OSError:
            pass   # e.g. FAT/exFAT: no symlinks; harmless
        snaps = list_snapshots(task.right)
        for old in snaps[:-task.snapshots_keep]:
            ctx.log(f"Removing old snapshot {os.path.basename(old)}")
            ctx.op("prune", old)
            try:
                _remove(old)
            except OSError as e:
                ctx.error(f"remove {old}: {e.strerror or e}")
        _run_hook(task.post_cmd, task, ctx, "post")
        ctx.log(f"Snapshot {stamp} complete")
        return final


# ----------------------------------------------------------------------------- logs

class RunLog:
    def __init__(self, profile: str):
        os.makedirs(logs_dir(), mode=0o700, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(logs_dir(), f"{profile}_{stamp}.log")
        self.f = open(self.path, "a", encoding="utf-8", errors="backslashreplace")
        self._prune()

    def write(self, msg: str) -> None:
        self.f.write(f"{_dt.datetime.now():%H:%M:%S} {fsutil.display_name(msg)}\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()

    @staticmethod
    def _prune(keep: int = 100) -> None:
        try:
            logs = sorted((os.path.join(logs_dir(), n) for n in os.listdir(logs_dir()) if n.endswith(".log")),
                          key=os.path.getmtime)
            for p in logs[:-keep]:
                os.unlink(p)
        except OSError:
            pass


def run_task(task: Task, log: RunLog | None, checkpoint=None, progress=None,
             dry_run: bool = False) -> tuple[Context, Plan]:
    """Compare + execute one task (used by CLI and scheduled runs). Conflicts are skipped."""
    ctx = Context(checkpoint, progress, (log.write if log else None))
    ctx.log(f"Task “{task.name}”: {MODE_LABELS[task.mode]}  {task.left}  ->  {task.right}")
    plan = compare(task, checkpoint)
    for w in plan.warnings:
        ctx.log("note: " + w)
    if plan.errors and (not plan.left_scan and not plan.right_scan):
        for e in plan.errors:
            ctx.error(e)
        return ctx, plan
    for e in plan.errors:
        ctx.error(e)
    s = plan.summary()
    ctx.log(f"Plan: {s['copy_lr']} →, {s['copy_rl']} ←, {s['del_l'] + s['del_r']} deletions, "
            f"{fsutil.human_size(s['bytes'])}, {s['conflicts']} conflicts")
    for it in plan.items:
        if it.category == CONFLICT:
            ctx.warnings.append(f"Conflict left unresolved: {it.rel} ({it.note})")
    if dry_run:
        for it in plan.actionable():
            ctx.log(f"[dry run] {ACTION_LABELS[it.action]} {it.rel}")
        return ctx, plan
    execute(plan, ctx)
    return ctx, plan


def run_profile_cli(name: str, dry_run: bool = False, quiet: bool = False) -> int:
    """`gsexplorer-plus --sync NAME [--dry-run]` — headless, prints a JSON summary, returns exit code."""
    import signal
    cancelled = {"v": False}

    def _cancel(*_):
        cancelled["v"] = True

    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)

    def checkpoint():
        if cancelled["v"]:
            raise Cancelled()

    try:
        prof = load_profile(name)
    except (OSError, ValueError) as e:
        print(json.dumps({"profile": name, "status": "error", "error": str(e)}))
        return EXIT_ERROR
    log = RunLog(name)
    results, code = [], EXIT_OK
    try:
        for t in prof.tasks:
            if not t.enabled:
                continue
            try:
                ctx, plan = run_task(t, log, checkpoint, dry_run=dry_run)
            except PreflightError as e:
                log.write("ERROR " + str(e))
                results.append({"task": t.name, "errors": [str(e)]})
                code = max(code, EXIT_ERROR)
                continue
            results.append({"task": t.name, "stats": ctx.stats, "errors": ctx.errors[:50],
                            "warnings": ctx.warnings[:50]})
            if ctx.errors:
                code = max(code, EXIT_ERROR)
            elif ctx.warnings:
                code = max(code, EXIT_WARN)
    except Cancelled:
        log.write("Cancelled")
        code = EXIT_CANCELLED
    finally:
        log.close()
    status = {0: "success", 1: "warnings", 2: "errors", 3: "cancelled"}[code]
    print(json.dumps({"profile": name, "status": status, "dry_run": dry_run, "log": log.path,
                      "tasks": results}, ensure_ascii=False, indent=None if quiet else 2))
    if code in (EXIT_WARN, EXIT_ERROR) and shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "GSExplorer+", f"Sync “{name}”: {status}", log.path],
                           timeout=5, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    return code
