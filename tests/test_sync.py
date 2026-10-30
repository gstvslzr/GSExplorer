"""Sync & Backup engine tests (no GUI). Run: python tests/test_sync.py

Every test runs in a throwaway HOME/XDG tree; nothing outside it is touched.
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gsexplorer import syncengine as se  # noqa: E402


def _home():
    h = tempfile.mkdtemp(prefix="gx-sync-")
    os.environ["HOME"] = h
    os.environ["XDG_DATA_HOME"] = os.path.join(h, ".local", "share")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(h, ".config")
    return h


def w(path, data="x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def r(path):
    with open(path) as f:
        return f.read()


def tree(root):
    out = {}
    for d, ds, fs in os.walk(root, followlinks=False):
        for n in fs + ds:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out[rel] = "->" + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = "/"
            else:
                out[rel] = r(p)
    return out


def run(task, **kw):
    ctx = se.Context()
    plan = se.compare(task)
    assert not plan.errors, plan.errors
    se.execute(plan, ctx)
    return plan, ctx


T0 = time.time() - 10_000


def test_mirror_create_update_delete_with_trash():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a.txt", "new", T0 + 100)
    w(f"{L}/sub/b.txt", "b", T0)
    w(f"{L}/sub/deep/c.txt", "c", T0)
    os.symlink("/etc", f"{L}/link-etc")
    w(f"{R}/a.txt", "old", T0)
    w(f"{R}/extra/z.txt", "z", T0)
    t = se.Task(mode="mirror", left=L, right=R, versioning="trash")
    plan, ctx = run(t)
    cats = {i.rel: (i.category, i.action) for i in plan.items}
    assert cats["a.txt"] == (se.LEFT_NEWER, se.COPY_LR)
    assert cats["extra"] == (se.RIGHT_ONLY, se.DEL_R)
    assert "sub/b.txt" not in cats and cats["sub"][1] == se.COPY_LR   # one-sided folder collapsed
    assert tree(L) == tree(R), (tree(L), tree(R))
    assert os.path.islink(f"{R}/link-etc") and os.readlink(f"{R}/link-etc") == "/etc"
    trash = os.path.join(os.environ["XDG_DATA_HOME"], "Trash", "files")
    assert sorted(os.listdir(trash)) == ["a.txt", "extra"]           # overwritten + deleted kept
    assert r(os.path.join(trash, "a.txt")) == "old"
    # second compare: nothing to do
    assert not se.compare(t).actionable()


def test_mirror_refuses_empty_source():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    os.makedirs(L)
    w(f"{R}/precious.txt")
    plan = se.compare(se.Task(mode="mirror", left=L, right=R))
    assert any("Source is empty" in e for e in plan.errors)
    assert not plan.items
    plan = se.compare(se.Task(mode="mirror", left=L, right=R, allow_empty_source=True))
    assert not plan.errors and plan.items[0].action == se.DEL_R


def test_unmounted_target_and_nested_pairs_blocked():
    h = _home()
    L = os.path.join(h, "L")
    w(f"{L}/a")
    e = se.compare(se.Task(left=L, right="/media/nobody/NOT_MOUNTED/backup")).errors
    assert any("not mounted" in x for x in e)
    e = se.compare(se.Task(left=L, right=os.path.join(L, "inner"))).errors
    assert any("inside" in x for x in e)
    w_, e2 = se.preflight(se.Task(left=L, right=os.path.join(h, "newdir")))
    assert not e2 and any("will be created" in x for x in w_)


def test_update_never_deletes_or_overwrites_newer():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/new.txt", "n", T0)
    w(f"{L}/older.txt", "left-old", T0)
    w(f"{R}/older.txt", "right-new", T0 + 500)
    w(f"{R}/only-right.txt", "keep", T0)
    run(se.Task(mode="update", left=L, right=R))
    assert r(f"{R}/new.txt") == "n"
    assert r(f"{R}/older.txt") == "right-new"
    assert r(f"{R}/only-right.txt") == "keep"


def test_twoway_full_cycle_with_conflicts():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/shared.txt", "s", T0)
    w(f"{L}/from-left.txt", "l", T0)
    w(f"{R}/from-right.txt", "r", T0)
    w(f"{L}/del-me.txt", "d", T0)
    w(f"{L}/both.txt", "base", T0)
    w(f"{L}/mod-vs-del.txt", "m", T0)
    os.makedirs(R, exist_ok=True)
    t = se.Task(mode="twoway", left=L, right=R, versioning="folder", versions_dir=os.path.join(h, "V"))
    plan, _ = run(t)
    assert plan.first_twoway
    assert tree(L) == tree(R)
    # changes after first sync
    os.unlink(f"{L}/del-me.txt")                       # deleted left -> delete right
    w(f"{R}/shared.txt", "s2", T0 + 100)               # changed right -> copy to left
    w(f"{L}/both.txt", "L!", T0 + 200)                 # changed both -> conflict
    w(f"{R}/both.txt", "R!", T0 + 300)
    w(f"{L}/mod-vs-del.txt", "m2", T0 + 50)            # modified left, deleted right -> conflict
    os.unlink(f"{R}/mod-vs-del.txt")
    plan = se.compare(t)
    got = {i.rel: (i.category, i.action) for i in plan.items if i.category != se.EQUAL}
    assert got["del-me.txt"] == (se.RIGHT_ONLY, se.DEL_R), got
    assert got["shared.txt"][1] == se.COPY_RL, got
    assert got["both.txt"] == (se.CONFLICT, se.NONE), got
    assert got["mod-vs-del.txt"] == (se.CONFLICT, se.NONE), got
    ctx = se.Context()
    se.execute(plan, ctx)
    assert not os.path.exists(f"{R}/del-me.txt")
    assert r(f"{L}/shared.txt") == "s2"
    assert r(f"{L}/both.txt") == "L!" and r(f"{R}/both.txt") == "R!"     # conflict untouched
    v = os.listdir(os.path.join(h, "V"))
    assert any(n.startswith("del-me ") and n.endswith(".txt") for n in v), v
    assert any(n.startswith("shared ") for n in v), v                     # overwritten left kept


def test_versions_prune_by_age():
    h = _home()
    V = os.path.join(h, "V")
    w(f"{V}/old 2001-01-01_000000.txt")
    w(f"{V}/recent {time.strftime('%Y-%m-%d_%H%M%S')}.txt")
    t = se.Task(versioning="folder", versions_dir=V, versions_keep_days=30)
    se.Versioner(t, se.Context()).prune()
    assert os.listdir(V) == [n for n in os.listdir(V) if n.startswith("recent")]


def test_snapshots_hardlink_unchanged_and_prune():
    h = _home()
    L, B = os.path.join(h, "src"), os.path.join(h, "backups")
    w(f"{L}/same.bin", "unchanging", T0)
    w(f"{L}/edit.txt", "v1", T0)
    os.symlink("same.bin", f"{L}/rel-link")
    t = se.Task(mode="snapshot", left=L, right=B, snapshots_keep=2)
    first = se.execute_snapshot(t, se.Context())
    time.sleep(1.1)
    w(f"{L}/edit.txt", "v2", T0 + 99)
    os.makedirs(os.path.join(B, "2000-01-01_000000.partial"))      # leftover of a crashed run
    ctx = se.Context()
    second = se.execute_snapshot(t, ctx)
    assert not os.path.exists(os.path.join(B, "2000-01-01_000000.partial"))
    s1, s2 = os.stat(f"{first}/same.bin"), os.stat(f"{second}/same.bin")
    assert s1.st_ino == s2.st_ino and ctx.stats["linked"] == 1           # hard-linked
    assert r(f"{first}/edit.txt") == "v1" and r(f"{second}/edit.txt") == "v2"
    assert os.readlink(f"{second}/rel-link") == "same.bin"
    assert os.readlink(os.path.join(B, "latest")) == os.path.basename(second)
    time.sleep(1.1)
    third = se.execute_snapshot(t, se.Context())
    snaps = [os.path.basename(p) for p in se.list_snapshots(B)]
    assert snaps == [os.path.basename(second), os.path.basename(third)], snaps   # keep 2
    plan = se.compare(t)
    assert all(i.action == se.LINK for i in plan.items if i.left and i.left.kind == "f")


def test_filters():
    f = se.Filter(["*"], ["node_modules/", "*.tmp", "build/out/"])
    assert f.excluded("a/node_modules", True) and not f.excluded("a/node_modules", False)
    assert f.excluded("x/y/Z.TMP", False)
    assert f.excluded("build/out", True) and not f.excluded("src/build/out", True)
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/docs/a.pdf")
    w(f"{L}/docs/b.txt")
    w(f"{L}/node_modules/x.js")
    run(se.Task(mode="mirror", left=L, right=R, include=["*.pdf"], exclude=["node_modules/"]))
    assert sorted(tree(R)) == ["docs", "docs/a.pdf"], tree(R)


def test_content_compare_detects_same_size_change():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/f", "AAAA", T0)
    w(f"{R}/f", "BBBB", T0)
    assert not se.compare(se.Task(left=L, right=R, compare="time_size")).actionable()
    p = se.compare(se.Task(left=L, right=R, compare="content"))
    assert [i.category for i in p.items] == [se.DIFFERENT]


def test_overwrite_failure_keeps_original():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/f", "new", T0 + 10)
    w(f"{R}/f", "old", T0)
    t = se.Task(left=L, right=R, versioning="folder", versions_dir="/proc/not-writable")
    plan = se.compare(t)
    ctx = se.Context()
    se.execute(plan, ctx)
    assert ctx.errors and r(f"{R}/f") == "old"
    assert not [n for n in os.listdir(R) if n.endswith(".gxsync-tmp")]


def test_lock_blocks_concurrent_run():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a")
    t = se.Task(left=L, right=R)
    with se._Lock(t):
        try:
            se.execute(se.compare(t), se.Context())
            raise AssertionError("second run should be blocked")
        except se.PreflightError:
            pass


def test_cli_profile_exit_codes():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a", "1")
    se.save_profile(se.Profile("Nightly", [se.Task(name="Docs", left=L, right=R)]))
    assert se.list_profiles() == ["Nightly"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = se.run_profile_cli("Nightly", dry_run=True)
    out = json.loads(buf.getvalue())
    assert code == 0 and out["dry_run"] and not os.path.exists(f"{R}/a")
    with redirect_stdout(io.StringIO()):
        assert se.run_profile_cli("Nightly") == 0
    assert r(f"{R}/a") == "1"
    se.save_profile(se.Profile("Broken", [se.Task(name="X", left="/nope", right=R)]))
    with redirect_stdout(io.StringIO()):
        assert se.run_profile_cli("Broken") == se.EXIT_ERROR
    with redirect_stdout(io.StringIO()):
        assert se.run_profile_cli("Missing") == se.EXIT_ERROR
    assert se.valid_profile_name("bad/name") and se.valid_profile_name("ok name-1") is None


def test_compare_scales():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    for d in range(200):
        for i in range(250):
            p = f"{L}/d{d:03d}/f{i:03d}.txt"
            if i == 0:
                os.makedirs(os.path.dirname(p), exist_ok=True)
            os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o644))
    t0 = time.perf_counter()
    plan = se.compare(se.Task(left=L, right=R))
    dt = time.perf_counter() - t0
    print(f"\n   50k files, empty target: compare {dt:.2f}s, {len(plan.items)} rows "
          f"(one per top-level folder), {plan.summary()['copy_lr']} files to copy")
    assert len(plan.items) == 200 and plan.summary()["copy_lr"] == 50_000
    assert dt < 10


def test_mtime_restored_when_rename_resets_it():
    """Regression (26.2.1): some drivers (FUSE FAT, seen in testing) reset mtime on rename, which
    made every later Compare re-copy everything. Simulate that driver."""
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a.txt", "a", T0)
    w(f"{L}/sub/b.txt", "b", T0)
    real_replace = os.replace

    def resetting_replace(src, dst, *a, **k):
        real_replace(src, dst, *a, **k)
        os.utime(dst, None)                      # driver stamps "now"
    se.os.replace = resetting_replace
    try:
        run(se.Task(mode="mirror", left=L, right=R))
    finally:
        se.os.replace = real_replace
    assert abs(os.stat(f"{R}/a.txt").st_mtime - T0) < 1
    assert not se.compare(se.Task(mode="mirror", left=L, right=R)).actionable()


def test_chmod_failure_does_not_lose_mtime():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a.txt", "a", T0)
    real_chmod = os.chmod

    def failing_chmod(*a, **k):
        raise OSError(38, "Function not implemented")   # what FAT returned in testing
    se.os.chmod = failing_chmod
    try:
        run(se.Task(mode="mirror", left=L, right=R))
    finally:
        se.os.chmod = real_chmod
    assert abs(os.stat(f"{R}/a.txt").st_mtime - T0) < 1


def test_one_hour_shift_option():
    h = _home()
    L, R = os.path.join(h, "L"), os.path.join(h, "R")
    w(f"{L}/a.txt", "same", T0)
    w(f"{R}/a.txt", "same", T0 + 3600)                   # e.g. FAT after a DST change
    t = se.Task(mode="mirror", left=L, right=R)
    assert [i.category for i in se.compare(t).actionable()] == [se.RIGHT_NEWER]   # off on ext4
    t = se.Task(mode="mirror", left=L, right=R, ignore_hour_shift=True)
    assert not se.compare(t).actionable()
    w(f"{R}/a.txt", "same", T0 + 3600 + 30)              # not an exact hour -> still different
    assert se.compare(t).actionable()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name, flush=True)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", name, repr(e), flush=True)
    print(f"{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
