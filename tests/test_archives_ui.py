"""Compress / Extract in the context menu + terminal folder, end to end (headless).

Run: QT_QPA_PLATFORM=offscreen python tests/test_archives_ui.py
"""
import os
import stat
import subprocess
import sys
import tempfile
import time
import faulthandler
if os.environ.get("GX_HANGDUMP"):
    faulthandler.dump_traceback_later(int(os.environ["GX_HANGDUMP"]), exit=True)

H = tempfile.mkdtemp(prefix="gx-archui-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication, QInputDialog, QMenu  # noqa: E402

app = QApplication([])
from gsexplorer import archives, dialogs, system, themes  # noqa: E402
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
        pump(0.05)
        if time.time() - t > timeout:
            return False
    return True


# ---------------------------------------------------------------- terminal command building (fake PATH)
B = os.path.join(H, "fakebin")
os.makedirs(B)


def fake(name, body="#!/bin/sh\nexit 0\n"):
    p = os.path.join(B, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, 0o755)
    return p


real_path = os.environ["PATH"]
os.environ["PATH"] = B
gt = fake("gnome-terminal")
fake("gnome-terminal.wrapper")
os.symlink(os.path.join(B, "gnome-terminal.wrapper"), os.path.join(B, "x-terminal-emulator"))
D = "/tmp/some dir"
cmd = system.terminal_command(D)
check("x-terminal-emulator -> Ubuntu's gnome-terminal.wrapper: calls gnome-terminal with --working-directory",
      cmd == [gt, f"--working-directory={D}"])
for n in ("x-terminal-emulator", "gnome-terminal", "gnome-terminal.wrapper"):
    os.unlink(os.path.join(B, n))
xt = fake("xterm")
check("xterm (no folder option): relies on the started process's folder", system.terminal_command(D) == [xt])
os.unlink(xt)
mystery = fake("wezterm", "#!/bin/sh\necho '  --cwd <DIR>  start in DIR'\n")
system._probe_cache.clear()
check("unknown terminal: its --help is read and the listed option is used",
      system.terminal_command(D) == [mystery, "--cwd", D])
ko = fake("konsole")
check("custom command with {dir} placeholder", system.terminal_command(D, "konsole --separate --workdir {dir}")
      == ["konsole", "--separate", "--workdir", D])
check("custom command without placeholder: known terminal's option added",
      system.terminal_command(D, "konsole --separate") == ["konsole", "--separate", "--workdir", D])
os.environ["PATH"] = real_path

# ---------------------------------------------------------------- main window
W = os.path.join(H, "Work")
os.makedirs(os.path.join(W, "Reports", "2026"))
open(os.path.join(W, "Reports", "2026", "q1.txt"), "w").write("quarter one\n" * 1000)
open(os.path.join(W, "notes.txt"), "w").write("notes")
R = os.path.join(H, "Right side")
os.makedirs(R)
cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
lv = win.left.current()
lv.navigate(W)
win.right.current().navigate(R)
pump(0.6)
win.set_active(win.left)

opened = []
system.open_terminal = lambda folder, custom="": opened.append(folder)
menus = []
import gsexplorer.mainwindow as _mw  # noqa: E402


class _RecMenu(QMenu):
    def exec(self, *a, **k):          # record the entries instead of showing the menu
        menus.append([x.text() for x in self.actions() if not x.isSeparator()])


_mw.QMenu = _RecMenu

win.a_terminal.trigger()
check("F12 / Open terminal here: the active pane's folder", opened[-1] == W)
win.set_active(win.right)
win.a_terminal.trigger()
check("…and the other pane's folder when that pane is active", opened[-1] == R)
win.set_active(win.left)

lv.select_path(os.path.join(W, "Reports"))
pump(0.1)
win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), True)
items = menus[-1]
check("folder context menu has Compress… and 'Open terminal in “Reports”'",
      "&Compress…" in items and "Open terminal in “Reports”" in items)
check("no Extract entries for a plain folder", not any(t.startswith("Extract") for t in items))
win.open_terminal(os.path.join(W, "Reports"))
check("'Open terminal in “Reports”' opens that folder", opened[-1] == os.path.join(W, "Reports"))
win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), False)
check("empty-space menu keeps 'Open terminal here'", any("terminal" in t.lower() for t in menus[-1]))

# ---------------------------------------------------------------- compress through the dialog + queue
answers = {}


def fake_exec(dlg):
    dlg.name.setText(answers.get("name", dlg.name.text()))
    i = dlg.fmt.findData(answers.get("fmt", "zip"))
    dlg.fmt.setCurrentIndex(i)
    if answers.get("pw"):
        dlg.pw.setText(answers["pw"])
    if answers.get("other"):
        dlg.where.setCurrentIndex(1)
    answers["seen_name"] = dlg.name.text()
    answers["pw_enabled"] = dlg.pw.isEnabled()
    dlg._accept()
    return True


