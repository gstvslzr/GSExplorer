# GSExplorer+ 26.7.0

Created by **Gus Salazar**. © 2026 Gus Salazar. All rights reserved.

Dual-pane, keyboard-first file manager for Ubuntu 24.04 (Total Commander / Double Commander
style), with tabs, breadcrumb path bar, a background operation queue and themes.
Self-contained `.deb`: ships its own Python 3.12 runtime and Qt 6 (PySide6) under `/opt/gexplorer`.

**Privacy/OPSEC:** no telemetry, no auto-update; the network is used only when you use Connect or open a network location. Settings live in
`~/.config/gexplorer/`. The Qt TUIO touch plugin (the only component that would open a UDP
socket) is removed from the bundle at build time. QtNetwork stays bundled because Qt Multimedia
requires it; GSExplorer+ makes no network requests of its own through it.

## Install / remove

```bash
sha256sum -c gexplorer_26.7.0_amd64.deb.sha256
sudo apt install ./gexplorer_26.7.0_amd64.deb      # apt resolves libraries + recommended helpers
gexplorer                                          # or launch "GSExplorer+" from the app grid
sudo apt remove gexplorer                          # or: sudo apt purge gexplorer
```

It does **not** become your default file manager. To make it the default for folders:
`xdg-mime default gsexplorer.desktop inode/directory` (undo: `xdg-mime default org.gnome.Nautilus.desktop inode/directory`).

Purge does not delete `~/.config/gexplorer` (Debian Policy forbids packages touching home
directories). Remove it by hand: `rm -rf ~/.config/gexplorer ~/.cache/gexplorer`.

## Phase 1 features

| Area | What works |
|---|---|
| Layout | Dual/single pane (F9), tabs per pane, sidebar (Places, Bookmarks, Devices, Trash), breadcrumb with click-to-type (Ctrl+L), F-key bar, command line |
| Views | Details (sortable, natural sort, hide/show columns) and icons (Ctrl+1/Ctrl+2), hidden files (Ctrl+H), quick filter with text or `*.glob` (Ctrl+S) |
| File ops | Copy/move (F5/F6, drag-and-drop, Ctrl+C/X/V, Nautilus clipboard compatible), rename (F2), new folder/file (F7/Shift+F4), trash (F8/Del), permanent delete (Shift+Del) |
| Queue | Background worker, pause/resume/cancel, progress and speed (Ctrl+J); conflict dialog: overwrite / skip / keep both / cancel, apply to all |
| Safety | Atomic overwrite (temp file + rename), symlinks copied as links and never followed, copy-into-itself blocked, free-space check before cross-device copy, ENOSPC aborts cleanly, FIFOs never read |
| Trash | freedesktop.org Trash spec (home trash + `$topdir/.Trash-$uid` on other drives), restore, empty; offers permanent delete if a drive can't trash |
| Other | Properties with size calculation and chmod/chown (pkexec only when needed), Open With (.desktop apps + custom command), F3 viewer (text, hex, images; ←/→ previous/next file), F4 editor, terminal here (F12), bookmarks (Ctrl+D), unmount (udisksctl), session restore |
| Themes | Follow system, Light, Dark, Midnight, High Contrast; user themes as JSON in `~/.config/gexplorer/themes/` (View > Theme > Open themes folder) |
| Filenames | Non-UTF-8 bytes and newlines survive every operation exactly; shown as U+FFFD and ⏎ |

## 26.7.0 — Synchronize: live progress window

Pressing **Synchronize** in Sync & Backup now opens a progress window for the whole run (every task of the
profile, in order):

* **Ring gauge** with the % done, a **turning gear** while it works, and a **countdown** of the estimated
  time left (`1:23 LEFT`). The gear stops and the ring says PAUSED while paused; at the end it shows
  COMPLETE / FINISHED / CANCELLED and how long the run took.
* **Direction strip**: LEFT folder ⟶ RIGHT folder with moving arrows the way data is going right now —
  left → right, right → left (two-way), into a new snapshot, or a red ✕ on the side where files are being
  deleted. The side being read and the side being written are labelled.
