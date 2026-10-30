"""Sync & Backup window end-to-end (headless). Run: QT_QPA_PLATFORM=offscreen python tests/test_sync_ui.py"""
import os
import sys
import tempfile
import time

H = tempfile.mkdtemp(prefix="gx-syncui-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import syncengine as se  # noqa: E402
from gsexplorer import themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402
from gsexplorer import syncui  # noqa: E402

_popups = []
_orig_init = syncui.ScanProgressDialog.__init__


def _rec_init(self, *a, **k):
    _orig_init(self, *a, **k)
    _popups.append(self)


syncui.ScanProgressDialog.__init__ = _rec_init
_updates = []
_orig_upd = syncui.ScanProgressDialog.update_progress
syncui.ScanProgressDialog.update_progress = lambda self, d: (_updates.append(dict(d)), _orig_upd(self, d))


def _accept_box(self):
    self._clicked = next((b for b in self.buttons()
                          if self.buttonRole(b) in (QMessageBox.AcceptRole, QMessageBox.DestructiveRole)), None)
    return 0


QMessageBox.exec = _accept_box
QMessageBox.clickedButton = lambda self: getattr(self, "_clicked", None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Discard)
QMessageBox.information = staticmethod(lambda *a, **k: 0)
QMessageBox.warning = staticmethod(lambda *a, **k: print("   warning:", a[2] if len(a) > 2 else a))
_name = {"v": "Nightly"}
QInputDialog.getText = staticmethod(lambda *a, **k: (_name["v"], True))

T0 = time.time() - 9000


def w(p, data="x", t=T0):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(data)
    os.utime(p, (t, t))


def r(p):
    with open(p) as f:
        return f.read()


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout=20):
    t = time.time()
    while not cond():
        pump(0.05)
        assert time.time() - t < timeout, "timeout"


results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


L, R = f"{H}/L", f"{H}/R"
w(f"{L}/a.txt", "A-new", T0 + 100)
w(f"{L}/dir/b.txt", "B")
w(f"{R}/a.txt", "A-old")
w(f"{R}/stale.txt", "S")

cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
win.left.current().navigate(L)
win.right.current().navigate(R)
win.set_active(win.left)
pump(0.4)

# 1. quick start from panes -> mirror task prefilled
win.sync_panes()
sw = win._sync_win
t = sw.current_task()
check("pane quick-start prefills task", t and t.left == L and t.right == R and t.mode == "mirror")

# 2. compare -> preview rows
sw.f_ver.setCurrentIndex(se.VERSIONING.index("folder"))
sw.f_ver_dir.setText(f"{H}/versions")
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
check("scan popup shown with progress, then closed",
      len(_popups) == 1 and _updates and _updates[-1]["phase"] == "compare" and sw._scan_dlg is None)
cats = {i.rel: i.action for p in sw.model.plans for i in p.items}
check("compare shows plan", cats == {"a.txt": se.COPY_LR, "dir": se.COPY_LR, "stale.txt": se.DEL_R})
check("Synchronize enabled after compare", sw.btn_sync.isEnabled())

# 3. edit an action from the preview: keep stale.txt
for plan, it in [sw.model.item(i) for i in range(sw.model.rowCount())]:
    if it.rel == "stale.txt":
        it.action = se.NONE
sw._update_summary()

# 4. run through the job queue
sw.synchronize()
wait(lambda: not sw._jobs and sw.btn_compare.isEnabled(), 30)
pump(0.5)
check("sync copied", r(f"{R}/a.txt") == "A-new" and r(f"{R}/dir/b.txt") == "B")
check("per-row override respected", os.path.exists(f"{R}/stale.txt"))
check("overwritten file versioned", any(n.startswith("a ") for n in os.listdir(f"{H}/versions")))
check("log written", os.listdir(se.logs_dir()))
wait(lambda: sw.btn_compare.isEnabled())
left = [i for p in sw.model.plans for i in p.actionable()]
check("auto re-compare shows only the kept override", [i.rel for i in left] == ["stale.txt"])

# 5. editing settings invalidates the preview
sw.f_mode.setCurrentIndex(se.MODES.index("update"))
check("settings change clears stale preview", not sw.model.plans and "Compare" in sw.summary.text())

