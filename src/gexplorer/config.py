"""User configuration stored in $XDG_CONFIG_HOME/gexplorer/config.json (local only)."""
from __future__ import annotations

import json
import os
import tempfile

from . import APP_ID

DEFAULTS: dict = {
    "theme": "system",            # "system" or a theme name
    "dual_pane": True,
    "show_hidden": False,
    "show_sidebar": True,
    "confirm_trash": False,
    "view_mode": "details",       # details | icons
    "bookmarks": [],               # list of absolute paths
    "editor": "",                  # command for F4; empty = default app
    "terminal": "",                # command for "Open terminal here"; empty = auto
    "session": {"left": [], "right": [], "active": "left"},
    "window": {"w": 1280, "h": 800, "split": [], "sidebar_w": 190},
    "sync_last_profile": "",
    "icon_size": 96,
    "thumbnails": "local",         # never | local | all
    "preview_panel": False,
    "media_player": "builtin",     # builtin | vlc
    "preview_autoplay": False,
    "media_open": "builtin",       # builtin (GExplorer Player) | default (system app) | vlc
    "player_volume": 80,
    "player_muted": False,
    "player_repeat": "off",
    "player_shuffle": False,
    "player_playlist": True,
    "dupes_mode": "content",       # Find Duplicates: content | name_size | name
    "dupes_min_kb": 0,
    "dupes_max_mb": 0,
    "dupes_include": "",
    "dupes_exclude": "",
    "dupes_hidden": False,
    "dupes_empty": False,
    "dupes_hardlinks": False,
    "dupes_skip_trash": True,
    "dupes_audit_dir": "",
    "dupes_autocheck": True,       # tick all but the first copy of each group after a search
    "archive_format": "zip",       # last format chosen in Compress…
    "search_history": [],          # Find Files: recent name patterns
    "search_saved": {},            # Find Files: named saved searches
    "search_last": {},             # Find Files: criteria of the last search
    "sync_close_when_done": False, # Synchronize progress window: close by itself after a clean run
    "hidden_columns": [4],         # 1 Size, 2 Type, 3 Modified, 4 Permissions
}


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_ID)


def themes_dir() -> str:
    return os.path.join(config_dir(), "themes")


class Config:
    def __init__(self) -> None:
        self.path = os.path.join(config_dir(), "config.json")
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if k in self.data and isinstance(self.data[k], dict) and isinstance(v, dict):
                        self.data[k].update(v)
                    else:
                        self.data[k] = v
        except (OSError, ValueError):
            pass  # first run or corrupt file -> defaults

    def save(self) -> None:
        os.makedirs(config_dir(), mode=0o700, exist_ok=True)
        # atomic write
        fd, tmp = tempfile.mkstemp(dir=config_dir(), prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # ensure_ascii keeps surrogate-escaped (non-UTF-8) paths round-trippable
                json.dump(self.data, f, indent=2, ensure_ascii=True)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value) -> None:
        self.data[key] = value
