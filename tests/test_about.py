# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""About dialog: licensing/compliance content and the Help menu wiring (offscreen)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
H = tempfile.mkdtemp()
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")

from PySide6.QtWidgets import QApplication, QTableWidget, QTextBrowser, QPlainTextEdit  # noqa: E402

import gsexplorer  # noqa: E402
from gsexplorer import about  # noqa: E402

app = QApplication([])
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL {name}")


# fake install root with a build-time inventory
root = tempfile.mkdtemp()
os.makedirs(f"{root}/share/licenses/pyside6-essentials")
with open(f"{root}/share/licenses/pyside6-essentials/LGPL-3", "w") as f:
    f.write("GNU LESSER GENERAL PUBLIC LICENSE Version 3")
with open(f"{root}/LICENSE", "w") as f:
    f.write("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007")
json.dump({"components": [
    {"name": "PySide6_Essentials", "version": "6.12.0", "spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
     "homepage": "https://pyside.org", "license_files": ["pyside6-essentials/LGPL-3"]},
    {"name": "FFmpeg", "version": "9.0.1", "spdx": "LGPL-2.1-or-later", "homepage": "https://ffmpeg.org",
     "license_files": []}]}, open(f"{root}/share/licenses/licenses.json", "w"))

d = about.AboutDialog(root=__import__("pathlib").Path(root))
tabs = [d.tabs.tabText(i) for i in range(d.tabs.count())]
check("six tabs in order", tabs == ["About", "License", "Third-party software", "Source code offer",
                                     "Privacy", "System info"])
first = d.tabs.widget(0).toPlainText()
check("name, version and author shown", "GSExplorer+" in first and gsexplorer.VERSION in first and "Gus Salazar" in first)
check("license with SPDX id", "GPL-3.0-or-later" in first)
check("AI-assistance statement present", "AI model" in first)
check("no unreplaced placeholders anywhere",
      not any(s in about.__dict__.get("APP_INFO").__repr__() for s in ("GITHUB_USER", "CONTACT ADDRESS", "LicenseRef")))
check("License tab shows LICENSE file", "GENERAL PUBLIC LICENSE" in d.tabs.widget(1).toPlainText())
tp = d.tabs.widget(2)
table = tp.findChild(QTableWidget)
check("third-party rows from licenses.json", table is not None and table.rowCount() == 2)
viewer = tp.findChild(QPlainTextEdit)
table.selectRow(1)
app.processEvents()
check("selecting a row shows its license text", "LESSER GENERAL PUBLIC LICENSE" in viewer.toPlainText())
offer = d.tabs.widget(3).toPlainText()
check("LGPL notice + written offer", "Lesser General Public License" in offer and "three years" in offer)
check("source offer links the release", f"v{gsexplorer.VERSION}" in offer)
check("privacy uses new folder names", "~/.config/gsexplorer-plus/" in d.tabs.widget(4).toPlainText())
sysinfo = d.tabs.widget(5).findChild(QPlainTextEdit).toPlainText()
check("system info lists versions and FFmpeg from inventory", "PySide6:" in sysinfo and "9.0.1" in sysinfo)

# missing inventory: dialog still opens and says so
d2 = about.AboutDialog(root=__import__("pathlib").Path(tempfile.mkdtemp()))
check("works without licenses.json", "licenses.json not found" in
      d2.tabs.widget(2).findChild(QPlainTextEdit).toPlainText())

# Help > About opens the new dialog
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402
win = MainWindow(Config(), H)
check("Help menu has About action", win.a_about.text().replace("&", "") == "About GSExplorer+")
from PySide6.QtCore import QTimer  # noqa: E402
seen = {}


def grab():
    w = QApplication.activeModalWidget()
    seen["cls"] = type(w).__name__ if w else None
    if w:
        w.reject()


QTimer.singleShot(200, grab)
win.about()
check("About action opens AboutDialog", seen.get("cls") == "AboutDialog")

print(f"{passed}/{passed + failed} about checks passed")
sys.exit(1 if failed else 0)
