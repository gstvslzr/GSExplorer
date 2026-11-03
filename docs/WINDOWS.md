# GSExplorer+ for Windows (portable)

`GSExplorer+_26.8.0_win64.exe` is a single file: no installer, no registry entries, no
administrator rights. Double-click it to run. It bundles its own Python 3.12, Qt 6.12 (PySide6)
and FFmpeg (LGPL build from the PySide6 wheel).

**Requires** 64-bit Windows 10 version 1809 or later, or Windows 11 — Qt 6.12's supported
Windows versions (doc.qt.io/qt-6/windows.html). Qt states 6.12 is the last Qt version to support
Windows 10, so a future Qt upgrade will drop Windows 10. Qt uses the ICU library that Windows
itself ships in `System32`.

**Unsigned:** the .exe is not code-signed, so Microsoft Defender SmartScreen shows "Windows
protected your PC" on first run from a download (More info ▸ Run anyway). Check the published
SHA-256 first. Signing with an Authenticode certificate removes the warning over time.

**Start-up:** a one-file build unpacks itself to a temporary folder (`%TEMP%\_MEI…`) on every
launch and deletes it on exit, so the first window takes a few seconds longer than an installed
program. Some antivirus products scan that unpacking; this is normal for PyInstaller one-file
programs.

## Where it keeps its data

