"""UI-level integration tests (headless). Modal dialogs are auto-answered.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_ui.py
Uses a throwaway HOME so your real config, trash and files are never touched.
"""
import os
import stat
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="gx-ui-")
os.environ["HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, ".local", "share")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, ".cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import dialogs, fileops, fsutil, themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

# ---------------------------------------------------------------- fixtures
H = TMP
for d in ("Projects/Sub", "Target"):
    os.makedirs(os.path.join(H, d), exist_ok=True)
for name, data in (("Projects/notes.txt", b"hello"), ("Projects/b.bin", b"\0\1\2" * 100),
                   ("Projects/.hidden", b"")):
    with open(os.path.join(H, name), "wb") as f:
        f.write(data)
os.mkfifo(os.path.join(H, "Projects", "pipe"))

# ---------------------------------------------------------------- auto-answer dialogs
dialogs.TransferDialog.exec = lambda self: 1
_next_text = {"value": None}


def _fake_input_exec(self):
    self.setTextValue(_next_text["value"])
    return 1


QInputDialog.exec = _fake_input_exec
QInputDialog.getText = staticmethod(lambda *a, **k: (_next_text["value"], True))
_conflict_choice = {"value": fileops.SKIP}


def _fake_conflict_exec(self):
    self.choice = _conflict_choice["value"]
    return 1


dialogs.ConflictDialog.exec = _fake_conflict_exec


def _fake_box_exec(self):
    self._clicked = next((b for b in self.buttons() if self.buttonRole(b) == QMessageBox.DestructiveRole), None)
    return 0


QMessageBox.exec = _fake_box_exec
QMessageBox.clickedButton = lambda self: getattr(self, "_clicked", None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.warning = staticmethod(lambda *a, **k: print("   warning:", a[2] if len(a) > 2 else a))

themes.ensure_user_themes()
cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()


def pump(sec=0.3):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait_jobs(timeout=15):
    t = time.time()
    while win.jobs.active():
        pump(0.05)
        assert time.time() - t < timeout, "jobs stuck"
    pump(0.6)   # let watcher debounce refresh views


def select(view, name):
    path = os.path.join(view.path, name)
    for _ in range(50):
        if view.select_path(path):
            return
        pump(0.05)
    raise AssertionError(f"{name} not visible in {view.path}")


L = win.left.current()
R = win.right.current()
L.navigate(os.path.join(H, "Projects"))
R.navigate(os.path.join(H, "Target"))
pump(0.8)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


# 1. listing + hidden toggle + fifo shown
names = [e.name for e in L.model.rows]
check("listing hides dotfiles by default", ".hidden" not in names and "notes.txt" in names and "pipe" in names)
win.a_hidden.setChecked(True)
win.toggle_hidden()
pump()
check("Ctrl+H shows hidden files", ".hidden" in [e.name for e in L.model.rows])
win.a_hidden.setChecked(False)
win.toggle_hidden()

# 2. F5 copy to other pane
win.set_active(win.left)
select(L, "notes.txt")
win.a_copy_to.trigger()
wait_jobs()
check("F5 copies to other pane", os.path.isfile(os.path.join(H, "Target", "notes.txt")))
check("other pane auto-refreshed", "notes.txt" in [e.name for e in R.model.rows])

# 3. conflict -> skip, then keep both
with open(os.path.join(H, "Target", "notes.txt"), "wb") as f:
    f.write(b"EXISTING")
select(L, "notes.txt")
_conflict_choice["value"] = fileops.SKIP
win.a_copy_to.trigger()
wait_jobs()
check("conflict skip keeps target", open(os.path.join(H, "Target", "notes.txt"), "rb").read() == b"EXISTING")
_conflict_choice["value"] = fileops.RENAME
select(L, "notes.txt")
win.a_copy_to.trigger()
wait_jobs()
check("conflict keep-both renames", os.path.isfile(os.path.join(H, "Target", "notes (2).txt")))

# 4. rename (F2)
select(L, "notes.txt")
_next_text["value"] = "renamed.txt"
win.a_rename.trigger()
pump(0.6)
check("F2 renames", os.path.isfile(os.path.join(H, "Projects", "renamed.txt"))
      and not os.path.exists(os.path.join(H, "Projects", "notes.txt")))
check("renamed item is selected", L.current_entry() is not None and L.current_entry().name == "renamed.txt")

# 5. F7 new folder
_next_text["value"] = "Fresh"
win.a_mkdir.trigger()
pump(0.6)
check("F7 creates folder", os.path.isdir(os.path.join(H, "Projects", "Fresh")))

# 6. cut + paste into folder
select(L, "renamed.txt")
win.a_cut.trigger()
L.navigate(os.path.join(H, "Projects", "Fresh"))
pump(0.5)
win.a_paste.trigger()
wait_jobs()
check("cut/paste moves", os.path.isfile(os.path.join(H, "Projects", "Fresh", "renamed.txt"))
      and not os.path.exists(os.path.join(H, "Projects", "renamed.txt")))

# 7. trash + restore
select(L, "renamed.txt")
win.a_trash.trigger()
wait_jobs()
trash_files = os.path.join(fsutil.home_trash(), "files")
check("F8 moves to trash", os.path.exists(os.path.join(trash_files, "renamed.txt"))
      and not os.path.exists(os.path.join(H, "Projects", "Fresh", "renamed.txt")))
L.navigate(trash_files)
pump(0.5)
select(L, "renamed.txt")
win.restore_trash()
pump(0.5)
check("restore from trash", os.path.isfile(os.path.join(H, "Projects", "Fresh", "renamed.txt")))

# 8. delete permanently (confirmed)
L.navigate(os.path.join(H, "Projects", "Fresh"))
pump(0.5)
select(L, "renamed.txt")
win.a_delete.trigger()
wait_jobs()
check("Shift+Del deletes permanently", not os.path.exists(os.path.join(H, "Projects", "Fresh", "renamed.txt")))

# 9. Backspace goes up and lands on the folder we came from
L.focus_view()
QTest.keyClick(L.view, Qt.Key_Backspace)
pump(0.6)
check("Backspace = parent, selects previous folder",
      L.path == os.path.join(H, "Projects") and L.current_entry() and L.current_entry().name == "Fresh")

# 10. Tab switches pane
L.focus_view()
win.set_active(win.left)
QTest.keyClick(L.view, Qt.Key_Tab)
pump(0.2)
check("Tab switches active pane", win.active_pane() is win.right)
win.set_active(win.left)

# 11. quick filter
L.show_filter()
L.filter_edit.setText("*.bin")
pump(0.2)
check("quick filter", [e.name for e in L.model.rows if not e.parent_link] == ["b.bin"])
L.hide_filter()

# 12. tabs
n = win.left.tabs.count()
win.a_newtab.trigger()
pump(0.3)
check("Ctrl+T opens tab", win.left.tabs.count() == n + 1)
win.a_closetab.trigger()
pump(0.2)
check("Ctrl+W closes tab", win.left.tabs.count() == n)

# 13. viewer must not block on a FIFO; text and binary render
t0 = time.time()
dialogs.ViewerDialog(win, os.path.join(H, "Projects", "pipe"))
check("F3 on FIFO returns immediately", time.time() - t0 < 1.0)
dlg = dialogs.ViewerDialog(win, os.path.join(H, "Projects", "b.bin"))
check("F3 binary shows hex", "hex view" in " ".join(w.text() for w in dlg.findChildren(type(win.cmd_prompt))))

# 14. properties on file & folder
select(L, "b.bin")
pd = dialogs.PropertiesDialog(win, L.selected_entries(), win.icons)
pump(0.5)
check("properties computes size", "300 B" in pd.size_lbl.text())
pd.done(0)

# 15. drag & drop from left to right pane (copy)
select(L, "b.bin")
md = L.model.mimeData([L.model.index(L.model.row_of(os.path.join(L.path, "b.bin")), 0)])
R.model.dropMimeData(md, Qt.CopyAction, -1, -1, R.model.index(-1, -1))
wait_jobs()
check("drag-drop copies", os.path.isfile(os.path.join(H, "Target", "b.bin")))

# 15b. selection survives an external change (inotify auto-refresh)
L.navigate(os.path.join(H, "Projects"))
pump(0.5)
L.select_pattern("*.bin")
L.select_pattern("Sub")
before = {e.name for e in L.selected_entries(use_current=False)}
with open(os.path.join(H, "Projects", "external-new.txt"), "w") as f:
    f.write("x")
pump(1.0)
after = {e.name for e in L.selected_entries(use_current=False)}
check("auto-refresh shows new file", "external-new.txt" in [e.name for e in L.model.rows])
check("selection kept across auto-refresh", before == after == {"b.bin", "Sub"})

# 16. themes switch without error, user theme loads
with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "gsexplorer-plus", "themes", "mine.json"), "w") as f:
    f.write('{"name": "Mine", "base": "dark", "colors": {"accent": "#ff8800", "bogus": "x"}}')
win._fill_theme_menu()
check("user theme discovered", "Mine" in win.themes)
for name in ("Light", "Dark", "Midnight", "High Contrast", "Mine", "system"):
    win.apply_theme(name)
pump(0.2)
check("all themes apply", True)

# 17. session persistence
win.save_session()
cfg2 = Config()
check("session saved", cfg2["session"]["left"] and cfg2["session"]["right"] == [os.path.join(H, "Target")])

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
win.jobs.cancel_all()
sys.exit(1 if failed else 0)
