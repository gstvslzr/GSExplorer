#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""
GSExplorer+ license inventory — packaging/collect_licenses.py

Run at BUILD time against the frozen bundle (e.g. build/frozen/gsexplorer-plus or /opt/gsexplorer-plus).
It produces the files the About dialog and the package need:

    <out>/licenses.json              machine-readable inventory (read by the About dialog)
    <out>/THIRD-PARTY-NOTICES.txt    human-readable notices, shipped in the package
    <out>/<component>/...            copies of every license file found

and it FAILS the build (exit 2) on licensing blockers:

    * GPL-only Qt modules in the bundle (Qt Charts, Qt Data Visualization,
      Qt Virtual Keyboard are GPLv3-only for open-source use per the Qt FAQ)
    * an FFmpeg build that reports a GPL or "nonfree" license string
    * the bundle could not be read

Warnings (exit 1 with --strict, else 0) cover components with no license file
or an unknown license.

Usage:
    python3 packaging/collect_licenses.py BUNDLE_DIR [--out OUT_DIR] [--strict]

Note: to read FFmpeg's real license string the script loads the bundle's own
libavcodec/libavutil with ctypes. Run it only on bundles you built yourself.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import email.parser
import json
import os
import re
import shutil
import sys
from pathlib import Path

SCRIPT_VERSION = "26.8.0"

# Qt modules that are GPLv3-only for open-source users (Qt FAQ 3.7).
GPL_ONLY_QT = {
    "Qt Charts": re.compile(r"(^|/)(lib)?Qt6Charts[^/]*\.so|QtCharts\.(abi3\.)?so|QtCharts\.cpython", re.I),
    "Qt Data Visualization": re.compile(r"(^|/)(lib)?Qt6DataVisualization[^/]*\.so|QtDataVisualization\.(abi3\.)?so|QtDataVisualization\.cpython", re.I),
    "Qt Virtual Keyboard": re.compile(r"(^|/)(lib)?Qt6VirtualKeyboard[^/]*\.so|QtVirtualKeyboard\.(abi3\.)?so|qtvirtualkeyboardplugin", re.I),
}

LICENSE_NAME_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|COPYRIGHT|AUTHORS)([._-].*)?$", re.I)

# Well-known components whose license metadata in wheels is incomplete or absent.
KNOWN = {
    "pyside6": {"spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only", "homepage": "https://wiki.qt.io/Qt_for_Python"},
    "pyside6-essentials": {"spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only", "homepage": "https://wiki.qt.io/Qt_for_Python"},
    "pyside6-addons": {"spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only", "homepage": "https://wiki.qt.io/Qt_for_Python"},
    "shiboken6": {"spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only", "homepage": "https://wiki.qt.io/Qt_for_Python"},
    "pyinstaller": {"spdx": "GPL-2.0-or-later WITH Bootloader-exception", "homepage": "https://pyinstaller.org"},
}

