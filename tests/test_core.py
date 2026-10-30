"""Core tests for GExplorer: trash spec, file-operation queue, model scanning.

Run:  HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen python -m pytest -q tests/
(Or plain:  python tests/test_core.py)
"""
import os
import stat
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gexplorer import fsutil  # noqa: E402
from gexplorer.fileops import OVERWRITE, RENAME, SKIP, JobManager  # noqa: E402

app = QApplication.instance() or QApplication([])


def _wait(job, timeout=30):
    t0 = time.time()
    while job.state in ("queued", "running", "paused"):
        QCoreApplication.processEvents()
        time.sleep(0.01)
        assert time.time() - t0 < timeout, "job timed out"


def _tree(root):
    out = {}
    for r, ds, fs in os.walk(root, followlinks=False):
        for n in ds + fs:
            p = os.path.join(r, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out[rel] = ("link", os.readlink(p))
            elif os.path.isdir(p):
                out[rel] = ("dir",)
            elif stat.S_ISFIFO(os.lstat(p).st_mode):
                out[rel] = ("fifo",)   # opening a FIFO for reading would block
            else:
                with open(p, "rb") as f:
                    out[rel] = ("file", f.read())
    return out


def make_sample(root):
    os.makedirs(os.path.join(root, "src", "sub"))
    with open(os.path.join(root, "src", "a.txt"), "wb") as f:
        f.write(b"alpha")
    with open(os.path.join(root, "src", "sub", "big.bin"), "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024 + 17))
    bad = os.fsdecode(b"bad\xffname.txt")
    with open(os.path.join(root, "src", bad), "wb") as f:
        f.write(b"bytes")
    with open(os.path.join(root, "src", "new\nline.txt"), "wb") as f:
        f.write(b"nl")
    os.symlink("a.txt", os.path.join(root, "src", "link-a"))
    os.symlink("/etc", os.path.join(root, "src", "link-etc"))   # must NOT be followed
    os.symlink("/nonexistent", os.path.join(root, "src", "broken"))
    os.mkfifo(os.path.join(root, "src", "fifo"))
    return os.path.join(root, "src")


def test_copy_tree_preserves_everything():
    with tempfile.TemporaryDirectory() as d:
        src = make_sample(d)
        os.mkdir(os.path.join(d, "dst"))
        jm = JobManager()
        job = jm.submit("copy", [src], os.path.join(d, "dst"))
        _wait(job)
        assert job.state == "done", job.errors
        assert _tree(src) == _tree(os.path.join(d, "dst", "src"))
        assert os.path.islink(os.path.join(d, "dst", "src", "link-etc"))
        assert stat.S_ISFIFO(os.lstat(os.path.join(d, "dst", "src", "fifo")).st_mode)
        assert not [n for n in os.listdir(os.path.join(d, "dst", "src")) if n.endswith(".gxpart")]


def test_copy_into_itself_rejected():
    with tempfile.TemporaryDirectory() as d:
        src = make_sample(d)
        jm = JobManager()
        job = jm.submit("copy", [src], os.path.join(src, "sub"))
        _wait(job)
        assert job.state == "failed"
        assert any("into itself" in e for e in job.errors)


def test_duplicate_in_same_folder_gets_unique_name():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.txt")
        with open(p, "w") as f:
            f.write("1")
        jm = JobManager()
        job = jm.submit("copy", [p], d)
        _wait(job)
        assert sorted(os.listdir(d)) == ["x (2).txt", "x.txt"]


def _conflict_test(choice, expect):
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "a"))
        os.mkdir(os.path.join(d, "b"))
        for sub, data in (("a", b"NEW"), ("b", b"OLD")):
            with open(os.path.join(d, sub, "f.txt"), "wb") as f:
                f.write(data)
        jm = JobManager()
        asked = []
        jm.conflict.connect(lambda job, s, t: (asked.append((s, t)), job.answer(choice, False)),
                            Qt.DirectConnection)
        job = jm.submit("copy", [os.path.join(d, "a", "f.txt")], os.path.join(d, "b"))
        _wait(job)
        assert asked, "conflict not raised"
        assert expect(d), sorted(os.listdir(os.path.join(d, "b")))


def test_conflict_overwrite():
    _conflict_test(OVERWRITE, lambda d: open(os.path.join(d, "b", "f.txt"), "rb").read() == b"NEW")


def test_conflict_skip():
    _conflict_test(SKIP, lambda d: open(os.path.join(d, "b", "f.txt"), "rb").read() == b"OLD")


