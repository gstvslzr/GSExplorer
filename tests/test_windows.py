# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows-port checks: run with the Windows Python that builds the .exe.

    set QT_QPA_PLATFORM=offscreen
    python tests\\test_windows.py

Covers what the Linux suites can't: data folders (%APPDATA% / portable), drive-root navigation,
Windows file-name rules, the Recycle Bin, hidden attributes, the job queue, Sync & Backup
(mirror, two-way, snapshots with hard links, lock, pre/post commands, headless CLI), archives,
Find Files, Find Duplicates, and the main window at a drive root.
"""
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

if sys.platform != "win32":
    print("SKIP: Windows-only tests")
    sys.exit(0)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = tempfile.mkdtemp(prefix="gx-win-")
os.environ["APPDATA"] = os.path.join(BASE, "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(BASE, "Local")
for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ.pop(k, None)

from gsexplorer import winplat  # noqa: E402

winplat.setup_dirs()
winplat.install_subprocess_defaults()

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsexplorer import fsutil  # noqa: E402

app = QApplication.instance() or QApplication([])
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def w(path, data="x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def r(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def tree(root):
    out = {}
    for d, ds, fs in os.walk(root):
        for n in fs + ds:
            p = os.path.join(d, n)
            if os.path.isdir(p):
                out[os.path.relpath(p, root).replace("\\", "/")] = "/"
            else:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root).replace("\\", "/")] = f.read()
    return out


def section(t):
    print(f"--- {t}")


# ------------------------------------------------------------------ data folders
section("data folders")
from gsexplorer.config import Config, config_dir  # noqa: E402

check("config dir is %APPDATA%\\gsexplorer-plus",
      os.path.normcase(config_dir()) == os.path.normcase(os.path.join(BASE, "Roaming", "gsexplorer-plus")),
      config_dir())
c = Config()
c["theme"] = "Dark"
c.save()
check("config.json written under %APPDATA%", os.path.isfile(os.path.join(config_dir(), "config.json")))
check("data dir is %LOCALAPPDATA%", os.path.normcase(os.environ["XDG_DATA_HOME"]) ==
      os.path.normcase(os.path.join(BASE, "Local")))
_saved_exe = winplat.exe_dir
port = os.path.join(BASE, "usb")
os.makedirs(os.path.join(port, winplat.PORTABLE_DIRNAME))
winplat.exe_dir = lambda: port
winplat.setup_dirs()
check("portable mode: config inside gsexplorer-plus-data next to the exe",
      config_dir().startswith(os.path.join(port, winplat.PORTABLE_DIRNAME)), config_dir())
winplat.exe_dir = _saved_exe
os.rmdir(os.path.join(port, winplat.PORTABLE_DIRNAME))
winplat.setup_dirs()
check("without the folder: back to %APPDATA%", config_dir().startswith(os.path.join(BASE, "Roaming")))

# ------------------------------------------------------------------ paths and names
section("paths and names")
check("C:\\ is a root", fsutil.is_root("C:\\"))
check("C:\\Users is not a root", not fsutil.is_root("C:\\Users"))
check("parent of C:\\Users is C:\\", fsutil.parent_dir("C:\\Users") == "C:\\", fsutil.parent_dir("C:\\Users"))
check("parent of C:\\ is itself", fsutil.parent_dir("C:\\") == "C:\\")
check("UNC share root is a root", fsutil.is_root("\\\\server\\share\\"))
for bad in ("a:b", "q?", "x*", "p|q", "lt<", 'qu"', "CON", "con.txt", "LPT1", "trail.", "trail "):
    check(f"rejects {bad!r}", fsutil.validate_new_name(bad) is not None)
for good in ("report v1.2.docx", "Ünïcødé ✓.txt", "console.log", ".gitignore"):
    check(f"accepts {good!r}", fsutil.validate_new_name(good) is None, fsutil.validate_new_name(good) or "")
check("unique_name gives (2)", fsutil.unique_name(BASE, "Roaming") == "Roaming (2)")

# ------------------------------------------------------------------ Recycle Bin
section("Recycle Bin")
victim = os.path.join(BASE, "recycle me.txt")
w(victim, "bye")
try:
    ret = fsutil.move_to_trash(victim)
    check("move_to_trash -> Recycle Bin, file gone", ret == fsutil.RECYCLE_BIN and not os.path.exists(victim))
except OSError as e:
    check("move_to_trash -> Recycle Bin, file gone", False, repr(e))
vd = os.path.join(BASE, "recycle dir")
w(os.path.join(vd, "inner", "f.txt"))
try:
    fsutil.move_to_trash(vd)
    check("folder to Recycle Bin", not os.path.exists(vd))
except OSError as e:
    check("folder to Recycle Bin", False, repr(e))
try:
    fsutil.move_to_trash(os.path.join(BASE, "missing.txt"))
    check("missing file raises", False)
except FileNotFoundError:
    check("missing file raises", True)
check("no trash view on Windows", fsutil.is_trash_files_dir(fsutil.home_trash() + "\\files") is None)
check("recycle_bin_info returns numbers", all(isinstance(x, int) for x in winplat.recycle_bin_info()))

# ------------------------------------------------------------------ hidden attribute
section("hidden attribute")
import ctypes  # noqa: E402

hd = os.path.join(BASE, "hid")
w(os.path.join(hd, "visible.txt"))
w(os.path.join(hd, "secret.txt"))
w(os.path.join(hd, ".dotfile"))
ctypes.windll.kernel32.SetFileAttributesW(os.path.join(hd, "secret.txt"), 0x2)
from gsexplorer.dirmodel import make_entry  # noqa: E402

ents = {n: make_entry(hd, n) for n in os.listdir(hd)}
check("FILE_ATTRIBUTE_HIDDEN file is hidden", ents["secret.txt"].hidden)
check("dot-file is hidden", ents[".dotfile"].hidden)
check("normal file is visible", not ents["visible.txt"].hidden)

# ------------------------------------------------------------------ job queue
section("job queue")
from gsexplorer.fileops import OVERWRITE, JobManager  # noqa: E402


def _wait(job, timeout=60):
    t0 = time.time()
    while job.state in ("queued", "running", "paused"):
        QCoreApplication.processEvents()
        time.sleep(0.01)
        if time.time() - t0 > timeout:
            return False
    return True


jq = os.path.join(BASE, "jobs")
src = os.path.join(jq, "src")
w(os.path.join(src, "a.txt"), "alpha")
w(os.path.join(src, "sub", "b.txt"), "beta")
with open(os.path.join(src, "big.bin"), "wb") as f:
    f.write(os.urandom(5 * 1024 * 1024 + 3))
w(os.path.join(src, "Ünïcødé ✓.txt"), "u")
os.makedirs(os.path.join(jq, "dst"))
jm = JobManager()
job = jm.submit("copy", [src], os.path.join(jq, "dst"))
_wait(job)
check("copy tree", job.state == "done" and tree(src) == tree(os.path.join(jq, "dst", "src")), str(job.errors))
w(os.path.join(jq, "dst2", "a.txt"), "OLD")
jm.conflict.connect(lambda j, *_: j.answer(OVERWRITE, True) if hasattr(j, "answer") else None)
job = jm.submit("copy", [os.path.join(src, "a.txt")], os.path.join(jq, "dst2"))
_wait(job, 20)
check("copy over existing file (overwrite, atomic replace)", r(os.path.join(jq, "dst2", "a.txt")) == "alpha",
      f"{job.state} {job.errors}")
job = jm.submit("move", [os.path.join(jq, "dst", "src", "sub")], os.path.join(jq, "dst2"))
_wait(job)
check("move folder (same volume)", os.path.isfile(os.path.join(jq, "dst2", "sub", "b.txt"))
      and not os.path.exists(os.path.join(jq, "dst", "src", "sub")), str(job.errors))
job = jm.submit("trash", [os.path.join(jq, "dst2", "sub")])
_wait(job)
check("trash job uses Recycle Bin", job.state == "done" and not os.path.exists(os.path.join(jq, "dst2", "sub")),
      str(job.errors))
job = jm.submit("delete", [os.path.join(jq, "dst2")])
_wait(job)
check("permanent delete job", not os.path.exists(os.path.join(jq, "dst2")), str(job.errors))

# ------------------------------------------------------------------ Sync & Backup
section("Sync & Backup")
from gsexplorer import syncengine as se  # noqa: E402

T0 = time.time() - 10_000
sb = os.path.join(BASE, "sync")


def run(task):
    ctx = se.Context()
    plan = se.compare(task)
    assert not plan.errors, plan.errors
    se.execute(plan, ctx)
    return plan, ctx


L, R = os.path.join(sb, "L"), os.path.join(sb, "R")
w(f"{L}/a.txt", "new", T0 + 100)
w(f"{L}/sub/deep/c.txt", "c", T0)
w(f"{R}/a.txt", "old", T0)
w(f"{R}/extra/z.txt", "z", T0)
try:
    plan, ctx = run(se.Task(mode="mirror", left=L, right=R, versioning="trash"))
    check("mirror makes R == L", tree(L) == tree(R), str(ctx.errors))
    check("mirror: second compare has nothing to do", not se.compare(se.Task(mode="mirror", left=L, right=R)).actionable())
except Exception as e:  # noqa: BLE001
    check("mirror makes R == L", False, repr(e))

L2, R2 = os.path.join(sb, "L2"), os.path.join(sb, "R2")
w(f"{L2}/both.txt", "same", T0)
w(f"{R2}/both.txt", "same", T0)
t2 = se.Task(mode="twoway", left=L2, right=R2, versioning="delete")
run(t2)
w(f"{L2}/fromL.txt", "L", T0 + 10)
w(f"{R2}/fromR.txt", "R", T0 + 10)
os.remove(f"{R2}/both.txt")
plan, ctx = run(t2)
check("two-way: new files flow both ways and a deletion propagates",
      os.path.exists(f"{R2}/fromL.txt") and os.path.exists(f"{L2}/fromR.txt") and not os.path.exists(f"{L2}/both.txt"),
      f"{tree(L2)} {tree(R2)} {ctx.errors}")

S, B = os.path.join(sb, "snapsrc"), os.path.join(sb, "backups")
w(f"{S}/same.bin", "unchanging", T0)
w(f"{S}/edit.txt", "v1", T0)
st = se.Task(mode="snapshot", left=S, right=B, snapshots_keep=2)
first = se.execute_snapshot(st, se.Context())
time.sleep(1.1)
w(f"{S}/edit.txt", "v2", T0 + 99)
ctx = se.Context()
second = se.execute_snapshot(st, ctx)
s1, s2 = os.stat(f"{first}/same.bin"), os.stat(f"{second}/same.bin")
check("snapshot: unchanged file hard-linked (NTFS)", s1.st_ino == s2.st_ino and ctx.stats.get("linked") == 1,
      f"ino {s1.st_ino}/{s2.st_ino} stats {ctx.stats}")
check("snapshot: changed file has its own copy", r(f"{first}/edit.txt") == "v1" and r(f"{second}/edit.txt") == "v2")

with se._Lock(se.Task(left=L, right=R)):
    try:
        se.execute(se.compare(se.Task(left=L, right=R)), se.Context())
        check("lock blocks a concurrent run of the same pair (msvcrt)", False)
    except se.PreflightError:
        check("lock blocks a concurrent run of the same pair (msvcrt)", True)

marker = os.path.join(sb, "hook.txt")
L3, R3 = os.path.join(sb, "L3"), os.path.join(sb, "R3")
w(f"{L3}/f.txt", "1")
th = se.Task(mode="update", left=L3, right=R3, post_cmd=f'echo %GX_ERRORS% > "{marker}"') \
    if "post_cmd" in se.Task.__dataclass_fields__ else None
if th is not None:
    _p, hctx = run(th)
    if hctx.errors:
        print("   hook errors:", hctx.errors)
    se_ok = os.path.isfile(marker) and r(marker).strip() == "0"
    check("post-task command runs through cmd.exe with %GX_ERRORS%", se_ok, r(marker) if os.path.isfile(marker) else "no file")
else:
    fields = [k for k in se.Task.__dataclass_fields__ if "cmd" in k or "post" in k]
    check("post-task command field exists", False, str(fields))

se.save_profile(se.Profile("Nightly", [se.Task(name="Docs", left=L3, right=os.path.join(sb, "R4"))]))
buf = io.StringIO()
with redirect_stdout(buf):
    code = se.run_profile_cli("Nightly", dry_run=False, quiet=True)
check("headless --sync profile exit code 0 and target written",
      code == 0 and os.path.isfile(os.path.join(sb, "R4", "f.txt")), f"code={code} {buf.getvalue()[:200]}")
logs = os.path.join(os.environ["XDG_DATA_HOME"], "gsexplorer-plus", "sync-logs")
check("sync log written under %LOCALAPPDATA%", os.path.isdir(logs) and os.listdir(logs), logs)

# ------------------------------------------------------------------ archives
section("archives")
from gsexplorer import archives as ar  # noqa: E402

asrc = os.path.join(BASE, "arc", "Project Ü")
w(os.path.join(asrc, "sub dir", "deeper", "n.txt"), "nested")
w(os.path.join(asrc, "top.txt"), "top")
for fmt, ext in (("zip", ".zip"), ("tar.gz", ".tar.gz"), ("tar.xz", ".tar.xz")):
    out = os.path.join(BASE, "arc", "out" + ext)
    try:
        ar.compress([asrc], out, fmt)
        dest = os.path.join(BASE, "arc", "x-" + fmt)
        os.makedirs(dest)
        ar.extract(out, dest, mode="smart")
        check(f"{fmt}: compress + extract round-trip", tree(os.path.join(dest, "Project Ü")) == tree(asrc),
              str(os.listdir(dest)))
    except Exception as e:  # noqa: BLE001
        check(f"{fmt}: compress + extract round-trip", False, repr(e))
evil = os.path.join(BASE, "arc", "evil.zip")
import zipfile  # noqa: E402

with zipfile.ZipFile(evil, "w") as z:
    z.writestr("..\\..\\escaped.txt", "x")
    z.writestr("C:/abs.txt", "x")
    z.writestr("ok.txt", "fine")
ed = os.path.join(BASE, "arc", "evil-out")
os.makedirs(ed)
try:
    ar.extract(evil, ed, mode="folder")
except Exception:  # noqa: BLE001
    pass
check("zip with ..\\ and drive-letter paths can't escape",
      not os.path.exists(os.path.join(BASE, "escaped.txt")) and not os.path.exists("C:\\abs.txt"))

# ------------------------------------------------------------------ search / duplicates
section("Find Files / Find Duplicates")
from gsexplorer import dupes  # noqa: E402
from gsexplorer import search as sr  # noqa: E402

sroot = os.path.join(BASE, "find")
w(os.path.join(sroot, "Docs", "Report Q3.txt"), "quarterly revenue TOTAL 42")
w(os.path.join(sroot, "Docs", "notes.md"), "nothing here")
w(os.path.join(sroot, "copy1", "dup.bin"), "same bytes" * 1000)
w(os.path.join(sroot, "copy2", "dup.bin"), "same bytes" * 1000)
hits = []
sr.search(sr.Criteria(roots=[sroot], name="*.txt", name_mode="wildcard"), hits.extend)
check("search by wildcard", sorted(h.name for h in hits) == ["Report Q3.txt"], str([h.name for h in hits]))
hits = []
sr.search(sr.Criteria(roots=[sroot], text="revenue"), hits.extend)
check("search by contained text", [h.name for h in hits] == ["Report Q3.txt"], str([h.name for h in hits]))
res = dupes.scan(dupes.Options(roots=[sroot]))
grp = [sorted(os.path.relpath(f.path, sroot) for f in g.files) for g in res.groups]
check("duplicates by content (Windows DirEntry has no inode)", grp == [[os.path.join("copy1", "dup.bin"), os.path.join("copy2", "dup.bin")]], str(grp))

os.link(os.path.join(sroot, "copy1", "dup.bin"), os.path.join(sroot, "copy1", "hardlink.bin"))
res = dupes.scan(dupes.Options(roots=[sroot]))
grp = [sorted(os.path.relpath(f.path, sroot) for f in g.files) for g in res.groups]
check("a hard link is not reported as a duplicate copy", len(grp) == 1 and len(grp[0]) == 2, str(grp))

# ------------------------------------------------------------------ main window at a drive root
section("main window")
from gsexplorer import themes  # noqa: E402
from gsexplorer.icons import setup_icon_theme  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

setup_icon_theme()
cfg = Config()
themes.ensure_user_themes()
themes.apply(app, cfg["theme"], themes.load_themes())
win = MainWindow(cfg, "C:\\")
win.show()
for _ in range(50):
    QCoreApplication.processEvents()
    time.sleep(0.01)
v = win.active_view()
check("opens at C:\\", os.path.normcase(v.path) == os.path.normcase("C:\\"), v.path)
segs = v.crumb._segments()
check("breadcrumb at C:\\ terminates with one segment", len(segs) == 1 and segs[0][1] == "C:\\", str(segs))
check("Up disabled at drive root", not v.crumb.btn_up.isEnabled())
check("no '..' row at drive root", v.model._parent_entry() is None)
v.navigate(os.path.join(sroot, "Docs"))
for _ in range(50):
    QCoreApplication.processEvents()
    time.sleep(0.01)
segs = v.crumb._segments()
check("breadcrumb for a deep path ends with the folder and starts at Home or a root",
      segs[-1][0] == "Docs" and (segs[0][0] == "Home" or fsutil.is_root(segs[0][1])), str(segs))
v.navigate(hd)
for _ in range(50):
    QCoreApplication.processEvents()
    time.sleep(0.01)
listed = sorted(e.name for e in v.model.rows if not e.parent_link)
check("listing hides Windows-hidden files by default (scandir path)", listed == ["visible.txt"], str(listed))
v.navigate(os.path.join(sroot, "Docs"))
for _ in range(50):
    QCoreApplication.processEvents()
    time.sleep(0.01)
names = sorted(e.name for e in v.model.rows if not e.parent_link)
check("folder listing", names == ["Report Q3.txt", "notes.md"], str(names))
v.go_up()
for _ in range(30):
    QCoreApplication.processEvents()
    time.sleep(0.01)
check("Up goes to the parent", os.path.normcase(v.path) == os.path.normcase(sroot), v.path)
sb_items = []
top = win.sidebar
for i in range(top.topLevelItemCount()):
    sec = top.topLevelItem(i)
    sb_items += [(sec.text(0), sec.child(j).text(0), sec.child(j).data(0, 0x0101)) for j in range(sec.childCount())]
labels = [t for _s, t, _k in sb_items]
check("sidebar has a Recycle Bin and no Linux 'File System'/'Trash'",
      "Recycle Bin" in labels and "File System" not in labels and "Trash" not in labels, str(labels))
check("sidebar lists drive C:", any("(C:)" in t for t in labels), str(labels))
check("no Network section / Connect menu",
      not any(s == "Network" for s, *_ in sb_items) and
      "&Connect" not in [a.text() for a in win.menuBar().actions()])
idx = v.model.index(0, 0)
ic = v.model.data(idx, 1)
check("file rows get an icon", ic is not None and not ic.isNull())
win.close()

# ------------------------------------------------------------------ summary
bad = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} Windows checks passed")
sys.exit(1 if bad else 0)