TROVE_TO_SPDX = {
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "Apache-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "ISC License (ISCL)": "ISC",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "component"


def _read_metadata(dist_info: Path) -> dict:
    meta_file = dist_info / "METADATA"
    if not meta_file.is_file():
        meta_file = dist_info / "PKG-INFO"
    if not meta_file.is_file():
        return {}
    msg = email.parser.Parser().parsestr(meta_file.read_text(encoding="utf-8", errors="replace"), headersonly=True)
    out = {
        "name": msg.get("Name", dist_info.name.split("-")[0]),
        "version": msg.get("Version", ""),
        "license_expression": msg.get("License-Expression", "") or "",
        "license": (msg.get("License", "") or "").strip(),
        "homepage": msg.get("Home-page", "") or "",
        "classifiers": msg.get_all("Classifier") or [],
        "license_files": msg.get_all("License-File") or [],
    }
    if not out["homepage"]:
        for url in msg.get_all("Project-URL") or []:
            label, _, link = url.partition(",")
            if label.strip().lower() in ("homepage", "home", "source", "repository"):
                out["homepage"] = link.strip()
                break
    return out


def _spdx_for(meta: dict) -> str:
    key = meta.get("name", "").lower()
    if key in KNOWN:
        return KNOWN[key]["spdx"]
    if meta.get("license_expression"):
        return meta["license_expression"]
    lic = meta.get("license", "")
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    for c in meta.get("classifiers", []):
        if c.startswith("License ::"):
            tail = c.split("::")[-1].strip()
            return TROVE_TO_SPDX.get(tail, tail)
    return ""


def _license_files_in(directory: Path) -> list[Path]:
    found = []
    if not directory.is_dir():
        return found
    for p in sorted(directory.rglob("*")):
        if p.is_file() and not p.is_symlink() and LICENSE_NAME_RE.match(p.name):
            found.append(p)
    return found


def _copy_licenses(files: list[Path], dest_root: Path, component: str, base: Path) -> list[str]:
    rel_paths = []
    dest_dir = dest_root / _slug(component)
    for f in files:
        try:
            rel_src = f.relative_to(base)
        except ValueError:
            rel_src = Path(f.name)
        target = dest_dir / str(rel_src).replace(os.sep, "__")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        rel_paths.append(str(target.relative_to(dest_root)))
    return rel_paths


def scan_wheels(bundle: Path, out: Path, components: list, warnings: list) -> None:
    seen = set()
    for dist_info in sorted(bundle.rglob("*.dist-info")):
        if not dist_info.is_dir():
            continue
        meta = _read_metadata(dist_info)
        if not meta:
            continue
        key = (meta["name"].lower(), meta["version"])
        if key in seen:
            continue
        seen.add(key)
        files = _license_files_in(dist_info)
        spdx = _spdx_for(meta)
        homepage = meta["homepage"] or KNOWN.get(meta["name"].lower(), {}).get("homepage", "")
        rel = _copy_licenses(files, out, meta["name"], dist_info.parent)
        comp = {
            "name": meta["name"],
            "version": meta["version"],
            "spdx": spdx or "UNKNOWN",
            "homepage": homepage,
            "kind": "python-package",
            "license_files": rel,
            "source": str(dist_info.relative_to(bundle)),
        }
        components.append(comp)
        if not spdx:
            warnings.append(f"{meta['name']} {meta['version']}: license not declared in metadata")
        if not rel:
            warnings.append(f"{meta['name']} {meta['version']}: no license file in {dist_info.name}")


def scan_qt(bundle: Path, components: list, blockers: list, warnings: list) -> None:
    so_files = [p for p in bundle.rglob("*") if p.is_file() and (".so" in p.name)]
    rels = [str(p.relative_to(bundle)) for p in so_files]
    qt_libs = sorted({Path(r).name for r in rels if re.search(r"libQt6[A-Za-z0-9]+\.so", Path(r).name)})
    if qt_libs:
        if not any(c["name"].lower().startswith("pyside6") for c in components):
            components.append({
                "name": "Qt 6 libraries", "version": "", "spdx": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
                "homepage": "https://www.qt.io", "kind": "native-library", "license_files": [],
                "source": f"{len(qt_libs)} libQt6*.so files",
            })
            warnings.append("Qt libraries found but no PySide6 dist-info: add the LGPL-3.0 text manually")
    for module, rx in GPL_ONLY_QT.items():
        hits = [r for r in rels if rx.search(r)]
        if hits:
            blockers.append(f"GPL-only {module} is in the bundle ({hits[0]}{' …' if len(hits) > 1 else ''}); "
                            f"exclude it in the PyInstaller spec / prune_bundle.py (debian/copyright declares it absent)")
    return None


# FFmpeg's possible license strings (FFMPEG_LICENSE in its configure script).
_FFMPEG_LICENSES = [b"nonfree and unredistributable", b"LGPL version 3 or later", b"LGPL version 2.1 or later",
                    b"GPL version 3 or later", b"GPL version 2 or later"]


def _ffmpeg_static(lib: Path) -> tuple[str, str]:
    """Read the license and configure strings from the binary without loading it."""
    try:
        blob = lib.read_bytes()
    except OSError:
        return "", ""
    lic = ""
    for cand in _FFMPEG_LICENSES:           # order matters: "LGPL…" before "GPL…"
        if cand in blob:
            lic = cand.decode()
            break
    conf = ""
    m = re.search(rb"--(?:prefix|arch|target-os|disable|enable)[\x20-\x7e]{20,4000}", blob)
    if m:
        conf = m.group(0).decode("ascii", "replace")
    return lic, conf


def _ffmpeg_query(lib: Path, func: str) -> str:
    """Fallback: call into the library. Preloads libcrypto and sibling libs (RTLD_GLOBAL)."""
    import ctypes.util
    try:
        crypto = ctypes.util.find_library("crypto")
        if crypto:
            ctypes.CDLL(crypto, mode=ctypes.RTLD_GLOBAL)
        for sib in ("libavutil", "libswresample", "libswscale"):
            for p in sorted(lib.parent.glob(f"{sib}.so.*")):
                ctypes.CDLL(str(p), mode=ctypes.RTLD_GLOBAL)
                break
        handle = ctypes.CDLL(str(lib))
        fn = getattr(handle, func)
        fn.restype = ctypes.c_char_p
        fn.argtypes = []
        val = fn()
        return val.decode("utf-8", "replace") if val else ""
    except (OSError, AttributeError):
        return ""


# Standard license texts on Debian/Ubuntu build hosts (package base-files).
COMMON_LICENSES = Path("/usr/share/common-licenses")
SPDX_TO_COMMON = [  # (substring in SPDX expression, files to ship)
    ("LGPL-3.0", ["LGPL-3", "GPL-3"]),   # LGPLv3 4(b): ship the GPL text with the LGPL text
    ("LGPL-2.1", ["LGPL-2.1"]),
    ("GPL-3.0", ["GPL-3"]),
    ("GPL-2.0", ["GPL-2"]),
    ("Apache-2.0", ["Apache-2.0"]),
    ("MPL-2.0", ["MPL-2.0"]),
]


def add_standard_texts(out: Path, components: list, warnings: list) -> None:
    """Give every component without a shipped license file the standard text(s) for its SPDX id."""
    for c in components:
        if c.get("license_files"):
            continue
        spdx = c.get("spdx", "")
        wanted = []
        for key, files in SPDX_TO_COMMON:
            if key in spdx:
                wanted = files
                break
        if not wanted:
            continue
        rel = []
        for name in wanted:
            src = COMMON_LICENSES / name
            if not src.is_file():
                warnings.append(f"{c['name']}: {src} not found on this build host; add the {name} text manually")
                continue
            dest = out / _slug(c["name"]) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            rel.append(str(dest.relative_to(out)))
        if rel:
            c["license_files"] = rel
            c.setdefault("notes", "")
            c["notes"] = (c["notes"] + "; " if c["notes"] else "") + \
                "Upstream ships no license file; standard text(s) added from /usr/share/common-licenses."
            warnings[:] = [w for w in warnings if not w.startswith(f"{c['name']} ") or "no license file" not in w]


def scan_ffmpeg(bundle: Path, components: list, blockers: list, warnings: list) -> None:
    libs = sorted((p for p in bundle.rglob("libavcodec*.so*") if p.is_file() and not p.is_symlink()),
                  key=lambda p: -len(p.name))
    if not libs:
        libs = sorted(p for p in bundle.rglob("libavcodec*.so*") if p.is_file())
    if not libs:
        return
    lib = libs[0]
    lic, conf = _ffmpeg_static(lib)
    if not lic:
        lic = _ffmpeg_query(lib, "avcodec_license")
    if not conf:
        conf = _ffmpeg_query(lib, "avcodec_configuration")
    m = re.search(r"libavcodec\.so\.(\d+\.\d+\.\d+)", lib.name)
    ver = f"libavcodec {m.group(1)}" if m else ""
    rel = re.search(r"FFmpeg-n?(\d+(?:\.\d+)+)", conf)
    if rel:
        ver = f"{rel.group(1)} ({ver})" if ver else rel.group(1)
    spdx = "UNKNOWN"
    if lic:
        low = lic.lower()
        if "nonfree" in low:
            spdx = "LicenseRef-FFmpeg-nonfree"
            blockers.append(f"FFmpeg reports '{lic}': nonfree builds are not redistributable")
        elif "lgpl" in low:
            spdx = "LGPL-3.0-or-later" if "3" in low else "LGPL-2.1-or-later"
        elif "gpl" in low:
            spdx = "GPL-3.0-or-later" if "3" in low else "GPL-2.0-or-later"
            blockers.append(f"FFmpeg reports '{lic}': a GPL FFmpeg makes the combined app GPL — "
                            f"debian/copyright and the About box declare an LGPL FFmpeg — update them before shipping")
    else:
        warnings.append(f"Could not read FFmpeg license string from {lib.name}; verify its license manually")
    if "--enable-gpl" in conf and not any("FFmpeg" in b for b in blockers):
        blockers.append("FFmpeg configuration contains --enable-gpl")
    components.append({
        "name": "FFmpeg", "version": ver, "spdx": spdx, "homepage": "https://ffmpeg.org",
        "kind": "native-library", "license_files": [], "source": str(lib.relative_to(bundle)),
        "notes": f"avcodec_license(): {lic or 'n/a'}",
        "configuration": conf,
    })
    warnings.append("FFmpeg: host the matching FFmpeg source on your own server (LGPL source obligation)")


def scan_python(bundle: Path, out: Path, components: list, warnings: list) -> None:
    libpy = sorted(p for p in bundle.rglob("libpython3*.so*") if p.is_file())
    if not libpy:
        return
    m = re.search(r"libpython(3\.\d+)", libpy[0].name)
    ver = m.group(1) if m else ""
    lic_candidates = [Path(sys.base_prefix) / "lib" / f"python{ver}" / "LICENSE.txt",
                      Path("/usr/lib") / f"python{ver}" / "LICENSE.txt",
                      Path("/usr/share/doc") / f"python{ver}" / "copyright"]
    lic_files = [p for p in lic_candidates if p.is_file()][:1]
    rel = []
    if lic_files:
        dest = out / "cpython"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lic_files[0], dest / lic_files[0].name)
        rel = [f"cpython/{lic_files[0].name}"]
    else:
        warnings.append(f"CPython {ver}: PSF license text not found on the build host; copy it from python.org")
    components.append({
        "name": "CPython", "version": ver, "spdx": "PSF-2.0", "homepage": "https://www.python.org",
        "kind": "runtime", "license_files": rel, "source": str(libpy[0].relative_to(bundle)),
    })


