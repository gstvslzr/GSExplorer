"""Launching: default app (xdg-open), desktop-entry apps (Open With), terminal, editor.

All launches use argument lists (never a shell) unless the user typed a shell
command into the command line. Child processes are detached into their own
session so closing GSExplorer+ doesn't kill them.
"""
from __future__ import annotations

import configparser
import os
import shlex
import shutil
import subprocess
import sys

from PySide6.QtCore import QMimeDatabase


def _spawn(args: list[str], cwd: str | None = None, extra_env: dict | None = None) -> None:
    # Strip PyInstaller's bundled-library path so launched apps use system libs.
    env = dict(os.environ)
    env.update(extra_env or {})
    if getattr(sys, "frozen", False):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    for k in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(k, None)
    subprocess.Popen(args, cwd=cwd if cwd and os.path.isdir(cwd) else None, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def open_default(path: str) -> None:
    exe = shutil.which("xdg-open")
    if not exe:
        raise OSError("xdg-open not found (install xdg-utils)")
    _spawn([exe, path], cwd=os.path.dirname(path))


# ----------------------------------------------------------------------- desktop entries

class DesktopApp:
    def __init__(self, file_id: str, name: str, exec_: str, icon: str, mimes: list[str], terminal: bool):
        self.id, self.name, self.exec, self.icon, self.mimes, self.terminal = (
            file_id, name, exec_, icon, mimes, terminal)

    def command(self, paths: list[str]) -> list[str]:
        try:
            tokens = shlex.split(self.exec)
        except ValueError:
            tokens = self.exec.split()
        out: list[str] = []
        used = False
        for t in tokens:
            if t in ("%f", "%u"):
                out.extend(paths[:1]); used = True
            elif t in ("%F", "%U"):
                out.extend(paths); used = True
            elif t in ("%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"):
                continue
            else:
                out.append(t.replace("%%", "%"))
        if not used:
            out.extend(paths)
        return out


def _app_dirs() -> list[str]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = [home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    out = [os.path.join(d, "applications") for d in dirs]
    out += ["/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications")]
    return [d for d in dict.fromkeys(out) if os.path.isdir(d)]


def list_apps() -> list[DesktopApp]:
    seen: dict[str, DesktopApp] = {}
    for d in _app_dirs():
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".desktop"):
                    continue
                file_id = os.path.relpath(os.path.join(root, fn), d).replace("/", "-")
                if file_id in seen:
                    continue  # earlier dirs take precedence (user overrides)
                cp = configparser.RawConfigParser(strict=False, interpolation=None)
                cp.optionxform = str
                try:
                    cp.read(os.path.join(root, fn), encoding="utf-8")
                    e = cp["Desktop Entry"]
                except (configparser.Error, KeyError, UnicodeDecodeError, OSError):
                    continue
                if e.get("Type", "Application") != "Application" or not e.get("Exec"):
                    seen[file_id] = None  # type: ignore[assignment]
                    continue
                if e.get("NoDisplay", "false").lower() == "true" or e.get("Hidden", "false").lower() == "true":
                    seen[file_id] = None  # type: ignore[assignment]
                    continue
                mimes = [m for m in e.get("MimeType", "").split(";") if m]
                seen[file_id] = DesktopApp(file_id, e.get("Name", fn[:-8]), e["Exec"], e.get("Icon", ""),
                                           mimes, e.get("Terminal", "false").lower() == "true")
    return sorted((a for a in seen.values() if a), key=lambda a: a.name.casefold())


def apps_for_mime(mime: str, apps: list[DesktopApp]) -> list[DesktopApp]:
    db = QMimeDatabase()
    mt = db.mimeTypeForName(mime)
    wanted = {mime} | (set(mt.allAncestors()) if mt.isValid() else set())
    exact = [a for a in apps if mime in a.mimes]
    related = [a for a in apps if a not in exact and wanted.intersection(a.mimes)]
    return exact + related


def launch_app(app: DesktopApp, paths: list[str], terminal_cmd: str = "") -> None:
    cmd = app.command(paths)
    if app.terminal:
        run_in_terminal(shlex.join(cmd), os.path.dirname(paths[0]) if paths else None, terminal_cmd, keep_open=False)
    else:
        _spawn(cmd, cwd=os.path.dirname(paths[0]) if paths else None)


def launch_command_line(cmdline: str, paths: list[str]) -> None:
    """User-entered 'Open with' command; %f/%F are replaced, else paths are appended."""
    app = DesktopApp("custom", cmdline, cmdline, "", [], False)
    _spawn(app.command(paths), cwd=os.path.dirname(paths[0]) if paths else None)


# ----------------------------------------------------------------------- terminal

_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("ptyxis", ["--"]),
    ("kgx", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["-x"]),
    ("tilix", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("foot", []),
    ("xterm", ["-e"]),
]


