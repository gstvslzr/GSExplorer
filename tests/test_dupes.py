"""Find Duplicates engine tests. Run: python tests/test_dupes.py"""
import csv
import os
import shutil
import sys
import tempfile
import threading
import time

H = tempfile.mkdtemp(prefix="gx-dupes-")
os.environ.update(HOME=H, XDG_DATA_HOME=f"{H}/.local/share", XDG_CONFIG_HOME=f"{H}/.config")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gexplorer import dupes  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def w(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))


R = os.path.join(H, "Data")
big = os.urandom(300_000)
w(f"{R}/a/photo.jpg", big, 1_600_000_000)
w(f"{R}/b/photo copy.jpg", big, 1_700_000_000)
w(f"{R}/c/deep/IMG_1.jpg", big, 1_650_000_000)
# same size, same head and tail, different middle -> must NOT match
tricky = bytearray(big)
tricky[150_000] ^= 0xFF
w(f"{R}/c/tricky.jpg", bytes(tricky))
w(f"{R}/small1.txt", b"hello world")
w(f"{R}/x/small2.txt", b"hello world")
w(f"{R}/x/other.txt", b"hello WORLD")                     # same size, different content
w(f"{R}/empty1", b"")
w(f"{R}/empty2", b"")
w(f"{R}/.hidden/dup.txt", b"hello world")
os.link(f"{R}/a/photo.jpg", f"{R}/a/zz-hardlink.jpg")
os.symlink(f"{R}/a/photo.jpg", f"{R}/link.jpg")
os.mkfifo(f"{R}/pipe")
w(f"{R}/node_modules/x/small3.txt", b"hello world")
w(f"{R}/{dupes.AUDIT_PREFIX}old/small4.txt", b"hello world")

res = dupes.scan(dupes.Options(roots=[R, f"{R}/a"]))       # overlapping roots
by_key = {tuple(sorted(os.path.relpath(f.path, R) for f in g.files)): g for g in res.groups}
check("content groups found", ("a/photo.jpg", "b/photo copy.jpg", "c/deep/IMG_1.jpg") in by_key
      and ("node_modules/x/small3.txt", "small1.txt", "x/small2.txt") in by_key)
check("same size + same ends but different middle is not a duplicate",
      not any("c/tricky.jpg" in k for k in by_key))
check("same size, different content not grouped", not any("x/other.txt" in k for k in by_key))
check("hard link collapsed (deleting it frees nothing)",
      not any("a/zz-hardlink.jpg" in k for k in by_key)
      and any(f.links for g in res.groups for f in g.files))
check("symlinks, FIFOs, hidden, empty, our audit folders skipped",
      all(not any(x in p for p in k for x in ("link.jpg", "pipe", ".hidden", "empty", "small4")) for k in by_key))
check("overlapping roots don't list a file twice",
      sum(len(g.files) for g in res.groups) == len({f.path for g in res.groups for f in g.files}))
g_big = by_key[("a/photo.jpg", "b/photo copy.jpg", "c/deep/IMG_1.jpg")]
check("wasted space = size × (copies-1)", g_big.wasted == 300_000 * 2)

res_x = dupes.scan(dupes.Options(roots=[R], exclude="node_modules", hidden=True, min_size=0))
keys = [tuple(sorted(os.path.relpath(f.path, R) for f in g.files)) for g in res_x.groups]
check("include hidden + empty, exclude pattern",
      ("empty1", "empty2") in keys and any(".hidden/dup.txt" in k for k in keys)
      and not any("node_modules" in p for k in keys for p in k))
res_i = dupes.scan(dupes.Options(roots=[R], include="*.JPG"))
check("include filter (case-insensitive)", all(f.name.lower().endswith(".jpg") for g in res_i.groups for f in g.files)
      and res_i.groups)
res_h = dupes.scan(dupes.Options(roots=[R], hardlinks=True, include="*.jpg"))
gh = [g for g in res_h.groups if len(g.files) == 4]
check("hard links listed when asked; wasted still counts the inode once", gh and gh[0].wasted == 600_000)
res_n = dupes.scan(dupes.Options(roots=[R], mode="name", hidden=True, exclude="node_modules"))
check("name mode", not res_n.groups or all(len({f.name.lower() for f in g.files}) == 1 for g in res_n.groups))
w(f"{R}/n1/Report.PDF", b"aaa")
w(f"{R}/n2/report.pdf", b"bbbb")
res_n = dupes.scan(dupes.Options(roots=[R], mode="name"))
check("name mode ignores case, different content allowed",
      any({f.name for f in g.files} == {"Report.PDF", "report.pdf"} for g in res_n.groups))
res_ns = dupes.scan(dupes.Options(roots=[R], mode="name_size"))
check("name+size mode needs equal size", not any({f.name for f in g.files} == {"Report.PDF", "report.pdf"}
                                                   for g in res_ns.groups))

# regression (26.4.3): 16-32 KiB files used to be compared on their first 16 KiB only
M = os.path.join(H, "mid")
a = bytearray(os.urandom(20_000))
w(f"{M}/a.bin", bytes(a))
a[18_000] ^= 0xFF
w(f"{M}/b.bin", bytes(a))                                 # differs only after the first 16 KiB
w(f"{M}/c.bin", bytes(a))                                 # true copy of b
rm = dupes.scan(dupes.Options(roots=[M]))
check("16-32 KiB files: compared in full (no false match on the first 16 KiB)",
      len(rm.groups) == 1 and {f.name for f in rm.groups[0].files} == {"b.bin", "c.bin"}
      and rm.groups[0].key == dupes.file_hash(f"{M}/b.bin"))
