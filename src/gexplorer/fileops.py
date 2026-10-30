"""Background file-operation queue: copy, move, delete, trash.

* One worker thread runs jobs sequentially (Total Commander-style queue), so a
  long copy never freezes the UI and two jobs never fight over the same disk.
* Pause/cancel are checked between 1 MiB chunks.
* Conflicts block only the job: the worker emits ``conflict`` and waits for the
  UI's answer (overwrite / skip / rename / cancel, optionally "apply to all").
* Files are copied to a hidden temp name in the destination and atomically
  renamed into place, so a failed or cancelled overwrite never destroys the
  original target.
* Symlinks are copied as symlinks and never followed during recursion.
"""
from __future__ import annotations

import errno
import itertools
import os
import queue
import shutil
import stat
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import fsutil

CHUNK = 1024 * 1024
_ids = itertools.count(1)

OVERWRITE, SKIP, RENAME, CANCEL = "overwrite", "skip", "rename", "cancel"


class Cancelled(Exception):
    pass


class JobFailed(Exception):
    """A job that stops with a message for the user (not an OS error)."""


# Sync operations that count as one "file" in progress displays (folders are created on the way).
SYNC_FILE_OPS = ("copy", "symlink", "delete", "hardlink")
# Weight of one file operation in a sync's % and time estimate, in bytes-equivalent, so runs of many
# small files or of deletions only still progress (see syncprogress).
SYNC_PER_FILE = 64 * 1024


def sync_progress_state(plan) -> dict:
    """Totals for a sync job's live progress (see JobManager._do_sync, syncui.SyncRunDialog)."""
    from . import syncengine as se
    s = plan.summary()
    bytes_lr = sum(i.bytes for i in plan.items if i.action == se.COPY_LR)
    bytes_rl = sum(i.bytes for i in plan.items if i.action == se.COPY_RL)
    links = sum(1 for i in plan.items if i.action == se.LINK) if plan.task.mode == "snapshot" else 0
    total = s["copy_lr"] + s["copy_rl"] + s["del_l"] + s["del_r"] + links
    return {"op": None, "total_ops": total, "done_ops": 0, "lr": 0, "rl": 0, "deleted": 0, "linked": 0,
            "bytes_lr": bytes_lr, "bytes_rl": bytes_rl, "done_lr": 0, "done_rl": 0,
            "planned": {"lr": s["copy_lr"], "rl": s["copy_rl"], "del": s["del_l"] + s["del_r"], "link": links}}


class Job:
    def __init__(self, kind: str, sources: list[str], dest: str = "", dest_name: str = ""):
        self.id = next(_ids)
        self.dest_name = dest_name           # single-source copy/move under a new name
        self.kind = kind                     # copy | move | delete | trash
        self.sources = [os.path.abspath(s) for s in sources]
        self.dest = os.path.abspath(dest) if dest else ""
        self.state = "queued"                # queued running paused done failed cancelled
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_items = 0
        self.done_items = 0
        self.current = ""
        self.errors: list[str] = []
        self.trash_failed: list[str] = []
        self.started = 0.0
        self.finished = 0.0
        self._run = threading.Event()
        self._run.set()
        self._cancel = threading.Event()
        self._answer_ev = threading.Event()
        self._answer: tuple[str, bool] | None = None
        self.sticky_answer: str | None = None
        self.payload = None                  # sync jobs: (Plan, RunLog | None)
        self.warnings: list[str] = []
        self.stats: dict = {}
        self.log_path = ""
        self.sync: dict | None = None      # sync jobs: live detail (sync_progress_state)

    # ---------------------------------------------------------------- control (UI thread)
    @property
    def title(self) -> str:
        if self.kind == "sync" and self.payload is not None:
            t = self.payload[0].task
            return f"Sync “{t.name}” ({t.mode}) {fsutil.display_path(t.left)} → {fsutil.display_path(t.right)}"
        n = len(self.sources)
        what = fsutil.display_name(os.path.basename(self.sources[0])) if n == 1 else f"{n} items"
        if self.kind == "compress" and self.payload:
            return f"Compress {what} → {fsutil.display_name(os.path.basename(self.payload['archive']))}"
        if self.kind == "extract":
            return f"Extract {what} → {fsutil.display_path(self.dest)}"
        verb = {"copy": "Copy", "move": "Move", "delete": "Delete", "trash": "Trash"}[self.kind]
        tail = f" → {fsutil.display_path(self.dest)}" if self.dest else ""
        return f"{verb} {what}{tail}"

    def pause(self) -> None:
        if self.state == "running":
            self._run.clear()
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "running"
            self._run.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._run.set()
        self._answer_ev.set()
        if self.state == "queued":
            self.state = "cancelled"

    def answer(self, choice: str, apply_all: bool) -> None:
        self._answer = (choice, apply_all)
        self._answer_ev.set()

    @property
    def fraction(self) -> float:
        st = self.sync
        if st is not None and st["total_ops"] + self.total_bytes > 0:      # same measure as the sync window
            total = self.total_bytes + st["total_ops"] * SYNC_PER_FILE
            done = min(self.done_bytes, self.total_bytes) + min(st["done_ops"], st["total_ops"]) * SYNC_PER_FILE
            return min(1.0, done / total)
        if self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        if self.total_items > 0:
            return min(1.0, self.done_items / self.total_items)
        return 0.0

    def checkpoint(self) -> None:
        self._run.wait()
        if self._cancel.is_set():
            raise Cancelled()


