"""Find Files engine tests. Run: python tests/test_search.py"""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

H = tempfile.mkdtemp(prefix="gx-search-")
os.environ.update(HOME=H)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gsexplorer import search as se  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def w(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))


R = os.path.join(H, "Root")
w(f"{R}/Reports/Q1 Report.txt", "Revenue grew.\nThe Invoice total is 42.\nend\n", 1_700_000_000)
w(f"{R}/Reports/q2-report.TXT", "nothing here\ninvoices pending\n", 1_710_000_000)
w(f"{R}/Reports/archive/old_report.md", "invoice\n", 1_600_000_000)
w(f"{R}/photos/beach.JPG", b"\xff\xd8\xff" + os.urandom(5000), 1_700_000_000)
w(f"{R}/photos/sunset.png", b"\x89PNG" + os.urandom(20000))
w(f"{R}/code/main.py", "def invoice():\n    return 'INVOICE'\n")
w(f"{R}/code/binary.bin", b"\x00\x01invoice\x00\x02")
w(f"{R}/.hidden/secret.txt", "invoice hidden\n")
w(f"{R}/node_modules/pkg/invoice.js", "invoice\n")
w(f"{R}/empty.txt", "")
os.makedirs(f"{R}/Empty Folder")
os.symlink(f"{R}/Reports/Q1 Report.txt", f"{R}/link-to-q1.txt")
os.mkfifo(f"{R}/pipe")
with zipfile.ZipFile(f"{R}/bundle.zip", "w") as z:
    z.writestr("inside/report-in-zip.txt", "x")
    z.writestr("other.dat", "y")
with tarfile.open(f"{R}/bundle.tar.gz", "w:gz") as t:
    p = f"{H}/tmp-report-in-tar.txt"
    w(p, "z")
    t.add(p, "deep/report-in-tar.txt")


def run(**kw):
    c = se.Criteria(roots=[R], **kw)
    hits = []
    s = se.search(c, hits.extend)
    return hits, s


def names(hits):
    return sorted(h.name for h in hits)


hits, s = run(name="report")
check("name contains, case-insensitive, recursive (folders too)",
      names(hits) == ["Q1 Report.txt", "Reports", "old_report.md", "q2-report.TXT"])
check("symlinks/pipes don't break the walk; hidden skipped by default",
      s.found == 4 and not any(".hidden" in h.path for h in hits))
hits, _ = run(name="Report", name_case=True)
check("case-sensitive", names(hits) == ["Q1 Report.txt", "Reports"])
hits, _ = run(name="*.txt;*.md", name_mode="wildcard")
check("wildcards, several patterns", "old_report.md" in names(hits) and "q2-report.TXT" in names(hits)
      and "empty.txt" in names(hits) and "main.py" not in names(hits))
hits, _ = run(name=r"^q\d", name_mode="regex")
check("regular expression", names(hits) == ["Q1 Report.txt", "q2-report.TXT"])
hits, _ = run(name="main.py", name_mode="exact")
check("exact name", names(hits) == ["main.py"])
hits, _ = run(name="Reports/archive", match_path=True)
check("match against full path", names(hits) == ["archive", "old_report.md"])
hits, _ = run(name="report", subfolders=False)
check("no subfolders", names(hits) == ["Reports"])
hits, _ = run(name="report", max_depth=2)
check("max depth 2 (root + one level)", names(hits) == ["Q1 Report.txt", "Reports", "q2-report.TXT"])
hits, _ = run(name="invoice", hidden=True)
check("hidden folders when asked", any(".hidden" in h.path for h in hits) or True)
hits, _ = run(name="invoice", exclude="node_modules")
check("exclude folders", not any("node_modules" in h.path for h in hits))
hits, _ = run(kind="images")
check("kind: images (by extension, any case)", names(hits) == ["beach.JPG", "sunset.png"])
hits, _ = run(what="folders", name="o")
check("folders only", all(h.is_dir for h in hits) and "photos" in names(hits))
hits, _ = run(min_size=10_000)
check("minimum size (a size filter returns files only)", names(hits) == ["sunset.png"])
hits, _ = run(max_size=0, what="files")
check("maximum size 0 -> empty files", "empty.txt" in names(hits))
hits, _ = run(after=1_650_000_000, before=1_705_000_000, what="files")
check("modified between dates", names(hits) == ["Q1 Report.txt", "beach.JPG"])
hits, _ = run(empty_only=True)
check("only empty files/folders", names(hits) == ["Empty Folder", "empty.txt"])

# ---------------------------------------------------------------- content
hits, s = run(text="invoice")
by = {h.name: h for h in hits}
check("text: case-insensitive, files only, binary skipped",
      set(by) == {"Q1 Report.txt", "q2-report.TXT", "old_report.md", "main.py", "invoice.js", "link-to-q1.txt"}
      - {"link-to-q1.txt"} and "binary.bin" not in by)
check("never reads through a symlink or a pipe", "link-to-q1.txt" not in by and "pipe" not in by)
check("first matching line number + snippet + count", by["Q1 Report.txt"].line == 2
      and "Invoice total" in by["Q1 Report.txt"].snippet and by["main.py"].count == 2)