* **Current operation** in words (e.g. *Copying file (left → right)*, *Deleting on the right (moved to
  Trash)*, *Unchanged — hard link to the previous snapshot*, *Running the pre-task command*) with the
  **full source and destination paths** (wrapped, never shortened; selectable for copying).
* **Current-file bar**: `12.0 MB of 45.0 MB (26%)`.
* **Statistics**: files n of N, data done of total, speed (last 5 s), elapsed, remaining, copied → and ←,
  deleted, hard-linked, errors, warnings, task n of N.
* **Pause / Resume**, **Cancel** (asks first; files already copied stay, the file being copied is discarded,
  never left half-written), **Run in background** (also: closing the window). **Show progress** in the Sync
  window's status line brings it back. After the run: **Show problems…**, **Open log**, **Close**, and an
  option to close the window by itself after a clean run.
* The Sync window's status line also shows the % and the full path being synced.

How the estimate works: the % and the time left count bytes plus a fixed 64 KiB per file, so runs of many
small files or deletions only still move the gauge. Time left = remaining work ÷ the rate over the last
5 seconds; the first 2 seconds show *estimating*, and the clock stops while paused. It is an estimate:
it changes when file sizes or the disk speed change.

Fixed on the way: the Compare/Synchronize buttons were not disabled at the start of a run.

## 26.6.0 — Find Files (🔍 toolbar button, Ctrl+F, Alt+F7)

Open it with the **magnifying glass** next to the filter funnel, **Ctrl+F**, **Alt+F7** (Total Commander's
key), Edit/Tools ▸ *Find files…*, or right-click ▸ *Search in this folder…* / *Search in “folder”…*. It
starts in the active pane's folder.

**What you can search by**

| Criterion | Options |
|---|---|
| Name | *contains*, *wildcards* (`*.pdf;*.docx`, several separated by `;`), *regular expression*, *exact name*; match case; match against the whole path |
| Look in | one or more folders (`;`), subfolders on/off, depth limit, *Pane folders* button |
| Containing text | plain text or regular expression; match case; whole word; **NOT containing**; shows the line number and the matching line |
| Inside documents | DOCX, XLSX, PPTX, ODT/ODS/ODP (parsed locally); PDF via `pdftotext` (package poppler-utils) |
| Type | files and folders / files only / folders only; kind: documents, images, audio, video, archives, code & text |
| Size | at least / at most, in KB, MB or GB (a size filter returns files only) |
| Modified | today, last 7 / 30 days, last 12 months, this year, or between two dates |
| Skip | names to leave out, e.g. `node_modules;.git;*.tmp` |
| Options | hidden items; stay on this drive; names inside .zip/.tar archives; text inside binary files; only empty files/folders; result limit (default 100,000) |
| System index | with **plocate** installed, name searches can use the system file index (instant on whole disks; the index is updated periodically, usually daily, so very new files may be missing; every result is re-checked on disk) |

When *More options* is folded, the active options are listed next to it, so nothing filters silently.

**Results**
- **Live and responsive:** results appear while the search runs, with a turning gear and live counts.
  **Stop** or Esc ends it.
- **Sort and filter:** sort by name, folder, size, date or match by clicking a column title. *Filter
  these results…* narrows the list instantly (Ctrl+L).
- **Preview:** selecting a result shows it in the preview, with the matching line and the hit
  highlighted.
- **Actions:** Enter opens, **F3** views (← → step through the results), **Delete** moves to the trash,
  Ctrl+C copies paths. The right-click menu offers Open, View, Show in GSExplorer+, Open folder in a new
  tab, Open terminal in the folder, Copy path(s), Copy/Move to the other pane, Compress…, Move to trash.
- **Search within results** applies new criteria to the current results only (no new folder walk).
  **Export CSV…** and **Copy paths** cover all results or the selection.
- **Saved searches** store every criterion under a name. The last 20 name patterns are kept as history,
  and the last search is restored when the window opens.

**Safety and privacy:** only regular files are read (never through a symlink, never pipes or devices).
`/proc`, `/sys`, `/dev` and `/run` are skipped. Everything runs locally.
**Measured** (automated test, offscreen): a text search across 20,000 files took 0.5 s, and the window's
longest pause was under 50 ms.
`plocate` and `poppler-utils` are listed as *Suggests* (not installed automatically).

