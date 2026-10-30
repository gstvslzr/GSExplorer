#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
#
# GSExplorer+ 26.8.0 — build a self-contained .deb for Ubuntu 24.04 (amd64).
#
#   ./packaging/build-deb_v26.8.0.sh            # build
#   ./packaging/build-deb_v26.8.0.sh --test     # run all tests first
#
# Network: downloads PySide6-Essentials and PyInstaller from PyPI at BUILD time
# only (into a throwaway venv). The resulting package makes no network calls.
#
# Output: dist/gsexplorer-plus_26.8.0_amd64.deb (+ .sha256, licenses/, SPDX SBOM)
set -euo pipefail

VERSION="26.8.0"
PKGNAME="gsexplorer-plus"
APPID="io.github.gstvslzr.GSExplorer"
ARCH="amd64"
PYTHON="${PYTHON:-python3.12}"                 # Ubuntu 24.04 system Python
PYSIDE_VER="${PYSIDE_VER:-6.12.0}"
PYINSTALLER_VER="${PYINSTALLER_VER:-6.22.3}"
MAINTAINER="${MAINTAINER:-Gus Salazar <gstvslzr@users.noreply.github.com>}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
PKG="$BUILD/pkgroot"
VENV="$BUILD/venv-$PYTHON"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null || die "$PYTHON not found (sudo apt install python3.12-venv)"
command -v dpkg-deb >/dev/null || die "dpkg-deb not found"
command -v readelf >/dev/null || die "readelf not found (sudo apt install binutils)"
[ "$(dpkg --print-architecture)" = "$ARCH" ] || die "this script builds $ARCH packages on $ARCH hosts only"

# ---------------------------------------------------------------- 1. build venv
if [ ! -x "$VENV/bin/python" ]; then
    log "Creating build venv ($PYTHON)"
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --require-hashes -r "$ROOT/packaging/requirements-build.txt"

if [ "${1:-}" = "--test" ]; then
    log "Running all tests"
    TMPHOME="$(mktemp -d)"
    for f in "$ROOT"/tests/test_*.py; do
        HOME="$TMPHOME" XDG_DATA_HOME="$TMPHOME/.local/share" XDG_CONFIG_HOME="$TMPHOME/.config" \
        XDG_CACHE_HOME="$TMPHOME/.cache" QT_QPA_PLATFORM=offscreen PYTHONPATH="$ROOT/src" \
            "$VENV/bin/python" -u "$f" || die "test failed: $(basename "$f")"   # live connect parts need GX_LIVE=1
    done
    rm -rf "$TMPHOME"
fi

# ---------------------------------------------------------------- 2. freeze
log "Freezing with PyInstaller"
rm -rf "$BUILD/pyi" "$BUILD/frozen"
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN \
    --name "$PKGNAME" --onedir \
    --paths "$ROOT/src" \
    --add-data "$ROOT/src/gsexplorer/resources:gsexplorer/resources" \
    --add-data "$ROOT/LICENSE:." \
    --hidden-import PySide6.QtSvg --hidden-import PySide6.QtMultimedia \
    --hidden-import PySide6.QtMultimediaWidgets \
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc_data \
    --hidden-import PySide6.QtNetwork --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.QtOpenGL --exclude-module PySide6.QtDBus --exclude-module PySide6.QtSql \
    --exclude-module PySide6.QtTest --exclude-module PySide6.QtConcurrent --exclude-module PySide6.QtWebEngineCore \
    --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtCharts \
    --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtVirtualKeyboard \
    --exclude-module PySide6.QtGraphs --exclude-module PySide6.QtPdf --exclude-module PySide6.QtQuick3D \
    --workpath "$BUILD/pyi" --distpath "$BUILD/frozen" --specpath "$BUILD/pyi" \
    "$ROOT/packaging/launcher.py"

FROZEN="$BUILD/frozen/$PKGNAME"
[ -x "$FROZEN/$PKGNAME" ] || die "PyInstaller output missing"

