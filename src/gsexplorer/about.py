#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""About dialog: version, license, third-party notices, LGPL source offer, privacy, system info.

Third-party data comes from share/licenses/licenses.json, written at build time by
packaging/collect_licenses.py, so the notices always match the shipped bundle.
Self-test: QT_QPA_PLATFORM=offscreen python3 -m gsexplorer.about --selftest OUT_DIR
"""
from __future__ import annotations

import html
import json
import os
import platform
import sys
from pathlib import Path

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import APP_NAME, AUTHOR, COPYRIGHT, HOMEPAGE, ISSUES, LICENSE_NAME, LICENSE_SPDX, RDNN_ID, VERSION

APP_INFO = {
    "name": APP_NAME,
    "app_id": RDNN_ID,
    "version": VERSION,
    "tagline": "Dual-pane, keyboard-first file manager for Linux",
    "author": AUTHOR,
    "copyright": COPYRIGHT,
    "license_spdx": LICENSE_SPDX,
    "license_name": LICENSE_NAME,
    "homepage": HOMEPAGE,
    "issues": ISSUES,
    "security_contact": HOMEPAGE + "/security/advisories/new (private report)",
    "source_offer_url": HOMEPAGE + "/releases/tag/v" + VERSION,
    "source_offer_contact": "Open an issue at " + ISSUES + " or a private advisory as above",
    "ai_statement": (
        "Most of GSExplorer+'s source code, tests and documentation were generated with the "
        "assistance of an AI model (Anthropic Claude) and reviewed and tested by the author."
    ),
}

# Optional helper programs GSExplorer+ runs as separate processes (not shipped).
OPTIONAL_TOOLS = [
    ("7-Zip (7zip)", "7z/RAR/ISO archives"),
    ("zstd", ".zst archives"),
    ("poppler-utils", "PDF text search and thumbnails"),
    ("plocate", "system file index for Find Files"),
    ("rclone", "cloud remotes"),
    ("Remmina / FreeRDP / TigerVNC", "remote desktop"),
    ("VLC", "alternative media player"),
    ("bubblewrap", "sandboxed thumbnailers"),
    ("GVFS", "network mounts (SMB, SFTP, FTP, WebDAV, NFS)"),
]


def _install_root() -> Path:
    """Directory that holds share/ — the bundle root when frozen, else this file's folder."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        return base.parent if base.name == "_internal" else base
    env = os.environ.get("GSEXPLORER_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p and p.is_file():
            return p
    return None


def load_build_info(root: Path) -> dict:
    p = _first_existing(root / "share" / "build-info.json", root / "build-info.json")
    if not p:
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_licenses(root: Path) -> tuple[dict, Path | None]:
    p = _first_existing(root / "share" / "licenses" / "licenses.json", root / "licenses" / "licenses.json")
    if not p:
        return {"components": [], "warnings": ["licenses.json not found — run collect_licenses at build time"]}, None
    try:
        return json.loads(p.read_text(encoding="utf-8")), p.parent
    except (OSError, ValueError) as exc:
        return {"components": [], "warnings": [f"licenses.json unreadable: {exc}"]}, p.parent


def ffmpeg_version(licenses: dict) -> str:
    """Read from the build-time inventory; never loads the library at run time."""
    for c in licenses.get("components", []):
        if c.get("name", "").lower().startswith("ffmpeg"):
            return f"{c.get('version') or 'present'} ({c.get('spdx', 'license unknown')})"
    return "not bundled"


def os_release() -> str:
    try:
        data = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            data[k] = v.strip().strip('"')
        return data.get("PRETTY_NAME", "Linux")
    except OSError:
        return platform.platform()


class AboutDialog(QDialog):
    def __init__(self, parent=None, app_info: dict | None = None, root: Path | None = None):
        super().__init__(parent)
        self.info = dict(APP_INFO, **(app_info or {}))
        self.root = root or _install_root()
        build = load_build_info(self.root)
        for k in ("version", "commit", "build_date"):
            if build.get(k):
                self.info[k] = build[k]
        self.licenses, self.license_dir = load_licenses(self.root)

        self.setWindowTitle(f"About {self.info['name']}")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_about(), "About")
        self.tabs.addTab(self._tab_license(), "License")
        self.tabs.addTab(self._tab_third_party(), "Third-party software")
        self.tabs.addTab(self._tab_source_offer(), "Source code offer")
        self.tabs.addTab(self._tab_privacy(), "Privacy")
        self.tabs.addTab(self._tab_system(), "System info")
        layout.addWidget(self.tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- helpers --------------------------------------------------------- #
    @staticmethod
    def _browser(html_text: str) -> QTextBrowser:
        b = QTextBrowser()
        b.setOpenExternalLinks(True)
        b.setHtml(html_text)
        return b

    @staticmethod
    def _plain(text: str) -> QPlainTextEdit:
        t = QPlainTextEdit(text)
        t.setReadOnly(True)
        t.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = t.font()
        f.setFamily("monospace")
        t.setFont(f)
        return t

    def _a(self, url: str) -> str:
        u = html.escape(url)
        return f'<a href="{u}">{u}</a>' if url.startswith("http") else u

    # -- tabs ------------------------------------------------------------ #
    def _tab_about(self) -> QWidget:
        i = self.info
        commit = f" · commit {html.escape(i['commit'])}" if i.get("commit") else ""
        built = f" · built {html.escape(i['build_date'])}" if i.get("build_date") else ""
        body = f"""
        <h2 style="margin-bottom:0">{html.escape(i['name'])} {html.escape(i['version'])}</h2>
        <p style="margin-top:2px">{html.escape(i['tagline'])}</p>
        <p>Created by <b>{html.escape(i['author'])}</b>.<br>{html.escape(i['copyright'])}.</p>
        <p><b>License:</b> {html.escape(i['license_name'])}<br>
           <span style="color:gray">SPDX: {html.escape(i['license_spdx'])}</span></p>
        <p><b>Homepage:</b> {self._a(i['homepage'])}<br>
           <b>Report a bug:</b> {self._a(i['issues'])}<br>
           <b>Security issues:</b> {html.escape(i['security_contact'])}</p>
        <p><b>Third-party software:</b> GSExplorer+ includes Qt 6 and Qt for Python (PySide6),
           used under the GNU Lesser General Public License v3, plus other components listed
           on the <i>Third-party software</i> tab. See <i>Source code offer</i> for your rights.</p>
        <p><b>AI assistance:</b> {html.escape(i['ai_statement'])}</p>
        <p style="color:gray">Version {html.escape(i['version'])}{commit}{built}</p>
        <p style="color:gray">This program comes with ABSOLUTELY NO WARRANTY. It is free software, and you are welcome to redistribute it under the terms of the GNU GPL v3 or later.</p>
        """
        return self._browser(body)

    def _tab_license(self) -> QWidget:
        text = None
        for cand in (self.root / "LICENSE", self.root / "share" / "LICENSE",
                     Path("/usr/share/doc/gsexplorer-plus/copyright")):
            if cand.is_file():
                text = cand.read_text(encoding="utf-8", errors="replace")
                break
        if text is None:
            text = (f"{self.info['copyright']}\n\nLicense: {self.info['license_name']}\n"
                    f"SPDX: {self.info['license_spdx']}\n\n"
                    "The full license file was not found in this installation.\n"
                    f"See {self.info['homepage']}")
        return self._plain(text)

    def _tab_third_party(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        comps = sorted(self.licenses.get("components", []), key=lambda c: c.get("name", "").lower())
        intro = QLabel(f"{len(comps)} bundled components. Select one to read its license.")
        v.addWidget(intro)
        split = QSplitter(Qt.Vertical)
        table = QTableWidget(len(comps), 4)
        table.setHorizontalHeaderLabels(["Component", "Version", "License (SPDX)", "Homepage"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        for r, c in enumerate(comps):
            for col, key in enumerate(("name", "version", "spdx", "homepage")):
                table.setItem(r, col, QTableWidgetItem(str(c.get(key, ""))))
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        viewer = self._plain("")
        split.addWidget(table)
        split.addWidget(viewer)
        split.setSizes([220, 260])
        v.addWidget(split)

        tools = ", ".join(f"{n} ({p})" for n, p in OPTIONAL_TOOLS)
        note = QLabel(f"<span style='color:gray'>Used if installed, not included: {html.escape(tools)}.</span>")
        note.setWordWrap(True)
        v.addWidget(note)
        warns = self.licenses.get("warnings", [])
        if not comps and warns:
            viewer.setPlainText("\n".join(warns))

        def show(row: int) -> None:
            if row < 0 or row >= len(comps):
                return
            c = comps[row]
            parts = [f"{c.get('name')} {c.get('version', '')}".strip(),
                     f"License: {c.get('spdx', 'UNKNOWN')}", ""]
            if c.get("notes"):
                parts += [c["notes"], ""]
            files = c.get("license_files", [])
            if not files:
                parts.append("No license file was shipped for this component in this build.")
            for rel in files:
                p = (self.license_dir / rel) if self.license_dir else None
                parts.append("-" * 72)
                parts.append(rel)
                parts.append("-" * 72)
                if p and p.is_file():
                    parts.append(p.read_text(encoding="utf-8", errors="replace"))
                else:
                    parts.append("(file missing from this installation)")
            viewer.setPlainText("\n".join(parts))
            viewer.moveCursor(viewer.textCursor().MoveOperation.Start)

        table.currentCellChanged.connect(lambda r, *_: show(r))
        if comps:
            table.selectRow(0)
            show(0)
        return w

    def _tab_source_offer(self) -> QWidget:
        i = self.info
        lib_dir = html.escape(str(self.root / "_internal"))
        body = f"""
        <h3>LGPL notice and written offer</h3>
        <p>{html.escape(i['name'])} includes <b>Qt 6</b> and <b>Qt for Python (PySide6 / Shiboken6)</b>
        under the GNU Lesser General Public License version 3, and <b>FFmpeg</b> under the
        GNU Lesser General Public License version 2.1 or later. Copies of these licenses are on
        the <i>Third-party software</i> tab.</p>
        <p><b>Replacing the libraries.</b> These libraries are separate shared-library files in
        <code>{lib_dir}</code>. You may replace them with compatible versions you build or modify
        yourself; GSExplorer+ does not verify their checksums.</p>
        <p><b>Source code.</b> GSExplorer+ itself is free software under the GPL-3.0-or-later; its
        source is at {self._a(i['homepage'])}. The complete corresponding source code for the exact
        versions of Qt, PySide6 and FFmpeg shipped in this build is attached to the release page<br>
        {self._a(i['source_offer_url'])}</p>
        <p>For at least three years after this release you may also request a copy on physical
        media, for no more than the cost of shipping, from:<br>{html.escape(i['source_offer_contact'])}</p>
        """
        return self._browser(body)

    def _tab_privacy(self) -> QWidget:
        body = """
        <h3>Privacy</h3>
        <ul>
          <li><b>No telemetry, no analytics, no automatic updates.</b></li>
          <li>The network is used only when you use <i>Connect</i> or open a network location.</li>
          <li>Settings: <code>~/.config/gsexplorer-plus/</code>. Data and logs: <code>~/.local/share/gsexplorer-plus/</code>.
              Cache: <code>~/.cache/gsexplorer-plus/</code> and the shared thumbnail cache <code>~/.cache/thumbnails/</code>.</li>
          <li>Saved passwords are stored only in the system keyring (Secret Service), never in GSExplorer+'s files.</li>
          <li>Media resume positions: <code>~/.local/share/gsexplorer-plus/media-positions.json</code> (last 500 files).</li>
          <li>Search, duplicate finding, compression and sync all run locally.</li>
        </ul>
        <p>Removing the package does not delete these folders. To remove them:<br>
        <code>rm -rf ~/.config/gsexplorer-plus ~/.cache/gsexplorer-plus ~/.local/share/gsexplorer-plus</code></p>
        """
        return self._browser(body)

    def _system_text(self) -> str:
        i = self.info
        lines = [
            f"{i['name']} {i['version']}" + (f" ({i['commit']})" if i.get("commit") else ""),
            f"App ID: {i['app_id']}",
            f"Python: {platform.python_version()}",
            f"PySide6: {PYSIDE_VERSION}",
            f"Qt: {qVersion()}",
            f"FFmpeg: {ffmpeg_version(self.licenses)}",
            f"OS: {os_release()}",
            f"Kernel: {platform.release()}",
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}",
            f"Session: {os.environ.get('XDG_SESSION_TYPE', 'unknown')}",
            f"Qt platform: {QGuiApplication.platformName()}",
            f"Frozen: {'yes' if getattr(sys, 'frozen', False) else 'no'}",
        ]
        return "\n".join(lines)

    def _tab_system(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        text = self._system_text()
        v.addWidget(self._plain(text))
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("Copy to clipboard")
        btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        row.addWidget(btn)
        v.addLayout(row)
        return w


def _selftest(out_dir: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dlg = AboutDialog()
    dlg.show()
    app.processEvents()
    ok = True
    for idx in range(dlg.tabs.count()):
        dlg.tabs.setCurrentIndex(idx)
        app.processEvents()
        name = dlg.tabs.tabText(idx).lower().replace(" ", "-")
        path = out / f"about-{idx + 1}-{name}.png"
        ok &= dlg.grab().save(str(path))
        print(f"saved {path}")
    dlg.close()
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        sys.exit(_selftest(sys.argv[2] if len(sys.argv) > 2 else "about-selftest"))
    _app = QApplication(sys.argv)
    sys.exit(AboutDialog().exec())
