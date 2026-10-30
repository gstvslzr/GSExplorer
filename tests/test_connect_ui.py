# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Connect UI end-to-end against live servers (run inside a D-Bus session with gvfsd).

GX_LIVE=1 QT_QPA_PLATFORM=offscreen python tests/test_connect_ui.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
H = tempfile.mkdtemp(prefix="gx-cui-")
os.environ.update(XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share", XDG_CACHE_HOME=f"{H}/.cache")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

app = QApplication([])
from gsexplorer import connect as cn, themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.connectui import ConnectDialog  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

PW = os.environ.get("GX_PW", "Secr3t!pw")
asked = []
QInputDialog.getText = staticmethod(lambda *a, **k: (asked.append(a[2]) or (PW, True)))
QMessageBox.warning = staticmethod(lambda *a, **k: print("   warning:", a[2]))
QMessageBox.information = staticmethod(lambda *a, **k: 0)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.02)


def wait(cond, timeout=30):
    t = time.time()
    while not cond():
        pump(0.05)
        if time.time() - t > timeout:
            return False
    return True


cfg = Config()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, None)
win.show()
pump(0.3)

# dialog behaviour
d = ConnectDialog(win, preset="smb")
check("SMB shows share+domain, hides SSH key", not d.share.isHidden() and not d.domain.isHidden() and d.key.isHidden())
d.host.setText("nas.local")
check("Connect disabled until share entered", not d.ok_btn.isEnabled())
d.share.setText("Docs")
d.user.setText("gus")
check("address preview", "smb://gus@nas.local/Docs/" in d.uri.text())
d.proto.setCurrentIndex(d.proto.findData("ftp"))
check("FTP shows clear-text warning", "clear text" in d.warn.text())
d.proto.setCurrentIndex(d.proto.findData("ssh"))
check("SSH shows command preview", "ssh gus@nas.local" in d.uri.text() and not d.key.isHidden())
d.proto.setCurrentIndex(d.proto.findData("rdp"))
check("RDP hides folder field", d.path.isHidden())
d.deleteLater()

if os.environ.get("GX_LIVE") == "1":
    c = cn.Connection(name="Test NAS", protocol="smb", host="127.0.0.1", share="gxshare", user="gxtest",
                      domain="WORKGROUP", remember_password=True)
    win.connector.save(c)
    pump(0.3)
    names = [win.sidebar.topLevelItem(i).text(0) for i in range(win.sidebar.topLevelItemCount())]
    net = next(win.sidebar.topLevelItem(i) for i in range(win.sidebar.topLevelItemCount())
               if win.sidebar.topLevelItem(i).text(0) == "Network")
    check("saved connection in sidebar Network", any(net.child(j).text(0) == "Test NAS" for j in range(net.childCount())))
    tabs_before = win.left.tabs.count()
    win.connector.connect(c)
    ok = wait(lambda: win.left.tabs.count() > tabs_before or win.right.tabs.count() > 1, 40)
    check("password asked once, share opened in new tab", ok and len(asked) == 1)
    v = win.active_view()
    check("remote file listed", wait(lambda: "remote.txt" in [e.name for e in v.model.rows]))
    check("password remembered in keyring", cn.keyring_lookup(c) == PW)
    mounts = [m for m in cn.list_gio_mounts() if m.mounted]
    check("mount appears under Network", wait(lambda: any(
        net2.child(j).text(0).startswith("gxshare on 127.0.0.1")
        for net2 in [next(win.sidebar.topLevelItem(i) for i in range(win.sidebar.topLevelItemCount())
                          if win.sidebar.topLevelItem(i).text(0) == "Network")]
        for j in range(net2.childCount())), 10))
    # second connect uses keyring silently
    asked.clear()
    win.connector.disconnect(mounts[0])
    check("disconnect moved tab away from share", not win.active_view().path.startswith(cn.gvfs_dir()))
    total = lambda: win.left.tabs.count() + win.right.tabs.count()
    n_tabs = total()
    win.connector.connect(c)
    ok = wait(lambda: total() > n_tabs, 40)
    print("   reconnect: tab opened", ok, "prompts", asked)
    check("reconnect uses keyring (no prompt)", ok and not asked)
    win.connector.disconnect_all()
    win.connector.delete(c)
    check("delete clears keyring", cn.keyring_lookup(c) is None)

print(f"\n{sum(results)}/{len(results)} connect UI checks passed")
sys.exit(0 if all(results) else 1)