for size in (32 * 1024, 32 * 1024 + 1, 5 * 1024 * 1024 + 7):   # boundary + multi-chunk full read
    d = os.urandom(size)
    w(f"{H}/edge{size}/x", d)
    w(f"{H}/edge{size}/y", d)
    re_ = dupes.scan(dupes.Options(roots=[f"{H}/edge{size}"]))
    check(f"{size:,}-byte copies found, group key = full-file hash",
          len(re_.groups) == 1 and re_.groups[0].key == dupes.file_hash(f"{H}/edge{size}/x"))

ev = threading.Event()
ev.set()
check("cancel", dupes.scan(dupes.Options(roots=[R]), cancel=ev).cancelled)
seen = []
dupes.scan(dupes.Options(roots=[R]), progress=seen.append)
check("progress phases reported", {"walk", "done"} <= {d["phase"] for d in seen})

# ---------------------------------------------------------------- selection rules
sel = dupes.select_rule([g_big], "all_but_newest")
check("keep newest", sel == {f"{R}/a/photo.jpg", f"{R}/c/deep/IMG_1.jpg"})
sel = dupes.select_rule([g_big], "all_but_oldest")
check("keep oldest", f"{R}/a/photo.jpg" not in sel and len(sel) == 2)
sel = dupes.select_rule([g_big], "in_folder", f"{R}/c")
check("select in folder", sel == {f"{R}/c/deep/IMG_1.jpg"})
sel = dupes.select_rule([g_big], "in_folder", R)
check("select in folder keeps one when all copies are inside", len(sel) == 2)
check("fully-selected detector", dupes.groups_fully_selected([g_big], {f.path for f in g_big.files}) == [g_big])

# ---------------------------------------------------------------- actions
res = dupes.scan(dupes.Options(roots=[R]))
g_big = next(g for g in res.groups if len(g.files) == 3)
g_small = next(g for g in res.groups if g.size == 11)
everything = {f.path for f in g_small.files}
r = dupes.apply([g_small], everything, "trash")
check("all copies selected: nothing is removed", not r.done and len(r.skipped) == 3
      and all(os.path.exists(f.path) for f in g_small.files))
r = dupes.apply([g_small], everything - {g_small.files[0].path}, "trash")
check("all but one selected: those go, one copy remains", len(r.done) == 2
      and [os.path.exists(f.path) for f in g_small.files] == [True, False, False])
trash_files = os.listdir(os.path.join(H, ".local/share/Trash/files"))
check("trash goes to the freedesktop trash", len(trash_files) == 2)

res = dupes.scan(dupes.Options(roots=[R]))
g_big = next(g for g in res.groups if len(g.files) == 3)
newest = max(g_big.files, key=lambda f: f.mtime)
oldest = min(g_big.files, key=lambda f: f.mtime)
with open(newest.path, "ab") as f:
    f.write(b"!")                                     # changed after the scan
r = dupes.apply([g_big], {newest.path}, "delete")
check("file changed since scan is skipped", not r.done and "changed since the scan" in r.skipped[0]
      and os.path.exists(newest.path))

res = dupes.scan(dupes.Options(roots=[R]))
g_big = next(g for g in res.groups if g.size == 300_000 and len(g.files) == 2)
AUD = os.path.join(H, "Audit")
os.makedirs(AUD)
victim = max(g_big.files, key=lambda f: f.mtime).path
keeper = min(g_big.files, key=lambda f: f.mtime).path
r = dupes.apply([g_big], {victim}, "move", audit_root=AUD)
moved = os.path.join(r.audit_dir, victim.lstrip("/"))
check("move for audit keeps the full original path", os.path.exists(moved) and not os.path.exists(victim))
with open(r.manifest, newline="") as fh:
    rows = list(csv.DictReader(fh))
check("audit manifest records original, new path, hash and the kept copy",
      len(rows) == 1 and rows[0]["original_path"] == victim and rows[0]["audit_path"] == moved
      and rows[0]["match"].startswith("blake2b-256:") and rows[0]["kept_copy"] == keeper)
check("audit folder is skipped by later scans",
      not any(dupes.AUDIT_PREFIX in f.path for g in dupes.scan(dupes.Options(roots=[H])).groups for f in g.files))
u = dupes.undo_audit(r.manifest)
check("undo puts files back", u.done == [victim] and os.path.exists(victim) and not os.path.exists(moved))

# cross-file-system move (copy + verify + delete): simulate EXDEV
real_rename = os.rename


def no_rename(a, b):
    raise OSError(18, "Invalid cross-device link")


os.rename = no_rename
try:
    dupes._move_verified(victim, os.path.join(H, "xdev", "v.jpg"), g_big.key)
finally:
    os.rename = real_rename
check("cross-device move copies, verifies the hash, then removes the original",
      os.path.exists(os.path.join(H, "xdev", "v.jpg")) and not os.path.exists(victim))

out = os.path.join(H, "list.csv")
dupes.export_csv(res.groups, out, {keeper})
with open(out, newline="") as fh:
    rows = list(csv.DictReader(fh))
check("CSV export", rows and any(r["selected"] == "yes" for r in rows))

# speed: 20k small unique-size files should be cheap (no hashing)
P = os.path.join(H, "perf")
os.makedirs(P)
for i in range(20000):
    with open(f"{P}/f{i}", "wb") as f:
        f.write(b"x" * (i + 1))
t = time.time()
rp = dupes.scan(dupes.Options(roots=[P]))
dt = time.time() - t
check(f"20k unique sizes scanned in {dt:.2f}s with 0 bytes hashed", rp.bytes_hashed == 0 and not rp.groups and dt < 10)

shutil.rmtree(H, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} duplicate-engine checks passed")
sys.exit(0 if all(results) else 1)