## 26.5.0 — Compress / Extract in the right-click menu; terminal opens in the right folder

### Compress (right-click ▸ Compress…, Alt+F5, or File menu)
Choose the archive name, format, compression level (Fast / Normal / Maximum), where to save it (this
folder or the other pane), and, for 7Z only, an optional password. With 7Z the file names are encrypted
too.

| Format | Needs | Notes |
|---|---|---|
| ZIP | built in | opens everywhere; symlinks stored as links |
| TAR.GZ / TAR.XZ / TAR.BZ2 / TAR | built in | keeps Linux permissions and symlinks |
| TAR.ZST | `zstd` | fast; offered only if zstd is installed |
| 7Z | `7zip` | small; optional password; offered only if 7-Zip is installed |

The archive is written under a temporary name and renamed when complete, so a cancelled or failed run
leaves no broken file. It gets normal permissions (your umask). An existing name is never overwritten:
the new one gets " (2)". The job runs in the operation queue (Ctrl+J) with progress, Pause and Cancel,
and the new archive is selected when it's done.

### Extract (right-click an archive)
- **Extract here**: an archive with one top-level folder or file puts it next to the archive; one with
  several items gets a folder named after the archive.
- **Extract to “name/”**: always a folder named after the archive.
- **Extract to…** (Alt+F9): you choose the destination (the other pane is offered first).
- **Formats:** .zip, .tar(.gz/.bz2/.xz), .tgz, single .gz/.bz2/.xz are built in. .tar.zst/.zst need
  `zstd`; .7z, .rar, .iso, .cab need `7zip`. When a tool is missing, the entry is greyed out with the
  install command.
- **Passwords:** encrypted ZIP and 7Z ask for the password and ask again if it's wrong. AES-encrypted
  ZIPs need 7-Zip.
- **Safety:** files are extracted into a hidden staging folder and moved into place only when complete,
  so nothing is ever half-written or overwritten. Tar uses Python's `data` filter (PEP 706). ZIP and 7Z
  entries are checked before writing. Absolute and `..` paths, links pointing outside the folder and
  device files are refused, and setuid bits are dropped. Extraction stops before the disk fills up
  (zip-bomb guard).
- `7zip` and `zstd` are listed as *Suggests* in the package (not installed automatically):
  `sudo apt install 7zip zstd`.

### Terminal
- **F12 / Open terminal here** opens in the folder shown in the **active pane**. Right-clicking a
  **folder** adds **Open terminal in “folder”**.
- The folder is passed three ways: the terminal's own option, the started process's folder, and `$PWD`.
  The option for each terminal was checked against its `--help` on Ubuntu 24.04 packages and tested live
  (the shell's actual folder read from `/proc`, using a folder name with spaces). It worked in all 13
  configurations tested: gnome-terminal (also through `x-terminal-emulator`), GNOME Console (kgx),
  Terminator (also as `x-terminal-emulator`), xfce4-terminal, MATE Terminal, Tilix, Konsole, kitty,
  Alacritty, xterm, and custom commands.
- **Unverified terminals:** Ptyxis and WezTerm aren't in Ubuntu 24.04's archive, so their option is read
  from their `--help` at run time and used only if listed.
- **Custom command (Preferences):** may contain `{dir}`, e.g. `konsole --separate --workdir {dir}`.
- **Before this release** the folder was passed only as the process's starting folder. In tests with
  stock settings that already worked for every terminal above, so I could not reproduce a wrong folder
  without knowing your terminal and setup. 26.5.0 removes that dependency by passing the folder
  explicitly.

## 26.4.3 — Find Duplicates: no hang after large searches, less memory, a correctness fix

**Correctness fix (affects 26.4.0–26.4.2).** Files between 16 KiB and 32 KiB were compared on their first
16 KiB only. Two such files that differ only after byte 16,384 were reported as identical. This was
reproduced on 26.4.2 with two 20,000-byte files that differ at byte 18,000. Files up to 32 KiB are now
hashed in full, and regression tests cover that case plus the 32 KiB boundary and a 5 MB multi-chunk
file. If you removed duplicates of that size with 26.4.x, the CSV export, audit manifest or trash still
lists what was removed.

**Why it hung.** After the search, the window built one Qt item per file and ticked them all on its own
thread, so nothing could repaint until that finished.

**What changed**
- **Results list:** a model/view table. Rows are formatted only when drawn, so no per-file objects are
  created. Groups are header rows in a flat list (▾/▸, or ←/→, collapse and expand them), which avoids
  Qt's tree layout pass over every row.
- **Background work with a progress gear:** preparing results, sorting, selection rules, updating the
  list after an action, export, and the selected-size total run on a background thread when there are
  5,000+ files. A turning gear covers the window with what it's doing ("Preparing 120,000 results…",
  "Sorting 120,000 files…").
