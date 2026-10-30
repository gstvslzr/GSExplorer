"""GExplorer Player: built-in video/audio player window (Qt Multimedia + bundled FFmpeg).

Features: playlist from the file's folder, previous/next, auto-advance, repeat (off/all/one),
shuffle, seek bar with time, volume + mute, speed 0.5x-2x, audio/subtitle track selection,
fullscreen with auto-hiding controls, cover art and tags for music, resume where you left off
(stored locally), drag and drop to add files, "Open in VLC" hand-off.

Keys: Space/K play-pause · ←/→ 5 s · J/L 10 s · ↑/↓ volume · M mute · F/F11/double-click
fullscreen · Esc leave fullscreen · N/PgDown next · P/PgUp previous · [ ] speed · Home restart
· Ctrl+L playlist · Ctrl+O open file · S stop.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import time

from PySide6.QtCore import QMimeDatabase, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QSizePolicy, QSlider, QSplitter,
                               QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, fsutil, system

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
RESUME_MIN_LEN_MS = 3 * 60 * 1000          # only remember positions in media longer than 3 min
RESUME_MIN_POS_MS = 30 * 1000
_num_re = re.compile(r"(\d+)")
_db = QMimeDatabase()


def media_kind(path: str) -> str:
    """'video', 'audio' or '' — by file name, never by reading the file (safe on FIFOs/network)."""
    m = _db.mimeTypeForFile(path, QMimeDatabase.MatchExtension).name()
    if m.startswith("video/") or m in ("application/x-matroska", "application/vnd.rn-realmedia",
                                         "application/x-shockwave-flash"):
        return "video"
    if m.startswith("audio/") or m in ("application/ogg", "application/x-ogg"):
        return "audio"
    return ""


def _nat(s: str):
    return [int(t) if t.isdigit() else t for t in _num_re.split(s.casefold())]


def folder_playlist(path: str) -> list[str]:
    """All media files in the folder of *path*, natural-sorted (Track 2 before Track 10)."""
    d = os.path.dirname(path)
    try:
        names = os.listdir(d)
    except OSError:
        return [path]
    out = [os.path.join(d, n) for n in names
           if not n.startswith(".") and media_kind(n) and os.path.isfile(os.path.join(d, n))]
    out.sort(key=lambda p: _nat(fsutil.display_name(os.path.basename(p))))
    if path not in out:
        out.insert(0, path)
    return out


def fmt_ms(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------- resume store (local)

def _positions_path() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "gexplorer", "media-positions.json")


def load_positions() -> dict:
    try:
        with open(_positions_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_positions(d: dict) -> None:
    if len(d) > 500:                                       # keep the 500 most recent
        d = dict(sorted(d.items(), key=lambda kv: kv[1].get("t", 0))[-500:])
    p = _positions_path()
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".pos.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=True)
        os.replace(tmp, p)
    except OSError:
        pass


def _key(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{os.fsencode(path).hex()}:{st.st_size}"   # renamed/replaced files don't inherit
    except OSError:
        return os.fsencode(path).hex()


# --------------------------------------------------------------------------- widgets

class _Art(QLabel):
    """Cover art / placeholder for audio, scaled to fit."""

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(120, 120)
        self._pm: QPixmap | None = None
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("#000000"))
        pal.setColor(QPalette.WindowText, QColor("#d0d4dc"))
        self.setPalette(pal)

    def set_art(self, img: QImage | None, title: str = "") -> None:
        self._pm = QPixmap.fromImage(img) if img is not None and not img.isNull() else None
        self._title = title
        self._rescale()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._rescale()

    def _rescale(self) -> None:
        if self._pm is not None:
            s = self.size() * 0.8
            self.setPixmap(self._pm.scaled(s, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return
        f = QFont(self.font())
        f.setPointSize(max(28, min(self.height() // 5, 96)))
        self.setFont(f)
        self.setText("♪")


class _Seek(QSlider):
    """Seek bar that jumps to the clicked position (Qt's default only pages)."""
    seek = Signal(int)

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setRange(0, 0)
        self.sliderMoved.connect(self.seek.emit)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.maximum() > 0:
            v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(ev.position().x()),
                                               max(1, self.width()))
            self.setValue(v)
            self.seek.emit(v)
        super().mousePressEvent(ev)


