"""F3 viewer navigation tests (headless). Run: QT_QPA_PLATFORM=offscreen python tests/test_viewer.py"""
import os
import sys
import tempfile
import time

H = tempfile.mkdtemp(prefix="gx-viewer-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

app = QApplication([])
from gexplorer import dialogs, themes  # noqa: E402
from gexplorer.config import Config  # noqa: E402
from gexplorer.mainwindow import MainWindow  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


D = os.path.join(H, "Files")
os.makedirs(os.path.join(D, "Sub folder"))
for name, txt in (("a notes.txt", "alpha text"), ("c code.py", "print('c')"), ("e.bin", "\0\1\2")):
    with open(os.path.join(D, name), "w") as f:
        f.write(txt)
img = QImage(3000, 2000, QImage.Format_RGB32)
img.fill(QColor("#3fc1b0"))
img.save(os.path.join(D, "b photo.png"))
os.mkfifo(os.path.join(D, "d pipe"))

cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
v = win.left.current()
v.navigate(D)
pump(0.6)
v.select_path(os.path.join(D, "a notes.txt"))
files = [e.path for e in v.model.rows if not e.is_dir and not e.parent_link]
check("file list skips folders and '..'", [os.path.basename(p) for p in files] ==
      ["a notes.txt", "b photo.png", "c code.py", "d pipe", "e.bin"])

dlg = dialogs.ViewerDialog(win, files[0], files, on_change=v.select_path)
dlg.show()
pump(0.2)
check("opens on the selected file", dlg.current().endswith("a notes.txt") and dlg.counter.text() == "1 of 5")
check("text shown", isinstance(dlg._content, QPlainTextEdit) and "alpha text" in dlg._content.toPlainText())

dlg.b_next.click()
pump(0.2)
check("▶ button goes to next file (image)", dlg.current().endswith("b photo.png") and "3000×2000" in dlg.info.text())
check("pane selection follows the viewer", v.current_entry().name == "b photo.png")

QTest.keyClick(dlg, Qt.Key_Right)
pump(0.1)
check("→ key goes to next", dlg.current().endswith("c code.py"))
QTest.keyClick(dlg._content, Qt.Key_Right)           # key pressed while the text area has the mouse/focus
pump(0.1)
check("→ works over the text area too", dlg.current().endswith("d pipe"))
check("special file shown without blocking", "special file" in dlg._content.text())
QTest.keyClick(dlg, Qt.Key_N)
QTest.keyClick(dlg, Qt.Key_N)
pump(0.1)
check("wraps from last to first", dlg.current().endswith("a notes.txt"))
QTest.keyClick(dlg, Qt.Key_Left)
pump(0.1)
check("← from first wraps to last", dlg.current().endswith("e.bin") and "hex view" in dlg.info.text())
dlg.b_prev.click()
pump(0.1)
check("◀ button goes back", dlg.current().endswith("d pipe"))
check("title follows file", dlg.windowTitle().startswith("d pipe"))
QTest.keyClick(dlg, Qt.Key_Escape)
pump(0.1)
check("Esc closes; pane stays on last viewed file", not dlg.isVisible() and v.current_entry().name == "d pipe")

single = dialogs.ViewerDialog(win, files[0])
check("single file: arrows disabled", not single.b_next.isEnabled() and not single.b_prev.isEnabled())

print(f"\n{sum(results)}/{len(results)} viewer checks passed")
sys.exit(0 if all(results) else 1)
