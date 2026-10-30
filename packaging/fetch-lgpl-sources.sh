#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Download the corresponding source of the LGPL libraries bundled in the GSExplorer+ .deb,
# so it can be attached to the GitHub release (LGPL-3.0 section 4 / GPL-3.0 section 6).
#
#   ./packaging/fetch-lgpl-sources.sh [OUT_DIR]      # default: dist/lgpl-sources
#
# Versions must match packaging/requirements-build.txt and the FFmpeg version reported in
# dist/licenses/licenses.json. The script stops if any download fails; it never guesses.
# Size: about 450 MB for 6.12.0 / 9.0.1 (the Qt "everywhere" archive is about 423 MB).
set -euo pipefail

QT_VER="6.12.0"
FFMPEG_VER="9.0.1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist/lgpl-sources}"
mkdir -p "$OUT"

QT_MM="${QT_VER%.*}"
URLS=(
  "https://download.qt.io/official_releases/qt/${QT_MM}/${QT_VER}/single/qt-everywhere-src-${QT_VER}.tar.xz"
  "https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-${QT_VER}-src/pyside-setup-everywhere-src-${QT_VER}.tar.xz"
  "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VER}.tar.xz"
  "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VER}.tar.xz.asc"
)

for u in "${URLS[@]}"; do
  f="$OUT/$(basename "$u")"
  if [ -s "$f" ]; then echo "have  $(basename "$f")"; continue; fi
  echo "fetch $u"
  curl -fL --retry 3 -o "$f.part" "$u" || { rm -f "$f.part"; echo "FAILED: $u (check the URL on the upstream site)" >&2; exit 1; }
  mv "$f.part" "$f"
done

# How the bundled FFmpeg was configured (from the build-time inventory), so the source is usable.
INV="${LICENSES_JSON:-$ROOT/dist/licenses/licenses.json}"   # override when running outside the source tree
if [ -f "$INV" ]; then
  python3 - "$INV" > "$OUT/FFMPEG-BUILD-CONFIGURATION.txt" <<'EOF'
import json, sys
for c in json.load(open(sys.argv[1]))["components"]:
    if c["name"].startswith("FFmpeg"):
        print(f"FFmpeg {c.get('version')} as shipped in the PySide6-Addons wheel")
        print(c.get("notes", ""))
        print("configure flags:", c.get("configuration", "(not recorded)"))
EOF
fi

cat > "$OUT/README-LGPL-SOURCES.txt" <<EOF
Corresponding source for the LGPL libraries bundled in GSExplorer+ (gsexplorer-plus .deb):

  qt-everywhere-src-${QT_VER}.tar.xz           Qt ${QT_VER} (LGPL-3.0)
  pyside-setup-everywhere-src-${QT_VER}.tar.xz Qt for Python / PySide6 / Shiboken6 ${QT_VER} (LGPL-3.0)
  ffmpeg-${FFMPEG_VER}.tar.xz (+ .asc)          FFmpeg ${FFMPEG_VER} (LGPL-2.1-or-later)
  FFMPEG-BUILD-CONFIGURATION.txt                configure flags of the bundled FFmpeg build

The binaries were taken unmodified from the official PySide6-Essentials / PySide6-Addons
${QT_VER} wheels on PyPI. They are installed as replaceable shared libraries in
/opt/gsexplorer-plus/_internal/PySide6/.
EOF

( cd "$OUT" && rm -f SHA256SUMS-lgpl-sources && sha256sum -- * > SHA256SUMS-lgpl-sources.tmp && mv SHA256SUMS-lgpl-sources.tmp SHA256SUMS-lgpl-sources )
echo "Done: $OUT"
ls -lh "$OUT"
