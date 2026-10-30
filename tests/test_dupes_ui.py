# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find Duplicates window, end to end (headless). Run: QT_QPA_PLATFORM=offscreen python tests/test_dupes_ui.py"""
import csv
import os
import sys
import tempfile
import time

H = tempfile.mkdtemp(prefix="gx-dupesui-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import dupes, themes  # noqa: E402
from gsexplorer import dupesui as du  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

results = []
BOXES = []
ANSWER = {"role": None}


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout=20):
    t = time.time()
    while not cond():
        pump(0.05)
        if time.time() - t > timeout:
            return False
    return True


def fake_exec(box):
    """Answer message boxes without showing them: click the button with ANSWER['role'] if any."""
    BOXES.append(box.text())
    role = ANSWER["role"]
    for b in box.buttons():
        if role is not None and box.buttonRole(b) == role:
            b.click()
            return 0
    return QMessageBox.Ok


QMessageBox.exec = fake_exec
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

class TI:
    """Item-style view of a (group, file) position in the flat DupModel (keeps the tests readable)."""

    def __init__(self, idx=None, gi=None, k=-1):
        if idx is not None:
            gi, k = w.model.locate(idx.row())
        self.gi, self.k = gi, k

    @property
    def idx(self):
        return w.model.group_index(self.gi) if self.k < 0 else w.model.file_index(self.gi, self.k)

    def text(self, col):
        t = w.model.data(self.idx.siblingAtColumn(col)) or ""
        return t[2:] if self.k < 0 and col == 0 and t[:2] in ("▾ ", "▸ ") else t

    def data(self, col, role):
        return w.model.data(self.idx.siblingAtColumn(col), role)

    def child(self, k):
        return TI(gi=self.gi, k=k)

    def childCount(self):
        return len(w.model.rows[self.gi][1])

    def checkState(self, col):
        return w.model.data(self.idx, Qt.CheckStateRole)

    def setCheckState(self, col, state):
        w.model.setData(self.idx, state, Qt.CheckStateRole)

    def mtime(self):
        return w.by_path[self.data(0, du.PATH)][1].mtime

    def __eq__(self, other):
        return isinstance(other, TI) and (self.gi, self.k) == (other.gi, other.k)

    def __hash__(self):
        return hash((self.gi, self.k))


def top(i):
    return TI(gi=i)


def ntop():
    return len(w.model.rows)


def sel_items():
    return {TI(i) for i in w.selected_file_items()}


D = os.path.join(H, "Photos")
for sub in ("2023", "2024/Trip", "Backup"):
    os.makedirs(os.path.join(D, sub))


def img(path, color, mtime):
    im = QImage(800, 500, QImage.Format_RGB32)
    im.fill(QColor(color))
    im.save(path)
    os.utime(path, (mtime, mtime))


img(f"{D}/2023/beach.png", "#2a9df4", 1_690_000_000)
img(f"{D}/2024/Trip/beach (1).png", "#2a9df4", 1_710_000_000)
img(f"{D}/Backup/beach.png", "#2a9df4", 1_700_000_000)
img(f"{D}/2023/sunset.png", "#f49d2a", 1_690_000_000)
img(f"{D}/Backup/sunset.png", "#f49d2a", 1_695_000_000)
img(f"{D}/2024/unique.png", "#22aa22", 1_700_000_000)
big = os.urandom(2_000_000)
for p in (f"{D}/2023/video.bin", f"{D}/Backup/video.bin"):
    with open(p, "wb") as f:
        f.write(big)

cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
win.left.current().navigate(D)
pump(0.5)

check("Tools menu has Find Duplicates (Ctrl+Shift+D)",
      win.a_dupes.shortcut().toString() == "Ctrl+Shift+D"
      and any(a is win.a_dupes for m in win.menuBar().findChildren(type(win.menuBar().actions()[0].menu()))
              for a in m.actions()))
win.a_dupes.trigger()
w = win._dupes_win
pump(0.3)
check("window opens with the active pane folder and a title", w.isVisible() and w.folder_list() == [D]
      and w.windowTitle() == "Find Duplicates — GSExplorer+")
check("actions disabled until something is selected", not w.b_trash.isEnabled() and not w.b_delete.isEnabled())

w.b_scan.click()
check("scan finishes", wait(lambda: w._worker is None and w.groups))
check("3 groups found (unique file not listed)", len(w.groups) == 3
      and not any(f.name == "unique.png" for g in w.groups for f in g.files))
