# Changelog

## 26.8.0 Windows — 2026-11-02 — portable Windows build

- **New: `GSExplorer+_26.8.0_win64.exe`**, a single-file portable build for 64-bit Windows 10
  (1809+) / 11, built on Linux with Wine (`packaging/windows/build-win_v26.8.0.sh`). See
  `docs/WINDOWS.md`.
- Windows integration (`winplat.py`): `%APPDATA%`/`%LOCALAPPDATA%` data folders and portable mode,
  Windows Recycle Bin (Windows asks before anything would be destroyed instead of recycled),
  drive letters in the sidebar, hidden attribute, Windows file-name rules, shell file-type icons,
  Windows "Open with" and Properties, Windows Terminal/PowerShell/cmd, `cmd.exe` pre/post sync
  commands, msvcrt lock for sync pairs, console output for `--sync` from cmd/PowerShell.
- Fixed for Windows: drive roots (breadcrumb loop at `C:\`, Up and `..` at a root); `os.scandir`
  reports no file IDs on Windows, which would have made Find Duplicates treat every same-size file
  as a hard link and report nothing; folder-loop guards in Find Files / Find Duplicates no longer
  skip folders on file systems that report no folder IDs.
- `collect_licenses.py` now recognizes Windows bundles (Qt `.dll`/`.pyd`, `avcodec-NN.dll`,
  `python312.dll`, Qt image plugins) and lists the bundled MSVC runtime, so the GPL-only-module
  and FFmpeg-license gates also apply to the Windows build.
- Tests: `tests/test_windows.py` (68 checks, run with the Windows Python). Linux behaviour and all
  Linux test suites unchanged.

## 26.8.0 — 2026-10-30 — first public release

- Renamed **GExplorer → GSExplorer+**. Command `gsexplorer-plus` (`gexplorer` kept as an alias),
  package `gsexplorer-plus` (replaces `gexplorer`), app ID `io.github.gstvslzr.GSExplorer`.
- Licensed under the **GPL-3.0-or-later**.
- **Help ▸ About** now has six tabs: About, License, Third-party software (every bundled
  component with its license text), Source code offer (LGPL notice), Privacy, System info.
- One-time migration of settings, data, cache, sync schedules and saved passwords from GExplorer.
- Packaging: AppStream MetaInfo, renamed desktop file and icons, DEP-5 copyright for the new
  license, build dependencies pinned by SHA-256, license inventory gate (fails on GPL-only Qt
  modules or a non-LGPL FFmpeg), SPDX SBOM, `build-info.json` with the git commit.
- README: corrected version and "Known limitations" (archives, search, preview pane and SFTP/SMB
  have shipped since 26.2.0–26.6.0); AI-assistance disclosure; security policy.

## Earlier versions (as GExplorer, personal use)

- **26.7.0** Synchronize progress window.
- **26.6.0** Find Files.
- **26.5.0** Compress / Extract in the context menu; terminal opens in the active pane's folder.
- **26.4.0–26.4.3** Find Duplicates.
- **26.3.0–26.3.1** Built-in media player; F3 viewer previous/next.
- **26.2.0–26.2.1** Connect menu, thumbnails, preview panel; sync fixes.
- **26.1.0** Sync & Backup.
- **26.0.1** First build.

See `README.md` for details of each version.
