"""Entry point.

  gsexplorer-plus [FOLDER | FILE | file:///URI]
  gsexplorer-plus --sync PROFILE [--dry-run] [--quiet]     headless sync/backup (no display needed)
  gsexplorer-plus --list-profiles
  gsexplorer-plus --version
"""
from __future__ import annotations

import os
import signal
import sys
from urllib.parse import unquote_to_bytes, urlparse

USAGE = """Usage:
  gsexplorer-plus [FOLDER | FILE | file:///URI]
  gsexplorer-plus --sync PROFILE [--dry-run] [--quiet]   run a saved Sync & Backup profile headless
  gsexplorer-plus --list-profiles
  gsexplorer-plus --version
Exit codes for --sync: 0 success, 1 warnings, 2 errors, 3 cancelled."""


def _cli(argv: list[str]) -> int | None:
    """Handle headless commands before any Qt import. Returns an exit code, or None for GUI."""
    args = argv[1:]
    if "--selftest" not in args and "--version" not in args and "-v" not in args:
        from . import migrate
        for msg in migrate.run():           # one-time GExplorer -> GSExplorer+ migration
            print(f"gsexplorer-plus: {msg}", file=sys.stderr)
    if "--selftest" in args:
        return _selftest(args[args.index("--selftest") + 1] if len(args) > args.index("--selftest") + 1 else "")
    if "--list-profiles" in args:
        from .syncengine import list_profiles
        print("\n".join(list_profiles()))
        return 0
    if "--sync" in args:
        i = args.index("--sync")
        if i + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        from .syncengine import run_profile_cli
        return run_profile_cli(args[i + 1], dry_run="--dry-run" in args, quiet="--quiet" in args)
    return None


def _selftest(media: str) -> int:
    """Build-time check of the packaged app: import every module and, if given, decode a media
    file. Catches modules or plugins the bundler left out (source-tree tests can't see that)."""
    import importlib
    import time
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    mods = ["PySide6.QtSvg", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNetwork"]
    mods += [f"gsexplorer.{m}" for m in ("mainwindow", "syncui", "syncengine", "schedule", "preview", "thumbs",
                                          "connect", "connectui", "mediaplayer", "dialogs", "dupes", "dupesui", "busygear", "archives", "search", "searchui",
                                          "syncprogress", "ringprogress", "migrate", "about")]
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            print(f"SELFTEST FAIL import {m}: {e!r}")
            return 1
    if media:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer
        p = QMediaPlayer()
        p.setSource(QUrl.fromLocalFile(media))
        t = time.time()
        while p.duration() <= 0 and time.time() - t < 15:
            app.processEvents()
            time.sleep(0.05)
        if p.duration() <= 0:
            print(f"SELFTEST FAIL media: {p.errorString() or 'no duration'}")
            return 1
        print(f"selftest media duration {p.duration()} ms")
    print("SELFTEST OK")
    return 0


def _arg_path(argv: list[str]) -> str | None:
    for a in argv[1:]:
        if a in ("--version", "-v"):
            from . import VERSION
            print(f"GSExplorer+ {VERSION}")
            sys.exit(0)
        if a in ("--help", "-h"):
            print(USAGE)
            sys.exit(0)
        if a.startswith("-"):
            continue
        if a.startswith("file://"):
            return os.fsdecode(unquote_to_bytes(urlparse(a).path))
        return os.path.abspath(os.path.expanduser(a))
    return None


def main() -> int:
    code = _cli(sys.argv)
    if code is not None:
        return code
    start = _arg_path(sys.argv)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from . import APP_ID, APP_NAME, RDNN_ID, VERSION, themes
    from .config import Config
    from .icons import setup_icon_theme

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setApplicationName(APP_ID)
    # (no setApplicationDisplayName: Qt would append " — GSExplorer+" to every window title)
    QApplication.setApplicationVersion(VERSION)
    QGuiApplication.setDesktopFileName(RDNN_ID)  # Wayland app-id -> matches io.github.gstvslzr.GSExplorer.desktop
    app = QApplication(sys.argv[:1])
    setup_icon_theme()

    cfg = Config()
    themes.ensure_user_themes()
    themes.apply(app, cfg["theme"], themes.load_themes())

    from .mainwindow import MainWindow
    win = MainWindow(cfg, start)
    win.show()

    # Save the session on Ctrl+C / SIGTERM (logout, kill) too, not only on window close.
    def _on_signal(*_):
        win.jobs.cancel_all()
        win.save_session()
        app.quit()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    app.aboutToQuit.connect(win.save_session)
    tick = QTimer()          # lets the Python interpreter run signal handlers
    tick.start(400)
    tick.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
