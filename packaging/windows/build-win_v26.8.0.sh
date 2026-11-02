#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build the portable Windows .exe of GSExplorer+ on Linux (Ubuntu 24.04), using Wine + a Windows
# CPython. Output: dist/GSExplorer+_<ver>_win64.exe (one file, no installation) plus .sha256.
#
#   sudo apt install wine64 wine xvfb gcc-mingw-w64-x86-64
#   packaging/windows/build-win_v26.8.0.sh [--test]
#
# Network (build time only): python-build-standalone CPython 3.12 for Windows from GitHub (checked
# against PBS_SHA256 below) and the wheels pinned with hashes in packaging/requirements-build.txt.
#
# Steps
#   1. Windows CPython + pinned wheels in a private Wine prefix (build/win/).
#   2. Freeze ONE-DIR first and run packaging/collect_licenses.py on it: third-party notices are
#      generated and licensing BLOCKERS (GPL-only Qt modules, GPL/nonfree FFmpeg) fail the build.
#   3. Freeze ONE-FILE with the notices, LICENSE, build-info and icon embedded.
#   4. Check the result: --selftest of the real .exe, no test-only DLL inside, size, sha256.
#
# Testing under Wine needs icuuc.dll, which real Windows 10/11 ships in System32 and Wine 9 does not.
# A converter-less stub (packaging/windows/icuuc_stub.c) is compiled into the Wine prefix only; step 4
# verifies it is NOT inside the .exe.
set -euo pipefail

VERSION="26.8.0"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
B="$ROOT/build/win"
DIST="$ROOT/dist"
OUT_NAME="GSExplorer+_${VERSION}_win64"
PBS_FILE="cpython-3.12.11+20250612-x86_64-pc-windows-msvc-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250612/$PBS_FILE"
PBS_SHA256="${PBS_SHA256:-7b93afa91931dbc37b307a81b8680b30193736b5ef29a44ef6452f702c306e7a}"  # 2 independent downloads, 2026-11-02

export WINEPREFIX="$B/wineprefix" WINEDEBUG=-all WINEARCH=win64 LANG=C.UTF-8
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
wpy() { wine "$B/winpy/python/python.exe" -X utf8 "$@" < /dev/null 2>&1 | cat; }
winpath() { printf 'Z:%s' "$(printf '%s' "$1" | sed 's#/#\\#g')"; }
command -v wine >/dev/null || die "wine not installed"
command -v x86_64-w64-mingw32-gcc >/dev/null || die "gcc-mingw-w64-x86-64 not installed"
mkdir -p "$B" "$DIST"

# ---------------------------------------------------------------- 1. toolchain
if [ ! -x "$B/winpy/python/python.exe" ] || [ ! -f "$B/.toolchain-ok" ]; then
    log "Windows CPython (python-build-standalone)"
    [ -f "$B/$PBS_FILE" ] || curl -sSfL -o "$B/$PBS_FILE" "$PBS_URL"
    got="$(sha256sum "$B/$PBS_FILE" | cut -d' ' -f1)"
    echo "    sha256 $got"
    [ -z "$PBS_SHA256" ] || [ "$got" = "$PBS_SHA256" ] || die "CPython archive checksum mismatch"
    rm -rf "$B/winpy" && mkdir -p "$B/winpy" && tar -xzf "$B/$PBS_FILE" -C "$B/winpy"
    log "Pinned Windows wheels (hash-checked)"
    rm -rf "$B/wheels" && mkdir -p "$B/wheels"
    pip download -q --require-hashes -r "$ROOT/packaging/requirements-build.txt" \
        --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp -d "$B/wheels"
    pip download -q --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp \
        -d "$B/wheels" "pywin32-ctypes==0.2.3" "pefile==2023.2.7"
    wineboot -i >/dev/null 2>&1 || true
    wpy -m pip install -q --no-index --find-links "$(winpath "$B/wheels")" \
        pyinstaller==6.22.3 PySide6_Essentials==6.12.0 PySide6_Addons==6.12.0 shiboken6==6.12.0
    log "Test-only icuuc.dll stub into the Wine prefix (never shipped)"
    x86_64-w64-mingw32-gcc -O2 -shared -o "$B/icuuc.dll" "$ROOT/packaging/windows/icuuc_stub.c" -Wl,--kill-at
    cp "$B/icuuc.dll" "$WINEPREFIX/drive_c/windows/system32/icuuc.dll"
    touch "$B/.toolchain-ok"