# Option that makes each terminal start in a given folder. Every entry was checked against that
# terminal's own --help on Ubuntu 24.04 packages (gnome-terminal 3.52, kgx 46, mate-terminal 1.26,
# xfce4-terminal 1.1.3, tilix 1.9.6, terminator 2.1.3, konsole 23.08, kitty 0.32, alacritty 0.13,
# foot 1.16). xterm has no such option and uses the process's current folder (verified).
# Anything else (ptyxis, wezterm, …) is probed at run time: its --help is read and a known option is
# used only if listed; otherwise the process's current folder is relied on.
_WORKDIR = {
    "gnome-terminal": ["--working-directory={d}"],
    "kgx": ["--working-directory={d}"],
    "mate-terminal": ["--working-directory={d}"],
    "xfce4-terminal": ["--working-directory={d}"],
    "tilix": ["--working-directory={d}"],
    "terminator": ["--working-directory", "{d}"],
    "konsole": ["--workdir", "{d}"],
    "kitty": ["--directory={d}"],
    "alacritty": ["--working-directory", "{d}"],
    "foot": ["--working-directory={d}"],
    "xterm": [],
}
_probe_cache: dict[str, list[str]] = {}


def _terminal_name(exe: str) -> str:
    """Real terminal behind a launcher name (x-terminal-emulator is a Debian alternatives link;
    on Ubuntu it points to gnome-terminal.wrapper)."""
    path = shutil.which(exe) or exe
    real = os.path.basename(os.path.realpath(path))
    if real.endswith(".wrapper"):
        real = real[: -len(".wrapper")]
    return real if real != "x-terminal-emulator" else os.path.basename(exe)


def _probe_workdir(exe: str) -> list[str]:
    if exe in _probe_cache:
        return _probe_cache[exe]
    flag: list[str] = []
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, errors="replace", timeout=3,
                           stdin=subprocess.DEVNULL)
        h = r.stdout + r.stderr
        for opt in ("--working-directory", "--workdir", "--cwd", "--directory"):
            if opt in h:
                flag = [opt + "={d}"] if opt != "--cwd" else [opt, "{d}"]
                break
    except (OSError, subprocess.SubprocessError):
        pass
    _probe_cache[exe] = flag
    return flag


def terminal_command(directory: str, custom: str = "") -> list[str]:
    """Command line that opens a terminal *in directory* (raises OSError if none is installed).

    Custom command (Preferences): '{dir}' is replaced by the folder, e.g. "konsole --workdir {dir}";
    without it, the folder option of a known terminal is added automatically."""
    if custom.strip():
        parts = shlex.split(custom)
        if parts and shutil.which(parts[0]):
            if any("{dir}" in p for p in parts):
                return [p.replace("{dir}", directory) for p in parts]
            return parts + _workdir_args(parts[0], directory)
    for exe, _flag in _TERMINALS:
        if shutil.which(exe):
            real = _terminal_name(exe)
            if exe == "x-terminal-emulator" and real in _WORKDIR and shutil.which(real):
                exe = real                     # call the real program, so its own option is honoured
            return [shutil.which(exe) or exe] + _workdir_args(exe, directory)
    raise OSError("No terminal emulator found. Set one in Preferences.")


def _workdir_args(exe: str, directory: str) -> list[str]:
    name = _terminal_name(exe)
    flag = _WORKDIR.get(name)
    if flag is None:
        flag = _probe_workdir(shutil.which(exe) or exe)
    return [a.replace("{d}", directory) for a in flag]


def _find_terminal(custom: str) -> tuple[list[str], list[str]] | None:
    if custom.strip():
        parts = shlex.split(custom)
        if parts and shutil.which(parts[0]):
            return parts, ["-e"]
    for exe, exec_flag in _TERMINALS:
        if shutil.which(exe):
            return [exe], exec_flag
    return None


def open_terminal(directory: str, custom: str = "") -> None:
    """Open a terminal whose shell starts in *directory*: the folder is passed three ways — the
    terminal's own option, the process's current folder, and $PWD."""
    if not os.path.isdir(directory):
        raise OSError(f"Folder not found: {directory}")
    _spawn(terminal_command(directory, custom), cwd=directory, extra_env={"PWD": directory})


def run_in_terminal(command: str, directory: str | None, custom: str = "", keep_open: bool = True) -> None:
    t = _find_terminal(custom)
    if t is None:
        raise OSError("No terminal emulator found. Set one in Preferences.")
    base, flag = t
    shell = os.environ.get("SHELL") or "/bin/sh"
    script = command + (f'; echo; echo "[exit $?] press Enter"; read _' if keep_open else "")
    args = list(base) + (_workdir_args(base[0], directory) if directory else [])
    _spawn(args + flag + [shell, "-c", script], cwd=directory,
           extra_env={"PWD": directory} if directory else None)


def run_detached_shell(command: str, directory: str) -> None:
    _spawn(["/bin/sh", "-c", command], cwd=directory)


def open_editor(path: str, custom: str = "") -> None:
    if custom.strip():
        parts = shlex.split(custom)
        _spawn(parts + [path], cwd=os.path.dirname(path))
    else:
        open_default(path)


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def pkexec(args: list[str]) -> tuple[bool, str]:
    exe = shutil.which("pkexec")
    if not exe:
        return False, "pkexec is not installed"
    p = subprocess.run([exe] + args, capture_output=True, text=True)
    if p.returncode == 0:
        return True, ""
    if p.returncode in (126, 127):
        return False, "Authorization was cancelled or denied."
    return False, (p.stderr or p.stdout or f"exit code {p.returncode}").strip()