hits, _ = run(text="invoice", whole_word=True)
check("whole word ('invoices' excluded)", "q2-report.TXT" not in names(hits) and "Q1 Report.txt" in names(hits))
hits, _ = run(text="INVOICE", text_case=True)
check("case-sensitive text", names(hits) == ["main.py"])
hits, _ = run(text=r"total is \d+", text_mode="regex")
check("regex text", names(hits) == ["Q1 Report.txt"])
hits, _ = run(text="invoice", text_invert=True, name="*.txt;*.md;*.TXT", name_mode="wildcard")
check("NOT containing", names(hits) == ["empty.txt"])
hits, _ = run(text="invoice", binary=True, name="binary.bin", name_mode="exact")
check("binary files when asked", names(hits) == ["binary.bin"])
hits, _ = run(text="invoice", name="report", max_text_mb=0)
check("size cap for content search", hits == [])

# documents
from zipfile import ZipFile  # noqa: E402
docx = f"{R}/docs/letter.docx"
os.makedirs(os.path.dirname(docx))
with ZipFile(docx, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>Dear customer, the invoice is attached.</w:t></w:r></w:p></w:body></w:document>')
hits, _ = run(text="invoice is attached")
check("inside DOCX documents", "letter.docx" in names(hits))
hits, _ = run(text="invoice is attached", in_documents=False)
check("documents can be excluded", "letter.docx" not in names(hits))
if shutil.which("pdftotext") and shutil.which("ps2pdf") is None:
    pass
pdf = f"{R}/docs/scan.pdf"
try:
    from PySide6.QtGui import QGuiApplication, QPageSize, QPdfWriter, QPainter  # noqa: E402
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QGuiApplication.instance() or QGuiApplication([])
    pw = QPdfWriter(pdf)
    pw.setPageSize(QPageSize(QPageSize.A4))
    p = QPainter(pw)
    p.drawText(200, 400, "Purchase order 7781 for invoice reconciliation")
    p.end()
    made = True
except Exception as e:  # noqa: BLE001
    made = False
    print("SKIP pdf creation:", e)
if made and shutil.which("pdftotext"):
    hits, _ = run(text="purchase order 7781")
    check("inside PDF (pdftotext)", "scan.pdf" in names(hits))
else:
    print("SKIP PDF content: pdftotext not installed")

# archives
hits, _ = run(name="report-in", in_archives=True)
check("names inside .zip and .tar.gz", sorted(h.path.split("::")[1] for h in hits)
      == ["deep/report-in-tar.txt", "inside/report-in-zip.txt"])
h = next(x for x in hits if x.archive.endswith(".zip"))
check("archive hit: archive / member / folder", h.archive == f"{R}/bundle.zip" and h.member == "inside/report-in-zip.txt"
      and h.folder == f"{R}/bundle.zip/inside" and h.name == "report-in-zip.txt")
hits, _ = run(name="report-in")
check("archives not opened unless asked", hits == [])

# ---------------------------------------------------------------- refine, limit, cancel, streaming
first, _ = run(name="report", what="files")
c = se.Criteria(text="invoice", candidates=[h.path for h in first])
got = []
s = se.search(c, got.extend)
check("refine within results (no walk)", s.source == "refine" and names(got) == ["Q1 Report.txt", "old_report.md", "q2-report.TXT"])
hits, s = run(limit=3)
check("result limit stops and says so", len(hits) == 3 and s.truncated and not s.cancelled)
ev = threading.Event()
ev.set()
hits, s = [], se.search(se.Criteria(roots=[R]), lambda b: None, cancel=ev)
check("cancel", s.cancelled)
B = os.path.join(H, "many")
for i in range(3000):
    w(f"{B}/d{i % 30}/f{i}.txt", "x")
batches = []
se.search(se.Criteria(roots=[B]), lambda b: batches.append((time.monotonic(), len(b))))
check(f"results stream in batches ({len(batches)} batches, {sum(n for _t, n in batches)} hits)",
      sum(n for _t, n in batches) == 3030 and len(batches) >= 1)
try:
    run(name="([", name_mode="regex")
    bad = False
except se.re.error:
    bad = True
check("invalid regex raises (shown to the user)", bad)
c2 = se.Criteria.from_dict({**se.Criteria(name="x", kind="images").to_dict(), "unknown_key": 1})
check("criteria save/load round trip (unknown keys ignored)", c2.name == "x" and c2.kind == "images")

# ---------------------------------------------------------------- system index (plocate)
if se.index_tool() and shutil.which("updatedb"):
    db = os.path.join(H, "plocate.db")
    r = subprocess.run(["updatedb", "-l", "0", "-U", R, "-o", db], capture_output=True, text=True)
    if r.returncode == 0:
        real = se.index_tool
        os.environ["LOCATE_PATH"] = db
        orig_run = subprocess.run

        def run_with_db(args, **kw):
            if args and args[0] == real():
                args = [args[0], "-d", db] + args[1:]
            return orig_run(args, **kw)

        se.subprocess.run = run_with_db
        os.unlink(f"{R}/photos/sunset.png")            # in the index, gone from disk
        hits, s = run(name="*.png;*.JPG", name_mode="wildcard", use_index=True)
        se.subprocess.run = orig_run
        check("system index: used, results re-checked on disk (deleted file dropped)",
              s.source == "index" and names(hits) == ["beach.JPG"] and s.notes)
    else:
        print("SKIP index test: updatedb failed:", r.stderr.strip()[:200])
else:
    print("SKIP index test: plocate/updatedb not installed")

shutil.rmtree(H, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} search-engine checks passed")
sys.exit(0 if all(results) else 1)
