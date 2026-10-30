# Changelog

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
