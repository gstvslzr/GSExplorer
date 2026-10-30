"""Synchronize progress: engine operation reporting, job live state, and the progress window.

Run: QT_QPA_PLATFORM=offscreen python tests/test_sync_progress.py
"""
import os
import sys
import tempfile
import time

H = tempfile.mkdtemp(prefix="gx-syncprog-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import fileops, syncprogress  # noqa: E402
from gsexplorer import syncengine as se  # noqa: E402
from gsexplorer import themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402


def _accept_box(self):
    self._clicked = next((b for b in self.buttons()
                          if self.buttonRole(b) in (QMessageBox.AcceptRole, QMessageBox.DestructiveRole)), None)
    return 0


QMessageBox.exec = _accept_box
QMessageBox.clickedButton = lambda self: getattr(self, "_clicked", None)
QMessageBox.information = staticmethod(lambda *a, **k: 0)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Discard)

T0 = time.time() - 9000
results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def w(p, data="x", t=T0):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.utime(p, (t, t))


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout=30):
    t = time.time()
    while not cond():
        pump(0.03)
        if time.time() - t > timeout:
            return False
    return True


def recorder():
    ops = []
    return ops, se.Context(on_op=lambda k, s, d, n, di: ops.append((k, s, d, n, di)))


# ===================================================================== engine: what is announced
L, R = f"{H}/m/L", f"{H}/m/R"
w(f"{L}/a.txt", "A" * 3000)
w(f"{L}/sub/b.txt", "BB")
os.symlink("a.txt", f"{L}/link")
w(f"{R}/stale.txt", "S")
task = se.Task(mode="mirror", left=L, right=R, versioning="none")
plan = se.compare(task)
ops, ctx = recorder()
se.execute(plan, ctx)
kinds = [o[0] for o in ops]
check("mirror: deletion announced first, on the right, with the versioning policy",
      ops[0][:3] == ("delete", f"{R}/stale.txt", "none") and ops[0][4] == "delete-right")
cp = [o for o in ops if o[0] == "copy"]
check("copies announced with full source, full destination, size, direction left→right",
      ("copy", f"{L}/a.txt", f"{R}/a.txt", 3000, "lr") in cp and ("copy", f"{L}/sub/b.txt", f"{R}/sub/b.txt", 2, "lr") in cp)
check("folder creation and symlinks announced", "mkdir" in kinds and ("symlink", f"{L}/link", f"{R}/link", 0, "lr") in ops)
check("finish step announced last (no direction)", ops[-1][0] == "finish" and ops[-1][4] == "")

# two-way: both directions
L2, R2 = f"{H}/t/L", f"{H}/t/R"
w(f"{L2}/from-left.txt", "l")
w(f"{R2}/from-right.txt", "rr")
t2 = se.Task(mode="twoway", left=L2, right=R2)
ops, ctx = recorder()
se.execute(se.compare(t2), ctx)
d = {o[1]: o[4] for o in ops if o[0] == "copy"}
check("two-way: right→left copy reported as 'rl', left→right as 'lr'",
      d.get(f"{R2}/from-right.txt") == "rl" and d.get(f"{L2}/from-left.txt") == "lr")

# snapshot: scan, copy, hard links, prune
S, B = f"{H}/s/src", f"{H}/s/backups"
w(f"{S}/same.bin", "same")
w(f"{S}/edit.txt", "v1")
st = se.Task(mode="snapshot", left=S, right=B, snapshots_keep=1)
se.execute_snapshot(st, se.Context())
time.sleep(1.1)
w(f"{S}/edit.txt", "v2-changed", time.time())
ops, ctx = recorder()
se.execute(se.compare(st), ctx)
k = [o[0] for o in ops]
check("snapshot: scan first, then copy of the changed file and a hard link for the unchanged one",
      k[0] == "scan" and ("copy" in k) and ("hardlink" in k) and all(o[4] in ("snapshot", "") for o in ops))
check("snapshot: old snapshot removal announced (keep limit)", "prune" in k)

# a failing display callback never stops a sync
L3, R3 = f"{H}/f/L", f"{H}/f/R"
w(f"{L3}/x.txt", "x")


def boom(*_a):
    raise RuntimeError("display bug")


c3 = se.Context(on_op=boom)
se.execute(se.compare(se.Task(mode="mirror", left=L3, right=R3)), c3)
check("an exception in the progress callback doesn't affect the sync", os.path.exists(f"{R3}/x.txt") and not c3.errors)

# ===================================================================== job live state (fileops)
jm = fileops.JobManager()
L4, R4 = f"{H}/j/L", f"{H}/j/R"
for i in range(5):
    w(f"{L4}/f{i}.bin", os.urandom(200_000))