check("results shown in tree with every copy", ntop() == 3
      and sum(top(i).childCount() for i in range(3)) == 7)
g0 = top(0)
check("default sort: most wasted space first", g0.text(du.C_NAME) == "video.bin" and g0.text(du.C_WASTED) != "")
check("status shows reclaimable space", "3 groups" in w.status.text() and "reclaimable" in w.status.text())

# ---------------------------------------------------------------- 26.4.2: auto-check, resizable Location, multi-select
firsts = {top(i).child(0).data(0, du.PATH) for i in range(ntop())}
allp = {f.path for g in w.groups for f in g.files}
check("after a search, all but the first copy of each group are checked", w.f_auto.isChecked()
      and w.selected == allp - firsts and len(w.selected) == 4)
hv = w.tree.header()
check("every column (incl. Location) is user-resizable",
      all(hv.sectionResizeMode(c) == du.QHeaderView.Interactive for c in range(len(du.COLS))))
hv.resizeSection(du.C_LOC, 700)
pump(0.1)
check("Location can be widened by the user", w.tree.columnWidth(du.C_LOC) == 700)
w.b_scan.click()
wait(lambda: w._worker is None)
check("user's Location width kept after a new search", w.tree.columnWidth(du.C_LOC) == 700)
w.clear_selection()

w.collapse_all(False)
w.tree.sortByColumn(du.C_SIZE, Qt.DescendingOrder)
pump(0.1)
rows = [top(i).child(k) for i in range(ntop())
        for k in range(top(i).childCount())]
vp = w.tree.viewport()


def centre(it):
    return w.tree.visualRect(it.idx).center()


QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, centre(rows[0]))
QTest.mouseClick(vp, Qt.LeftButton, Qt.ControlModifier, centre(rows[3]))
QTest.mouseClick(vp, Qt.LeftButton, Qt.ControlModifier, centre(rows[5]))
pump(0.1)
check("Ctrl+click adds rows to the selection", sel_items() == {rows[0], rows[3], rows[5]})
QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, centre(rows[1]))
QTest.mouseClick(vp, Qt.LeftButton, Qt.ShiftModifier, centre(rows[4]))
pump(0.1)
check("Shift+click selects a range", {rows[1], rows[2], rows[3], rows[4]} <= sel_items())
QTest.keyClick(w.tree, Qt.Key_Space)
pump(0.1)
rng = {r.data(0, du.PATH) for r in (rows[1], rows[2], rows[3], rows[4])}
check("Space checks all selected rows", rng <= w.selected
      and all(r.checkState(du.C_NAME) == Qt.Checked for r in (rows[1], rows[2], rows[3], rows[4])))
QTest.keyClick(w.tree, Qt.Key_Space)
pump(0.1)
check("Space again unchecks them", not (rng & w.selected))
cb = w.tree.visualRect(rows[2].idx)
cb_pt = QPoint(cb.left() + du.INDENT + 12, cb.center().y())   # checkbox of an indented file row
QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, cb_pt)
pump(0.1)
check("clicking one checkbox of a multi-row selection checks them all and keeps the selection",
      rng <= w.selected and len(w.selected_file_items()) >= 4)
QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, cb_pt)
pump(0.1)
check("…and unchecks them all", not (rng & w.selected))
QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, centre(rows[6]))
pump(0.8)                                            # longer than the double-click interval
QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, QPoint(w.tree.visualRect(rows[6].idx).left() + du.INDENT + 12, centre(rows[6]).y()))
pump(0.1)
check("clicking rows doesn't scroll the list sideways (checkboxes stay visible)",
      w.tree.horizontalScrollBar().value() == 0)
check("single row checkbox still works normally", rows[6].data(0, du.PATH) in w.selected)
w.clear_selection()
asked = []
_q = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
w.selected = {rows[1].data(0, du.PATH)}
w._set_checks()
QTest.keyClick(w.tree, Qt.Key_Delete)
pump(0.1)
QMessageBox.question = _q
check("Delete key / Move to Trash asks first (count and size), No keeps the file",
      asked and "1 selected file" in asked[0] and os.path.exists(rows[1].data(0, du.PATH)))
w.clear_selection()
w.tree.sortByColumn(du.C_WASTED, Qt.DescendingOrder)

