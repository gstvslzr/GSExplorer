# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview side panel (Alt+P): images, text/code, PDF first page, office documents (text +
thumbnail), video/audio playback (Qt Multimedia, or hand off to VLC), and file details.

All file reading/decoding happens in a worker thread; the panel only shows finished results,
so selecting a 200 MB image or a slow network file never freezes the window.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from xml.etree import ElementTree as ET

from PySide6.QtCore import QMimeDatabase, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QFontDatabase, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (QFormLayout, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QSlider, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget)

from . import fsutil, system, thumbs

TEXT_LIMIT = 256 * 1024
DOC_XML_LIMIT = 8 * 1024 * 1024          # zip-bomb guard for office XML parts
TEXTY = {"application/json", "application/xml", "application/x-shellscript", "application/javascript",
         "application/x-yaml", "application/toml", "application/sql", "application/x-desktop",
         "application/x-perl", "application/x-ruby", "application/x-php", "application/x-subrip",
         "application/x-ipynb+json", "application/ld+json", "application/x-sh"}
OFFICE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.oasis.opendocument.text": "odf",
    "application/vnd.oasis.opendocument.presentation": "odf",
    "application/vnd.oasis.opendocument.spreadsheet": "odf",
}


_IMG_MIMES: set[str] | None = None


def _qt_image_mimes() -> set[str]:
    global _IMG_MIMES
    if _IMG_MIMES is None:
        _IMG_MIMES = {bytes(m.data()).decode() for m in QImageReader.supportedMimeTypes()}
    return _IMG_MIMES


# --------------------------------------------------------------------------- content extraction

def _xml_text(data: bytes, para_tags: tuple[str, ...], text_tags: tuple[str, ...]) -> str:
    out, buf = [], []
    for ev, el in ET.iterparse(__import__("io").BytesIO(data), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in text_tags and el.text:
            buf.append(el.text)
        if tag in para_tags:
            line = "".join(buf).strip()
            if line:
                out.append(line)
            buf = []
        el.clear()
    if buf:
        out.append("".join(buf))
    return "\n".join(out)


def office_text(path: str, kind: str, limit: int = 20000) -> str:
    """Plain-text excerpt of DOCX/PPTX/XLSX/ODF files (local parsing, no external tools)."""
    with zipfile.ZipFile(path) as z:
        def part(name: str) -> bytes:
            info = z.getinfo(name)
            if info.file_size > DOC_XML_LIMIT:
                raise ValueError("document part too large to preview")
            return z.read(name)

        names = z.namelist()
        if kind == "docx":
            text = _xml_text(part("word/document.xml"), ("p",), ("t", "tab"))
        elif kind == "pptx":
            slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[-1]))
            chunks = []
            for i, s in enumerate(slides, 1):
                chunks.append(f"— Slide {i} —\n" + _xml_text(part(s), ("p",), ("t",)))
                if sum(map(len, chunks)) > limit:
                    break
            text = "\n\n".join(chunks)
        elif kind == "xlsx":
            text = _xml_text(part("xl/sharedStrings.xml"), ("si",), ("t",)) if "xl/sharedStrings.xml" in names else ""
            text = "(cell text, in workbook order)\n" + text
        else:
            text = _xml_text(part("content.xml"), ("p", "h"), ("p", "h", "span", "s"))
    return text[:limit] + ("\n…" if len(text) > limit else "")


def pdf_pages(path: str) -> str:
    exe = shutil.which("pdfinfo")
    if not exe:
        return ""
    try:
        p = subprocess.run([exe, path], capture_output=True, text=True, timeout=5)
        m = re.search(r"^Pages:\s+(\d+)", p.stdout, re.M)
        return m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class _LoadSignals(QObject):
    done = Signal(int, object)             # generation, result dict


