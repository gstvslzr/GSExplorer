"""Archive engine tests: round trips, malicious archives, passwords, cancel, space guard.

Run: python tests/test_archives.py     (7z / zstd parts run only when those tools are installed)
"""
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

H = tempfile.mkdtemp(prefix="gx-arch-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gsexplorer import archives as ar  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def tree(root):
    """{relative path: ('d'|'l:target'|'f:<bytes>:<exec>')}"""
    out = {}
    for r, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(r, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out[rel] = "l:" + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = "d"
            else:
                out[rel] = f"f:{open(p, 'rb').read()!r}:{bool(os.stat(p).st_mode & 0o100)}"
    return out


SRC = os.path.join(H, "src")
P = os.path.join(SRC, "Project Ü")
os.makedirs(os.path.join(P, "sub dir", "deeper"))
os.makedirs(os.path.join(P, "empty"))
open(os.path.join(P, "readme.txt"), "w").write("hello ñandú\n" * 100)
open(os.path.join(P, "sub dir", "data.bin"), "wb").write(os.urandom(300_000))
open(os.path.join(P, "sub dir", "deeper", "x.txt"), "w").write("x")
open(os.path.join(P, "run.sh"), "w").write("#!/bin/sh\necho hi\n")
os.chmod(os.path.join(P, "run.sh"), 0o755)
os.symlink("readme.txt", os.path.join(P, "link-to-readme"))
open(os.path.join(SRC, "loose.txt"), "w").write("loose file")
expected = tree(SRC)
expected = {k: v for k, v in expected.items()}

check("kind detection", ar.kind_of("a.TAR.GZ") == "tar.gz" and ar.kind_of("b.tgz") == "tar.gz"
      and ar.kind_of("c.zip") == "zip" and ar.kind_of("d.rar") == "7z" and ar.kind_of("e.txt") == ""
      and ar.strip_suffix("Photos.tar.xz") == "Photos")

# ---------------------------------------------------------------- round trips, every available format
for fid, label, ext, need in ar.available_formats():
    out = os.path.join(H, "out", fid)
    os.makedirs(out)
    arc = os.path.join(out, "bundle" + ext)
    prog = []
    ar.compress([P, os.path.join(SRC, "loose.txt")], arc, fid, progress=lambda d, t, n: prog.append((d, t)))
    dest = os.path.join(out, "x")
    made, warns = ar.extract(arc, dest, mode="smart")
    got = tree(made)
    check(f"{fid}: round trip identical (names, contents, empty dir, exec bit, symlink)", got == expected)
    check(f"{fid}: two top-level items -> folder named after the archive", made == os.path.join(dest, "bundle"))
    check(f"{fid}: progress reported", prog and prog[-1][0] > 0)
    um = os.umask(0); os.umask(um)
    check(f"{fid}: archive gets normal permissions (umask), not 0600", stat.S_IMODE(os.stat(arc).st_mode) == 0o666 & ~um)
    check(f"{fid}: no temp files left", not [n for n in os.listdir(out) if n.startswith(".gsexplorer-plus")]
          and not [n for n in os.listdir(dest) if n.startswith(".gsexplorer-plus")])

# smart mode with a single top-level folder, and name clashes
arc = os.path.join(H, "one.zip")
ar.compress([P], arc, "zip")
d2 = os.path.join(H, "smart")
m1, _ = ar.extract(arc, d2, "smart")
m2, _ = ar.extract(arc, d2, "smart")
m3, _ = ar.extract(arc, d2, "folder")
check("smart: single folder goes straight into the destination", m1 == os.path.join(d2, "Project Ü"))
check("never overwrites: second extract gets ' (2)'", m2 == os.path.join(d2, "Project Ü (2)") and tree(m1) == tree(m2))
check("'Extract to folder' always makes a folder named after the archive", m3 == os.path.join(d2, "one")
      and os.path.isdir(os.path.join(m3, "Project Ü")))
check("unique archive name when it exists", ar.unique_path(arc).endswith("one (2).zip"))

# single-file compressed files
open(os.path.join(H, "notes.txt"), "w").write("compressed alone")
import gzip  # noqa: E402
with gzip.open(os.path.join(H, "notes.txt.gz"), "wb") as f:
    f.write(b"compressed alone")
m, _ = ar.extract(os.path.join(H, "notes.txt.gz"), os.path.join(H, "gz"))
check(".gz single file extracts to its original name", m.endswith("notes.txt") and open(m).read() == "compressed alone")

# ---------------------------------------------------------------- malicious archives
EVIL = os.path.join(H, "evil")
os.makedirs(EVIL)
victim_dir = os.path.join(H, "victim")
os.makedirs(victim_dir)

zs = os.path.join(EVIL, "slip.zip")
with zipfile.ZipFile(zs, "w") as z:
    z.writestr("ok.txt", "fine")
    z.writestr("../../victim/pwned.txt", "bad")
    z.writestr("/tmp/abs-pwned.txt", "bad")
    zi = zipfile.ZipInfo("escape-link")
    zi.create_system = 3
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    z.writestr(zi, "../../../etc/passwd")
m, warns = ar.extract(zs, os.path.join(EVIL, "z"))
check("zip slip: '../' and absolute entries are not written outside",
      not os.path.exists(os.path.join(victim_dir, "pwned.txt")) and not os.path.exists("/tmp/abs-pwned.txt")
      and len([w for w in warns if "unsafe path" in w]) == 2)
check("zip: symlink pointing outside is not created", not os.path.lexists(os.path.join(m, "escape-link"))
      and any("outside" in w for w in warns))
check("zip: the safe entry is still extracted", open(os.path.join(m if os.path.isdir(m) else os.path.dirname(m),
                                                                    "ok.txt") if os.path.isdir(m) else m).read() == "fine")

ts = os.path.join(EVIL, "slip.tar.gz")
with tarfile.open(ts, "w:gz") as t:
    for name, data in (("good.txt", b"fine"), ("../victim/tar-pwned.txt", b"bad"), ("/tmp/tar-abs.txt", b"bad")):
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    ln = tarfile.TarInfo("evil-link")
    ln.type = tarfile.SYMTYPE
    ln.linkname = "/etc/passwd"
    t.addfile(ln)
    dev = tarfile.TarInfo("evil-dev")
    dev.type = tarfile.CHRTYPE
    dev.devmajor, dev.devminor = 1, 3
    t.addfile(dev)
    su = tarfile.TarInfo("setuid-prog")
    su.size, su.mode = 2, 0o4755
    t.addfile(su, io.BytesIO(b"#!"))
m, warns = ar.extract(ts, os.path.join(EVIL, "t"))
check("tar: '../' and absolute members refused", not os.path.exists(os.path.join(victim_dir, "tar-pwned.txt"))
      and not os.path.exists("/tmp/tar-abs.txt"))
check("tar: absolute symlink and device file refused", not os.path.lexists(os.path.join(m, "evil-link"))
      and not os.path.lexists(os.path.join(m, "evil-dev")))
check("tar: setuid bit dropped", os.path.exists(os.path.join(m, "setuid-prog"))
      and not os.stat(os.path.join(m, "setuid-prog")).st_mode & stat.S_ISUID)
check("tar: good member extracted", open(os.path.join(m, "good.txt")).read() == "fine")

check("path checker refuses absolute, '..' and drive paths; keeps normal ones",
      [ar._safe_rel(x) for x in ("../a", "a/../../b", "/etc/x", "C:/w", "a/./b", "dir/f.txt", "", "./")]
      == [None, None, None, None, os.path.join("a", "b"), os.path.join("dir", "f.txt"), None, None])

# ---------------------------------------------------------------- disk space guard (zip bomb)
old = ar.MIN_FREE
ar.MIN_FREE = shutil.disk_usage(H).free + 1
try:
    ar.extract(os.path.join(H, "one.zip"), os.path.join(H, "full"))
    stopped = False
except ar.ArchiveError as e:
    stopped = "almost full" in str(e)
ar.MIN_FREE = old
check("stops before filling the disk; nothing left behind", stopped
      and not [n for n in os.listdir(os.path.join(H, "full"))])

# ---------------------------------------------------------------- cancel
class Stop(Exception):
    pass


calls = [0]


def stopper():
    calls[0] += 1
    if calls[0] > 3:
        raise ar.Cancelled()


big = os.path.join(H, "big")
os.makedirs(big)
for i in range(5):
    open(os.path.join(big, f"f{i}.bin"), "wb").write(os.urandom(2_000_000))
try:
    ar.compress([big], os.path.join(H, "cancel.zip"), "zip", check=stopper)
    c1 = False
except ar.Cancelled:
    c1 = True
check("cancel while compressing: no archive and no temp file left", c1 and not os.path.exists(os.path.join(H, "cancel.zip"))
      and not [n for n in os.listdir(H) if n.startswith(".gsexplorer-plus")])
ar.compress([big], os.path.join(H, "big.tar.gz"), "tar.gz")
calls[0] = 0
try:
    ar.extract(os.path.join(H, "big.tar.gz"), os.path.join(H, "cx"), check=stopper)
    c2 = False
except ar.Cancelled:
    c2 = True
check("cancel while extracting: nothing half-written in the destination", c2 and os.listdir(os.path.join(H, "cx")) == [])

# ---------------------------------------------------------------- passwords (7-Zip needed to create them)
if ar.tool("7z"):
    seven = ar.tool("7z")
    pz = os.path.join(H, "secret.zip")
    subprocess.run([seven, "a", "-tzip", "-psecret", "-mem=ZipCrypto", pz, os.path.join(SRC, "loose.txt")],
                   capture_output=True, check=True)
    check("encrypted zip detected", ar.needs_password(pz))
    try:
        ar.extract(pz, os.path.join(H, "pz1"))
        np1 = False
    except ar.NeedsPassword:
        np1 = True
    try:
        ar.extract(pz, os.path.join(H, "pz2"), password="wrong")
        np2 = False
    except ar.NeedsPassword:
        np2 = True
    m, _ = ar.extract(pz, os.path.join(H, "pz3"), password="secret")
    check("encrypted zip: asks, rejects a wrong password, opens with the right one",
          np1 and np2 and open(m).read() == "loose file" and os.listdir(os.path.join(H, "pz2")) == [])
    p7 = os.path.join(H, "secret.7z")
    ar.compress([P], p7, "7z", password="s3cr3t")
    check("7z with password: names encrypted too (listing needs the password)", ar.needs_password(p7))
    try:
        ar.extract(p7, os.path.join(H, "p7a"), password="nope")
        bad = False
    except ar.NeedsPassword:
        bad = True
    m, _ = ar.extract(p7, os.path.join(H, "p7b"), password="s3cr3t")
    check("7z: wrong password refused, right one restores the folder", bad and tree(m) == tree(P))
    zs7 = os.path.join(EVIL, "slip.7z")
    os.makedirs(os.path.join(EVIL, "s7", "a"))
    open(os.path.join(EVIL, "s7", "a", "f.txt"), "w").write("x")
    subprocess.run([seven, "a", zs7, "-spf2", "../s7/a/f.txt"], cwd=os.path.join(EVIL, "s7", "a"), capture_output=True)
    lst = subprocess.run([seven, "l", "-slt", "-ba", zs7], capture_output=True, text=True).stdout
    if "Path = ../" in lst or "Path = .." in lst:
        try:
            ar.extract(zs7, os.path.join(EVIL, "s7out"))
            refused = False
        except ar.ArchiveError as e:
            refused = "unsafe path" in str(e)
        check("7z: archive with '../' paths is refused before extracting", refused)
    else:
        print("SKIP 7z '../' case: this 7-Zip would not create such an archive")
else:
    print("SKIP password tests: 7-Zip not installed")

shutil.rmtree(H, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} archive checks passed")
sys.exit(0 if all(results) else 1)