# ---------------------------------------------------------------- collapse / expand (flat list)
n_rows = w.model.rowCount()
hdr = top(0).idx
QTest.mouseClick(w.tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                 QPoint(w.tree.visualRect(hdr).left() + 6, w.tree.visualRect(hdr).center().y()))
pump(0.1)
check("click on ▾ collapses a group", w.model.rowCount() == n_rows - top(0).childCount()
      and w.model.data(top(0).idx).startswith("▸"))
w.tree.setCurrentIndex(top(0).idx)
QTest.keyClick(w.tree, Qt.Key_Right)
pump(0.1)
check("→ on a collapsed group expands it", w.model.rowCount() == n_rows)
w.b_scan.setFocus()
[b for b in w.findChildren(du.QToolButton) if b.text() == "Collapse all"][0].click()
check("Collapse all leaves only the group headers", w.model.rowCount() == ntop())
[b for b in w.findChildren(du.QToolButton) if b.text() == "Expand all"][0].click()
check("Expand all shows every copy again", w.model.rowCount() == n_rows)

# ---------------------------------------------------------------- sorting
w.tree.sortByColumn(du.C_NAME, Qt.AscendingOrder)
pump(0.1)
names = [top(i).text(du.C_NAME) for i in range(3)]
check("sort by name (header click)", names == sorted(names, key=str.lower)
      and w.sort.currentText() == "Name")
w.sort.setCurrentIndex([s[0] for s in du.SORTS].index("Location"))
pump(0.1)
g_beach = next(top(i) for i in range(3) if top(i).text(du.C_NAME).startswith("beach"))
locs = [g_beach.child(k).text(du.C_LOC) for k in range(g_beach.childCount())]
check("sort by location sorts the copies inside each group", locs == sorted(locs, key=str.lower))
w.sort.setCurrentIndex([s[0] for s in du.SORTS].index("Date modified"))
pump(0.1)
dates = [g_beach.child(k).mtime() for k in range(g_beach.childCount())]
check("sort by date (newest first)", dates == sorted(dates, reverse=True))
w.b_order.click()
pump(0.1)
dates = [g_beach.child(k).mtime() for k in range(g_beach.childCount())]
check("reverse order button", dates == sorted(dates))
w.sort.setCurrentIndex([s[0] for s in du.SORTS].index("Size"))
pump(0.1)
check("sort by size", top(0).text(du.C_NAME) == "video.bin")

# ---------------------------------------------------------------- compare preview
g_beach = next(top(i) for i in range(3) if top(i).text(du.C_NAME).startswith("beach"))
w.tree.setCurrentIndex((g_beach.child(0)).idx)
pump(0.8)
a = g_beach.child(0).data(0, du.PATH)
check("compare: A shows the selected copy, B another copy", w.prev_a._path == a and w.prev_b._path
      and w.prev_b._path != a and w.cmp_with.count() == 2)
check("compare: both previews render the image",
      wait(lambda: w.prev_a.stack.currentWidget() is w.prev_a.image
           and w.prev_b.stack.currentWidget() is w.prev_b.image, 10))
check("compare verdict: identical content + which is newer", "Identical content" in w.verdict.text()
      and "newer" in w.verdict.text())
w.cmp_with.setCurrentIndex(1)
pump(0.3)
check("compare-with picker switches B", w.prev_b._path == w.cmp_with.itemData(1))

# ---------------------------------------------------------------- "Check all but first" button
check("'Check all but first' button enabled with results", w.b_all_but_first.isEnabled())
w.b_all_but_first.click()
pump(0.1)
firsts = {top(i).child(0).data(0, du.PATH) for i in range(ntop())}
everyone = {top(i).child(k).data(0, du.PATH) for i in range(ntop())
            for k in range(top(i).childCount())}
check("button ticks every copy except the first (as sorted) in each group",
      w.selected == everyone - firsts and all(
          top(i).child(0).checkState(du.C_NAME) == Qt.Unchecked
          for i in range(ntop())))
w.clear_selection()

# ---------------------------------------------------------------- ring progress popup
wk = du._Worker(lambda _w: None)
pd = du.ProgressDialog(w, "Finding duplicates", wk)
pd.update_progress({"phase": "walk", "done": 0, "total": 0, "path": D, "files": 1234})
check("ring: busy while listing, shows live file count", pd.ring.isBusy() and pd.ring.center_text == "1,234")
pd.update_progress({"phase": "quick", "done": 50, "total": 100, "path": D})
check("ring: quick check fills 0-40 %", abs(pd.ring.value() - 0.20) < 1e-9 and not pd.ring.isBusy())
pd.update_progress({"phase": "full", "done": 50, "total": 100, "path": D})
check("ring: full compare fills 40-100 %", abs(pd.ring.value() - 0.70) < 1e-9)
pd.show()
check("ring animates to the shown percentage", wait(lambda: pd.ring.displayed_percent() == 70, 5))
pd.ring.grab().save(os.path.join(os.environ.get("GX_SHOT_DIR", H), "ring70.png"))
pd.update_progress({"phase": "act", "done": 3, "total": 4, "path": D})
check("ring: actions fill by files done", abs(pd.ring.value() - 0.75) < 1e-9)
pd.reject()
check("Cancel sets the worker's cancel flag", wk.cancel.is_set())
pd.accept()
pd.deleteLater()