fi
wpy -c "import PySide6, PyInstaller; print('    PySide6', PySide6.__version__, '/ PyInstaller', PyInstaller.__version__)"

# ---------------------------------------------------------------- 2. tests (optional)
if [ "${1:-}" = "--test" ]; then
    log "Windows test suite (offscreen)"
    out="$(cd "$ROOT" && QT_QPA_PLATFORM=offscreen wpy tests/test_windows.py)"
    echo "$out" | grep -E "^FAIL|checks passed" || true
    echo "$out" | grep -q "^FAIL" && die "Windows tests failed"
fi

# ---------------------------------------------------------------- 3. icon + version resource
log "Icon and version resource"
QT_QPA_PLATFORM=offscreen python3 "$ROOT/packaging/windows/make_icon.py" \
    "$ROOT/src/gsexplorer/resources/gsexplorer-plus.svg" "$B/gsexplorer-plus.ico" 2>/dev/null \
    || die "icon: run with a Python that has PySide6 and Pillow (python3 -m pip install PySide6 pillow)"
IFS=. read -r V1 V2 V3 <<< "$VERSION"
cat > "$B/version_info.txt" <<EOF
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($V1, $V2, $V3, 0), prodvers=($V1, $V2, $V3, 0), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Gus Salazar'),
      StringStruct('FileDescription', 'GSExplorer+ dual-pane file manager'),
      StringStruct('FileVersion', '$VERSION'),
      StringStruct('InternalName', 'GSExplorer+'),
      StringStruct('LegalCopyright', '(c) 2026 Gus Salazar. GPL-3.0-or-later.'),
      StringStruct('OriginalFilename', '$OUT_NAME.exe'),
      StringStruct('ProductName', 'GSExplorer+'),
      StringStruct('ProductVersion', '$VERSION')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
EOF

COMMON=(--noconfirm --clean --log-level WARN --windowed
    --paths "$(winpath "$ROOT/src")"
    --icon "$(winpath "$B/gsexplorer-plus.ico")"
    --add-data "$(winpath "$ROOT/src/gsexplorer/resources");gsexplorer/resources"
    --add-data "$(winpath "$ROOT/LICENSE");."
    --copy-metadata PySide6_Essentials --copy-metadata PySide6_Addons --copy-metadata shiboken6
    --hidden-import PySide6.QtSvg --hidden-import PySide6.QtMultimedia --hidden-import PySide6.QtMultimediaWidgets
    --hidden-import PySide6.QtNetwork
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc_data
    --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtOpenGL
    --exclude-module PySide6.QtDBus --exclude-module PySide6.QtSql --exclude-module PySide6.QtTest
    --exclude-module PySide6.QtConcurrent --exclude-module PySide6.QtWebEngineCore
    --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtCharts
    --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtVirtualKeyboard
    --exclude-module PySide6.QtGraphs --exclude-module PySide6.QtPdf --exclude-module PySide6.QtQuick3D)

# ---------------------------------------------------------------- 4. one-dir freeze + license inventory
log "Freezing one-dir bundle (license inventory)"
rm -rf "$B/pyi" "$B/frozen"
wpy -m PyInstaller "${COMMON[@]}" --name gsexplorer-plus --onedir \
    --workpath "$(winpath "$B/pyi")" --distpath "$(winpath "$B/frozen")" --specpath "$(winpath "$B/pyi")" \
    "$(winpath "$ROOT/packaging/launcher.py")" | grep -vE "^\s*$" | tail -5