- **Smoother animation:** while background work runs, Python is told to switch threads every 0.5 ms
  instead of 5 ms, and the gear draws over a snapshot, so it keeps turning and the scan ring stays smooth.
  The default is restored afterwards.
- **Search engine:** files are kept as compact records grouped by size while walking; full paths and
  objects are created only for real duplicates. Hashes are stored as raw bytes, one read buffer is reused,
  and the set of every path seen is gone (the folder-identity check already prevented double counting).

**Measured** on a test tree of 240,000 files (120,000 unique; 40,000 groups × 3 copies = 120,000 duplicates):

| | 26.4.2 | 26.4.3 |
|---|---|---|
| Search: peak Python memory (tracemalloc) | 183 MB | 85 MB |
| Window: time until usable after the search | 9.7 s (frozen) | ~0.8 s |
| Window: longest freeze while results load | 9.7 s | 0.27 s |
| Memory added by the results list | +370 MB | +23 MB |
| Whole app, peak memory (installed package, real X session) | 875 MB | 258 MB |

The window rows were measured offscreen with the same data; the last row comes from the installed
packages. On the X session the gear appears immediately and turns for most of each step, with pauses of
up to about 0.4 s at the start and end of a step on 120,000 files. Your timings will differ with disk
speed and file count.

## 26.4.2 — Find Duplicates fixes

- **Location column is resizable.** Every column can be dragged wider or narrower; double-click a divider to
  auto-fit. On first open, Location takes the free width; once you drag it, your width is kept across
  searches. If columns get wider than the list, it scrolls sideways. Clicking a row no longer jumps the list
  sideways, so the checkboxes stay in view.
- **Copies are checked after a search.** All but the first copy of each group (in the current sort) are
  ticked automatically. This is on by default; turn it off with *After searching, check all but the first
  copy*. The ☑ *Check all but first* button still does the same on demand.
- **Multi-select.** Click, Shift+click (range) and Ctrl+click (add/remove) select rows; Ctrl+A selects all.
  Then:
  - **Space** checks every selected row, or unchecks them if all are already checked.
  - Clicking **one checkbox** inside the selection applies it to all selected rows and keeps the
    selection.
  - **Right-click** offers *Check / Uncheck the N selected rows*.
- **Move to Trash always asks** in this window, showing the file count and total size. Copies are now
  pre-ticked, so a stray Delete key could otherwise trash many files at once. Nothing is removed without a
  confirmation.

## 26.4.1 — Check all but first, ring progress gauge

- **☑ Check all but first** button in the Find Duplicates toolbar. "First" means the top copy of each group
  as currently sorted. To keep the oldest copy, sort by *Date modified* ↑ first; for the shortest path,
  sort by *Location*.
- **Ring progress gauge.** The Find Duplicates and Sync & Backup scan popups now use a segmented ring:
  - **While listing files** (total unknown), a lit arc circles the ring and the centre shows a live
    file count.
  - **During comparison** it fills clockwise with "NN % completed". Duplicate searches weight the quick
    check as 0–40 % and the full-content compare as 40–100 %, the latter by bytes read. The exact counts
    are shown under the ring.
  - **Trash, delete and audit moves** fill 0–100 % by files done.
- **Faster with large results.** In the test at 15,000 groups / 45,000 files, the list fills in about 2 s
  and "Check all but first" takes under 1 s. Before the fix, selecting froze for a long time because each
  group was found by a linear search.
- Verified on the installed package by searching `/usr`: 117,209 files and 14,819 groups. "Check all but
  first" selected 17,272 files, which is 32,091 − 14,819.