# ---------------------------------------------------------------- selection rules
w.apply_rule("all_but_newest")
pump(0.1)
newest_beach = f"{D}/2024/Trip/beach (1).png"
check("select all but newest", len(w.selected) == 4 and newest_beach not in w.selected
      and f"{D}/Backup/sunset.png" not in w.selected)
check("checkboxes follow the rule", sum(g_beach.child(k).checkState(du.C_NAME) == Qt.Checked
                                        for k in range(g_beach.childCount())) == 2)
check("trash/delete/move enabled with a selection", w.b_trash.isEnabled() and w.b_move.isEnabled())
w.clear_selection()
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: f"{D}/Backup")
w.apply_rule("in_folder")
check("select everything in Backup", w.selected == {f"{D}/Backup/beach.png", f"{D}/Backup/sunset.png",
                                                    f"{D}/Backup/video.bin"})
# user ticks the last remaining copy of sunset too
g_sun = next(top(i) for i in range(3) if top(i).text(du.C_NAME) == "sunset.png")
for k in range(g_sun.childCount()):
    g_sun.child(k).setCheckState(du.C_NAME, Qt.Checked)
check("manual ticking updates the selection", f"{D}/2023/sunset.png" in w.selected)

# ---------------------------------------------------------------- move to audit folder
AUD = os.path.join(H, "Audit")
os.makedirs(AUD)
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: AUD)
ANSWER["role"] = QMessageBox.AcceptRole                     # "Keep newest and continue"
BOXES.clear()
w.act("move")
check("guard: a group with every copy selected asks first", any("every copy is selected" in b for b in BOXES))
ANSWER["role"] = None
check("move finishes", wait(lambda: w._worker is None))
audit = [d for d in os.listdir(AUD) if d.startswith(dupes.AUDIT_PREFIX)]
check("audit folder created with manifest", len(audit) == 1
      and os.path.exists(os.path.join(AUD, audit[0], dupes.MANIFEST)))
with open(os.path.join(AUD, audit[0], dupes.MANIFEST), newline="") as fh:
    rows = list(csv.DictReader(fh))
moved = {r["original_path"] for r in rows}
check("moved the Backup copies; one sunset kept (the newest)", moved == {
    f"{D}/Backup/beach.png", f"{D}/2023/sunset.png", f"{D}/Backup/video.bin"} and os.path.exists(f"{D}/Backup/sunset.png"))
check("files really moved, full path kept inside the audit folder",
      all(not os.path.exists(p) and os.path.exists(os.path.join(AUD, audit[0], p.lstrip("/"))) for p in moved))
check("result list updated (solved groups removed)", len(w.groups) == 1 and w.groups[0].copies == 2
      and not w.selected)
check("summary message", any("Moved to the audit folder: 3 file(s)" in b for b in BOXES))

# ---------------------------------------------------------------- trash
w.b_scan.click()
wait(lambda: w._worker is None)
check("re-scan skips the audit folder", len(w.groups) == 1)
w.apply_rule("all_but_oldest")
BOXES.clear()
w.act("trash")
check("trash finishes", wait(lambda: w._worker is None))
check("trashed one beach copy; oldest kept", os.path.exists(f"{D}/2023/beach.png")
      and not os.path.exists(f"{D}/2024/Trip/beach (1).png")
      and len(os.listdir(os.path.join(H, ".local/share/Trash/files"))) == 1)
check("no groups left -> empty state", not w.groups and w.empty.isVisible() and "No duplicates" in w.empty.text())

# ---------------------------------------------------------------- undo audit
from PySide6.QtWidgets import QFileDialog as QFD  # noqa: E402
QFD.getOpenFileName = staticmethod(lambda *a, **k: (os.path.join(AUD, audit[0], dupes.MANIFEST), ""))
w.undo_audit()
check("undo audit move puts the files back", all(os.path.exists(p) for p in moved))