# Prune host system libs + unneeded Qt plugins; derive Depends from what remains.
log "Pruning bundle and resolving dependencies"
SYS_DEPENDS="$(python3 "$ROOT/packaging/prune_bundle.py" "$FROZEN")" || die "dependency resolution failed"
# GPL-only Qt modules can arrive as plugins/QML even when their Python module is excluded.
find "$FROZEN" -regextype posix-extended \( -iregex '.*/(lib)?Qt6?(Charts|DataVisualization|VirtualKeyboard)[^/]*' \
     -o -iname '*virtualkeyboard*' \) -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------- 2b. license gate
log "Collecting third-party licenses (fails on GPL-only Qt modules or a non-LGPL FFmpeg)"
python3 "$ROOT/packaging/collect_licenses.py" "$FROZEN" --out "$FROZEN/share/licenses" \
    || die "license gate failed — see BLOCKER lines above"
printf '{"version": "%s", "commit": "%s", "build_date": "%s"}\n' "$VERSION" \
    "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" "$(date -u +%Y-%m-%d)" \
    > "$FROZEN/share/build-info.json"

# ---------------------------------------------------------------- 3. smoke test frozen app
log "Smoke-testing frozen binary"
"$FROZEN/$PKGNAME" --version | grep -q "$VERSION" || die "frozen binary failed --version"
# Import every module the packaged app uses and play real media, inside the frozen bundle itself.
SELFTEST_MEDIA="$(mktemp -d)"
if command -v ffmpeg >/dev/null; then
    ffmpeg -nostdin -loglevel error -f lavfi -i testsrc=duration=2:size=160x120:rate=25 -f lavfi \
        -i sine=duration=2 -c:v mpeg4 -c:a aac -shortest "$SELFTEST_MEDIA/t.mp4"
fi
QT_QPA_PLATFORM=offscreen HOME="$SELFTEST_MEDIA" "$FROZEN/$PKGNAME" --selftest "$SELFTEST_MEDIA/t.mp4" \
    || die "frozen self-test failed (a module or plugin is missing from the bundle)"
rm -rf "$SELFTEST_MEDIA"

# ---------------------------------------------------------------- 4. assemble package tree
log "Assembling package tree"
rm -rf "$PKG"
install -d "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
           "$PKG/usr/share/doc/$PKGNAME" "$PKG/usr/share/man/man1" \
           "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/metainfo"
cp -a "$FROZEN" "$PKG/opt/$PKGNAME"
find "$PKG/opt/$PKGNAME" -name "__pycache__" -prune -exec rm -rf {} +
find "$PKG/opt/$PKGNAME" -type f -name "*.so*" -exec chmod 0644 {} +   # libraries need not be executable

cat > "$PKG/usr/bin/$PKGNAME" <<'EOF'
#!/bin/sh
# GSExplorer+ launcher
exec /opt/gsexplorer-plus/gsexplorer-plus "$@"
EOF
chmod 0755 "$PKG/usr/bin/$PKGNAME"
# Transitional alias: keeps pre-26.8.0 scripts and not-yet-migrated timers working.
ln -s "$PKGNAME" "$PKG/usr/bin/gexplorer"

install -m 0644 "$ROOT/packaging/$APPID.desktop" "$PKG/usr/share/applications/$APPID.desktop"
install -m 0644 "$ROOT/packaging/$APPID.metainfo.xml" "$PKG/usr/share/metainfo/$APPID.metainfo.xml"
install -m 0644 "$ROOT/src/gsexplorer/resources/gsexplorer-plus.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$APPID.svg"

# PNG icons rendered from the SVG
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" - "$ROOT/src/gsexplorer/resources/gsexplorer-plus.svg" "$PKG" "$APPID" <<'EOF'
import os, sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
svg, root, appid = sys.argv[1], sys.argv[2], sys.argv[3]
r = QSvgRenderer(svg)
for s in (16, 22, 24, 32, 48, 64, 128, 256, 512):
    d = os.path.join(root, "usr/share/icons/hicolor", f"{s}x{s}", "apps")
    os.makedirs(d, exist_ok=True)
    img = QImage(s, s, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img); p.setRenderHint(QPainter.Antialiasing); r.render(p); p.end()
    img.save(os.path.join(d, appid + ".png"))
EOF