| | Normal | Portable mode |
|---|---|---|
| Settings, bookmarks, themes, sync profiles | `%APPDATA%\gsexplorer-plus\` | `gsexplorer-plus-data\config\` |
| Logs, sync state, media positions | `%LOCALAPPDATA%\gsexplorer-plus\` | `gsexplorer-plus-data\data\` |
| Thumbnail cache | `%LOCALAPPDATA%\gsexplorer-plus\cache\` | `gsexplorer-plus-data\cache\` |

**Portable mode:** create an empty folder named `gsexplorer-plus-data` next to the .exe (for
example on a USB drive). Everything is then stored there and nothing in your Windows profile.
Help ▸ About ▸ Privacy shows the folders in use. To remove GSExplorer+, delete the .exe and these
folders.

Privacy is the same as on Linux: no telemetry, no analytics, no automatic updates; the network is
used only when you open a network location.

## What is different from the Linux version

| Area | Windows behaviour |
|---|---|
| Trash | Items go to the **Windows Recycle Bin**. When Windows can't recycle an item (network share, drive without a Recycle Bin, item too large), **Windows asks first**; nothing is destroyed silently. Sidebar ▸ Recycle Bin opens Windows' own Recycle Bin window, where single items are restored. *Empty Recycle Bin…* empties all drives after a confirmation. |
| Sidebar | Drives appear as `Label (C:)`. There is no "File System" root; network shares are opened by typing `\\server\share` in the path bar (Ctrl+L) or through mapped drive letters. |
| Hidden files | Files with the Windows *Hidden* attribute, and names starting with a dot, are hidden (Ctrl+H shows them). |
| File names | Names with `< > : " / \ | ? *`, reserved names (`CON`, `NUL`, `COM1`…, `LPT1`…) and names ending in a space or dot are refused with an explanation. |
| Open / Open With | Files open with their Windows file association. *Open With…* shows Windows' own app chooser (single file); a custom command line still works for several files. |
| Properties | An **Attributes** tab (Read-only, Hidden) instead of Linux permissions, plus a button for Windows' own Properties sheet (owner and permissions/ACLs). |
| Terminal (F12) | Windows Terminal if installed (`wt -d <folder>`), otherwise PowerShell 7, Windows PowerShell, then `cmd.exe`, all started in the pane's folder. A custom command can use `{dir}`. |
| Command line | Commands run through `cmd.exe` exactly as typed (quotes, `&&`, redirection). |
| Icons | File-type icons come from Windows (the same icons Explorer shows). |
| Sync & Backup | All modes work. Snapshot backups hard-link unchanged files on **NTFS**; on FAT32/exFAT each snapshot is a full copy. Pre/post commands run through `cmd.exe` and use `%GX_LEFT%`, `%GX_RIGHT%`, `%GX_ERRORS%`. |
| Scheduled sync | *Schedule…* shows the exact command line to add in **Task Scheduler** (`GSExplorer+…exe --sync "Profile" --quiet`); it does not create the task itself. Exit codes: 0 ok, 1 warnings, 2 errors, 3 cancelled. |
| Command line use | `GSExplorer+_26.8.0_win64.exe --sync "Nightly" [--dry-run]`, `--list-profiles` and `--version` print to the console they were started from. |
| Archives | ZIP and TAR (.gz/.xz/.bz2) are built in. 7Z/RAR/ISO need [7-Zip](https://www.7-zip.org) and .zst needs zstd; GSExplorer+ finds 7-Zip in `Program Files` even when it isn't on `PATH`. |
| Media player | Built-in player with the bundled FFmpeg; sound through Windows audio. VLC is used as an alternative when installed. |

## Not available on Windows

- **Connect** (SMB/SFTP/FTP/WebDAV/NFS mounts, saved connections, keyring): it is built on GNOME's
  GVFS and Secret Service, which don't exist on Windows. Use `\\server\share` paths or mapped drives.
- **Unmount** of drives (udisks). Use Windows' "Safely Remove Hardware".
- **Thumbnails of PDFs and videos.** On Linux they are made by external programs inside a
  bubblewrap sandbox; Windows has no equivalent sandbox, so the Windows build shows image thumbnails
  only (decoded in-process by Qt) rather than run unsandboxed converters on arbitrary files. The
  preview panel still plays video and audio.
- **System file index** search (plocate). Find Files walks folders instead.
- **Restore** of single items inside GSExplorer+ (use Windows' Recycle Bin window).
- Files whose names differ only in letter case are separate on Linux but the same file on Windows;
  Sync & Backup compares names case-sensitively, so avoid syncing such pairs between systems.

## Build

On Ubuntu 24.04 (the build uses Wine and a Windows CPython; nothing runs on a Windows machine):

```bash
sudo apt install wine64 wine xvfb gcc-mingw-w64-x86-64
python3 -m pip install PySide6==6.12.0 pillow          # only for rendering the .ico
packaging/windows/build-win_v26.8.0.sh --test           # -> dist/GSExplorer+_26.8.0_win64.exe + .sha256
```

The script freezes a one-dir bundle first and runs `packaging/collect_licenses.py` on it, so the
third-party notices in Help ▸ About are generated from what is actually bundled, and the build
fails on licensing blockers (GPL-only Qt modules, GPL or non-free FFmpeg). The notices are also
written next to the .exe in `dist/GSExplorer+_26.8.0_win64_licenses/`.

Wine 9.0 lacks the `icuuc.dll` that Windows 10/11 ship. For running the tests under Wine only, the
script compiles a stub (`packaging/windows/icuuc_stub.c`) into its private Wine prefix and then
checks that it is **not** inside the .exe.

## Tests

`tests/test_windows.py` runs with the Windows Python (68 checks: data folders and portable mode,
drive roots, Windows name rules, Recycle Bin, hidden attribute, copy/move/overwrite/trash/delete
jobs, Sync & Backup mirror/two-way/snapshots/lock/pre-post commands/headless CLI, archives including
path-escape attempts, Find Files, Find Duplicates, and the main window at `C:\`). The Linux suites
are unchanged and still pass.

**Native window not exercised.** Qt's Windows platform plugin (`qwindows.dll`) calls the Windows
Color System API (`mscms.dll` `WcsCreateIccProfile`) at start-up; Wine 9.0 does not implement it
and aborts, so the GUI was tested with Qt's `offscreen` platform (the 68 checks above, including
the main window at `C:\`) and the real .exe with `--selftest`, `--version` and `--sync`. The first
launch with a real window happens on Windows.

**Verified under Wine 9.0, not on a physical Windows machine.** Wine reproduces the Win32 APIs used
here (SHFileOperation, SHEmptyRecycleBin, GetDriveType, hard links, msvcrt locking), but real
Windows-only behaviour — Explorer's Recycle Bin window, Windows Terminal, the Open With chooser,
SmartScreen, antivirus interaction, high-DPI scaling — was not exercised. Test on Windows 10 and 11
before publishing.