w(f"{R4}/old1.txt", "o")
w(f"{R4}/old2.txt", "o")
t4 = se.Task(mode="mirror", left=L4, right=R4, versioning="trash")
p4 = se.compare(t4)
job = jm.submit_sync(p4)
wait(lambda: job.state in ("done", "failed", "cancelled"))
s4 = job.sync
check("job state: totals from the plan (5 copies + 2 deletions, 1,000,000 bytes)",
      s4["total_ops"] == 7 and s4["bytes_lr"] == 1_000_000 and s4["planned"] == {"lr": 5, "rl": 0, "del": 2, "link": 0})
check("job state: every operation counted once when done", s4["done_ops"] == 7 and s4["lr"] == 5 and s4["deleted"] == 2)
check("job state: bytes per direction add up", s4["done_lr"] == 1_000_000 and s4["done_rl"] == 0)
check("job state: last op is the finish step", s4["op"]["kind"] == "finish")
check("queue % uses the same measure as the window (bytes + 64 KiB per file)", job.fraction == 1.0)
fake = fileops.Job("sync", ["/x"], "/y")
fake.total_bytes, fake.done_bytes = 1_000_000, 0
fake.sync = {"total_ops": 100, "done_ops": 50}
check("fraction counts file operations too", abs(fake.fraction - (50 * 65536) / (1_000_000 + 100 * 65536)) < 1e-9)

# slow copy (throttled progress) to look at a run in flight
_orig_progress = se.Context.progress


def slow_progress(self, nbytes, current):
    time.sleep(0.12)
    _orig_progress(self, nbytes, current)


se.Context.progress = slow_progress
L5, R5 = f"{H}/big/L", f"{H}/big/R"
w(f"{L5}/big.iso", os.urandom(24 << 20))       # 24 MiB = 24 chunks ≈ 3 s throttled
w(f"{L5}/small.txt", "s")
job = jm.submit_sync(se.compare(se.Task(mode="mirror", left=L5, right=R5)))
ok = wait(lambda: job.sync and job.sync["op"] and job.sync["op"]["kind"] == "copy"
          and job.sync["op"]["src"].endswith("big.iso") and job.sync["op"]["done"] >= 3 << 20, 15)
op = dict(job.sync["op"]) if ok else {}
check("in flight: current file, full destination, its size and bytes done so far",
      ok and op["dst"] == f"{R5}/big.iso" and op["size"] == 24 << 20 and 0 < op["done"] < op["size"])
job.pause()
pump(0.4)
d1 = job.sync["op"]["done"]
pump(0.5)
check("pause holds the copy where it is", job.state == "paused" and job.sync["op"]["done"] == d1)
job.cancel()
wait(lambda: job.state in ("done", "failed", "cancelled"))
left_over = [n for n in os.listdir(R5) if "gxsync-tmp" in n] if os.path.isdir(R5) else []
check("cancel mid-file: that file isn't counted as done, not created, no temp file left",
      job.state == "cancelled" and job.sync["lr"] == 0 and not os.path.exists(f"{R5}/big.iso") and not left_over)
se.Context.progress = _orig_progress

# ===================================================================== helpers
check("duration format", syncprogress.fmt_duration(5) == "0:05" and syncprogress.fmt_duration(83) == "1:23"
      and syncprogress.fmt_duration(3725) == "1:02:05")
tk = se.Task(mode="mirror", left="/l", right="/r", versioning="folder", versions_dir="/v")
v, fl, fp, tl, tp = syncprogress.describe_op({"kind": "delete", "src": "/r/x", "dst": "folder", "dir": "delete-right"}, tk)
check("delete wording: side + versioning + where it goes", v == "Deleting on the right (moved to the versions folder)"
      and fp == "/r/x" and tl == "Moved to" and tp == "/v")
v, *_ = syncprogress.describe_op({"kind": "copy", "src": "/r/x", "dst": "/l/x", "dir": "rl"}, tk)
check("copy wording names the direction", v == "Copying file (right → left)")
v, *_ = syncprogress.describe_op({"kind": "hook", "src": "make", "dst": "pre", "dir": ""}, tk)
check("hook wording", v == "Running the pre-task command")

# ===================================================================== the window
L6, R6 = f"{H}/ui/L", f"{H}/ui/R"
for i in range(3):
    w(f"{L6}/docs/file{i}.dat", os.urandom(16 << 20))
w(f"{R6}/remove-me.txt", "r")
cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
win.left.current().navigate(L6)
win.right.current().navigate(R6)
win.set_active(win.left)
pump(0.3)
win.sync_panes()
sw = win._sync_win
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
se.Context.progress = slow_progress
sw.synchronize()
dlg = sw._run_dlg
check("Synchronize opens the progress window", dlg is not None and dlg.isVisible())
check("Compare/Synchronize disabled while running", not sw.btn_sync.isEnabled() and not sw.btn_compare.isEnabled())
ok = wait(lambda: dlg.from_path.text().endswith(".dat") and dlg.file_bar.isVisible(), 15)
pump(0.6)
check("shows the action with direction and the full source/destination paths",
      ok and dlg.action.text() == "Copying file (left → right)" and dlg.from_path.text().startswith(f"{L6}/docs/file")
      and dlg.to_path.text().startswith(f"{R6}/docs/file"))
