"""Scheduled sync/backup runs via systemd *user* timers (no root, no cron edits).

Each profile gets ~/.config/systemd/user/gexplorer-sync-<slug>.{service,timer}.
The service runs `gexplorer --sync "<profile>" --quiet` headless (no display needed).
Note: user timers only fire while you are logged in, unless "linger" is enabled
(`loginctl enable-linger $USER`), which the schedule dialog can do for you.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

UNIT_PREFIX = "gexplorer-sync-"


def _units_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user")


def slug(profile: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", profile.lower()).strip("-")
    return s or "profile"


def unit_names(profile: str) -> tuple[str, str]:
    base = UNIT_PREFIX + slug(profile)
    return base + ".service", base + ".timer"


def available() -> bool:
    return shutil.which("systemctl") is not None


def _exec_cmd(profile: str) -> str:
    exe = shutil.which("gexplorer") or ("/usr/bin/gexplorer" if os.path.exists("/usr/bin/gexplorer") else "")
    if exe:
        prefix = exe
    else:  # running from source
        prefix = f"{sys.executable} -m gexplorer"
    # profile names are restricted to [A-Za-z0-9 _.-], so double quoting is safe for systemd
    return f'{prefix} --sync "{profile}" --quiet'


def _systemctl(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def validate_calendar(expr: str) -> tuple[bool, str]:
    """Use systemd-analyze to validate an OnCalendar expression and show the next run."""
    exe = shutil.which("systemd-analyze")
    if not exe:
        return True, ""
    try:
        p = subprocess.run([exe, "calendar", expr], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else "invalid"
    m = re.search(r"Next elapse:\s*(.+)", p.stdout)
    return True, (m.group(1).strip() if m else "")


def install(profile: str, on_calendar: str, persistent: bool = True) -> tuple[bool, str]:
    ok, info = validate_calendar(on_calendar)
    if not ok:
        return False, f"Invalid schedule: {info}"
    svc, tmr = unit_names(profile)
    d = _units_dir()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, svc), "w", encoding="utf-8") as f:
        f.write(f"""[Unit]
Description=GExplorer sync profile "{profile}"

[Service]
Type=oneshot
ExecStart={_exec_cmd(profile)}
Nice=10
IOSchedulingClass=idle
""")
    with open(os.path.join(d, tmr), "w", encoding="utf-8") as f:
        f.write(f"""[Unit]
Description=Schedule for GExplorer sync profile "{profile}"

[Timer]
OnCalendar={on_calendar}
Persistent={"true" if persistent else "false"}
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
""")
    rc, out = _systemctl("daemon-reload")
    if rc != 0:
        return False, f"Unit files written to {d}, but systemctl --user isn't usable here: {out}"
    rc, out = _systemctl("enable", "--now", tmr)
    if rc != 0:
        return False, out
    return True, f"Scheduled. Next run: {info}" if info else "Scheduled."


def remove(profile: str) -> tuple[bool, str]:
    svc, tmr = unit_names(profile)
    _systemctl("disable", "--now", tmr)
    for u in (svc, tmr):
        try:
            os.unlink(os.path.join(_units_dir(), u))
        except FileNotFoundError:
            pass
    _systemctl("daemon-reload")
    return True, "Schedule removed."


def run_now(profile: str) -> tuple[bool, str]:
    svc, _ = unit_names(profile)
    rc, out = _systemctl("start", "--no-block", svc)
    return rc == 0, out or "Started in the background."


def status(profile: str) -> dict:
    svc, tmr = unit_names(profile)
    installed = os.path.exists(os.path.join(_units_dir(), tmr))
    info = {"installed": installed, "calendar": "", "persistent": True, "next": "", "last": "", "active": ""}
    if not installed:
        return info
    try:
        with open(os.path.join(_units_dir(), tmr), encoding="utf-8") as f:
            for line in f:
                if line.startswith("OnCalendar="):
                    info["calendar"] = line.split("=", 1)[1].strip()
                elif line.startswith("Persistent="):
                    info["persistent"] = line.split("=", 1)[1].strip() == "true"
    except OSError:
        pass
    rc, out = _systemctl("show", tmr, "-p", "ActiveState", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec")
    if rc == 0:
        for line in out.splitlines():
            k, _, v = line.partition("=")
            if k == "ActiveState":
                info["active"] = v
            elif k == "NextElapseUSecRealtime":
                info["next"] = v
            elif k == "LastTriggerUSec":
                info["last"] = v
    return info


def linger_enabled() -> bool | None:
    exe = shutil.which("loginctl")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                           capture_output=True, text=True, timeout=5)
        return "Linger=yes" in p.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def enable_linger() -> tuple[bool, str]:
    exe = shutil.which("loginctl")
    if not exe:
        return False, "loginctl not found"
    p = subprocess.run([exe, "enable-linger"], capture_output=True, text=True)
    return p.returncode == 0, (p.stderr or p.stdout).strip() or "Enabled."


PRESETS = {
    "Hourly": "hourly",
    "Daily": "*-*-* {hh}:{mm}:00",
    "Weekly": "{dow} *-*-* {hh}:{mm}:00",
    "Monthly (1st)": "*-*-01 {hh}:{mm}:00",
}