class _VideoArea(QStackedWidget):
    double_clicked = Signal()
    mouse_moved = Signal()

    def mouseDoubleClickEvent(self, ev):
        self.double_clicked.emit()

    def mouseMoveEvent(self, ev):
        self.mouse_moved.emit()
        super().mouseMoveEvent(ev)


# --------------------------------------------------------------------------- main window

class PlayerWindow(QMainWindow):
    def __init__(self, win):
        super().__init__(None)                      # own top-level window (taskbar entry)
        self.win = win
        self.cfg = win.cfg
        self.setWindowTitle(f"Player — {APP_NAME}")
        self.setWindowIcon(win.windowIcon())
        self.resize(1100, 680)
        self.setAcceptDrops(True)
        self.playlist: list[str] = []
        self.index = -1
        self.repeat = self.cfg["player_repeat"] if self.cfg["player_repeat"] in ("off", "all", "one") else "off"
        self.shuffle = bool(self.cfg["player_shuffle"])
        self._shuffle_order: list[int] = []
        self.positions = load_positions()
        self._pending_resume = 0
        self._start_at = 0                  # one-shot start position (hand-off from the preview panel)
        self._last_saved = 0.0
        self.player = None
        self._build_ui()
        self._init_player()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2500)
        self._hide_timer.timeout.connect(self._auto_hide)
        self._osd_timer = QTimer(self)
        self._osd_timer.setSingleShot(True)
        self._osd_timer.timeout.connect(lambda: self.osd.hide())

    # ================================================================== UI
    def _btn(self, sp, tip, slot, text="") -> QToolButton:
        b = QToolButton()
        if sp is not None:
            b.setIcon(self.style().standardIcon(sp))
        if text:
            b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setIconSize(QSize(22, 22))
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        return b

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.split = QSplitter(Qt.Horizontal)
        self.area = _VideoArea()
        self.area.setMouseTracking(True)
        self.area.setMinimumSize(320, 180)
        self.area.setAutoFillBackground(True)
        pal = self.area.palette()
        pal.setColor(QPalette.Window, QColor("#000000"))
        self.area.setPalette(pal)
        self.art = _Art()
        self.area.addWidget(self.art)
        self.area.double_clicked.connect(self.toggle_fullscreen)
        self.area.mouse_moved.connect(self._show_controls)
        self.split.addWidget(self.area)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 6, 6)
        hl = QHBoxLayout()
        self.pl_title = QLabel("<b>Playlist</b>")
        hl.addWidget(self.pl_title, 1)
        hl.addWidget(self._btn(None, "Add files…", self.open_files, "+"))
        hl.addWidget(self._btn(None, "Remove selected", self._remove_selected, "−"))
        sl.addLayout(hl)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.itemActivated.connect(lambda it: self.play_index(self.list.row(it)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        sl.addWidget(self.list, 1)
        self.side = side
        self.split.addWidget(side)
        self.split.setStretchFactor(0, 1)
        self.split.setSizes([800, 280])
        root.addWidget(self.split, 1)

        # info line + controls
        self.controls = QFrame()
        self.controls.setObjectName("PlayerControls")
        cl = QVBoxLayout(self.controls)
        cl.setContentsMargins(10, 6, 10, 8)
        cl.setSpacing(4)
        top = QHBoxLayout()
        self.t_pos = QLabel("0:00")
        self.seekbar = _Seek()
        self.seekbar.seek.connect(self.seek_to)
        self.t_dur = QLabel("0:00")
        for w in (self.t_pos, self.t_dur):
            w.setMinimumWidth(52)
            w.setAlignment(Qt.AlignCenter)
        top.addWidget(self.t_pos)
        top.addWidget(self.seekbar, 1)
        top.addWidget(self.t_dur)
        cl.addLayout(top)
        row = QHBoxLayout()
        self.b_prev = self._btn(QStyle.SP_MediaSkipBackward, "Previous (P)", self.prev)
        self.b_play = self._btn(QStyle.SP_MediaPlay, "Play / pause (Space)", self.toggle)
        self.b_play.setIconSize(QSize(30, 30))
        self.b_stop = self._btn(QStyle.SP_MediaStop, "Stop (S)", self.stop)
        self.b_next = self._btn(QStyle.SP_MediaSkipForward, "Next (N)", self.next)
        for b in (self.b_prev, self.b_play, self.b_stop, self.b_next):
            row.addWidget(b)
        row.addSpacing(8)
        self.now = QLabel("")
        self.now.setMinimumWidth(120)
        self.now.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.now, 1)
        self.b_repeat = self._btn(None, "", self.cycle_repeat)
        self.b_shuffle = self._btn(None, "Shuffle", self.toggle_shuffle, "Shuffle")
        self.b_shuffle.setCheckable(True)
        self.b_shuffle.setChecked(self.shuffle)
        self.speed = QComboBox()
        for s in SPEEDS:
            self.speed.addItem(f"{s:g}×", s)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        self.speed.setToolTip("Playback speed ([ and ])")
        self.speed.setFocusPolicy(Qt.NoFocus)
        self.speed.currentIndexChanged.connect(lambda _i: self._apply_speed())
        self.b_tracks = self._btn(None, "Audio and subtitle tracks", self._tracks_menu, "Tracks")
        self.b_mute = self._btn(QStyle.SP_MediaVolume, "Mute (M)", self.toggle_mute)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(110)
        self.vol.setToolTip("Volume (↑/↓)")
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setValue(int(self.cfg["player_volume"] if self.cfg["player_volume"] is not None else 80))
        self.b_vlc = self._btn(None, "Play this file in VLC", self.open_vlc, "VLC")
        self.b_vlc.setVisible(shutil.which("vlc") is not None)
        self.b_list = self._btn(QStyle.SP_FileDialogListView, "Show/hide playlist (Ctrl+L)", self.toggle_playlist)
        self.b_full = self._btn(QStyle.SP_TitleBarMaxButton, "Fullscreen (F)", self.toggle_fullscreen)
        for w in (self.b_repeat, self.b_shuffle, self.speed, self.b_tracks, self.b_mute, self.vol, self.b_vlc,
                  self.b_list, self.b_full):
            row.addWidget(w)
        cl.addLayout(row)
        root.addWidget(self.controls)
        self.setCentralWidget(central)

        # on-screen display (seek/volume feedback), floats over the video area
        self.osd = QLabel(self.area)
        self.osd.setStyleSheet("background: rgba(0,0,0,170); color: white; padding: 6px 12px; "
                               "border-radius: 6px; font-size: 13pt;")
        self.osd.hide()
        self._update_repeat_btn()
        self.side.setVisible(bool(self.cfg["player_playlist"]))
        self._shortcuts()

    def _shortcuts(self) -> None:
        keys = [
            (["Space", "K"], self.toggle), (["Right"], lambda: self.skip(5000)), (["Left"], lambda: self.skip(-5000)),
            (["L"], lambda: self.skip(10000)), (["J"], lambda: self.skip(-10000)),
            (["Shift+Right"], lambda: self.skip(60000)), (["Shift+Left"], lambda: self.skip(-60000)),
            (["Up"], lambda: self.change_volume(5)), (["Down"], lambda: self.change_volume(-5)),
            (["M"], self.toggle_mute), (["F", "F11"], self.toggle_fullscreen), (["Escape"], self.leave_fullscreen),
            (["N", "PgDown", "Media Next"], self.next), (["P", "PgUp", "Media Previous"], self.prev),
            (["]"], lambda: self.step_speed(1)), (["["], lambda: self.step_speed(-1)),
            (["Home"], lambda: self.seek_to(0)), (["S", "Media Stop"], self.stop),
            (["Ctrl+L"], self.toggle_playlist), (["Ctrl+O"], self.open_files), (["Ctrl+W", "Ctrl+Q"], self.close),
            (["Media Play"], self.toggle),
        ]
        for seqs, fn in keys:
            a = QAction(self)
            a.setShortcuts([QKeySequence(s) for s in seqs])
            a.setShortcutContext(Qt.WindowShortcut)
            a.triggered.connect(fn)
            self.addAction(a)

    # ================================================================== player backend
    def _init_player(self) -> None:
        from .preview import _choose_audio_backend
        _choose_audio_backend()                     # avoid PipeWire hang when no daemon runs
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.QMediaPlayer = QMediaPlayer
        self.video = QVideoWidget()
        self.video.setMouseTracking(True)
        self.area.addWidget(self.video)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(self.vol.value() / 100)
        self.audio.setMuted(bool(self.cfg["player_muted"]))
        self.vol.valueChanged.connect(self._volume_changed)
        self.player.durationChanged.connect(self._duration)
        self.player.positionChanged.connect(self._position)
        self.player.playbackStateChanged.connect(self._state)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(self._error)
        self.player.hasVideoChanged.connect(self._has_video)
        self.player.metaDataChanged.connect(self._metadata)
        self.player.tracksChanged.connect(self._tracks_changed)
        self._update_mute_btn()
        self.b_tracks.setEnabled(False)
        self.video.installEventFilter(self)
        self.art.installEventFilter(self)
        self.art.setMouseTracking(True)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == ev.Type.MouseButtonDblClick and ev.button() == Qt.LeftButton:
            self.toggle_fullscreen()
            return True
        if t == ev.Type.MouseButtonRelease and ev.button() == Qt.LeftButton and obj in (self.video, self.art):
            self.toggle()                            # click on the picture = play/pause (like VLC/YouTube)
            return True
        if t == ev.Type.MouseMove:
            self._show_controls()
        if t == ev.Type.Wheel and obj in (self.video, self.art):
            self.change_volume(5 if ev.angleDelta().y() > 0 else -5)
            return True
        return super().eventFilter(obj, ev)

    # ================================================================== playlist
    def open_media(self, path: str, playlist: list[str] | None = None, autoplay: bool = True,
                   start_at: int = 0) -> None:
        """Play *path*; the playlist defaults to all media in its folder."""
        self._start_at = max(0, int(start_at))
        self.playlist = list(playlist) if playlist else folder_playlist(path)
        self._shuffle_order = []
        self._fill_list()
        idx = self.playlist.index(path) if path in self.playlist else 0
        self.play_index(idx, autoplay)
        if not getattr(self, "_placed", False):
            self._placed = True
            g = self.win.frameGeometry()
            scr = self.win.screen().availableGeometry() if self.win.screen() else None
            w, h = self.width(), self.height()
            if scr is not None:
                w, h = min(w, scr.width() - 40), min(h, scr.height() - 60)
                self.resize(w, h)
            x, y = g.center().x() - w // 2, g.center().y() - h // 2
            if scr is not None:
                x = max(scr.left() + 10, min(x, scr.right() - w - 10))
                y = max(scr.top() + 10, min(y, scr.bottom() - h - 40))
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def add_files(self, paths: list[str]) -> None:
        new = [p for p in paths if media_kind(p) and os.path.isfile(p) and p not in self.playlist]
        if not new:
            return
        start_now = not self.playlist
        self.playlist += new
        self._shuffle_order = []
        self._fill_list()
        if start_now:
            self.play_index(0)

    def _fill_list(self) -> None:
        self.list.clear()
        for p in self.playlist:
            it = QListWidgetItem(("🎬 " if media_kind(p) == "video" else "♪ ") +
                                 fsutil.display_name(os.path.basename(p)))
            it.setToolTip(fsutil.display_path(p))
            self.list.addItem(it)
        self.pl_title.setText(f"<b>Playlist</b> ({len(self.playlist)})")
        self._mark_current()

    def _mark_current(self) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            f = it.font()
            f.setBold(i == self.index)
            it.setFont(f)
        if 0 <= self.index < self.list.count():
            self.list.setCurrentRow(self.index)

    def _remove_selected(self) -> None:
        rows = sorted({self.list.row(i) for i in self.list.selectedItems()}, reverse=True)
        if not rows:
            return
        cur = self.playlist[self.index] if 0 <= self.index < len(self.playlist) else None
        for r in rows:
            del self.playlist[r]
        self._shuffle_order = []
        self.index = self.playlist.index(cur) if cur in self.playlist else -1
        if self.index < 0:
            self.stop()
        self._fill_list()

    def _list_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        if not it:
            return
        r = self.list.row(it)
        p = self.playlist[r]
        m = QMenu(self)
        m.addAction("Play", lambda: self.play_index(r))
        m.addAction("Show in GExplorer", lambda: self.win.active_view().navigate(p))
        m.addAction("Remove from playlist", self._remove_selected)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def open_files(self) -> None:
        start = os.path.dirname(self.current_path()) if self.current_path() else os.path.expanduser("~")
        files, _ = QFileDialog.getOpenFileNames(self, "Add media", start,
                                                "Media (*.mp4 *.mkv *.webm *.avi *.mov *.m4v *.mpg *.mpeg *.wmv "
                                                "*.ts *.mp3 *.flac *.ogg *.oga *.opus *.m4a *.aac *.wav *.wma);;"
                                                "All files (*)")
        self.add_files(files)

    def current_path(self) -> str:
        return self.playlist[self.index] if 0 <= self.index < len(self.playlist) else ""

    # ================================================================== transport
    def play_index(self, i: int, autoplay: bool = True) -> None:
        if not 0 <= i < len(self.playlist):
            return
        self._remember_position()
        self.index = i
        path = self.playlist[i]
        self._mark_current()
        title = fsutil.display_name(os.path.basename(path))
        self.setWindowTitle(f"{title} — Player — {APP_NAME}")
        self.now.setText(title)
        self.now.setToolTip(fsutil.display_path(path))
        self.art.set_art(None)
        self.area.setCurrentWidget(self.art if media_kind(path) == "audio" else self.video)
        self.seekbar.setRange(0, 0)
        self.t_pos.setText("0:00")
        self.t_dur.setText("0:00")
        saved = self.positions.get(_key(path))
        self._pending_resume = int(saved.get("pos", 0)) if isinstance(saved, dict) else 0
        self.player.setSource(QUrl.fromLocalFile(path))
        self._apply_speed()
        if autoplay:
            self.player.play()

    def toggle(self) -> None:
        if self.index < 0:
            if self.playlist:
                self.play_index(0)
            return
        if self.player.playbackState() == self.QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        self._remember_position()
        self.player.stop()

    def _order(self) -> list[int]:
        n = len(self.playlist)
        if not self.shuffle:
            return list(range(n))
        if len(self._shuffle_order) != n:
            self._shuffle_order = list(range(n))
            random.shuffle(self._shuffle_order)
        return self._shuffle_order

    def next(self, auto: bool = False) -> None:
        if not self.playlist:
            return
        if auto and self.repeat == "one":
            self.play_index(self.index)
            return
        order = self._order()
        pos = order.index(self.index) if self.index in order else -1
        if pos + 1 < len(order):
            self.play_index(order[pos + 1])
        elif self.repeat == "all" or not auto:
            if self.shuffle:
                self._shuffle_order = []
                order = self._order()
            self.play_index(order[0])
        else:
            self._remember_position(finished=True)
            self.player.stop()                     # end of playlist

    def prev(self) -> None:
        if not self.playlist:
            return
        now = time.monotonic()
        # like most players: the first press restarts the file, a second press within 2 s goes back
        # (position() can still report the old time right after the seek, so don't trust it alone)
        if self.player.position() > 3000 and now - getattr(self, "_restarted_at", 0) > 2:
            self._restarted_at = now
            self.seek_to(0)
            return
        self._restarted_at = 0
        order = self._order()
        pos = order.index(self.index) if self.index in order else 0
        self.play_index(order[pos - 1] if pos > 0 else order[-1])

    def seek_to(self, ms: int) -> None:
        if self.player.isSeekable() or ms == 0:
            self.player.setPosition(max(0, int(ms)))

    def skip(self, delta: int) -> None:
        if self.player.duration() <= 0:
            return
        t = max(0, min(self.player.duration() - 500, self.player.position() + delta))
        self.seek_to(t)
        self._show_osd(f"{'⏩' if delta > 0 else '⏪'} {fmt_ms(t)} / {fmt_ms(self.player.duration())}")

    def change_volume(self, d: int) -> None:
        self.vol.setValue(max(0, min(100, self.vol.value() + d)))
        self._show_osd(f"Volume {self.vol.value()}%")

    def toggle_mute(self) -> None:
        self.audio.setMuted(not self.audio.isMuted())
        self.cfg["player_muted"] = self.audio.isMuted()
        self._update_mute_btn()
        self._show_osd("Muted" if self.audio.isMuted() else f"Volume {self.vol.value()}%")

    def step_speed(self, d: int) -> None:
        i = max(0, min(len(SPEEDS) - 1, self.speed.currentIndex() + d))
        self.speed.setCurrentIndex(i)
        self._show_osd(f"Speed {SPEEDS[i]:g}×")

    def _apply_speed(self) -> None:
        if self.player is not None:
            self.player.setPlaybackRate(float(self.speed.currentData() or 1.0))

    def cycle_repeat(self) -> None:
        self.repeat = {"off": "all", "all": "one", "one": "off"}[self.repeat]
        self.cfg["player_repeat"] = self.repeat
        self._update_repeat_btn()
        self._show_osd({"off": "Repeat off", "all": "Repeat playlist", "one": "Repeat this file"}[self.repeat])

    def _update_repeat_btn(self) -> None:
        self.b_repeat.setText({"off": "Repeat: off", "all": "Repeat: all", "one": "Repeat: one"}[self.repeat])
        self.b_repeat.setToolTip("Repeat off → playlist → this file")

    def toggle_shuffle(self) -> None:
        self.shuffle = self.b_shuffle.isChecked()
        self.cfg["player_shuffle"] = self.shuffle
        self._shuffle_order = []
        self._show_osd("Shuffle on" if self.shuffle else "Shuffle off")

    def toggle_playlist(self) -> None:
        self.side.setVisible(not self.side.isVisible())
        self.cfg["player_playlist"] = self.side.isVisible()

    def open_vlc(self) -> None:
        exe = shutil.which("vlc")
        p = self.current_path()
        if exe and p:
            pos = self.player.position()
            self.player.pause()
            system._spawn([exe, f"--start-time={pos / 1000:.1f}", "--", p], cwd=os.path.dirname(p))

    # ================================================================== fullscreen
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self._was_side = self.side.isVisible()
            self.side.hide()
            self.showFullScreen()
            self.setCursor(Qt.ArrowCursor)
            self._hide_timer.start()

    def leave_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        self._hide_timer.stop()
        self.showNormal()
        self.controls.show()
        self.side.setVisible(getattr(self, "_was_side", True))
        self.unsetCursor()

    def _show_controls(self) -> None:
        if self.isFullScreen():
            self.controls.show()
            self.area.unsetCursor()
            self._hide_timer.start()

    def _auto_hide(self) -> None:
        if self.isFullScreen() and self.player.playbackState() == self.QMediaPlayer.PlayingState:
            self.controls.hide()
            self.area.setCursor(Qt.BlankCursor)

    def mouseMoveEvent(self, ev):
        self._show_controls()
        super().mouseMoveEvent(ev)

    # ================================================================== signals
    def _volume_changed(self, v: int) -> None:
        self.audio.setVolume(v / 100)
        self.cfg["player_volume"] = v
        if v and self.audio.isMuted():
            self.audio.setMuted(False)
            self.cfg["player_muted"] = False
        self._update_mute_btn()

    def _update_mute_btn(self) -> None:
        muted = self.audio.isMuted() or self.vol.value() == 0
        self.b_mute.setIcon(self.style().standardIcon(QStyle.SP_MediaVolumeMuted if muted else QStyle.SP_MediaVolume))

    def _duration(self, d: int) -> None:
        self.seekbar.setRange(0, max(0, d))
        self.t_dur.setText(fmt_ms(d))

    def _position(self, p: int) -> None:
        if not self.seekbar.isSliderDown():
            self.seekbar.setValue(p)
        self.t_pos.setText(fmt_ms(p))
        if time.monotonic() - self._last_saved > 15:          # periodic resume checkpoint
            self._last_saved = time.monotonic()
            self._remember_position(write=True)

    def _state(self, st) -> None:
        playing = st == self.QMediaPlayer.PlayingState
        self.b_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))
        if not playing and self.isFullScreen():
            self._show_controls()

    def _status(self, s) -> None:
        MP = self.QMediaPlayer
        if s in (MP.LoadedMedia, MP.BufferedMedia) and self._start_at:
            pos, self._start_at, self._pending_resume = self._start_at, 0, 0
            self.player.setPosition(pos)
        elif s in (MP.LoadedMedia, MP.BufferedMedia) and self._pending_resume:
            pos, self._pending_resume = self._pending_resume, 0
            if self.player.duration() > RESUME_MIN_LEN_MS and pos < self.player.duration() - 10000:
                self.player.setPosition(pos)
                self._show_osd(f"Resumed at {fmt_ms(pos)} — Home to restart", 3500)
        elif s == MP.EndOfMedia:
            self._remember_position(finished=True)
            self.next(auto=True)
        elif s == MP.InvalidMedia:
            self._error(None, "This file can't be played (unsupported or damaged).")

    def _error(self, _e, msg: str) -> None:
        if not msg:
            return
        name = fsutil.display_name(os.path.basename(self.current_path()))
        self.now.setText(f"⚠ {name}: {msg}")
        self._show_osd(f"Can't play: {msg}", 5000)

    def _has_video(self, v: bool) -> None:
        self.area.setCurrentWidget(self.video if v else self.art)
        if not v:
            self._update_art()

    def _update_art(self) -> None:
        """Cover art from tags, else cover.jpg/folder.jpg, else a note symbol. Decided from the
        file type too, because hasVideo can still describe the previous file when tags arrive."""
        from PySide6.QtMultimedia import QMediaMetaData
        if self.player.hasVideo() and media_kind(self.current_path()) != "audio":
            return
        md = self.player.metaData()
        img = None
        for k in (QMediaMetaData.CoverArtImage, QMediaMetaData.ThumbnailImage):
            v = md.value(k)
            if isinstance(v, QImage) and not v.isNull():
                img = v
                break
        if img is None:
            img = _folder_cover(self.current_path())
        self.art.set_art(img)

    def _metadata(self) -> None:
        from PySide6.QtMultimedia import QMediaMetaData
        md = self.player.metaData()
        title = md.stringValue(QMediaMetaData.Title)
        artist = md.stringValue(QMediaMetaData.ContributingArtist) or md.stringValue(QMediaMetaData.AlbumArtist)
        album = md.stringValue(QMediaMetaData.AlbumTitle)
        name = fsutil.display_name(os.path.basename(self.current_path()))
        parts = [p for p in (title or name, artist, album) if p]
        self.now.setText(" — ".join(parts))
        self._update_art()

    def _tracks_changed(self) -> None:
        n = len(self.player.audioTracks()) + len(self.player.subtitleTracks())
        self.b_tracks.setEnabled(len(self.player.audioTracks()) > 1 or len(self.player.subtitleTracks()) > 0)
        self.b_tracks.setToolTip("Audio and subtitle tracks" if n else "This file has no extra tracks")

    def _tracks_menu(self) -> None:
        from PySide6.QtMultimedia import QMediaMetaData
        m = QMenu(self)

        def label(md, i, kind):
            lang = md.value(QMediaMetaData.Language)
            title = md.stringValue(QMediaMetaData.Title)
            lang_s = ""
            try:
                from PySide6.QtCore import QLocale
                lang_s = QLocale.languageToString(lang) if lang is not None else ""
            except (TypeError, ValueError):
                lang_s = ""
            return " · ".join(x for x in (f"{kind} {i + 1}", title, lang_s) if x and x != "C")

        am = m.addMenu("Audio track")
        for i, md in enumerate(self.player.audioTracks()):
            a = am.addAction(label(md, i, "Track"), lambda i=i: self.player.setActiveAudioTrack(i))
            a.setCheckable(True)
            a.setChecked(i == self.player.activeAudioTrack())
        sm = m.addMenu("Subtitles")
        off = sm.addAction("Off", lambda: self.player.setActiveSubtitleTrack(-1))
        off.setCheckable(True)
        off.setChecked(self.player.activeSubtitleTrack() < 0)
        for i, md in enumerate(self.player.subtitleTracks()):
            a = sm.addAction(label(md, i, "Subtitle"), lambda i=i: self.player.setActiveSubtitleTrack(i))
            a.setCheckable(True)
            a.setChecked(i == self.player.activeSubtitleTrack())
        m.exec(self.b_tracks.mapToGlobal(self.b_tracks.rect().bottomLeft()))

    # ================================================================== resume
    def _remember_position(self, finished: bool = False, write: bool = False) -> None:
        p = self.current_path()
        if not p or self.player is None:
            return
        if not finished and self.player.playbackState() == self.QMediaPlayer.StoppedState:
            return          # stopped players report 0; never let that erase a saved resume point
        dur, pos = self.player.duration(), self.player.position()
        k = _key(p)
        if finished or dur < RESUME_MIN_LEN_MS or pos < RESUME_MIN_POS_MS or pos > dur - 15000:
            if self.positions.pop(k, None) is not None:
                save_positions(self.positions)
            return
        self.positions[k] = {"pos": pos, "t": time.time()}
        save_positions(self.positions)

    # ================================================================== misc
    def _show_osd(self, text: str, ms: int = 1500) -> None:
        self.osd.setText(text)
        self.osd.adjustSize()
        self.osd.move(16, 16)
        self.osd.raise_()
        self.osd.show()
        self._osd_timer.start(ms)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        files = []
        for p in paths:
            if os.path.isdir(p):
                files += [x for x in folder_playlist(os.path.join(p, "_")) if x != os.path.join(p, "_")]
            else:
                files.append(p)
        self.add_files(files)

    def closeEvent(self, ev):
        self._remember_position()
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.cfg.save()
        super().closeEvent(ev)


def _folder_cover(path: str) -> QImage | None:
    """cover.jpg / folder.jpg next to the track (common for ripped albums)."""
    d = os.path.dirname(path)
    for n in ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "Cover.jpg", "Folder.jpg"):
        p = os.path.join(d, n)
        if os.path.isfile(p):
            img = QImage(p)
            if not img.isNull():
                return img
    return None