# ---------------------------------------------------------------- delete + name mode warning
w.b_scan.click()
wait(lambda: w._worker is None)
check("files back -> duplicates found again", len(w.groups) == 3)
g = next(x for x in w.groups if x.files[0].name == "video.bin")
w.selected = {g.files[1].path}
w._set_checks()
ANSWER["role"] = QMessageBox.RejectRole                     # Cancel the confirmation
w.act("delete")
pump(0.2)
check("delete asks for confirmation; Cancel keeps the file", os.path.exists(g.files[1].path) and w._worker is None)
ANSWER["role"] = QMessageBox.DestructiveRole
w.act("delete")
wait(lambda: w._worker is None)
check("delete permanently", not os.path.exists(f"{D}/Backup/video.bin") and os.path.exists(f"{D}/2023/video.bin"))
ANSWER["role"] = None

w.f_mode.setCurrentIndex(2)                                 # name only
w.b_scan.click()
wait(lambda: w._worker is None)
w.tree.setCurrentIndex((top(0).child(0)).idx)
pump(0.3)
check("name mode shows a 'contents not compared' warning", "not compared" in w.verdict.text())
check("options remembered", cfg["dupes_mode"] == "name")

# ---------------------------------------------------------------- large result sets stay responsive
big_groups = [dupes.DupGroup(i + 1, 1000, f"{i:064x}",
                             [dupes.DupFile(f"/big/{c}/f{i}.bin", 1000, 1_700_000_000 + c, 99, i * 4 + c)
                              for c in range(2 + i % 3)], "content") for i in range(15000)]
w.mode = "content"
n_files = sum(len(g.files) for g in big_groups)
gaps, last = [], [time.time()]


def tick():
    now = time.time()
    gaps.append(now - last[0])
    last[0] = now


from PySide6.QtCore import QTimer as _QT  # noqa: E402
tk = _QT()
tk.setInterval(20)
tk.timeout.connect(tick)
tk.start()
t0 = time.time()
w.set_results(big_groups, autocheck=True)
gear_shown = w.busy() and "Preparing" in w.gear.text
wait(lambda: not w.busy(), 60)
t_ready = time.time() - t0
pump(0.2)
gaps.clear()
last[0] = time.time()
t0 = time.time()
w.clear_selection()
w.b_all_but_first.click()
sel_gear = w.busy()
wait(lambda: not w.busy(), 60)
t_sel = time.time() - t0
pump(0.2)
tk.stop()
stall = max(gaps) if gaps else 0
check(f"{n_files:,} files: gear shown while preparing ({gear_shown}), ready in {t_ready:.1f}s; "
      f"'Check all but first' {t_sel:.1f}s with gear ({sel_gear}); longest window freeze {stall*1000:.0f} ms",
      gear_shown and sel_gear and t_ready < 10 and t_sel < 10 and stall < 1.0
      and len(w.selected) == n_files - 15000)
w.tree.sortByColumn(du.C_NAME, Qt.AscendingOrder)
sort_gear = w.busy()
wait(lambda: not w.busy(), 60)
names = [top(i).text(du.C_NAME) for i in range(50)]
check("sorting a large list runs with the gear and gives the right order", sort_gear
      and names == sorted(names, key=str.lower))
w.clear_selection()
w.b_scan.click()                                            # back to the real results for the next checks
wait(lambda: w._worker is None)

# ---------------------------------------------------------------- export + F3 + context entry
out = os.path.join(H, "list.csv")
QFD.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
w.export()
check("export CSV", os.path.exists(out) and "group,copies,match" in open(out).read())
from gsexplorer import dialogs  # noqa: E402
opened = []
dialogs.ViewerDialog.exec = lambda self: opened.append((self.current(), self.files))
w.tree.setCurrentIndex((top(0).child(0)).idx)
QTest.keyClick(w.tree, Qt.Key_F3)
check("F3 views the copies of a group (arrows step between them)",
      opened and len(opened[0][1]) == top(0).childCount())
w.close()
win.open_dupes([f"{D}/2023"])
check("'Find duplicates in this folder' presets the folder", win._dupes_win.folder_list() == [f"{D}/2023"])
win._dupes_win.grab().save(os.path.join(os.environ.get("GX_SHOT_DIR", H), "dupes.png"))
win._dupes_win.close()

print(f"\n{sum(results)}/{len(results)} duplicates UI checks passed")
sys.exit(0 if all(results) else 1)