def write_notices(out: Path, data: dict) -> Path:
    lines = [
        "GSExplorer+ — third-party software notices",
        f"Generated {data['generated']} by packaging/collect_licenses.py",
        "",
        "GSExplorer+ bundles the components below. Each keeps its own license.",
        "The full license texts are in the folders next to this file.",
        "",
    ]
    for c in sorted(data["components"], key=lambda c: c["name"].lower()):
        lines.append("=" * 72)
        lines.append(f"{c['name']} {c.get('version', '')}".rstrip())
        lines.append(f"License:  {c['spdx']}")
        if c.get("homepage"):
            lines.append(f"Homepage: {c['homepage']}")
        if c.get("notes"):
            lines.append(f"Notes:    {c['notes']}")
        for lf in c.get("license_files", []):
            lines.append(f"Text:     {lf}")
    lines.append("=" * 72)
    target = out / "THIRD-PARTY-NOTICES.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inventory licenses in a frozen GSExplorer+ bundle.")
    ap.add_argument("bundle", type=Path, help="bundle directory (e.g. build/frozen/gsexplorer-plus or /opt/gsexplorer-plus)")
    ap.add_argument("--out", type=Path, help="output directory (default: BUNDLE/share/licenses)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when there are warnings")
    args = ap.parse_args(argv)

    bundle = args.bundle.resolve()
    if not bundle.is_dir():
        print(f"error: {bundle} is not a directory", file=sys.stderr)
        return 2
    out = (args.out or bundle / "share" / "licenses").resolve()
    out.mkdir(parents=True, exist_ok=True)

    components: list = []
    blockers: list = []
    warnings: list = []
    scan_wheels(bundle, out, components, warnings)
    scan_qt(bundle, components, blockers, warnings)
    scan_ffmpeg(bundle, components, blockers, warnings)
    scan_python(bundle, out, components, warnings)
    add_standard_texts(out, components, warnings)
    if any(c["name"].lower().startswith(("pyside6", "shiboken6", "qt 6")) for c in components):
        warnings.append("Qt/PySide6: host the matching Qt and PySide6 source on your own server (LGPL source obligation)")

    data = {
        "schema": 1,
        "generated": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "bundle": str(bundle),
        "components": components,
        "blockers": blockers,
        "warnings": warnings,
    }
    (out / "licenses.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    notices = write_notices(out, data)

    print(f"Components: {len(components)}  Warnings: {len(warnings)}  Blockers: {len(blockers)}")
    print(f"Wrote {out / 'licenses.json'}")
    print(f"Wrote {notices}")
    for w in warnings:
        print(f"WARNING: {w}")
    for b in blockers:
        print(f"BLOCKER: {b}")
    if blockers:
        return 2
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