gzip -9n -c "$ROOT/packaging/gsexplorer-plus.1" > "$PKG/usr/share/man/man1/$PKGNAME.1.gz"
ln -s "$PKGNAME.1.gz" "$PKG/usr/share/man/man1/gexplorer.1.gz"
install -m 0644 "$ROOT/packaging/debian/copyright" "$PKG/usr/share/doc/$PKGNAME/copyright"
PYI_LIC="$("$VENV/bin/python" -c 'import PyInstaller,os,glob;d=os.path.dirname(PyInstaller.__file__);print((glob.glob(os.path.join(d,"..","pyinstaller-*.dist-info","licenses","COPYING.txt"))+[""])[0])')"
[ -n "$PYI_LIC" ] && install -m 0644 "$PYI_LIC" "$PKG/usr/share/doc/$PKGNAME/PYINSTALLER-COPYING.txt"
PY_LIC="$("$VENV/bin/python" -c 'import os,sysconfig;p=os.path.join(sysconfig.get_paths()["stdlib"],"LICENSE.txt");print(p if os.path.exists(p) else "")')"
if [ -n "$PY_LIC" ]; then
    install -m 0644 "$PY_LIC" "$PKG/usr/share/doc/$PKGNAME/PYTHON-LICENSE.txt"
elif [ -f "/usr/share/doc/$PYTHON/copyright" ]; then
    install -m 0644 "/usr/share/doc/$PYTHON/copyright" "$PKG/usr/share/doc/$PKGNAME/PYTHON-LICENSE.txt"
fi
printf 'gsexplorer-plus (%s) unstable; urgency=low\n\n  * 26.8.0: Renamed GExplorer to GSExplorer+ (command gsexplorer-plus; gexplorer kept as an alias) and released under the GPL-3.0-or-later. Help > About shows the license, every bundled component with its license text, the LGPL source offer, privacy and system info. Settings, sync profiles, schedules and saved passwords migrate automatically. The build fails on GPL-only Qt modules or a non-LGPL FFmpeg.\n  * 26.7.0: Synchronize progress window: ring with %% done, turning gear and time-left countdown; direction (left to right, right to left, deleting on a side, snapshot); action with full source and destination paths; current-file bar; files, data, speed, elapsed, remaining, per-direction counts; pause, cancel, run in background, open log.\n  * 26.6.0: Find Files (toolbar magnifier, Ctrl+F, Alt+F7): name/wildcard/regex, text inside files and documents, type, size, date, archives, system index, saved searches, refine, export.\n  * 26.5.0: Compress (Alt+F5) and Extract here / to folder / to… (Alt+F9) in the right-click menu (zip, tar.gz/xz/bz2, tar.zst, 7z with password; safe extraction); terminal always opens in the folder of the active pane (explicit working-directory option per terminal), plus "Open terminal in <folder>".\n  * 26.4.3: Find Duplicates: no freeze after large searches (model/view list, background preparation with a progress gear), about half the search memory, fix false matches for 16-32 KiB files.\n  * 26.4.2: Find Duplicates: Location column resizable; all but the first copy checked after a search (option); Shift/Ctrl+click multi-select, Space or a checkbox checks all selected rows; Move to Trash confirms.\n  * 26.4.1: Find Duplicates: "Check all but first" button; ring progress gauge for duplicate and sync scan popups.\n  * 26.4.0: Find Duplicates (Tools, Ctrl+Shift+D): content/name matching, sortable groups, side-by-side preview, selection rules, trash/delete/move-to-audit-folder with manifest and undo, CSV export.\n  * 26.3.1: F3 viewer: previous/next file arrows (◀ ▶, Left/Right, N/P), file counter, pane selection follows.\n  * 26.3.0: GSExplorer+ Player: built-in video/audio player with playlist, fullscreen, speed, tracks, resume.\n  * 26.2.1: Sync & Backup fixes: keep modification times on USB/FAT/network targets (no endless re-copy), ignore 1-hour DST shifts on FAT/exFAT, window opens with a ready task, Synchronize compares first, result stays visible, clear same-folder message.\n  * 26.2.0: Connect menu (SMB/SFTP/FTP/WebDAV/NFS, SSH, RDP/VNC, OneDrive/Google, rclone), thumbnails, preview panel with media playback, sync scan progress window.\n  * 26.1.0: Sync & Backup (mirror, update, two-way, snapshots, scheduling).\n  * 26.0.1: Phase 1 release.\n\n -- %s  %s\n' \
    "$VERSION" "$MAINTAINER" "$(date -R)" | gzip -9n > "$PKG/usr/share/doc/$PKGNAME/changelog.gz"

