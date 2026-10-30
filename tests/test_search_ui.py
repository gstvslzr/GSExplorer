"""Find Files window, end to end (headless). Run: QT_QPA_PLATFORM=offscreen python tests/test_search_ui.py"""
import faulthandler
import os
import sys
import tempfile
import time

H = tempfile.mkdtemp(prefix="gx-searchui-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("GX_HANGDUMP"):
    faulthandler.dump_traceback_later(int(os.environ["GX_HANGDUMP"]), exit=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import searchui as su  # noqa: E402
from gsexplorer import themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


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


def w(path, text="", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


D = os.path.join(H, "Docs")
w(f"{D}/Budget 2026.txt", "rent 1200\nInvoice #17 paid\n" + "pad\n" * 2000, 1_760_000_000)
w(f"{D}/Notes/meeting-notes.md", "Discussed the invoice backlog.\n", 1_750_000_000)
w(f"{D}/Notes/todo.txt", "buy milk\n")
w(f"{D}/Photos/trip.jpg", "jpeg-ish")
w(f"{D}/old/invoice-2019.txt", "old invoice\n", 1_560_000_000)
for i in range(30):
    w(f"{D}/bulk/file{i:02d}.log", "line\n" * (i + 1))

cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
win.left.current().navigate(D)
pump(0.5)

boxes = []
QMessageBox.warning = staticmethod(lambda *a, **k: (boxes.append(a[2]), QMessageBox.Ok)[1])
QMessageBox.information = staticmethod(lambda *a, **k: (boxes.append(a[2]), QMessageBox.Ok)[1])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
menus = []


class RecMenu(QMenu):
    def exec(self, *a, **k):
        menus.append([x.text() for x in self.actions() if not x.isSeparator()])


su.QMenu = RecMenu

# ---------------------------------------------------------------- entry points
tb_actions = [a for tb in win.findChildren(type(win.addToolBar("x"))) for a in tb.actions()]
check("toolbar has the magnifying-glass Search button with an icon",
      win.a_search in tb_actions and not win.a_search.icon().isNull())
check("shortcuts Ctrl+F and Alt+F7", [k.toString() for k in win.a_search.shortcuts()] == ["Ctrl+F", "Alt+F7"])
idx_filter = tb_actions.index(win.a_filter)
check("placed right after the filter funnel", tb_actions[idx_filter + 1] is win.a_search)
win.a_search.trigger()
sw = win._search_win
pump(0.3)
check("window opens on the active pane's folder", sw.isVisible() and sw.f_where.text() == D
      and sw.windowTitle() == "Find Files — GSExplorer+")

# ---------------------------------------------------------------- name search via Enter
sw.f_name.setEditText("invoice")
QTest.keyClick(sw.f_name.lineEdit(), Qt.Key_Return)
check("Enter starts the search; gear turns while running", sw.worker is not None and sw.gear.isVisible())
check("search finishes", wait(lambda: sw.worker is None))
names = sorted(sw.model.hit(r).name for r in range(sw.model.rowCount()))
check("name results", names == ["invoice-2019.txt"])
check("gear stops; status summarises", not sw.gear.isVisible() and "1 found" in sw.status.text())

# ---------------------------------------------------------------- content search
sw.f_name.setEditText("")
sw.f_text.setText("invoice")
sw.start_search()
wait(lambda: sw.worker is None)
names = sorted(sw.model.hit(r).name for r in range(sw.model.rowCount()))
check("text search finds files containing the text", names == ["Budget 2026.txt", "invoice-2019.txt", "meeting-notes.md"])
r = next(r for r in range(sw.model.rowCount()) if sw.model.hit(r).name == "Budget 2026.txt")
match_txt = sw.model.data(sw.model.index(r, su.C_MATCH))
check("Match column: line number and the matching text", match_txt.startswith("2: Invoice #17 paid"))
sw.view.selectRow(r)
pump(0.3)
check("selecting shows the line with the match highlighted", "Line 2" in sw.match.text() and "background" in sw.match.text())
check("…and the file in the preview", sw.preview._path.endswith("Budget 2026.txt"))
sw.f_word.setChecked(True)
sw.f_text.setText("invoice")
sw.start_search()
wait(lambda: sw.worker is None)
check("whole word: 'invoice' in 'invoice backlog' and 'old invoice', not 'Invoice #17'? (case-insensitive: all 3)",
      sw.model.rowCount() == 3)
sw.f_word.setChecked(False)
sw.f_text.setText("")

# ---------------------------------------------------------------- more options, sorting, filtering
sw.f_name.setEditText("*.log")
sw.f_mode.setCurrentIndex(1)
sw.f_min.setValue(0)
sw.start_search()
wait(lambda: sw.worker is None)
check("wildcards", sw.model.rowCount() == 30)
sw.view.sortByColumn(su.C_SIZE, Qt.DescendingOrder)
pump(0.1)
check("sort by size (largest first)", sw.model.hit(0).name == "file29.log" and sw.model.hit(29).name == "file00.log")
sw.r_filter.setText("file1")
pump(0.1)
check("filter the results", sw.model.rowCount() == 10 and "10 shown of 30" in sw.r_count.text())
sw.r_filter.setText("")
check("more options are folded; active ones are listed", not sw.more.isVisible())
sw.f_kind.setCurrentIndex(1)
check("summary mentions the kind filter", "documents" in sw.more_summary.text())
sw.f_kind.setCurrentIndex(0)
sw.b_more.setChecked(True)
check("More options unfolds", sw.more.isVisible())
sw.f_date.setCurrentIndex([v for _, v in su.DATE_CHOICES].index("between"))
from PySide6.QtCore import QDate  # noqa: E402
sw.f_from.setDate(QDate(2019, 1, 1))
sw.f_to.setDate(QDate(2019, 12, 31))
sw.f_name.setEditText("")
sw.f_mode.setCurrentIndex(0)
sw.start_search()
wait(lambda: sw.worker is None)
check("modified between dates", [sw.model.hit(r).name for r in range(sw.model.rowCount())] == ["invoice-2019.txt"])
sw.f_date.setCurrentIndex(0)

# ---------------------------------------------------------------- refine, history, saved searches
sw.f_name.setEditText(".txt")
sw.start_search()
wait(lambda: sw.worker is None)
before = sw.model.rowCount()
sw.f_name.setEditText("")
sw.f_text.setText("milk")
sw.start_search(refine=True)
wait(lambda: sw.worker is None)
check("search within results", before == 3 and [sw.model.hit(r).name for r in range(sw.model.rowCount())] == ["todo.txt"]
      and "within previous results" in sw.status.text())
sw.f_text.setText("")
check("history keeps recent names", [sw.f_name.itemText(i) for i in range(sw.f_name.count())][:2] == [".txt", "*.log"])
QInputDialog.getText = staticmethod(lambda *a, **k: ("Log files", True))
sw.f_name.setEditText("*.log")
sw.f_mode.setCurrentIndex(1)
sw._save_search()
sw.reset_criteria()
check("reset clears criteria but keeps the folder", sw.f_name.currentText() == "" and sw.f_where.text() == D)
sw._load_saved(sw.saved.findText("Log files"))
check("saved search restores the criteria", sw.f_name.currentText() == "*.log" and sw.f_mode.currentIndex() == 1
      and "Log files" in cfg["search_saved"])

# ---------------------------------------------------------------- context menu + actions
sw.f_name.setEditText("todo")
sw.f_mode.setCurrentIndex(0)
sw.start_search()
wait(lambda: sw.worker is None)
sw.view.selectRow(0)
pos = sw.view.visualRect(sw.model.index(0, 0)).center()
sw._context_menu(pos)
check("context menu: open, view, show, tab, terminal, copy, move, compress, trash",
      {"Open", "View (F3)", "Show in GSExplorer+", "Open folder in a new tab", "Open terminal in the folder",
       "Copy path", "Copy to other pane…", "Move to other pane…", "Compress…", "Move to trash"} <= set(menus[-1]))
sw.show_in_pane(sw.model.hit(0))
pump(0.5)
cv = win.active_view()
check("Show in GSExplorer+: navigates and selects", cv.path == os.path.join(D, "Notes")
      and cv.current_entry() is not None and cv.current_entry().name == "todo.txt")
sw.copy_paths([sw.model.hit(0)])
check("copy path", QApplication.clipboard().text() == os.path.join(D, "Notes", "todo.txt"))
sw.activateWindow()
sw.view.setFocus()
sw.view.selectRow(0)
pump(0.1)
QTest.keyClick(sw.view, Qt.Key_Delete)
check("Delete moves to trash and drops the row", wait(lambda: not os.path.exists(os.path.join(D, "Notes", "todo.txt")))
      and sw.model.rowCount() == 0)
out = os.path.join(H, "res.csv")
from PySide6.QtWidgets import QFileDialog  # noqa: E402
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
sw.f_name.setEditText("")
sw.start_search()
wait(lambda: sw.worker is None)
sw.export()
check("export CSV", os.path.exists(out) and open(out).read().startswith("name,folder,type"))

# ---------------------------------------------------------------- bad regex
boxes.clear()
sw.f_name.setEditText("([")
sw.f_mode.setCurrentIndex(2)
sw.start_search()
check("invalid regex: explained, nothing started", sw.worker is None and boxes and "regular expression" in boxes[-1])
sw.f_mode.setCurrentIndex(0)

# ---------------------------------------------------------------- big search: stop + responsiveness
B = os.path.join(H, "Big")
for i in range(20000):
    p = f"{B}/d{i % 200}/f{i}.txt"
    if i % 200 == 0 or not os.path.isdir(os.path.dirname(p)):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(f"row {i}\n" * 20)
sw.set_folders([B])
sw.f_name.setEditText("")
sw.f_text.setText("row 19999")
gaps, last = [], [time.time()]


def tick():
    now = time.time()
    gaps.append(now - last[0])
    last[0] = now


tk = QTimer()
tk.setInterval(30)
tk.timeout.connect(tick)
tk.start()
t0 = time.time()
sw.start_search()
wait(lambda: sw.worker is None, 120)
dt = time.time() - t0
tk.stop()
check(f"20,000-file content search: {dt:.1f}s, found the one file, longest window pause {max(gaps)*1000:.0f} ms",
      sw.model.rowCount() == 1 and max(gaps) < 0.5)
sw.f_text.setText("")
sw.set_folders(["/usr"])                      # big enough to still be running when Stop is pressed
sw.f_name.setEditText("zz-no-such-name-zz")
sw.start_search()
pump(0.3)
running = sw.worker is not None
sw.stop_search()
check("Stop ends the search early and says so", running and wait(lambda: sw.worker is None, 20)
      and "Stopped" in sw.status.text())

# ---------------------------------------------------------------- main window context entries
win.left.current().navigate(D)
pump(0.4)
recorded = []


class RecMenu2(QMenu):
    def exec(self, *a, **k):
        recorded.append([x.text() for x in self.actions() if not x.isSeparator()])


import gsexplorer.mainwindow as mw  # noqa: E402
mw.QMenu = RecMenu2
lv = win.left.current()
win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), False)
check("empty space menu: 'Search in this folder…'", "Search in this folder…" in recorded[-1])
lv.select_path(os.path.join(D, "Notes"))
pump(0.1)
win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), True)
check("folder menu: 'Search in “Notes”…'", "Search in “Notes”…" in recorded[-1])
win.open_search([os.path.join(D, "Notes")])
check("…opens Search on that folder", sw.f_where.text() == os.path.join(D, "Notes"))
sw.grab().save(os.path.join(os.environ.get("GX_SHOT_DIR", H), "search.png"))
sw.close()
check("last criteria remembered", cfg["search_last"].get("roots") == [os.path.join(D, "Notes")])

print(f"\n{sum(results)}/{len(results)} search UI checks passed")
sys.exit(0 if all(results) else 1)