class _Loader(QRunnable):
    def __init__(self, gen: int, path: str, mime: str, kind: str, px: int, sig: _LoadSignals, tmgr):
        super().__init__()
        self.gen, self.path, self.mime, self.kind, self.px, self.sig, self.tmgr = gen, path, mime, kind, px, sig, tmgr
        self.setAutoDelete(True)

    def run(self) -> None:
        r: dict = {"kind": self.kind}
        try:
            if self.kind == "image":
                rd = QImageReader(self.path)
                rd.setAutoTransform(True)
                sz = rd.size()
                r["dims"] = f"{sz.width()} × {sz.height()} px" if sz.isValid() else ""
                if sz.isValid() and (sz.width() > self.px or sz.height() > self.px):
                    rd.setScaledSize(sz.scaled(QSize(self.px, self.px), Qt.KeepAspectRatio))
                img = rd.read()
                r["image"] = None if img.isNull() else img
                if img.isNull():
                    r["error"] = rd.errorString()
            elif self.kind == "text":
                with open(self.path, "rb") as f:
                    data = f.read(TEXT_LIMIT)
                if b"\0" in data[:4096]:
                    r["kind"], r["text"] = "info", None
                else:
                    r["text"] = data.decode("utf-8", "replace")
                    r["truncated"] = os.path.getsize(self.path) > TEXT_LIMIT
            elif self.kind == "office":
                r["text"] = office_text(self.path, OFFICE[self.mime])
                r["image"] = thumbs.thumbnail(self.path, 512, self.mime, self.tmgr.thumbnailers) \
                    if self.tmgr.enabled_for(self.path) else None
            elif self.kind == "pdf":
                r["image"] = thumbs.thumbnail(self.path, 512, self.mime, self.tmgr.thumbnailers) \
                    if self.tmgr.enabled_for(self.path) else None
                r["pages"] = pdf_pages(self.path)
            elif self.kind in ("video", "info"):
                r["image"] = thumbs.thumbnail(self.path, 512, self.mime, self.tmgr.thumbnailers) \
                    if self.tmgr.enabled_for(self.path) and self.tmgr.can_thumbnail(self.path, self.mime) else None
        except (OSError, ValueError, zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            r["error"] = str(e)
        self.sig.done.emit(self.gen, r)


# --------------------------------------------------------------------------- widgets

class _ScaledImage(QLabel):
    """QLabel that keeps its pixmap scaled to the available size."""

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(80, 80)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._pm: QPixmap | None = None

    def set_image(self, img: QImage | None) -> None:
        self._pm = QPixmap.fromImage(img) if img is not None else None
        self._rescale()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._rescale()

    def _rescale(self) -> None:
        if self._pm is None:
            self.clear()
            return
        s = self.size()
        pm = self._pm
        if pm.width() > s.width() or pm.height() > s.height():
            pm = pm.scaled(s, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(pm)


def _choose_audio_backend() -> None:
    """Qt's PipeWire audio backend blocks when no PipeWire daemon is running. Ubuntu desktops
    run PipeWire, but if its socket is missing, fall back to the PulseAudio backend, which fails
    fast (video then plays silently instead of freezing the window)."""
    if os.environ.get("QT_AUDIO_BACKEND"):
        return
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    remote = os.environ.get("PIPEWIRE_REMOTE", "pipewire-0")
    sock = remote if remote.startswith("/") else os.path.join(rt, remote)
    if not os.path.exists(sock):
        os.environ["QT_AUDIO_BACKEND"] = "pulseaudio"


class MediaView(QWidget):
    """Video/audio playback with Qt Multimedia (FFmpeg backend bundled with Qt)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.player = None
        self.path = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.poster = _ScaledImage()
        self.stack.addWidget(self.poster)
        self.video = None                  # created lazily (keeps startup fast)
        lay.addWidget(self.stack, 1)
        ctl = QHBoxLayout()
        self.play_btn = QToolButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self.toggle)
        self.pos = QSlider(Qt.Horizontal)
        self.pos.setRange(0, 0)
        self.pos.sliderMoved.connect(lambda v: self.player and self.player.setPosition(v))
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("Muted")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(70)
        self.vol.setFixedWidth(70)
        self.vol.setToolTip("Volume")
        self.vlc_btn = QPushButton("Open in VLC")
        self.vlc_btn.setToolTip("Play in VLC media player")
        self.vlc_btn.clicked.connect(self.open_vlc)
        self.vlc_btn.setVisible(shutil.which("vlc") is not None)
        self.full_btn = QPushButton("Open in Player")
        self.full_btn.setToolTip("Open in GSExplorer+ Player: playlist, fullscreen, speed, tracks")
        self.full_btn.clicked.connect(self._open_player)
        for w in (self.play_btn, self.pos, self.time, self.vol, self.full_btn, self.vlc_btn):
            ctl.addWidget(w, 1 if w is self.pos else 0)
        lay.addLayout(ctl)
        self.msg = QLabel()
        self.msg.setObjectName("Muted")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        _choose_audio_backend()
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except ImportError:
            self.msg.setText("Built-in playback isn't available in this build.")
            return False
        self.video = QVideoWidget()
        self.stack.addWidget(self.video)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.vol.value() / 100)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.durationChanged.connect(self._duration)
        self.player.positionChanged.connect(self._position)
        self.player.playbackStateChanged.connect(self._state)
        self.player.errorOccurred.connect(lambda _e, s: self.msg.setText(f"Can't play: {s}"))
        self.player.hasVideoChanged.connect(lambda v: self.stack.setCurrentWidget(self.video if v else self.poster))
        return True

    def load(self, path: str, poster: QImage | None, audio_only: bool, autoplay: bool) -> None:
        self.stop()
        self.path = path
        self.msg.setText("")
        self.poster.set_image(poster)
        self.stack.setCurrentWidget(self.poster)
        if not poster:
            self.poster.setText("♪" if audio_only else "▶")
        self.pos.setRange(0, 0)
        self.time.setText("0:00 / 0:00")
        if autoplay:
            self.toggle()

    def toggle(self) -> None:
        if self.win.cfg["media_player"] == "vlc" and shutil.which("vlc"):
            return self.open_vlc()
        if not self.path or not self._ensure_player():
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.source().toLocalFile() != self.path:
            self.player.setSource(QUrl.fromLocalFile(self.path))
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())

    def _open_player(self) -> None:
        if self.path:
            pos = self.player.position() if self.player is not None else 0
            self.stop()
            self.win.play_media(self.path, start_at=pos)

    def open_vlc(self) -> None:
        exe = shutil.which("vlc")
        if exe and self.path:
            self.stop()
            system._spawn([exe, "--", self.path], cwd=os.path.dirname(self.path))

    @staticmethod
    def _fmt(ms: int) -> str:
        s = max(0, ms // 1000)
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _duration(self, d: int) -> None:
        self.pos.setRange(0, d)
        self.time.setText(f"0:00 / {self._fmt(d)}")
        self.win.preview.set_detail("Duration", self._fmt(d))

    def _position(self, p: int) -> None:
        if not self.pos.isSliderDown():
            self.pos.setValue(p)
        self.time.setText(f"{self._fmt(p)} / {self._fmt(self.pos.maximum())}")

    def _state(self, st) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        playing = st == QMediaPlayer.PlayingState
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))


class PreviewPanel(QFrame):
    def __init__(self, win, closable: bool = True):
        super().__init__()
        self.win = win
        self.setObjectName("PaneFrame")
        self.setMinimumWidth(260)
        self.db = QMimeDatabase()
        self._gen = 0
        self._path = ""
        self._sig = _LoadSignals()
        self._sig.done.connect(self._loaded)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._refresh_now)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        head = QHBoxLayout()
        self.icon = QLabel()
        self.icon.setFixedSize(32, 32)
        self.name = QLabel()
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        close = QToolButton()
        close.setText("✕")
        close.setAutoRaise(True)
        close.setToolTip("Close preview (Alt+P)")
        close.clicked.connect(lambda: self.win.a_preview.trigger())
        head.addWidget(self.icon)
        head.addWidget(self.name, 1)
        head.addWidget(close)
        close.setVisible(closable)
        lay.addLayout(head)

        self.stack = QStackedWidget()
        self.image = _ScaledImage()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.doc = QWidget()
        dl = QVBoxLayout(self.doc)
        dl.setContentsMargins(0, 0, 0, 0)
        self.doc_img = _ScaledImage()
        self.doc_img.setMinimumHeight(160)
        self.doc_text = QPlainTextEdit()
        self.doc_text.setReadOnly(True)
        dl.addWidget(self.doc_img, 2)
        dl.addWidget(self.doc_text, 3)
        self.media = MediaView(win)
        self.big_icon = QLabel()
        self.big_icon.setAlignment(Qt.AlignCenter)
        for w in (self.image, self.text, self.doc, self.media, self.big_icon):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)

        self.details = QFormLayout()
        self.details.setLabelAlignment(Qt.AlignRight)
        dw = QWidget()
        dw.setLayout(self.details)
        lay.addWidget(dw)
        self._detail_rows: dict[str, QLabel] = {}
        self.clear()

    # ------------------------------------------------------------------ public
    def show_entry(self, path: str | None) -> None:
        if path == self._path:
            return
        self._path = path or ""
        self._debounce.start()

    def clear(self) -> None:
        self.media.stop()
        self.name.setText("Nothing selected")
        self.icon.clear()
        self.big_icon.clear()
        self.stack.setCurrentWidget(self.big_icon)
        self._clear_details()

    def set_detail(self, key: str, value: str) -> None:
        lbl = self._detail_rows.get(key)
        if lbl is None:
            lbl = QLabel()
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            k = QLabel(key + ":")
            k.setObjectName("Muted")
            self.details.addRow(k, lbl)
            self._detail_rows[key] = lbl
        lbl.setText(value)

    def hideEvent(self, ev):
        self.media.stop()
        super().hideEvent(ev)

    # ------------------------------------------------------------------ internals
    def _clear_details(self) -> None:
        while self.details.rowCount():
            self.details.removeRow(0)
        self._detail_rows.clear()

    def _refresh_now(self) -> None:
        self._gen += 1
        self.pool.clear()
        self.media.stop()
        path = self._path
        self._clear_details()
        if not path:
            return self.clear()
        try:
            st = os.lstat(path)
        except OSError as e:
            self.name.setText(fsutil.display_name(os.path.basename(path)))
            self.set_detail("Error", e.strerror or str(e))
            return
        from .dirmodel import make_entry
        e = make_entry(os.path.dirname(path), os.path.basename(path))
        icon = self.win.icons.for_entry(e) if e else None
        if icon:
            self.icon.setPixmap(icon.pixmap(32, 32))
            self.big_icon.setPixmap(icon.pixmap(128, 128))
        self.name.setText(fsutil.display_name(os.path.basename(path) or path))
        is_dir = stat.S_ISDIR(st.st_mode) or (e is not None and e.is_dir)
        mime = "inode/directory" if is_dir else self._mime(path, st)
        self.set_detail("Type", (self.win.icons.type_label(e) if e else "") + f"  ({mime})")
        if is_dir:
            self.set_detail("Contains", self._count(path))
        else:
            self.set_detail("Size", f"{fsutil.human_size(st.st_size)} ({st.st_size:,} bytes)")
        self.set_detail("Modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
        self.set_detail("Permissions", stat.filemode(st.st_mode))
        if stat.S_ISLNK(st.st_mode):
            try:
                self.set_detail("Link to", fsutil.display_path(os.readlink(path)))
            except OSError:
                pass
        self.set_detail("Location", fsutil.display_path(os.path.dirname(path)))
        try:
            target = os.stat(path)
        except OSError:
            target = None                  # broken link
        if is_dir or target is None or not stat.S_ISREG(target.st_mode):
            self.stack.setCurrentWidget(self.big_icon)   # never read FIFOs/devices
            return
        kind = self._kind(mime)
        if kind == "media":
            audio = mime.startswith("audio/")
            self.stack.setCurrentWidget(self.media)
            self.media.load(path, None, audio, bool(self.win.cfg["preview_autoplay"]))
            if not audio:
                self.pool.start(_Loader(self._gen, path, mime, "video", 512, self._sig, self.win.thumbs))
            return
        self.stack.setCurrentWidget(self.big_icon)
        px = max(256, min(1600, max(self.width(), self.stack.height()) * 2))
        self.pool.start(_Loader(self._gen, path, mime, kind, px, self._sig, self.win.thumbs))

    def _mime(self, path: str, st) -> str:
        if stat.S_ISREG(st.st_mode) and st.st_size > 0 and not thumbs.is_remote(path):
            return self.db.mimeTypeForFile(path).name()        # content sniffing for local files
        return self.db.mimeTypeForFile(path, QMimeDatabase.MatchExtension).name()

    def _kind(self, mime: str) -> str:
        mt = self.db.mimeTypeForName(mime)
        if mime in _qt_image_mimes():
            return "image"
        if mime.startswith("image/"):
            return "info"                  # e.g. RAW photos: system thumbnailer if one exists
        if mime.startswith(("video/", "audio/")):
            return "media"
        if mime == "application/pdf":
            return "pdf"
        if mime in OFFICE:
            return "office"
        if mime.startswith("text/") or mime in TEXTY or (mt.isValid() and mt.inherits("text/plain")):
            return "text"
        return "info"

    @staticmethod
    def _count(path: str) -> str:
        n = d = 0
        try:
            with os.scandir(path) as it:
                for de in it:
                    n += 1
                    if de.is_dir(follow_symlinks=False):
                        d += 1
                    if n >= 100_000:
                        return "100,000+ items"
        except OSError as e:
            return e.strerror or "unreadable"
        return f"{n:,} items ({d:,} folders)"

    def _loaded(self, gen: int, r: dict) -> None:
        if gen != self._gen:
            return                         # user already moved on
        kind = r.get("kind")
        if r.get("error"):
            self.set_detail("Preview", r["error"])
        if kind == "image":
            if r.get("image") is not None:
                self.image.set_image(r["image"])
                self.stack.setCurrentWidget(self.image)
            if r.get("dims"):
                self.set_detail("Dimensions", r["dims"])
        elif kind == "text" and r.get("text") is not None:
            self.text.setPlainText(r["text"] + ("\n\n… (preview truncated)" if r.get("truncated") else ""))
            self.stack.setCurrentWidget(self.text)
        elif kind == "office":
            self.doc_img.set_image(r.get("image"))
            self.doc_img.setVisible(r.get("image") is not None)
            self.doc_text.setPlainText(r.get("text") or "")
            self.stack.setCurrentWidget(self.doc)
        elif kind == "pdf":
            if r.get("image") is not None:
                self.image.set_image(r["image"])
                self.stack.setCurrentWidget(self.image)
            else:
                self.set_detail("Preview", "Install poppler-utils for PDF previews.")
            if r.get("pages"):
                self.set_detail("Pages", r["pages"])
        elif kind == "video":
            if r.get("image") is not None:
                self.media.poster.set_image(r["image"])
        elif kind == "info" and r.get("image") is not None:
            self.image.set_image(r["image"])
            self.stack.setCurrentWidget(self.image)
