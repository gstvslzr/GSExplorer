"""Small reusable widgets: breadcrumb path bar, function-key bar."""
from __future__ import annotations

import os

from PySide6.QtCore import QDir, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QCompleter, QFileSystemModel, QFrame, QHBoxLayout, QLineEdit,
                               QMenu, QPushButton, QStackedWidget, QStyle, QToolButton, QWidget)

from .fsutil import display_name
from .icons import themed


class _CrumbArea(QWidget):
    clicked_empty = Signal()

    def mouseReleaseEvent(self, ev):  # click on empty space -> edit mode
        if ev.button() == Qt.LeftButton:
            self.clicked_empty.emit()
        super().mouseReleaseEvent(ev)


class Breadcrumb(QFrame):
    """Clickable path segments; click empty space or Ctrl+L to type a path."""
    navigate = Signal(str)
    nav_back = Signal()
    nav_forward = Signal()
    nav_up = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Breadcrumb")
        self.path = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self.btn_back = self._nav_btn(["go-previous"], QStyle.SP_ArrowBack, "Back (Alt+Left)", self.nav_back)
        self.btn_fwd = self._nav_btn(["go-next"], QStyle.SP_ArrowForward, "Forward (Alt+Right)", self.nav_forward)
        self.btn_up = self._nav_btn(["go-up"], QStyle.SP_ArrowUp, "Up (Backspace)", self.nav_up)
        for b in (self.btn_back, self.btn_fwd, self.btn_up):
            lay.addWidget(b)
        self.stack = QStackedWidget()
        self.crumbs = _CrumbArea()
        self.crumbs.clicked_empty.connect(self.start_edit)
        self.crumb_lay = QHBoxLayout(self.crumbs)
        self.crumb_lay.setContentsMargins(0, 0, 0, 0)
        self.crumb_lay.setSpacing(0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a path and press Enter")
        fsm = QFileSystemModel(self.edit)
        fsm.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Hidden)
        fsm.setRootPath("/")
        comp = QCompleter(fsm, self.edit)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.edit.setCompleter(comp)
        self.edit.returnPressed.connect(self._commit)
        self.edit.installEventFilter(self)
        self.stack.addWidget(self.crumbs)
        self.stack.addWidget(self.edit)
        lay.addWidget(self.stack, 1)
        self._buttons: list[QToolButton] = []

    def _nav_btn(self, icon, sp, tip, sig) -> QToolButton:
        b = QToolButton()
        b.setIcon(themed(icon, sp))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(sig.emit)
        return b

    def set_nav_state(self, can_back: bool, can_fwd: bool, can_up: bool) -> None:
        self.btn_back.setEnabled(can_back)
        self.btn_fwd.setEnabled(can_fwd)
        self.btn_up.setEnabled(can_up)

    def set_path(self, path: str) -> None:
        self.path = path
        self._rebuild()
        if self.stack.currentWidget() is self.edit:
            self.edit.setText(display_name(path))

    def _segments(self) -> list[tuple[str, str]]:
        segs: list[tuple[str, str]] = []
        p = self.path
        home = os.path.expanduser("~")
        while True:
            if p == home:
                segs.append(("Home", p))
                break
            if p in ("/", ""):
                segs.append(("/", "/"))
                break
            segs.append((display_name(os.path.basename(p)), p))
            p = os.path.dirname(p)
        segs.reverse()
        return segs

    def _rebuild(self) -> None:
        while self.crumb_lay.count():
            it = self.crumb_lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._buttons = []
        segs = self._segments()
        # Collapse leading segments into a "…" menu when the bar is narrow.
        avail = max(120, self.crumbs.width() or 400)
        fm = self.fontMetrics()
        widths = [fm.horizontalAdvance(t) + 22 for t, _ in segs]
        start = 0
        while start < len(segs) - 1 and sum(widths[start:]) + (28 if start else 0) > avail:
            start += 1
        if start:
            more = QToolButton()
            more.setText("…")
            more.setPopupMode(QToolButton.InstantPopup)
            more.setFocusPolicy(Qt.NoFocus)
            m = QMenu(more)
            for t, p in segs[:start]:
                act = QAction(t, m)
                act.triggered.connect(lambda _=False, p=p: self.navigate.emit(p))
                m.addAction(act)
            more.setMenu(m)
            self.crumb_lay.addWidget(more)
        for i, (t, p) in enumerate(segs[start:], start):
            b = QToolButton()
            b.setText(t if t != "/" else "")
            if t == "/":
                b.setIcon(themed(["drive-harddisk", "computer"], QStyle.SP_DriveHDIcon))
                b.setToolTip("File system root")
            elif t == "Home" and p == os.path.expanduser("~"):
                b.setIcon(themed(["user-home"], QStyle.SP_DirHomeIcon))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setFocusPolicy(Qt.NoFocus)
            b.setProperty("current", i == len(segs) - 1)
            b.clicked.connect(lambda _=False, p=p: self.navigate.emit(p))
            self.crumb_lay.addWidget(b)
            self._buttons.append(b)
            if i < len(segs) - 1 and t != "/":
                sep = QToolButton()
                sep.setText("›")
                sep.setEnabled(False)
                sep.setFocusPolicy(Qt.NoFocus)
                self.crumb_lay.addWidget(sep)
        self.crumb_lay.addStretch(1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.path:
            self._rebuild()

    def start_edit(self) -> None:
        self.edit.setText(display_name(self.path))
        self.stack.setCurrentWidget(self.edit)
        self.edit.setFocus()
        self.edit.selectAll()

    def cancel_edit(self) -> None:
        self.stack.setCurrentWidget(self.crumbs)

    def _commit(self) -> None:
        text = self.edit.text().strip()
        self.cancel_edit()
        if text:
            # If the displayed (lossy) path is unchanged, keep the exact original.
            if text == display_name(self.path):
                return
            self.navigate.emit(os.path.expanduser(text))

    def eventFilter(self, obj, ev):
        if obj is self.edit:
            if ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
                self.cancel_edit()
                return True
            if ev.type() == ev.Type.FocusOut:
                pop = self.edit.completer().popup()
                if not (pop and pop.isVisible()):
                    self.cancel_edit()
        return super().eventFilter(obj, ev)


class FKeyBar(QFrame):
    """Total Commander-style function-key buttons bound to existing QActions."""

    def __init__(self, entries: list[tuple[str, QAction]], parent=None):
        super().__init__(parent)
        self.setObjectName("FKeyBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for label, action in entries:
            b = QPushButton(label)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(action.trigger)
            b.setToolTip(action.toolTip() or action.text().replace("&", ""))
            lay.addWidget(b, 1)