## Find Duplicates (26.4.0)

**Tools ▸ Find Duplicates…** (Ctrl+Shift+D), or right-click a folder / empty space ▸ *Find duplicates in this folder*.

**How files are matched**

| Match by | What it compares | Reads file contents? |
|---|---|---|
| Identical content (default) | size → BLAKE2b of first + last 16 KiB → full BLAKE2b-256 | only for same-size candidates; full read only if the quick check still matches |
| Same name and size | file name (case-insensitive) + byte size | no — contents may differ |
| Same name only | file name (case-insensitive) | no — contents and size may differ |

Name-mode results are labelled "contents not compared", and actions on them ask before running.
Options: folders (several allowed; overlapping folders aren't double-counted), min/max size, *Only names*
and *Skip names* globs (`*.jpg;*.png`, `node_modules;Cache`), hidden files, empty files, hard links, skip trash.
Never read: symlinks, pipes, sockets, devices, `/proc`, `/sys`, `/dev`, `/run`, and earlier audit folders.
Hard links (same inode) are listed once by default because removing one frees no space.

**Results**
- Groups with each copy beneath. Sort by **wasted space, size, copies, name, location or date**, using
  the *Sort by* box or by clicking a column header. ↓/↑ reverses the order. Copies inside each group sort too.
- Clicking a copy shows a **side-by-side preview**: left is that copy, right is another copy of the group,
  chosen with *with:*. Previews cover images, text, PDF, Office documents and media. Above them a summary says
  "Identical content" (with the hash) or "name only", whether the names differ, which copy is newer, and which
  side is selected for removal.
- **F3** opens the viewer on the group's copies; ← → flip between them. The context menu has Open,
  *Show in GSExplorer+*, *Keep only this copy* and *Copy path*.

**Selecting.** Tick the boxes, click **☑ Check all but first** to tick every copy except the top one in each group (in the current sort order), or use **Select ▾**:
- All but the newest
- All but the oldest
- All but the first (in the current sort)
- All but the shortest path
- All copies inside a folder…

**Actions on the selected copies**

| Button / key | Result |
|---|---|
| Move to Trash (Delete) | freedesktop trash; restorable from GSExplorer+'s Trash. Always asks first (count and size) |
| Delete permanently… (Shift+Delete) | asks for confirmation; can't be undone |
| Move to folder (audit)… | moves copies into `<chosen folder>/GSExplorer-duplicates-YYYYMMDD-HHMMSS/<full original path>`, plus `manifest.csv` |
| Undo an audit move… | pick a `manifest.csv`; files go back to their original paths (existing files are never overwritten) |
| Export list… | CSV of every group and copy, with a *selected* column |

`manifest.csv` columns: moved_at, original_path, audit_path, size_bytes, modified, match (`blake2b-256:<hash>`),
group, kept_copy.

**Safety rules**
- Before acting on a file, its size and modification time are re-checked against the scan; if either
  changed, it is skipped.
- At least one unchanged, unselected copy must remain in its group. If every copy of a group is ticked,
  GSExplorer+ asks to keep the newest. The engine also refuses on its own, independent of the window.
- Moves across file systems copy the file, verify the size and BLAKE2b hash, and only then delete the
  original.
- Everything runs locally; nothing is uploaded.

**Limits (verified by the tests below, not by real-world volume):**
- BLAKE2b-256 is treated as proof of identical content (no byte-by-byte pass). An accidental collision is
  not a practical concern at 256 bits.
- Name modes use filename only; they don't look at photo EXIF or audio tags, and they don't do
  similar-image matching.
- Speed is bounded by disk reads. In the test, 20,000 files with unique sizes were scanned without reading
  any file contents.

## 26.3.1 — F3 viewer: previous / next file

The F3 viewer steps through the files of the current folder, in the pane's sort order and filter:

| Action | Keys / buttons |
|---|---|
| Next file | **▶** button, → , N, Space |
| Previous file | **◀** button, ← , P, Backspace |
| Scroll text/hex | ↑ ↓ PgUp PgDn Home End |
| Play media in GSExplorer+ Player | Enter (or the button shown for video/audio) |
| Close | Esc, F3, Q |

- A counter shows the position ("3 of 12"); folders and `..` are skipped; it wraps from last to first and back.
- The pane selection follows the viewer, so closing it leaves the cursor on the last file viewed.
- Pipes and other special files show a notice instead of being opened, so stepping past them can't hang the viewer.
- Large images are scaled to the window (decoded at most 2400 px, EXIF rotation applied).

## GSExplorer+ Player (26.3.0)

Double-click (or Enter on) any video or audio file to open the built-in player; also right-click >
**Play in GSExplorer+ Player**, **Play all media in this folder**, Tools > **Media player** (Ctrl+Shift+P),
or **Open in Player** from the preview panel (continues from the same position).
Preferences > *Open video/audio with* can switch back to the system app or VLC.

- Playlist = all media in the folder, natural order (Track 2 before Track 10); select several files
  first to play just those. Add files (+ or drag and drop), remove, double-click to jump.
- Previous/next (first Previous restarts, a second within 2 s goes back), auto-advance,
  repeat off/all/one, shuffle.
- Click-to-seek bar, time, volume and mute (remembered), speed 0.5x-2x, audio and subtitle track
  selection (embedded tracks), fullscreen with auto-hiding controls, on-screen feedback.
- Music: cover art from tags or cover.jpg/folder.jpg, title/artist/album.
- Resumes files longer than 3 minutes where you stopped (stored only in
  `~/.local/share/gexplorer/media-positions.json`, last 500 files).
- Keys: Space/K play-pause · ←/→ 5 s · J/L 10 s · Shift+←/→ 1 min · ↑/↓ volume · M mute ·
  F/F11/double-click fullscreen · Esc exit fullscreen · N/P next/previous · [ ] speed · Home restart ·
  S stop · Ctrl+L playlist · Ctrl+O add files. Mouse: click picture = pause, wheel = volume.
- Formats: whatever the bundled FFmpeg decodes (MP4/MKV/WebM/AVI/MOV, MP3/AAC/FLAC/Opus/WAV and
  more). Decoding is in software (no hardware decoder was detected in testing). Not supported:
  external .srt subtitle files, DVD/Blu-ray menus, network streams.

## 26.2.1 — Sync & Backup fixes

Found by driving the window with real mouse/keyboard input and by backing up to a FAT drive:

- **Endless re-copy on USB/FAT targets (engine bug).** The modification time was set on the temporary
  copy before it was renamed into place; some drivers reset it on rename, so every later Compare saw
  every file as "target newer", re-copied it and sent the previous copy to the trash. Times are now
  re-applied after the rename and verified; permissions are set separately (FAT rejects chmod). If a
  target still can't keep times, the run warns and suggests Compare by size/content.
- **Daylight-saving shifts:** an exact 1-hour difference is ignored automatically when either side is
  FAT/exFAT (they store local time), and optionally for any task ("Ignore exact 1-hour time differences").
- **Window opened unusable:** with no saved profile it showed a blank form that silently ignored typing.
  It now starts with a task prefilled from the panes; with no tasks it shows "Add a task".
- **Synchronize was greyed out until Compare:** it now compares first, shows the preview and asks.
- **No visible result:** the automatic re-compare after a run erased the result; "✓ Done: N copied…"
  now stays, with the log path.
- **Ctrl+Shift+S with both panes on the same folder** (the default on first launch) built a task that
  could never run; it now explains and leaves Target for you to choose. Blocking problems now pop up.

## Connect (26.2.0)

**Connect** menu and the sidebar's Network section; **Ctrl+K** = Connect to Server.

| Type | How it works | Needs |
|---|---|---|
| SMB (Windows/NAS), SFTP, FTP/FTPS, WebDAV(S), NFS | Mounted through GVFS (same as Nautilus); opens as a normal folder under `/run/user/UID/gvfs`, so copy/sync/preview all work | `gvfs-backends`, `gvfs-fuse` (default on Ubuntu desktop) |
| SSH terminal | `ssh [-p port] [-i key] user@host` in your terminal | `openssh-client` |
| RDP / VNC | Remmina, FreeRDP (`xfreerdp3 … /cert:tofu`), GNOME Connections or TigerVNC; the client asks for the password | `remmina` + plugins, or `freerdp3-x11` |
| OneDrive / Google Drive | Settings > Online Accounts (Microsoft 365 / Google, "Files" on); the drive then appears under Network | GNOME 46 (Ubuntu 24.04) |
| rclone remotes | `rclone mount remote: ~/.local/share/gexplorer/mnt/remote --daemon` | `rclone`, `fuse3` |

Security: passwords go to the mount helper's **stdin** (never argv/env); "Remember password" uses
only the **system keyring** (`secret-tool`); `~/.config/gexplorer/connections.json` (0600) never holds
passwords. Unknown SSH host keys must be confirmed; a **changed** key is refused. FTP and plain
WebDAV show a clear-text warning.

## Thumbnails and preview panel (26.2.0)

- Icon view (Ctrl+2) shows thumbnails; **Ctrl+wheel** or Ctrl+Alt+= / Ctrl+Alt+- resizes (48–256 px).
- freedesktop.org thumbnail spec: cache `~/.cache/thumbnails` is shared with Nautilus. Images by Qt;
  PDFs/videos/others by system thumbnailers (+ `pdftoppm`/`ffmpeg` fallbacks) **sandboxed with
  bubblewrap** (read-only filesystem, no network). Default: local files only.
- **Alt+P** preview panel: images, text/code, PDF first page + page count, DOCX/PPTX/XLSX/ODF text,
  **video/audio playback** (Qt Multimedia + bundled FFmpeg), **Open in VLC** when installed
  (Preferences can make VLC the default player).

## Sync & Backup (26.1.0; scan progress window in 26.2.0)

Compare opens a progress window: exact % when the pair was scanned before, busy indicator + live
item count on the first scan, current path, elapsed time, Cancel.


Inspired by FreeFileSync (compare/preview/sync variants, versioning, batch exit codes) and
luckyBackup (profiles of tasks, snapshots, scheduling, pre/post commands). Local folders only
(mounted network drives work; no SSH/email by design).

Open it with **Tools > Sync & Backup** (Ctrl+Shift+B), or **Ctrl+Shift+S** to compare the two
pane folders straight away (active pane = source).

| Mode | Behaviour |
|---|---|
| Mirror → | Target becomes an exact copy; extra target files are removed (versioned) |
| Update → | Copy new and newer files; never deletes; never overwrites a newer target file |
| Two-way ↔ | Creates, changes and deletions flow both ways using a per-pair state DB (`~/.local/share/gexplorer/sync-state/`). Changed-on-both-sides and changed-vs-deleted become **conflicts** you resolve per file. First run only merges |
| Snapshot backup | `TARGET/YYYY-MM-DD_HHMMSS/` full browsable copy per run; unchanged files are hard links to the previous snapshot (rsync `--link-dest` semantics); keep N; `latest` symlink; interrupted runs are left as `*.partial` and cleaned up next run |

- **Compare** scans both sides in the background and shows a preview grid (category, sizes,
  times, action). Right-click rows to change the action (→, ←, delete left/right, do nothing).
  Nothing is touched until **Synchronize**; runs go through the operations queue (pause/cancel).
- **Compare by:** time + size (2 s tolerance, FAT-safe), file content (byte-by-byte), or size.
- **Old versions:** move to trash, move to a versions folder (`name 2026-10-15_183000.ext`,
  optional keep-N-days), or delete permanently.
- **Filters:** include/exclude, one per line. `*.tmp` matches at any depth, `docs/*.pdf` is
  anchored to the root, trailing `/` means folders only.
- **Safety:** pre-flight blocks missing source, unmounted target (parent folder missing),
  nested pairs, and a mirror whose source is empty while the target isn't. Writes go to a temp
  file then atomic rename; a file that changed since Compare is skipped; a per-pair lock
  stops a scheduled run and a GUI run colliding.
- **Profiles:** `~/.config/gexplorer/sync/<name>.json`, several tasks each, enable/reorder.
  Optional before/after shell commands (`$GX_LEFT`, `$GX_RIGHT`, `$GX_ERRORS`).
- **Headless:** `gexplorer --sync "Nightly" [--dry-run] [--quiet]` prints JSON; exit
  0 success, 1 warnings (e.g. unresolved conflicts), 2 errors, 3 cancelled. Logs:
  `~/.local/share/gexplorer/sync-logs/`.
- **Schedule…** writes a systemd *user* timer (`~/.config/systemd/user/gexplorer-sync-*.timer`)
  — Hourly/Daily/Weekly/Monthly or any `OnCalendar` expression, validated live with
  `systemd-analyze`; "run missed jobs" = `Persistent=true`. Timers only fire while you're logged in
  unless you enable linger (button in the dialog). Errors trigger a desktop notification.
  Remove schedules (Schedule… > Remove) **before** uninstalling GSExplorer+.

## Build from source

```bash
sudo apt install python3.12-venv binutils dpkg-dev
./packaging/build-deb_v26.7.0.sh --test     # runs every test suite, builds dist/*.deb, then self-tests the frozen app
```

Build-time only: downloads `PySide6-Essentials==6.12.0`, `PySide6-Addons==6.12.0` and `pyinstaller==6.22.3` from PyPI
into a throwaway venv. `packaging/prune_bundle.py` strips host system libraries from the
bundle and derives the package `Depends:` from the ELF `NEEDED` entries of what remains.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python tests/test_core.py   # file ops, trash, 100k-entry model
QT_QPA_PLATFORM=offscreen python tests/test_ui.py     # end-to-end UI flows, throwaway $HOME
python tests/test_sync.py                             # sync engine: every mode + safety rule
QT_QPA_PLATFORM=offscreen python tests/test_sync_ui.py # sync window end to end
QT_QPA_PLATFORM=offscreen python tests/test_preview.py  # thumbnails + preview panel
QT_QPA_PLATFORM=offscreen python tests/test_player.py   # GSExplorer+ Player (needs ffmpeg)
QT_QPA_PLATFORM=offscreen python tests/test_connect.py  # Connect (live parts need GX_LIVE=1)
QT_QPA_PLATFORM=offscreen python tests/test_connect_ui.py
QT_QPA_PLATFORM=offscreen python tests/test_viewer.py   # F3 viewer previous/next
python tests/test_dupes.py                            # duplicate engine: matching, guards, audit, undo
QT_QPA_PLATFORM=offscreen python tests/test_dupes_ui.py # Find Duplicates window end to end
python tests/test_archives.py                         # compress/extract: formats, malicious archives, passwords
QT_QPA_PLATFORM=offscreen python tests/test_archives_ui.py # context menu, queue jobs, terminal folder
python tests/test_search.py                           # search engine: every criterion, documents, archives, index
QT_QPA_PLATFORM=offscreen python tests/test_search_ui.py   # Find Files window end to end
QT_QPA_PLATFORM=offscreen python tests/test_sync_progress.py # sync progress: engine ops, job state, window
```

## Known limitations (Phase 1)

- Requires glibc ≥ 2.38: installs on Ubuntu 24.04+, **not** on Debian 12 (glibc 2.36). A
  Debian 12 build must be produced on a Debian 12 machine with the same script.
- Connect tested live here: SMB and SFTP (mount, wrong password, keyring, reconnect, disconnect).
  Not tested live: FTP/FTPS, WebDAV, NFS (same GVFS path), RDP/VNC (no server here), OneDrive/Google
  (needs your account).
- Video/audio needs PipeWire or PulseAudio for sound; without either, video plays silently.
- Sync: no SSH/remote targets, no real-time (watch-and-sync) mode, no moved-file detection
  (a moved file is copied + deleted). Hard-link snapshots need a Linux filesystem on the target
  (ext4/btrfs/xfs); on exFAT/FAT/NTFS-via-FUSE each snapshot is a full copy (warned in preview).
- No undo; multi-rename, archives, search, preview pane, SFTP/SMB, custom actions
  and embedded terminal are Phase 2.
- Sorting a 100k-entry folder briefly blocks the UI (~90–130 ms measured); scanning itself is
  in the background.
- Network mounts that hang (dead NFS/SMB) can stall the folder being opened.
- File ops that need root (other than chmod/chown in Properties) report "Permission denied"
  rather than escalating.