check("direction strip highlights left → right", dlg.strip.direction == "lr" and dlg.strip.left == L6
      and dlg.strip.right == R6)
check("ring: % with SYNCHRONIZING and a turning gear", dlg.ring.top_text == "SYNCHRONIZING" and dlg.ring.gearTurning()
      and 0 < dlg.ring.value() < 1)
check("per-file bar text: bytes of size", " of 16.0 MB" in dlg.file_txt.text())
check("stats: files n of 4, data of 48.0 MB, task 1 of 1",
      dlg.stat["files"].text().endswith("of 4") and dlg.stat["data"].text().endswith("of 48.0 MB")
      and dlg.stat["task"].text() == "1 of 1")
ok = wait(lambda: dlg.eta() is not None and dlg.ring.bottom_text.endswith("LEFT"), 10)
check("countdown: time left shown once the rate is known", ok and dlg.stat["remaining"].text() != "estimating…"
      and dlg.stat["speed"].text().endswith("/s"))
check("status line in the Synchronize window shows % and the full path",
      wait(lambda: sw.status.text().startswith("Synchronizing — ") and f"→ {L6}/docs/" in sw.status.text(), 5))
dlg.toggle_pause()
pump(0.6)
e1 = dlg.elapsed()
pump(0.6)
check("Pause: ring says PAUSED, gear stops, clock stops, button says Resume",
      dlg.ring.top_text == "PAUSED" and not dlg.ring.gearTurning() and abs(dlg.elapsed() - e1) < 0.05
      and dlg.b_pause.text() == "Resume")
check("paused: queue/status % agree with the ring", sw.bar.value() == int(dlg.fraction() * 1000))
check("paused: countdown frozen at the last estimate", dlg.ring.bottom_text.endswith("LEFT"))
dlg.toggle_pause()
pump(0.3)
check("Resume", dlg.ring.top_text == "SYNCHRONIZING" and dlg.ring.gearTurning() and dlg.b_pause.text() == "Pause")
dlg.hide()
check("Run in background keeps running; 'Show progress' brings it back",
      sw.btn_progress.isVisible() and bool(sw._jobs))
sw.show_run_progress()
check("Show progress re-opens the window", dlg.isVisible())
dlg.close()                                       # closing while running = background
check("closing while running doesn't stop the run", not dlg.isVisible() and bool(sw._jobs) and not dlg.ended)
sw.show_run_progress()
wait(lambda: not sw._jobs, 60)
pump(0.5)
se.Context.progress = _orig_progress
check("finish: title, ring 100% COMPLETE, gear stopped",
      dlg.ended and "complete" in dlg.title.text() and dlg.ring.value() == 1.0 and dlg.ring.top_text == "COMPLETE"
      and not dlg.ring.gearTurning())
check("finish: summary with counts and time", "3 copied (48.0 MB), 1 deleted" in dlg.summary.text()
      and dlg.summary.isVisible())
check("finish: Close + Open log shown, Pause/Cancel hidden",
      dlg.b_close.isVisible() and dlg.b_log.isVisible() and not dlg.b_pause.isVisible() and not dlg.b_cancel.isVisible())
check("Copied → 3 of 3 / Deleted 1 of 1", dlg.stat["lr"].text() == "3 of 3" and dlg.stat["deleted"].text() == "1 of 1")
check("files really synced", sorted(os.listdir(f"{R6}/docs")) == ["file0.dat", "file1.dat", "file2.dat"]
      and not os.path.exists(f"{R6}/remove-me.txt"))
check("'Show progress' hidden after the run", not sw.btn_progress.isVisible())

# cancel from the window
w(f"{L6}/docs/more.dat", os.urandom(16 << 20))
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
se.Context.progress = slow_progress
sw.synchronize()
dlg2 = sw._run_dlg
wait(lambda: dlg2.from_path.text().endswith("more.dat"), 10)
dlg2.ask_cancel()                                 # confirmation auto-accepted ("Stop")
wait(lambda: not sw._jobs, 30)
pump(0.4)
se.Context.progress = _orig_progress
check("Cancel (confirmed): stops, says cancelled, no temp file left",
      dlg2.ended and dlg2.outcome == "cancelled" and "cancelled" in dlg2.title.text().lower()
      and not [n for n in os.listdir(f"{R6}/docs") if "gxsync-tmp" in n])

# auto-close option
cfg["sync_close_when_done"] = False
w(f"{L6}/docs/tiny.txt", "t")
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
sw.synchronize()
dlg3 = sw._run_dlg
dlg3.auto_close.setChecked(True)
check("auto-close option saved", cfg["sync_close_when_done"] is True)
wait(lambda: not sw._jobs, 30)
check("auto-close after a clean run", wait(lambda: not dlg3.isVisible(), 5) and dlg3.outcome == "ok")

print(f"\n{sum(results)}/{len(results)} sync-progress checks passed")
os._exit(0 if all(results) else 1)