# 6. save profile, reload
sw.save()
check("profile saved", se.list_profiles() == ["Nightly"] and not sw.dirty)
p = se.load_profile("Nightly")
check("profile round-trips", p.tasks[0].mode == "update" and p.tasks[0].versions_dir == f"{H}/versions")

# 7. two-way conflict resolved from the preview
sw.new_profile()
t = sw.current_task()
sw.f_mode.setCurrentIndex(se.MODES.index("twoway"))
TL, TR = f"{H}/TL", f"{H}/TR"
w(f"{TL}/c.txt", "base")
os.makedirs(TR)
sw.f_left.setText(TL)
sw.f_right.setText(TR)
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
sw.synchronize()
wait(lambda: not sw._jobs and sw.btn_compare.isEnabled(), 30)
w(f"{TL}/c.txt", "LEFT", T0 + 500)
w(f"{TR}/c.txt", "RIGHT", T0 + 600)
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
conf = [i for p in sw.model.plans for i in p.items if i.category == se.CONFLICT]
check("two-way conflict detected", [i.rel for i in conf] == ["c.txt"])
conf[0].action = se.COPY_LR          # user picks "left wins"
sw._update_summary()
sw.synchronize()
wait(lambda: not sw._jobs and sw.btn_compare.isEnabled(), 30)
check("conflict resolved left→right", r(f"{TR}/c.txt") == "LEFT")

# 8. snapshot task through the UI, twice
sw.new_profile()
sw.f_mode.setCurrentIndex(se.MODES.index("snapshot"))
SL, SB = f"{H}/S", f"{H}/SnapBackups"
w(f"{SL}/doc.txt", "d")
sw.f_left.setText(SL)
sw.f_right.setText(SB)
for _ in range(2):
    sw.compare()
    wait(lambda: sw.btn_compare.isEnabled())
    sw.synchronize()
    wait(lambda: not sw._jobs and sw.btn_compare.isEnabled(), 30)
    time.sleep(1.1)
snaps = se.list_snapshots(SB)
check("two snapshots created", len(snaps) == 2)
check("snapshot files hard-linked", os.stat(f"{snaps[0]}/doc.txt").st_ino == os.stat(f"{snaps[1]}/doc.txt").st_ino)

# 9. guard: mirror with empty source is blocked in the preview
sw.new_profile()
E, F = f"{H}/Empty", f"{H}/Full"
os.makedirs(E)
w(f"{F}/keep.txt")
sw.f_left.setText(E)
sw.f_right.setText(F)
sw.compare()
wait(lambda: sw.btn_compare.isEnabled())
sw.synchronize()
pump(0.5)
check("empty-source mirror blocked", "Source is empty" in sw.summary.text() and not sw._jobs
      and os.path.exists(f"{F}/keep.txt"))

# 10. regressions from real-click testing (26.2.1)
win._sync_win.close()
win._sync_win.deleteLater()
win._sync_win = None
pump(0.2)
cfg["sync_last_profile"] = ""
NL, NR = f"{H}/NewSrc", f"{H}/NewDst"
w(f"{NL}/n.txt", "new")
os.makedirs(NR)
win.left.current().navigate(NL)
win.right.current().navigate(NR)
win.set_active(win.left)
pump(0.4)
win.open_sync()
sw = win._sync_win
t = sw.current_task()
check("fresh window starts with a usable task from the panes", t is not None and t.left == NL and t.right == NR
      and sw.editor_stack.currentWidget() is not sw.empty_page and sw.btn_sync.isEnabled())
sw.synchronize()                               # without pressing Compare first
wait(lambda: not sw._jobs and sw.btn_compare.isEnabled() and not sw._comparing, 30)
pump(0.5)
check("Synchronize compares first, then runs", os.path.isfile(f"{NR}/n.txt"))
check("result stays visible after the automatic re-compare", "Done" in sw.summary.text())
sw.remove_task()
check("no tasks -> empty state with Add button, Synchronize off",
      sw.editor_stack.currentWidget() is sw.empty_page and not sw.btn_sync.isEnabled())
_infos = []
QMessageBox.information = staticmethod(lambda *a, **k: _infos.append(a[2]) or 0)
sw.dirty = False
sw.start_with_panes(NL, NL)
check("same folder in both panes -> explained, target left empty",
      sw.current_task().right == "" and _infos and "same folder" in _infos[-1])

print(f"\n{sum(results)}/{len(results)} sync UI checks passed")
sys.exit(0 if all(results) else 1)