class JobManager(QObject):
    job_added = Signal(object)
    job_updated = Signal(object)
    job_finished = Signal(object)
    conflict = Signal(object, str, str)      # job, src, dst
    dirs_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[Job] = []
        self._q: queue.Queue[Job] = queue.Queue()
        self._last_emit = 0.0
        self._thread = threading.Thread(target=self._loop, name="gexplorer-jobs", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- public API
    def submit_sync(self, plan, runlog=None) -> Job:
        """Queue execution of a compared sync/backup plan (see syncengine)."""
        job = Job("sync", [plan.task.left], plan.task.right)
        job.payload = (plan, runlog)
        self.jobs.append(job)
        self.job_added.emit(job)
        self._q.put(job)
        return job

    def submit(self, kind: str, sources: list[str], dest: str = "", dest_name: str = "", payload=None) -> Job:
        job = Job(kind, sources, dest, dest_name)
        job.payload = payload
        self.jobs.append(job)
        self.job_added.emit(job)
        self._q.put(job)
        return job

    def active(self) -> list[Job]:
        return [j for j in self.jobs if j.state in ("queued", "running", "paused")]

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.state in ("queued", "running", "paused")]

    def cancel_all(self) -> None:
        for j in self.active():
            j.cancel()

    # ---------------------------------------------------------------- worker
    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job.state == "cancelled":
                self.job_finished.emit(job)
                continue
            job.state = "running"
            job.started = time.time()
            self.job_updated.emit(job)
            try:
                getattr(self, "_do_" + job.kind)(job)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except JobFailed as e:
                job.errors.append(str(e))
                job.state = "failed"
            except OSError as e:
                job.errors.append(self._fmt_err(e, job.current))
                job.state = "failed"
            except Exception as e:  # noqa: BLE001 - never let the worker die
                job.errors.append(f"Unexpected error: {e!r}")
                job.state = "failed"
            job.finished = time.time()
            job.current = ""
            dirs = {os.path.dirname(s) for s in job.sources}
            if job.dest:
                dirs.add(job.dest)
            self.dirs_changed.emit(sorted(dirs))
            self.job_updated.emit(job)
            self.job_finished.emit(job)

    def _tick(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit > 0.08:
            self._last_emit = now
            self.job_updated.emit(job)

    @staticmethod
    def _fmt_err(e: OSError, where: str) -> str:
        msg = e.strerror or str(e)
        p = e.filename or where
        return f"{fsutil.display_path(p)}: {msg}" if p else msg

    def _ask(self, job: Job, src: str, dst: str) -> str:
        if job.sticky_answer:
            return job.sticky_answer
        job._answer_ev.clear()
        job._answer = None
        self.conflict.emit(job, src, dst)
        job._answer_ev.wait()
        if job._cancel.is_set() or job._answer is None:
            raise Cancelled()
        choice, apply_all = job._answer
        if choice == CANCEL:
            job.cancel()
            raise Cancelled()
        if apply_all:
            job.sticky_answer = choice
        return choice

    # ---------------------------------------------------------------- scanning
    def _measure(self, job: Job, paths: list[str]) -> None:
        total_b, total_n = 0, 0
        for p in paths:
            job.checkpoint()
            try:
                st = os.lstat(p)
            except OSError:
                continue
            total_n += 1
            if stat.S_ISDIR(st.st_mode):
                for root, dirs, files in os.walk(p, followlinks=False):
                    job.checkpoint()
                    total_n += len(dirs) + len(files)
                    for f in files:
                        try:
                            fst = os.lstat(os.path.join(root, f))
                            if stat.S_ISREG(fst.st_mode):
                                total_b += fst.st_size
                        except OSError:
                            pass
            elif stat.S_ISREG(st.st_mode):
                total_b += st.st_size
        job.total_bytes, job.total_items = total_b, total_n

    def _validate_dest(self, job: Job) -> None:
        if not os.path.isdir(job.dest):
            raise OSError(errno.ENOTDIR, "Destination is not a folder", job.dest)
        for s in job.sources:
            try:
                if stat.S_ISDIR(os.lstat(s).st_mode) and fsutil.is_inside(job.dest, s):
                    raise OSError(errno.EINVAL, "Can't copy or move a folder into itself", s)
            except FileNotFoundError:
                continue

    def _check_space(self, job: Job) -> None:
        try:
            free = shutil.disk_usage(job.dest).free
        except OSError:
            return
        if job.total_bytes > free:
            raise OSError(errno.ENOSPC,
                          f"Not enough free space: need {fsutil.human_size(job.total_bytes)}, "
                          f"{fsutil.human_size(free)} available", job.dest)

    # ---------------------------------------------------------------- copy
    def _do_copy(self, job: Job) -> None:
        self._validate_dest(job)
        self._measure(job, job.sources)
        self._check_space(job)
        self._tick(job, True)
        for src in job.sources:
            job.checkpoint()
            name = job.dest_name or os.path.basename(src.rstrip("/"))
            if os.path.dirname(src) == job.dest and not job.dest_name:   # duplicate in place
                name = fsutil.unique_name(job.dest, name)
            self._copy_any(job, src, os.path.join(job.dest, name))

    def _resolve_conflict(self, job: Job, src: str, dst: str) -> str | None:
        """Return the destination to use, or None to skip. Handles dir merge."""
        if not os.path.lexists(dst):
            return dst
        try:
            s_dir = stat.S_ISDIR(os.lstat(src).st_mode)
            d_dir = stat.S_ISDIR(os.lstat(dst).st_mode)
        except OSError:
            return dst
        if s_dir and d_dir:
            return dst                            # merge folders
        if os.path.realpath(src) == os.path.realpath(dst):
            return None
        choice = self._ask(job, src, dst)
        if choice == SKIP:
            return None
        if choice == RENAME:
            return os.path.join(os.path.dirname(dst), fsutil.unique_name(os.path.dirname(dst), os.path.basename(dst)))
        if d_dir and not s_dir:
            job.errors.append(f"{fsutil.display_path(dst)}: can't overwrite a folder with a file")
            return None
        return dst                                # overwrite

    def _copy_any(self, job: Job, src: str, dst: str) -> bool:
        """Copy one item. Returns True if fully copied (used by move)."""
        job.checkpoint()
        job.current = src
        try:
            st = os.lstat(src)
        except OSError as e:
            job.errors.append(self._fmt_err(e, src))
            return False
        target = self._resolve_conflict(job, src, dst)
        if target is None:
            job.done_items += 1
            if stat.S_ISREG(st.st_mode):
                job.done_bytes += st.st_size
            return False
        ok = True
        try:
            if stat.S_ISLNK(st.st_mode):
                link = os.readlink(src)
                if os.path.lexists(target):
                    os.unlink(target)
                os.symlink(link, target)
            elif stat.S_ISDIR(st.st_mode):
                if not os.path.isdir(target):
                    os.mkdir(target, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
                with os.scandir(src) as it:
                    children = sorted(d.name for d in it)
                for c in children:
                    ok &= self._copy_any(job, os.path.join(src, c), os.path.join(target, c))
                try:
                    shutil.copystat(src, target, follow_symlinks=False)
                except OSError:
                    pass
            elif stat.S_ISREG(st.st_mode):
                self._copy_file(job, src, target, st)
            elif stat.S_ISFIFO(st.st_mode):
                os.mkfifo(target, stat.S_IMODE(st.st_mode))
            else:
                job.errors.append(f"{fsutil.display_path(src)}: special file skipped")
                ok = False
        except Cancelled:
            raise
        except OSError as e:
            if e.errno == errno.ENOSPC:
                raise
            job.errors.append(self._fmt_err(e, src))
            ok = False
        job.done_items += 1
        self._tick(job)
        return ok

    def _copy_file(self, job: Job, src: str, dst: str, st: os.stat_result) -> None:
        d = os.path.dirname(dst)
        tmp = os.path.join(d, fsutil.unique_name(d, "." + os.path.basename(dst) + ".gxpart"))
        start_bytes = job.done_bytes
        try:
            with open(src, "rb") as fi, open(tmp, "xb") as fo:
                while True:
                    job.checkpoint()
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    job.done_bytes += len(buf)
                    self._tick(job)
            try:
                shutil.copystat(src, tmp, follow_symlinks=False)
            except OSError:
                pass
            os.replace(tmp, dst)
        except BaseException:
            job.done_bytes = start_bytes + st.st_size  # keep the bar monotonic
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- move
    def _do_move(self, job: Job) -> None:
        self._validate_dest(job)
        try:
            dest_dev = os.stat(job.dest).st_dev
        except OSError as e:
            raise OSError(e.errno, e.strerror, job.dest)
        cross = [s for s in job.sources if os.path.lexists(s) and os.lstat(s).st_dev != dest_dev]
        if cross:
            self._measure(job, job.sources)
            total = (job.total_bytes, job.total_items)
            self._measure(job, cross)
            self._check_space(job)
            job.total_bytes, job.total_items = total
        else:
            # same filesystem: each top-level item is one atomic rename
            job.total_bytes, job.total_items = 0, len(job.sources)
        self._tick(job, True)
        for src in job.sources:
            job.checkpoint()
            name = job.dest_name or os.path.basename(src.rstrip("/"))
            if os.path.dirname(src) == job.dest and name == os.path.basename(src):
                job.done_items += 1
                continue                              # already there
            dst = os.path.join(job.dest, name)
            self._move_any(job, src, dst, dest_dev)

    def _move_any(self, job: Job, src: str, dst: str, dest_dev: int) -> None:
        job.current = src
        try:
            st = os.lstat(src)
        except OSError as e:
            job.errors.append(self._fmt_err(e, src))
            return
        if st.st_dev == dest_dev:
            target = self._resolve_conflict(job, src, dst)
            if target is None:
                job.done_items += 1
                return
            try:
                if os.path.isdir(target) and not os.path.islink(target) and stat.S_ISDIR(st.st_mode) and target == dst \
                        and os.path.lexists(dst):
                    # merge folder into existing folder
                    with os.scandir(src) as it:
                        names = sorted(d.name for d in it)
                    for n in names:
                        self._move_any(job, os.path.join(src, n), os.path.join(dst, n), dest_dev)
                    try:
                        os.rmdir(src)
                    except OSError:
                        pass
                else:
                    if stat.S_ISDIR(st.st_mode):
                        os.rename(src, target)
                    else:
                        os.replace(src, target)
                    if stat.S_ISREG(st.st_mode) and job.total_bytes:
                        job.done_bytes += st.st_size
            except OSError as e:
                job.errors.append(self._fmt_err(e, src))
            job.done_items += 1
            self._tick(job)
            return
        # cross-device: copy, then delete source only if copy fully succeeded
        if self._copy_any(job, src, dst):
            try:
                self._remove_tree(job, src, count=False)
            except OSError as e:
                job.errors.append(self._fmt_err(e, src))

    # ---------------------------------------------------------------- delete / trash
    def _remove_tree(self, job: Job, path: str, count: bool = True) -> None:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(path)
            if count:
                job.done_items += 1
            return
        for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
            job.checkpoint()
            for f in files:
                p = os.path.join(root, f)
                job.current = p
                try:
                    os.unlink(p)
                except OSError as e:
                    job.errors.append(self._fmt_err(e, p))
                if count:
                    job.done_items += 1
                self._tick(job)
            for d in dirs:
                p = os.path.join(root, d)
                try:
                    if os.path.islink(p):
                        os.unlink(p)
                    else:
                        os.rmdir(p)
                except OSError as e:
                    job.errors.append(self._fmt_err(e, p))
                if count:
                    job.done_items += 1
        os.rmdir(path)
        if count:
            job.done_items += 1

    def _do_delete(self, job: Job) -> None:
        self._measure(job, job.sources)
        job.total_bytes = 0                     # progress by item count
        self._tick(job, True)
        for p in job.sources:
            job.checkpoint()
            job.current = p
            try:
                self._remove_tree(job, p)
            except Cancelled:
                raise
            except OSError as e:
                job.errors.append(self._fmt_err(e, p))
            self._tick(job)

    def _do_sync(self, job: Job) -> None:
        from . import syncengine as se
        plan, runlog = job.payload
        s = plan.summary()
        job.total_bytes = s["bytes"]
        job.total_items = len(plan.actionable()) or 1
        # Live detail for progress displays (read by the GUI thread; plain values, replaced whole).
        st = job.sync = sync_progress_state(plan)
        ctx = None

        def close_op() -> None:
            op = st["op"]
            if op is None or op["closed"] or op["kind"] not in SYNC_FILE_OPS:
                return
            op["closed"] = True
            st["done_ops"] += 1
            if ctx is not None and len(ctx.errors) == op["errors_at"]:
                key = {"delete": "deleted", "hardlink": "linked"}.get(op["kind"]) or \
                    ("rl" if op["dir"] == "rl" else "lr")
                st[key] += 1

        def on_op(kind: str, src: str, dst: str, size: int, direction: str) -> None:
            close_op()
            st["op"] = {"kind": kind, "src": src, "dst": dst, "size": size, "done": 0, "dir": direction,
                        "closed": False, "errors_at": len(ctx.errors) if ctx is not None else 0}
            job.current = src
            self._tick(job)

        self._tick(job, True)

        def progress(nbytes: int, current: str) -> None:
            job.done_bytes += nbytes
            job.current = current
            op = st["op"]
            if op is not None:
                op["done"] += nbytes
                st["done_rl" if op["dir"] == "rl" else "done_lr"] += nbytes
            self._tick(job)

        def log(msg: str) -> None:
            if runlog is not None:
                runlog.write(msg)
            if msg.startswith(("→", "←", "Delete")):
                job.done_items += 1

        def checkpoint() -> None:
            try:
                job.checkpoint()
            except Cancelled:
                raise se.Cancelled()

        ctx = se.Context(checkpoint, progress, log, on_op)
        try:
            se.execute(plan, ctx)
        except se.Cancelled:
            raise Cancelled()
        except se.PreflightError as e:
            raise OSError(errno.EBUSY, str(e))
        finally:
            if st["op"] is not None and st["op"]["kind"] in SYNC_FILE_OPS and len(ctx.errors) == st["op"]["errors_at"] \
                    and job._cancel.is_set():
                st["op"]["closed"] = True      # interrupted: the half-copied temp file was discarded
            close_op()
            job.errors.extend(ctx.errors)
            job.warnings.extend(ctx.warnings)
            job.stats = dict(ctx.stats)
            if runlog is not None:
                runlog.write(f"Done: {ctx.stats}; {len(ctx.errors)} errors, {len(ctx.warnings)} warnings")
                job.log_path = runlog.path
                runlog.close()

    def _do_trash(self, job: Job) -> None:
        job.total_items = len(job.sources)
        self._tick(job, True)
        for p in job.sources:
            job.checkpoint()
            job.current = p
            try:
                fsutil.move_to_trash(p)
            except OSError as e:
                job.trash_failed.append(p)
                job.errors.append(self._fmt_err(e, p))
            job.done_items += 1
            self._tick(job)


    # ---------------------------------------------------------------- archives
    def _archive_hooks(self, job: Job, base: int = 0, share: int = 0):
        from . import archives

        def progress(done: int, total: int, name: str) -> None:
            if share:                                   # several archives: each gets an equal share
                job.done_bytes = base + (int(share * done / total) if total else 0)
            else:
                job.total_bytes = max(total, 1)
                job.done_bytes = done
            if name:
                job.current = name
            self._tick(job)

        def check() -> None:
            try:
                job.checkpoint()
            except Cancelled:
                raise archives.Cancelled() from None

        return progress, check

    def _do_compress(self, job: Job) -> None:
        from . import archives
        o = job.payload
        job.current = o["archive"]
        progress, check = self._archive_hooks(job)
        try:
            warns = archives.compress(job.sources, o["archive"], o["fmt"], o.get("level", "normal"),
                                      o.get("password", ""), progress, check)
        except archives.Cancelled:
            raise Cancelled() from None
        except archives.ArchiveError as e:
            raise JobFailed(str(e)) from None
        job.warnings.extend(warns)
        job.stats["created"] = [o["archive"]]

    def _do_extract(self, job: Job) -> None:
        from . import archives
        o = job.payload or {}
        n = len(job.sources)
        job.total_items = n
        job.total_bytes = 1000 * n
        created = []
        for i, arc in enumerate(job.sources):
            job.current = arc
            progress, check = self._archive_hooks(job, base=1000 * i, share=1000)
            try:
                path, warns = archives.extract(arc, job.dest, o.get("mode", "smart"), o.get("password", ""),
                                               progress, check)
            except archives.Cancelled:
                raise Cancelled() from None
            except archives.NeedsPassword as e:
                job.stats["needs_password"] = arc
                raise JobFailed(f"{fsutil.display_name(os.path.basename(arc))}: {e}") from None
            except archives.ArchiveError as e:
                job.errors.append(f"{fsutil.display_name(os.path.basename(arc))}: {e}")
                continue
            created.append(path)
            job.warnings.extend(warns)
            job.done_items = i + 1
            job.done_bytes = 1000 * (i + 1)
            self._tick(job)
        job.stats["created"] = created
        if job.errors and not created:
            raise JobFailed(job.errors.pop())