install -m 0755 "$ROOT/packaging/debian/postinst" "$PKG/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/debian/postrm" "$PKG/DEBIAN/postrm"

# ---------------------------------------------------------------- 5. control file
# Minimum glibc actually required by the bundled binaries (scanned, not guessed).
GLIBC_MIN="$(find "$PKG/opt/$PKGNAME" -type f \( -name '*.so*' -o -name "$PKGNAME" \) -print0 \
    | xargs -0 objdump -T 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)"
[ -n "$GLIBC_MIN" ] || die "could not determine glibc requirement"
INSTALLED_KB="$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"

cat > "$PKG/DEBIAN/control" <<EOF
Package: $PKGNAME
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_KB
Depends: libc6 (>= $GLIBC_MIN), $SYS_DEPENDS
Recommends: xdg-utils, shared-mime-info, hicolor-icon-theme, pkexec | policykit-1, udisks2,
 gvfs-backends, gvfs-fuse, libsecret-tools, poppler-utils, bubblewrap, ffmpeg
Suggests: gnome-terminal | x-terminal-emulator, remmina | freerdp3-x11, vlc, rclone, 7zip, zstd, plocate, poppler-utils
Replaces: gexplorer (<< 26.8.0)
Breaks: gexplorer (<< 26.8.0)
Provides: gexplorer
Homepage: https://github.com/gstvslzr/GSExplorer
Section: utils
Priority: optional
Description: Dual-pane, keyboard-first file manager with sync and backup
 GSExplorer+ (formerly GExplorer) is a keyboard-driven, dual-pane file manager for Ubuntu with tabs,
 breadcrumb navigation, a background copy/move/delete queue with pause and
 cancel, freedesktop.org trash, quick filter, F-key bar, command line,
 Open With, properties with permissions editing (pkexec when needed) and
 light, dark, midnight and high-contrast themes plus user JSON themes.
 .
 Sync & Backup: profiles of folder pairs with mirror, update, two-way
 (with deletion and conflict detection) and hard-linked snapshot backups,
 versioning to trash or a versions folder, filters, a compare preview with
 per-file overrides, headless "gsexplorer-plus --sync" and systemd user timers.
 .
 Connect: SMB, SFTP, FTP/FTPS, WebDAV, NFS via GVFS; SSH terminal; RDP/VNC via
 Remmina/FreeRDP; OneDrive/Google Drive via GNOME Online Accounts or rclone.
 Passwords go to the system keyring only. Thumbnails (freedesktop cache,
 sandboxed thumbnailers) and a preview panel with video/audio playback.
 .
 GSExplorer+ Player: built-in media player with folder playlist, fullscreen,
 speed control, audio/subtitle tracks, cover art and resume position.
 .
 Self-contained: ships its own Python runtime and Qt under /opt/gsexplorer-plus.
 Collects no telemetry and never updates itself; it uses the network only
 when you connect to a server or open a network location.
 .
 Free software under the GPL-3.0-or-later. The bundled Qt, PySide6 and FFmpeg
 are LGPL; see /usr/share/doc/gsexplorer-plus/copyright.
EOF

# ---------------------------------------------------------------- 6. build
log "Building .deb"
find "$PKG" -type d -exec chmod 0755 {} +
mkdir -p "$DIST"
OUT="$DIST/${PKGNAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
log "Done: $OUT ($(du -h "$OUT" | cut -f1), installs ${INSTALLED_KB} KB, needs glibc >= $GLIBC_MIN)"
( cd "$DIST" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
rm -rf "$DIST/licenses" && cp -a "$PKG/opt/$PKGNAME/share/licenses" "$DIST/licenses"
python3 "$ROOT/packaging/make_sbom.py" "$DIST/licenses/licenses.json" "$VERSION" \
    > "$DIST/${PKGNAME}_${VERSION}.spdx.json"
log "License inventory: $DIST/licenses/  SBOM: $DIST/${PKGNAME}_${VERSION}.spdx.json"
