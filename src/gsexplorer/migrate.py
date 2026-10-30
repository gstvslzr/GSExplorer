# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-time migration from the pre-26.8.0 name "GExplorer" to "GSExplorer+".

Runs at start-up (GUI and headless --sync). It is idempotent and never deletes user data:

* ~/.config/gexplorer, ~/.local/share/gexplorer, ~/.cache/gexplorer are renamed to
  .../gsexplorer-plus, but only when the new folder does not exist yet.
* systemd user units gexplorer-sync-*.{service,timer} are rewritten as
  gsexplorer-plus-sync-*.{service,timer} (ExecStart points at the new command), the new
  timers are enabled and the old ones disabled. The old unit files are kept with a
  ".migrated" suffix so nothing is lost.
* Saved passwords need no migration: connect.keyring_lookup() also reads the old
  keyring attribute (application=gexplorer).

Returns a list of human-readable messages describing what was done.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import APP_ID, LEGACY_ID


def _home(env: str, *default: str) -> str:
    return os.environ.get(env) or os.path.join(os.path.expanduser("~"), *default)


def _move_dirs() -> list[str]:
    done = []
    for base in (_home("XDG_CONFIG_HOME", ".config"),
                 _home("XDG_DATA_HOME", ".local", "share"),
                 _home("XDG_CACHE_HOME", ".cache")):
        old, new = os.path.join(base, LEGACY_ID), os.path.join(base, APP_ID)
        if os.path.isdir(old) and not os.path.lexists(new):
            try:
                os.rename(old, new)
                done.append(f"moved {old} -> {new}")
            except OSError as e:
                done.append(f"could not move {old}: {e}")
    return done


def _systemctl(*args: str) -> int:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def _migrate_units() -> list[str]:
    from .schedule import LEGACY_UNIT_PREFIX, UNIT_PREFIX, _exec_cmd, _units_dir
    d = _units_dir()
    if not os.path.isdir(d):
        return []
    done, new_timers, old_timers = [], [], []
    for name in sorted(os.listdir(d)):
        if not name.startswith(LEGACY_UNIT_PREFIX) or not name.endswith((".service", ".timer")):
            continue
        new_name = UNIT_PREFIX + name[len(LEGACY_UNIT_PREFIX):]
        old_path, new_path = os.path.join(d, name), os.path.join(d, new_name)
        try:
            with open(old_path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if not os.path.exists(new_path):
            text = text.replace("GExplorer", "GSExplorer+")
            m = re.search(r'^ExecStart=.*--sync "([^"]+)"', text, re.M)
            if m:
                text = re.sub(r"^ExecStart=.*$", "ExecStart=" + _exec_cmd(m.group(1)), text, count=1, flags=re.M)
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(text)
        if name.endswith(".timer"):
            old_timers.append(name)
            new_timers.append(new_name)
        try:
            os.replace(old_path, old_path + ".migrated")
        except OSError:
            pass
        done.append(f"systemd unit {name} -> {new_name}")
    if done and shutil.which("systemctl"):
        for t in old_timers:
            _systemctl("disable", "--now", t)
        _systemctl("daemon-reload")
        for t in new_timers:
            _systemctl("enable", "--now", t)
    return done


def run() -> list[str]:
    msgs = _move_dirs()
    try:
        msgs += _migrate_units()
    except Exception as e:  # noqa: BLE001 — migration must never stop the app from starting
        msgs.append(f"schedule migration skipped: {e!r}")
    return msgs
