# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows integration for the portable Windows build.

Everything here is a no-op or unused on Linux; callers check ``IS_WIN`` first.
Only ctypes and the standard library are used (no pywin32), so the frozen .exe has no extra
dependencies. Nothing here touches the network.

Data locations (no installation, nothing written to the registry):
  * Portable mode: if a folder named ``gsexplorer-plus-data`` exists next to the .exe, every
    setting, log, profile and cache goes inside it (run from a USB stick, leave no trace).
  * Otherwise: settings in %APPDATA%\\gsexplorer-plus, data/logs/state in
    %LOCALAPPDATA%\\gsexplorer-plus, thumbnail cache in %LOCALAPPDATA%\\gsexplorer-plus\\cache.
The rest of the code finds these through the XDG_* variables set by ``setup_dirs()``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

IS_WIN = sys.platform == "win32"
PORTABLE_DIRNAME = "gsexplorer-plus-data"

# Characters and device names Windows refuses in a file name.
INVALID_NAME_CHARS = '<>:"/\\|?*'
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                  *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------- locations

def exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def portable_root() -> str | None:
    d = os.path.join(exe_dir(), PORTABLE_DIRNAME)
    return d if os.path.isdir(d) else None


def setup_dirs() -> None:
    """Point the XDG variables the app uses at Windows locations (or the portable folder)."""
    if not IS_WIN:
        return
    from . import APP_ID
    root = portable_root()
    if root:
        cfg = os.path.join(root, "config")
        data = os.path.join(root, "data")
        cache = os.path.join(root, "cache")
    else:
        appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        cfg = appdata                                    # -> %APPDATA%\gsexplorer-plus
        data = local                                     # -> %LOCALAPPDATA%\gsexplorer-plus
        cache = os.path.join(local, APP_ID, "cache")
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["XDG_DATA_HOME"] = data
    os.environ["XDG_CACHE_HOME"] = cache


def data_locations() -> dict[str, str]:
    from . import APP_ID
    return {"Settings": os.path.join(os.environ.get("XDG_CONFIG_HOME", ""), APP_ID),
            "Data and logs": os.path.join(os.environ.get("XDG_DATA_HOME", ""), APP_ID),
            "Cache": os.environ.get("XDG_CACHE_HOME", "")}


# --------------------------------------------------------------------------- names / paths