ONEDIR="$B/frozen/gsexplorer-plus"
[ -f "$ONEDIR/gsexplorer-plus.exe" ] || die "PyInstaller one-dir output missing"
INTERNAL="$ONEDIR/_internal"; [ -d "$INTERNAL" ] || INTERNAL="$ONEDIR"
[ ! -e "$INTERNAL/icuuc.dll" ] || die "the test-only icuuc.dll stub was bundled"

log "Collecting third-party licenses"
rm -rf "$B/share" && mkdir -p "$B/share"
set +e
(cd "$ROOT" && GX_COMMON_LICENSES=/usr/share/common-licenses wpy packaging/collect_licenses.py \
    "$(winpath "$ONEDIR")" --out "$(winpath "$B/share/licenses")") | tee "$B/licenses.log" | grep -E "^(Components|BLOCKER|WARNING)"
set -e
grep -q "^BLOCKER" "$B/licenses.log" && die "licensing blockers (see above)"
[ -f "$B/share/licenses/licenses.json" ] || die "licenses.json not produced"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '{"version": "%s", "commit": "%s", "build_date": "%s", "platform": "windows-x64", "python": "3.12", "pyside6": "6.12.0"}\n' \
    "$VERSION" "$COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$B/share/build-info.json"

# ---------------------------------------------------------------- 5. one-file freeze
log "Freezing one-file .exe"
rm -rf "$B/pyi1" "$B/onefile"
wpy -m PyInstaller "${COMMON[@]}" --name "$OUT_NAME" --onefile \
    --version-file "$(winpath "$B/version_info.txt")" \
    --add-data "$(winpath "$B/share");share" \
    --workpath "$(winpath "$B/pyi1")" --distpath "$(winpath "$B/onefile")" --specpath "$(winpath "$B/pyi1")" \
    "$(winpath "$ROOT/packaging/launcher.py")" | grep -vE "^\s*$" | tail -5
EXE="$B/onefile/$OUT_NAME.exe"
[ -f "$EXE" ] || die "one-file .exe missing"

# ---------------------------------------------------------------- 6. checks
log "Checking the .exe"
cat > "$B/toc_check.py" <<'PY'
import os, sys
from PyInstaller.archive.readers import CArchiveReader
exe = sys.argv[1]
names = [n.replace("\\", "/") for n in CArchiveReader(exe).toc]
low = [os.path.basename(n).lower() for n in names]
bad = sorted({n for n in low if n == "icuuc.dll" or n.startswith(("qt6charts", "qt6datavisualization", "qt6virtualkeyboard"))})
need = [n for n in ("share/licenses/licenses.json", "share/build-info.json", "LICENSE",
                    "gsexplorer/resources/gsexplorer-plus.svg") if n not in names]
print("    %d entries, %.1f MB; forbidden: %s; missing: %s"
      % (len(names), os.path.getsize(exe) / 1e6, bad or "none", need or "none"))
sys.exit(1 if bad or need else 0)
PY
wpy "$(winpath "$B/toc_check.py")" "$(winpath "$EXE")" | tee "$B/toc.log"
grep -q "forbidden: none; missing: none" "$B/toc.log" || die "archive content check failed"
st="$(QT_QPA_PLATFORM=offscreen wine "$EXE" --selftest < /dev/null 2>&1 | tr -d '\r' | grep -E "SELFTEST" | tail -1)"
echo "    $st"
[ "$st" = "SELFTEST OK" ] || die ".exe selftest failed"
cp "$EXE" "$DIST/$OUT_NAME.exe"
(cd "$DIST" && sha256sum "$OUT_NAME.exe" > "$OUT_NAME.exe.sha256")
rm -rf "$DIST/${OUT_NAME}_licenses" && cp -a "$B/share/licenses" "$DIST/${OUT_NAME}_licenses"
log "Done: $DIST/$OUT_NAME.exe"
cat "$DIST/$OUT_NAME.exe.sha256"
