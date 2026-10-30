# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the one-time GExplorer -> GSExplorer+ migration (no display, no systemctl needed)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

H = tempfile.mkdtemp()
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache", PATH="/nonexistent")   # PATH: no systemctl calls

from gsexplorer import migrate, schedule  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL {name}")


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# old layout
w(f"{H}/.config/gexplorer/config.json", '{"theme": "dark"}')
w(f"{H}/.config/gexplorer/sync/Photos.json", "{}")
w(f"{H}/.local/share/gexplorer/media-positions.json", "{}")
w(f"{H}/.cache/gexplorer/icons/x.svg", "<svg/>")
units = f"{H}/.config/systemd/user"
w(f"{units}/gexplorer-sync-photos.service",
  '[Unit]\nDescription=GExplorer sync profile "Photos"\n\n[Service]\nType=oneshot\n'
  'ExecStart=/usr/bin/gexplorer --sync "Photos" --quiet\n')
w(f"{units}/gexplorer-sync-photos.timer",
  '[Unit]\nDescription=Schedule for GExplorer sync profile "Photos"\n\n[Timer]\nOnCalendar=daily\n')
w(f"{units}/unrelated.timer", "[Timer]\n")

msgs = migrate.run()
check("config dir moved", os.path.isfile(f"{H}/.config/gsexplorer-plus/config.json")
      and not os.path.exists(f"{H}/.config/gexplorer"))
check("sync profiles moved", os.path.isfile(f"{H}/.config/gsexplorer-plus/sync/Photos.json"))
check("data dir moved", os.path.isfile(f"{H}/.local/share/gsexplorer-plus/media-positions.json"))
check("cache dir moved", os.path.isfile(f"{H}/.cache/gsexplorer-plus/icons/x.svg"))
svc = open(f"{units}/gsexplorer-plus-sync-photos.service").read()
check("service renamed and branded", 'GSExplorer+ sync profile "Photos"' in svc)
check("ExecStart points at the new command", '--sync "Photos" --quiet' in svc and "/usr/bin/gexplorer " not in svc)
check("timer renamed", os.path.isfile(f"{units}/gsexplorer-plus-sync-photos.timer"))
check("old units kept as .migrated", os.path.isfile(f"{units}/gexplorer-sync-photos.timer.migrated")
      and not os.path.exists(f"{units}/gexplorer-sync-photos.timer"))
check("unrelated units untouched", os.path.isfile(f"{units}/unrelated.timer"))
check("new name follows schedule.unit_names", schedule.unit_names("Photos")[1] == "gsexplorer-plus-sync-photos.timer")
check("messages describe the changes", len(msgs) >= 5)

# idempotent: second run does nothing and never overwrites
w(f"{H}/.config/gexplorer/config.json", '{"theme": "light"}')   # stale old dir reappears
msgs2 = migrate.run()
check("second run is a no-op", msgs2 == [])
check("existing new config is never overwritten",
      '"dark"' in open(f"{H}/.config/gsexplorer-plus/config.json").read())

print(f"{passed}/{passed + failed} migration checks passed")
sys.exit(1 if failed else 0)