def name_error(name: str) -> str | None:
    """Windows-specific reasons a single path component is invalid (None if fine)."""
    bad = sorted({c for c in name if c in INVALID_NAME_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(c if ord(c) >= 32 else "control characters" for c in bad)
        return f"Names can't contain {shown} on Windows."
    if name.rstrip(" .") != name:
        return "Windows names can't end with a space or a dot."
    stem = name.split(".")[0].upper()
    if stem in RESERVED_NAMES:
        return f"“{stem}” is a reserved device name on Windows."
    return None


def is_hidden_stat(st) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x2)          # FILE_ATTRIBUTE_HIDDEN (also covers most system files)


def drive_type(root: str) -> int:
    """GetDriveTypeW: 2 removable, 3 fixed, 4 network, 5 CD-ROM, 6 RAM disk."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- Recycle Bin

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
_com_local = threading.local()


def _com_init() -> None:
    if getattr(_com_local, "done", False):
        return
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED; harmless if already
    except Exception:  # noqa: BLE001
        pass
    _com_local.done = True


def recycle(path: str) -> None:
    """Send *path* to the Windows Recycle Bin.

    FOF_WANTNUKEWARNING makes Windows itself ask before an item would be destroyed instead of
    recycled (network share, drive without a Recycle Bin, item too large), so nothing is ever
    deleted permanently without a confirmation. Raises OSError on failure or cancel."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    _com_init()
    full = os.path.abspath(path)
    if full.startswith("\\\\?\\"):
        full = full[4:]
    buf = ctypes.create_unicode_buffer(full + "\0")      # double-NUL-terminated list
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise OSError(125, "Cancelled: the item was not moved to the Recycle Bin", path)
    if rc != 0:
        raise OSError(rc, f"Could not move to the Recycle Bin (SHFileOperation error 0x{rc:X})", path)
    if os.path.lexists(path):
        raise OSError(5, "The item is still there after moving it to the Recycle Bin", path)


def recycle_bin_info() -> tuple[int, int]:
    """(item count, total bytes) over all drives; (0, 0) if unknown."""
    try:
        import ctypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64),
                        ("i64NumItems", ctypes.c_int64)]
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return int(info.i64NumItems), int(info.i64Size)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def empty_recycle_bin() -> None:
    """Empty every drive's Recycle Bin (no Windows UI; the caller has already confirmed)."""
    import ctypes
    SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
    rc = ctypes.windll.shell32.SHEmptyRecycleBinW(
        None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    # 0x8000FFFF (E_UNEXPECTED) is returned when the bin is already empty.
    if rc not in (0, -2147418113, 0x8000FFFF):
        raise OSError(rc, f"Could not empty the Recycle Bin (error 0x{rc & 0xFFFFFFFF:X})")


def open_recycle_bin() -> None:
    """Open Windows' own Recycle Bin window (restore / delete individual items there)."""
    spawn(["explorer.exe", "shell:RecycleBinFolder"])


# --------------------------------------------------------------------------- launching

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
_dll_lock = threading.Lock()


def clean_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update(extra or {})
    for k in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_PLATFORM"):
        env.pop(k, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # a launched PyInstaller app must not reuse our _MEI dir
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not os.path.normcase(p).startswith(os.path.normcase(mei)))
    return env


def spawn(args: list[str] | str, cwd: str | None = None, extra_env: dict | None = None,
          console: bool = False) -> None:
    """Start a program detached from GSExplorer+ (it keeps running after we exit).

    The bundle's DLL directory is cleared while the child starts, so launched programs load
    their own DLLs, never the bundled Qt/FFmpeg ones."""
    flags = CREATE_NEW_CONSOLE if console else (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    with _dll_lock:
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            try:
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            except Exception:  # noqa: BLE001
                mei = None
        try:
            subprocess.Popen(args, cwd=cwd if cwd and os.path.isdir(cwd) else None, env=clean_env(extra_env),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
        finally:
            if mei:
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetDllDirectoryW(mei)
                except Exception:  # noqa: BLE001
                    pass


def run_hidden(args: list[str], **kw) -> subprocess.CompletedProcess:
    """subprocess.run without flashing a console window (for helper tools like 7z.exe)."""
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(args, **kw)


def open_default(path: str) -> None:
    os.startfile(path)            # noqa: S606 - the user's own file association (ShellExecute "open")


def open_with_dialog(path: str) -> None:
    """Windows' own "Open with" chooser for *path*."""
    spawn(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", os.path.abspath(path)])


def show_in_explorer(path: str) -> None:
    spawn(["explorer.exe", "/select,", os.path.abspath(path)])


def windows_properties(path: str) -> None:
    """Windows' own Properties sheet (security/owner tab lives there)."""
    try:
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                        ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = 0x0000000C            # SEE_MASK_INVOKEIDLIST
        sei.lpVerb = "properties"
        sei.lpFile = os.path.abspath(path)
        sei.nShow = 1
        ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    except Exception as e:  # noqa: BLE001
        raise OSError(str(e))


def terminal_command(directory: str, custom: str = "") -> tuple[list[str], bool]:
    """(command, needs_new_console) for a terminal that opens in *directory*.

    Order: custom command from Preferences ({dir} is replaced), Windows Terminal (wt -d),
    PowerShell 7 (pwsh), Windows PowerShell, cmd.exe. The folder is also the process's
    current folder, so every shell starts there."""
    import shlex
    if custom.strip():
        parts = shlex.split(custom, posix=False)
        parts = [p.strip('"') for p in parts]
        if parts and (shutil.which(parts[0]) or os.path.isfile(parts[0])):
            if any("{dir}" in p for p in parts):
                return [p.replace("{dir}", directory) for p in parts], True
            return parts, True
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if wt:
        return [wt, "-d", directory], False
    for sh in ("pwsh.exe", "powershell.exe"):
        exe = shutil.which(sh)
        if exe:
            return [exe, "-NoExit", "-NoLogo"], True
    return [os.environ.get("COMSPEC") or "cmd.exe", "/K"], True


def open_terminal(directory: str, custom: str = "") -> None:
    if not os.path.isdir(directory):
        raise OSError(f"Folder not found: {directory}")
    args, console = terminal_command(directory, custom)
    spawn(args, cwd=directory, console=console)


def cmd_line(command: str, keep_open: bool = False) -> str:
    """cmd.exe command line that runs *command* exactly as typed (quotes, redirection, &&).

    Passed to subprocess as a string: list-to-string quoting would turn " into \", which cmd.exe
    does not understand. /S + outer quotes: cmd removes just those and keeps the rest verbatim."""
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return f'"{comspec}" /S {"/K" if keep_open else "/C"} "{command}"'


def run_in_terminal(command: str, directory: str | None, keep_open: bool = True) -> None:
    spawn(cmd_line(command, keep_open), cwd=directory, console=True)


def run_detached_shell(command: str, directory: str) -> None:
    spawn(cmd_line(command), cwd=directory, console=False)


# --------------------------------------------------------------------------- helper tools

def find_tool(name: str) -> str | None:
    """shutil.which, plus the usual install folders of tools Windows users rarely put on PATH
    (7-Zip, zstd, VLC, Poppler, FFmpeg)."""
    found = shutil.which(name)
    if found or not IS_WIN:
        return found
    stem = os.path.splitext(name)[0].lower()
    pf = [os.environ.get(v) for v in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
    sub = {"7z": ["7-Zip"], "7zz": ["7-Zip"], "vlc": ["VideoLAN\\VLC"],
           "zstd": ["zstd"], "ffmpeg": ["ffmpeg\\bin"], "pdftotext": ["poppler\\Library\\bin", "poppler\\bin"],
           "pdftoppm": ["poppler\\Library\\bin", "poppler\\bin"], "pdfinfo": ["poppler\\Library\\bin", "poppler\\bin"]}
    for base in filter(None, pf):
        for s in sub.get(stem, []):
            cand = os.path.join(base, s, stem + ".exe")
            if os.path.isfile(cand):
                return cand
    return None


# --------------------------------------------------------------------------- file locking

class FileLock:
    """Exclusive, non-blocking lock on an open fd (msvcrt) — Windows stand-in for fcntl.flock."""

    @staticmethod
    def lock(fd: int) -> None:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError(str(e)) from e

    @staticmethod
    def unlock(fd: int) -> None:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# --------------------------------------------------------------------------- process defaults

def install_subprocess_defaults() -> None:
    """In the windowed .exe, helper tools (7z.exe, zstd.exe, …) would each flash a console
    window. Default every subprocess without explicit creationflags to CREATE_NO_WINDOW.
    Programs the user launches go through spawn(), which always passes its own flags."""
    if not IS_WIN or getattr(subprocess.Popen, "_gx_patched", False):
        return
    orig = subprocess.Popen.__init__

    def __init__(self, *args, **kw):
        if not kw.get("creationflags"):
            kw["creationflags"] = CREATE_NO_WINDOW
        orig(self, *args, **kw)

    subprocess.Popen.__init__ = __init__
    subprocess.Popen._gx_patched = True
