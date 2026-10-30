"""Thumbnails + preview panel tests (headless). Needs ffmpeg + pdftoppm for full coverage.

Run: QT_QPA_PLATFORM=offscreen python tests/test_preview.py
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

H = tempfile.mkdtemp(prefix="gx-prev-")
os.environ.update(HOME=H, XDG_CONFIG_HOME=f"{H}/.config", XDG_DATA_HOME=f"{H}/.local/share",
                  XDG_CACHE_HOME=f"{H}/.cache")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtCore import QMimeDatabase, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPageSize, QPainter, QPdfWriter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from gsexplorer import preview as pv  # noqa: E402
from gsexplorer import thumbs as th  # noqa: E402
from gsexplorer import themes  # noqa: E402
from gsexplorer.config import Config  # noqa: E402
from gsexplorer.mainwindow import MainWindow  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def pump(sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout=15):
    t = time.time()
    while not cond():
        pump(0.05)
        if time.time() - t > timeout:
            return False
    return True


# ---------------------------------------------------------------- fixtures
D = os.path.join(H, "My Files é")          # space + non-ASCII in path: URI escaping matters
os.makedirs(D)
img = QImage(1600, 1000, QImage.Format_RGB32)
img.fill(QColor("#3fc1b0"))
img.save(f"{D}/photo.jpg")
w = QPdfWriter(f"{D}/report.pdf")
w.setPageSize(QPageSize(QPageSize.Letter))
p = QPainter(w)
p.setFont(QFont("Sans", 30))
p.drawText(800, 1500, "Quarterly report")
w.newPage()
p.end()
with zipfile.ZipFile(f"{D}/notes.docx", "w") as z:
    z.writestr("word/document.xml",
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               '<w:p><w:r><w:t>Security+ plan</w:t></w:r></w:p><w:p><w:r><w:t>Domain 1</w:t></w:r></w:p>'
               '</w:body></w:document>')
with open(f"{D}/code.py", "w") as f:
    f.write("print('hi')\n")
with open(f"{D}/broken.pdf", "w") as f:
    f.write("not a pdf")
os.mkfifo(f"{D}/pipe")
have_ffmpeg = shutil.which("ffmpeg") is not None
if have_ffmpeg:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=5:size=320x240:rate=25", "-f", "lavfi", "-i", "sine=duration=5",
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", f"{D}/clip.mp4"], check=True)

db = QMimeDatabase()
tmap = th.load_thumbnailers()

# ---------------------------------------------------------------- spec compliance
if shutil.which("gio"):
    out = subprocess.run(["gio", "info", "-a", "standard::name", f"{D}/photo.jpg"], capture_output=True,
                         text=True).stdout
    uri = next((l.split("uri:", 1)[1].strip() for l in out.splitlines() if l.startswith("uri:")), "")
    check("file URI matches GLib (cache shared with Nautilus)", uri == th.file_uri(f"{D}/photo.jpg"))

t = th.thumbnail(f"{D}/photo.jpg", 256, "image/jpeg", tmap)
check("image thumbnail ≤256 keeps aspect", t is not None and t.width() == 256 and t.height() == 160)
cp = th.cache_path(f"{D}/photo.jpg", "large")
saved = QImage(cp)
check("cache file written with Thumb::URI/MTime",
      not saved.isNull() and saved.text("Thumb::URI") == th.file_uri(f"{D}/photo.jpg")
      and saved.text("Thumb::MTime") == str(int(os.stat(f"{D}/photo.jpg").st_mtime)))
check("cache file mode 0600, dir 0700", stat.S_IMODE(os.stat(cp).st_mode) == 0o600
      and stat.S_IMODE(os.stat(os.path.dirname(cp)).st_mode) == 0o700)
os.utime(f"{D}/photo.jpg", (time.time() + 5, time.time() + 5))
check("stale cache rejected after file changes", th.load_cached(f"{D}/photo.jpg", "large",
                                                                 int(os.stat(f"{D}/photo.jpg").st_mtime)) is None)
if shutil.which("pdftoppm"):
    t = th.thumbnail(f"{D}/report.pdf", 256, "application/pdf", tmap)
    check("PDF first-page thumbnail", t is not None and max(t.width(), t.height()) == 256)
    t = th.thumbnail(f"{D}/broken.pdf", 256, "application/pdf", tmap)
    check("broken PDF -> fail entry, no retry", t is None and os.path.exists(th.fail_path(f"{D}/broken.pdf")))
if have_ffmpeg:
    t = th.thumbnail(f"{D}/clip.mp4", 256, "video/mp4", tmap)
    check("video frame thumbnail", t is not None and t.width() > 0)
check("FIFO never thumbnailed", th.thumbnail(f"{D}/pipe", 128, "inode/fifo", tmap) is None)
print("   sandbox (bwrap):", th.sandbox_available())
check("office text extraction", "Security+ plan\nDomain 1" in pv.office_text(f"{D}/notes.docx", "docx"))

# ---------------------------------------------------------------- UI
cfg = Config()
themes.apply(app, "Dark", themes.load_themes())
win = MainWindow(cfg, None)
win.resize(1400, 820)
win.show()
L = win.left.current()
L.navigate(D)
pump(0.5)
win.a_preview.setChecked(True)
win.toggle_preview()
pump(0.3)
check("preview panel visible", win.preview.isVisible())


def show(name):
    L.select_path(os.path.join(D, name))
    win.update_actions()
    pump(0.4)


show("photo.jpg")
check("image preview shown", wait(lambda: win.preview.stack.currentWidget() is win.preview.image))
check("image dimensions detail", wait(lambda: "1600 × 1000" in win.preview._detail_rows.get("Dimensions",
                                                                                              type("x", (), {"text": lambda s: ""})()).text()))
show("code.py")
check("text preview", wait(lambda: win.preview.stack.currentWidget() is win.preview.text
                           and "print('hi')" in win.preview.text.toPlainText()))
show("notes.docx")
check("docx preview shows document text",
      wait(lambda: win.preview.stack.currentWidget() is win.preview.doc and "Domain 1" in win.preview.doc_text.toPlainText()))
if shutil.which("pdftoppm"):
    show("report.pdf")
    check("pdf preview image + page count",
          wait(lambda: win.preview.stack.currentWidget() is win.preview.image
               and ("Pages" not in win.preview._detail_rows or win.preview._detail_rows["Pages"].text() == "2")))
show("pipe")
check("FIFO preview doesn't block", win.preview.stack.currentWidget() is win.preview.big_icon)
if have_ffmpeg:
    show("clip.mp4")
    check("video shows media player", win.preview.stack.currentWidget() is win.preview.media)
    win.preview.media.toggle()
    ok = wait(lambda: win.preview.media.pos.maximum() > 0, 15)
    check("video decodes (duration known)", ok and abs(win.preview.media.pos.maximum() - 5000) < 600)
    win.preview.media.stop()
    pump(0.2)
    check("VLC button only when VLC installed", win.preview.media.vlc_btn.isHidden() == (shutil.which("vlc") is None))

# icon view thumbnails
win.set_view_mode("icons")
pump(0.3)
check("icon view thumbnails generated",
      wait(lambda: os.path.join(D, "photo.jpg") in L.model._thumb_icons
           and (shutil.which("pdftoppm") is None or os.path.join(D, "report.pdf") in L.model._thumb_icons), 20))
before = L.icons.iconSize().width()
L.zoom(1)
check("zoom changes icon size", L.icons.iconSize().width() > before and L.model.thumb_px == L.icons.iconSize().width())
win.grab().save(os.path.join(os.environ.get("GX_SHOT_DIR", H), "preview_test.png"))
win.thumbs.mode = "never"
check("thumbnails can be turned off", not win.thumbs.enabled_for(os.path.join(D, "photo.jpg")))

print(f"\n{sum(results)}/{len(results)} preview checks passed")
sys.exit(0 if all(results) else 1)
