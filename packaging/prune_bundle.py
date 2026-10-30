#!/usr/bin/env python3
"""Prune a PyInstaller onedir bundle and compute Debian Depends from what's left.

Policy (GSExplorer+ 26.0.1):
  * Keep: Python runtime, PySide6/Shiboken, Qt 6 libraries, ICU (Qt's private copy).
  * Drop: every *system* library PyInstaller copied from the build host (X11, GTK,
    glib, libstdc++ ...). Those come from Ubuntu packages listed in Depends, so
    security updates apply and system GL drivers never meet a mismatched libstdc++.
  * Drop Qt plugins a desktop file manager doesn't need. Notably the TUIO touch
    plugin, which listens on a UDP socket. (QtNetwork itself stays: Qt Multimedia needs it.)

Then walk ELF DT_NEEDED from every remaining binary, resolve sonames against the
bundle and the system (ldconfig), map system files to packages (dpkg -S), and
print the Depends list. Unresolvable sonames fail the build unless known.

Usage: prune_bundle.py <bundle_dir>   -> prints "Depends: ..." payload on stdout
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

KEEP_RE = re.compile(r"^(libQt6|libpyside6|libshiboken6|libpython3|libicu|libav|libsw)")
DROP_PLUGINS = [
    "generic",                                   # evdev + TUIO (network) input plugins
    "platformthemes/libqgtk3.so",                # would drag in GTK3; we theme ourselves
    "platforms/libqvnc.so", "platforms/libqlinuxfb.so", "platforms/libqeglfs.so",
    "platforms/libqvkkhrdisplay.so", "platforms/libqminimalegl.so",
    "networkinformation", "tls", "sqldrivers", "qmltooling", "designer", "egldeviceintegrations",
    "wayland-graphics-integration-server",
]
# libQt6Network stays: Qt Multimedia links it (GSExplorer+ code never uses it).
DROP_QT_LIBS = ["libQt6EglFSDeviceIntegration.so.6", "libQt6EglFsKmsSupport.so.6"]
# Sonames that may be absent on the build host but exist on Ubuntu 24.04.
KNOWN_PKG = {"libxcb-cursor.so.0": "libxcb-cursor0"}
# Libraries that are part of the dynamic loader / libc itself.
LIBC_SONAMES = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
                "libutil.so.1", "ld-linux-x86-64.so.2", "libresolv.so.2"}


def readelf_needed(path: str) -> list[str]:
    try:
        out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True, check=False).stdout
    except OSError:
        return []
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", out)


def is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def system_libs() -> dict[str, str]:
    out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    libs: dict[str, str] = {}
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\S+) \((.*?)\) => (\S+)", line)
        if m and "x86-64" in m.group(2):
            libs.setdefault(m.group(1), m.group(3))
    return libs


LIB_DIRS = ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib64", "/usr/lib64", "/lib", "/usr/lib")


def _find_lib(so: str) -> str:
    for d in LIB_DIRS:
        p = os.path.join(d, so)
        if os.path.exists(p):
            return p
    return ""


def main() -> int:
    bundle = os.path.abspath(sys.argv[1])
    internal = os.path.join(bundle, "_internal")
    qt = os.path.join(internal, "PySide6", "Qt")

    # 1. drop unneeded plugins and Qt libs
    for p in DROP_PLUGINS:
        full = os.path.join(qt, "plugins", p)
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.lexists(full):
            os.unlink(full)
    for lib in DROP_QT_LIBS:
        for d in (internal, os.path.join(qt, "lib")):
            p = os.path.join(d, lib)
            if os.path.lexists(p):
                os.unlink(p)

    # 2. drop host system libraries copied into the bundle root
    dropped = []
    for name in sorted(os.listdir(internal)):
        p = os.path.join(internal, name)
        if name.startswith("lib") and ".so" in name and not KEEP_RE.match(name):
            os.unlink(p)
            dropped.append(name)
    print(f"pruned {len(dropped)} host system libraries from bundle", file=sys.stderr)

    # 3. resolve every NEEDED soname
    bundled = {}
    for d in (internal, os.path.join(qt, "lib")):
        for n in os.listdir(d):
            bundled.setdefault(n, os.path.join(d, n))
    sysl = system_libs()
    pkgs: set[str] = set()
    unresolved: dict[str, list[str]] = {}
    files = []
    for root, _dirs, fs in os.walk(bundle):
        for f in fs:
            p = os.path.join(root, f)
            if not os.path.islink(p) and is_elf(p):
                files.append(p)
    needed_sys: set[str] = set()
    for f in files:
        for so in readelf_needed(f):
            if so in bundled or so in LIBC_SONAMES:
                continue
            if so in sysl:
                needed_sys.add(sysl[so])
            elif _find_lib(so):
                needed_sys.add(_find_lib(so))      # e.g. libbz2.so.1: a symlink, not a SONAME
            elif so in KNOWN_PKG:
                pkgs.add(KNOWN_PKG[so])
            else:
                unresolved.setdefault(so, []).append(os.path.relpath(f, bundle))
    if unresolved:
        for so, users in sorted(unresolved.items()):
            print(f"UNRESOLVED {so} (needed by {', '.join(users[:3])})", file=sys.stderr)
        return 2
    for path in sorted(needed_sys):
        real = os.path.realpath(path)
        for cand in (path, real, real.replace("/usr/lib/", "/lib/", 1), real.replace("/lib/", "/usr/lib/", 1)):
            r = subprocess.run(["dpkg", "-S", cand], capture_output=True, text=True)
            if r.returncode == 0:
                pkg = r.stdout.split(":")[0].strip()
                pkgs.add(pkg)
                break
        else:
            print(f"NO PACKAGE for {path}", file=sys.stderr)
            return 3
    pkgs.discard("libc6")
    print(", ".join(sorted(pkgs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