def test_conflict_rename():
    _conflict_test(RENAME, lambda d: sorted(os.listdir(os.path.join(d, "b"))) == ["f (2).txt", "f.txt"])


def test_move_same_device_and_rename():
    with tempfile.TemporaryDirectory() as d:
        src = make_sample(d)
        before = _tree(src)
        os.mkdir(os.path.join(d, "dst"))
        jm = JobManager()
        job = jm.submit("move", [src], os.path.join(d, "dst"), dest_name="renamed")
        _wait(job)
        assert job.state == "done", job.errors
        assert not os.path.exists(src)
        assert _tree(os.path.join(d, "dst", "renamed")) == before


def test_cancel_and_pause_leave_no_partial_files():
    with tempfile.TemporaryDirectory() as d:
        big = os.path.join(d, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(64 * 1024 * 1024))
        os.mkdir(os.path.join(d, "dst"))
        jm = JobManager()
        job = jm.submit("copy", [big], os.path.join(d, "dst"))
        while job.state == "queued":
            time.sleep(0.001)
        job.pause()
        time.sleep(0.1)
        assert job.state == "paused"
        job.cancel()
        _wait(job)
        assert job.state == "cancelled"
        assert os.listdir(os.path.join(d, "dst")) == []


def test_delete_does_not_follow_symlinks():
    with tempfile.TemporaryDirectory() as d:
        keep = os.path.join(d, "keep")
        os.mkdir(keep)
        with open(os.path.join(keep, "precious"), "w") as f:
            f.write("x")
        victim = os.path.join(d, "victim")
        os.mkdir(victim)
        os.symlink(keep, os.path.join(victim, "link-to-keep"))
        jm = JobManager()
        job = jm.submit("delete", [victim])
        _wait(job)
        assert job.state == "done", job.errors
        assert not os.path.exists(victim)
        assert os.path.exists(os.path.join(keep, "precious"))


def test_trash_and_restore_roundtrip():
    with tempfile.TemporaryDirectory() as home:
        os.environ["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
        name = os.fsdecode(b"weird \xff name.txt")
        p = os.path.join(home, name)
        with open(p, "w") as f:
            f.write("t")
        dest = fsutil.move_to_trash(p)
        assert not os.path.exists(p)
        trash = fsutil.home_trash()
        info = os.path.join(trash, "info", os.path.basename(dest) + ".trashinfo")
        with open(info, "rb") as f:
            content = f.read().decode()
        assert content.startswith("[Trash Info]\nPath=")
        assert "DeletionDate=" in content
        # same name twice -> distinct trash entries
        with open(p, "w") as f:
            f.write("t2")
        dest2 = fsutil.move_to_trash(p)
        assert dest2 != dest
        restored = fsutil.restore_from_trash(trash, os.path.basename(dest))
        assert restored == p and os.path.exists(p)
        assert fsutil.is_trash_files_dir(os.path.join(trash, "files")) == trash


def test_display_name_never_raises():
    assert fsutil.display_name(os.fsdecode(b"a\xffb")) == "a�b"
    assert fsutil.display_name("x\ny") == "x⏎y"
    assert fsutil.validate_new_name("a/b")
    assert fsutil.validate_new_name("..")
    assert fsutil.validate_new_name("ok.txt") is None


def test_model_scans_100k_without_blocking():
    from gexplorer.dirmodel import DirModel
    from gexplorer.icons import IconCache
    with tempfile.TemporaryDirectory() as d:
        for i in range(100_000):
            os.close(os.open(os.path.join(d, f"f{i:06d}.txt"), os.O_CREAT | os.O_WRONLY, 0o644))
        m = DirModel(IconCache())
        t0 = time.perf_counter()
        m.set_path(d)
        call_return = time.perf_counter() - t0
        max_gap = 0.0
        last = time.perf_counter()
        while m.loading:
            app.processEvents()
            now = time.perf_counter()
            max_gap = max(max_gap, now - last)
            last = now
            time.sleep(0.005)
        total = time.perf_counter() - t0
        print(f"\n100k: set_path returned in {call_return*1000:.1f} ms; loaded in {total:.2f} s; "
              f"longest UI-thread stall {max_gap*1000:.0f} ms")
        assert m.rowCount() == 100_001          # + ".."
        assert call_return < 0.2
        t1 = time.perf_counter()
        m.sort(1, Qt.DescendingOrder)
        m.set_filter("*99*")
        print(f"sort+filter: {(time.perf_counter() - t1)*1000:.0f} ms, {m.rowCount()-1} matches")
        assert m.rowCount() - 1 == sum(1 for i in range(100_000) if "99" in f"f{i:06d}.txt")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