dialogs.CompressDialog.exec = fake_exec
lv.select_path(os.path.join(W, "Reports"))
pump(0.1)
win.a_compress.trigger()
check("default archive name is the folder name", answers["seen_name"] == "Reports")
check("password field only for 7z", answers["pw_enabled"] is False)
check("compress job finishes", wait(lambda: os.path.exists(os.path.join(W, "Reports.zip")) and not win.jobs.active()))
pump(0.5)
cur = lv.current_entry()
check("new archive is selected in the pane", cur is not None and cur.name == "Reports.zip")
check("queue button shows idle again (no crash on the new job kinds)", "idle" in win.queue_btn.text())

# compress two items into the other pane as tar.gz
lv.select_path(os.path.join(W, "notes.txt"))
lv.view.selectAll()
pump(0.1)
answers.update(name="Bundle", fmt="tar.gz", other=True)
win.a_compress.trigger()
check("save in the other pane, chosen format", wait(lambda: os.path.exists(os.path.join(R, "Bundle.tar.gz"))
                                                     and not win.jobs.active()))
answers.clear()

# ---------------------------------------------------------------- extract entries + jobs
lv.select_path(os.path.join(W, "Reports.zip"))
pump(0.1)
win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), True)
items = menus[-1]
check("archive context menu: Extract here / Extract to “Reports/” / Extract to…",
      {"Extract here", "Extract to “Reports/”", "Extract to…"} <= set(items))
os.rename(os.path.join(W, "Reports"), os.path.join(W, "Reports-original"))
win.extract("here", [os.path.join(W, "Reports.zip")])
check("Extract here: single top folder restored next to the archive",
      wait(lambda: os.path.exists(os.path.join(W, "Reports", "2026", "q1.txt")) and not win.jobs.active())
      and open(os.path.join(W, "Reports", "2026", "q1.txt")).read() == "quarter one\n" * 1000)
win.extract("folder", [os.path.join(W, "Reports.zip")])
check("Extract to “Reports/”: name taken, so “Reports (2)” — nothing overwritten",
      wait(lambda: os.path.isdir(os.path.join(W, "Reports (2)", "Reports")) and not win.jobs.active()))

from PySide6.QtWidgets import QFileDialog  # noqa: E402
X = os.path.join(H, "Elsewhere")
os.makedirs(X)
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: X)
win.extract("to", [os.path.join(R, "Bundle.tar.gz")])
check("Extract to…: chosen folder; two top items -> folder named after the archive",
      wait(lambda: os.path.exists(os.path.join(X, "Bundle", "notes.txt")) and not win.jobs.active()))

# ---------------------------------------------------------------- password flow (needs 7-Zip to create the file)
if archives.tool("7z"):
    enc = os.path.join(W, "secret.zip")
    subprocess.run([archives.tool("7z"), "a", "-tzip", "-ptopsecret", "-mem=ZipCrypto", enc,
                    os.path.join(W, "notes.txt")], capture_output=True, check=True)
    asked = []
    replies = ["wrong", "topsecret"]
    QInputDialog.getText = staticmethod(lambda *a, **k: (asked.append(a[2]), (replies.pop(0), True))[1])
    win.extract("folder", [enc])
    ok = wait(lambda: os.path.exists(os.path.join(W, "secret", "notes.txt")) and not win.jobs.active(), 30)
    check("encrypted zip: asks, wrong password -> asked again, right one extracts", ok and len(asked) == 2
          and asked[1].startswith("Wrong password") and open(os.path.join(W, "secret", "notes.txt")).read() == "notes")
    check("failed attempt left nothing behind", not [n for n in os.listdir(W) if n.startswith(".gsexplorer-plus")])
else:
    print("SKIP password flow: 7-Zip not installed")

# missing tool: entries shown disabled with the reason
if not archives.tool("7z"):
    pass
else:
    real = archives.tool
    archives.tool = lambda name: None if name == "7z" else real(name)
    open(os.path.join(W, "fake.7z"), "wb").write(b"7z\xbc\xaf\x27\x1c")
    lv.refresh()
    pump(0.4)
    lv.select_path(os.path.join(W, "fake.7z"))
    pump(0.1)
    win.show_context_menu(lv, lv.mapToGlobal(lv.rect().center()), True)
    check("without 7-Zip: Extract entries say what to install", any("sudo apt install 7zip" in t for t in menus[-1]))
    archives.tool = real

print(f"\n{sum(results)}/{len(results)} archive/terminal UI checks passed")
sys.exit(0 if all(results) else 1)
