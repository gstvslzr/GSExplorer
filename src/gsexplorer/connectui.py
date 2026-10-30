"""Connect menu, Connect-to-Server dialog and the controller that runs connections."""
from __future__ import annotations

import os
import shlex
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from . import APP_NAME, connect as cn, system


def _muted(t: str) -> QLabel:
    l = QLabel(t)
    l.setObjectName("Muted")
    l.setWordWrap(True)
    return l


class ConnectDialog(QDialog):
    """Connect to Server — one form for file shares, SSH and remote desktops."""

    def __init__(self, parent, conn: cn.Connection | None = None, preset: str | None = None, edit: bool = False):
        super().__init__(parent)
        self.setWindowTitle(("Edit connection" if edit else "Connect to Server") + f" — {APP_NAME}")
        self.setMinimumWidth(520)
        c = conn or cn.Connection(protocol=preset or "smb")
        self.conn_id = c.id
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.proto = QComboBox()
        for key, p in cn.PROTOCOLS.items():
            self.proto.addItem(p["label"], key)
        self.proto.setCurrentIndex(self.proto.findData(c.protocol))
        self.host = QLineEdit(c.host)
        self.host.setPlaceholderText("server name or IP address, e.g. nas.local or 192.168.1.20")
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("default")
        self.port.setValue(c.port)
        self.share = QLineEdit(c.share)
        self.share.setPlaceholderText("share name, e.g. Documents")
        self.path = QLineEdit(c.path)
        self.path.setPlaceholderText("optional folder, e.g. /home/gus or projects/2026")
        self.user = QLineEdit(c.user)
        self.user.setPlaceholderText("leave empty to be asked")
        self.domain = QLineEdit(c.domain)
        self.domain.setPlaceholderText("optional, e.g. WORKGROUP or CORP")
        self.key = QLineEdit(c.identity_file)
        self.key.setPlaceholderText("optional private key, e.g. ~/.ssh/id_ed25519")
        self.name = QLineEdit(c.name)
        self.name.setPlaceholderText("name shown in the Connect menu")
        self.save = QCheckBox("Save in the Connect menu and sidebar")
        self.save.setChecked(bool(conn) or edit)
        self.remember = QCheckBox("Remember password in the system keyring")
        self.remember.setChecked(c.remember_password)
        if not cn.keyring_available():
            self.remember.setEnabled(False)
            self.remember.setText("Remember password (install libsecret-tools to enable)")
        self.rows = {}
        for label, w in (("Type:", self.proto), ("Server:", self.host), ("Port:", self.port),
                         ("Share:", self.share), ("Folder:", self.path), ("User:", self.user),
                         ("Domain:", self.domain), ("SSH key:", self.key), ("Name:", self.name)):
            form.addRow(label, w)
            self.rows[w] = label
        form.addRow("", self.save)
        form.addRow("", self.remember)
        self.form = form
        lay.addLayout(form)
        self.uri = _muted("")
        self.warn = QLabel()
        self.warn.setObjectName("Danger")
        self.warn.setWordWrap(True)
        self.hint = _muted("")
        lay.addWidget(self.uri)
        lay.addWidget(self.warn)
        lay.addWidget(self.hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("Save" if edit else "Connect")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.host, self.share, self.path, self.user, self.domain, self.key, self.name):
            w.textChanged.connect(self._update)
        self.port.valueChanged.connect(self._update)
        self.proto.currentIndexChanged.connect(self._update)
        self.save.toggled.connect(self._update)
        self._update()

    def connection(self) -> cn.Connection:
        return cn.Connection(name=self.name.text().strip(), protocol=self.proto.currentData(),
                             host=self.host.text().strip(), port=self.port.value(),
                             share=self.share.text().strip().strip("/"), path=self.path.text().strip(),
                             user=self.user.text().strip(), domain=self.domain.text().strip(),
                             remember_password=self.remember.isChecked() and self.remember.isEnabled(),
                             identity_file=self.key.text().strip(), id=self.conn_id)

    def _update(self, *_):
        c = self.connection()
        kind = cn.PROTOCOLS[c.protocol]["kind"]
        f = self.form
        f.setRowVisible(self.share, c.protocol == "smb")
        f.setRowVisible(self.path, kind == "mount")
        f.setRowVisible(self.domain, c.protocol in ("smb", "rdp"))
        f.setRowVisible(self.key, c.protocol == "ssh")
        f.setRowVisible(self.name, self.save.isChecked())
        f.setRowVisible(self.remember, kind == "mount")
        self.port.setSpecialValueText(f"default ({cn.PROTOCOLS[c.protocol]['port']})")
        if kind == "mount" and c.host:
            self.uri.setText("Address: " + cn.gio_uri(c))
        elif kind == "terminal" and c.host:
            self.uri.setText("Command: " + shlex.join(cn.ssh_command(c)))
        elif kind == "desktop":
            client = cn.desktop_client(c.protocol)
            self.uri.setText(f"Client: {client}" if client else "")
        else:
            self.uri.setText("")
        self.warn.setText("Unencrypted: user name, password and files travel in clear text. Prefer "
                          + ("FTPS or SFTP." if c.protocol == "ftp" else "WebDAV over HTTPS.")
                          if c.protocol in cn.INSECURE else "")
        hints = {
            "smb": "Windows PCs, NAS boxes and Samba servers. Mounted through GVFS; the share opens as a folder.",
            "sftp": "Any server you can SSH into. Uses your ~/.ssh keys/agent; otherwise asks for the password.",
            "davs": "Nextcloud: davs://host/remote.php/dav/files/USER — put that path in Folder.",
            "nfs": "NFS exports (no password; access is by host).",
            "ssh": "Opens an SSH session in your terminal. Passwords/passphrases are typed there.",
            "rdp": "Opens Remmina or FreeRDP. The client asks for your password; it is never passed on a "
                   "command line. FreeRDP trusts a server certificate on first use and rejects changes.",
            "vnc": "Opens Remmina, GNOME Connections or TigerVNC.",
        }
        self.hint.setText(hints.get(c.protocol, ""))
        self.ok_btn.setEnabled(cn.validate(c) is None)

    def _accept(self):
        err = cn.validate(self.connection())
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        self.accept()


class _Sig(QObject):
    done = Signal(object, object, object)       # connection, MountResult, context dict


class ConnectController(QObject):
    """Runs connections off the UI thread and handles password / host-key prompts."""
    changed = Signal()                          # mounts or saved connections changed

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._sig = _Sig()
        self._sig.done.connect(self._mount_done)
        self.busy: set[str] = set()

    # ------------------------------------------------------------------ saved connections
    def saved(self) -> list[cn.Connection]:
        return cn.load_connections()

    def save(self, c: cn.Connection) -> None:
        conns = [x for x in self.saved() if x.id != c.id] + [c]
        cn.save_connections(conns)
        self.changed.emit()

    def delete(self, c: cn.Connection) -> None:
        if QMessageBox.question(self.win, APP_NAME, f"Remove saved connection “{c.title}”? "
                                "Its remembered password (if any) is removed from the keyring.") != QMessageBox.Yes:
            return
        cn.keyring_clear(c)
        cn.save_connections([x for x in self.saved() if x.id != c.id])
        self.changed.emit()

    def edit(self, c: cn.Connection) -> None:
        dlg = ConnectDialog(self.win, c, edit=True)
        if dlg.exec():
            new = dlg.connection()
            if c.remember_password and not new.remember_password:
                cn.keyring_clear(c)
            self.save(new)

    # ------------------------------------------------------------------ entry points
    def connect_dialog(self, preset: str | None = None) -> None:
        dlg = ConnectDialog(self.win, preset=preset)
        if not dlg.exec():
            return
        c = dlg.connection()
        if dlg.save.isChecked():
            self.save(c)
        self.connect(c)

    def connect(self, c: cn.Connection) -> None:
        try:
            if c.kind == "terminal":
                system.run_in_terminal(shlex.join(cn.ssh_command(c)), None, self.win.cfg["terminal"],
                                       keep_open=False)
                self.win.flash(f"Opening SSH session to {c.host} in your terminal…")
                return
            if c.kind == "desktop":
                argv, term = cn.desktop_command(c)
                if term:
                    system.run_in_terminal(shlex.join(argv), None, self.win.cfg["terminal"], keep_open=True)
                else:
                    system._spawn(argv)
                self.win.flash(f"Starting {argv[0]} for {c.host}…")
                return
        except OSError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return
        password = cn.keyring_lookup(c) if c.remember_password else None
        self._start_mount(c, password, "")

    def _start_mount(self, c: cn.Connection, password: str | None, trust: str) -> None:
        if c.id in self.busy:
            return
        self.busy.add(c.id)
        self.win.flash(f"Connecting to {c.display_target()}…", 60000)
        QApplication.setOverrideCursor(Qt.BusyCursor)

        def work():
            r = cn.mount(c, password, trust)
            self._sig.done.emit(c, r, {"password": password})

        threading.Thread(target=work, daemon=True).start()

    def _mount_done(self, c: cn.Connection, r: cn.MountResult, ctx: dict) -> None:
        QApplication.restoreOverrideCursor()
        self.busy.discard(c.id)
        password = ctx.get("password")
        if r.ok:
            if c.remember_password and password:
                if not cn.keyring_store(c, password):
                    self.win.flash("Connected, but the password couldn't be saved to the keyring.")
            self.changed.emit()
            if r.path:
                self.win.open_in_new_tab(r.path)
                self.win.flash(f"Connected to {c.display_target()}")
            else:
                QMessageBox.information(self.win, APP_NAME, r.message)
            return
        if r.host_key_changed:
            QMessageBox.critical(self.win, APP_NAME, r.message)
            return
        if r.host_key_question:
            box = QMessageBox(QMessageBox.Warning, APP_NAME,
                              "GSExplorer+ hasn't connected to this server before. Check the fingerprint with "
                              "the server's administrator before trusting it.\n\n" + r.host_key_question,
                              QMessageBox.Cancel, self.win)
            trust = box.addButton("Trust and connect", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is trust:
                self._start_mount(c, password, r.trust_choice)
            return
        if r.need_password:
            if password and c.remember_password:
                cn.keyring_clear(c)                     # stale stored password
            pw, ok = QInputDialog.getText(
                self.win, "Password required",
                (f"{r.message}\n\n" if password else "") +
                f"Password for {c.user or 'your account'} on {c.host}:", QLineEdit.Password)
            if ok:
                self._start_mount(c, pw, "")
            return
        QMessageBox.warning(self.win, APP_NAME, f"Couldn't connect to {c.display_target()}:\n\n{r.message}")

    # ------------------------------------------------------------------ mounts
    def disconnect(self, m: cn.NetMount) -> None:
        for pane in (self.win.left, self.win.right):
            for v in pane.views():
                if m.path and (v.path == m.path or v.path.startswith(m.path.rstrip("/") + "/")):
                    v.navigate("~")
        if m.source == "rclone":
            ok, msg = cn.rclone_unmount(m.path)
        else:
            ok, msg = cn.unmount(m.path or m.uri)
        if not ok:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't disconnect {m.name}:\n{msg}")
        self.changed.emit()

    def disconnect_all(self) -> None:
        for m in cn.list_gio_mounts():
            if m.mounted:
                self.disconnect(m)

    def open_mount(self, m: cn.NetMount) -> None:
        if m.mounted and m.path:
            self.win.active_view().navigate(m.path)
            return
        self.win.flash(f"Connecting {m.name}…", 60000)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        r = cn.rclone_mount(m.uri.split(":", 1)[1]) if m.source == "rclone" else cn.mount_uri(m.uri)
        QApplication.restoreOverrideCursor()
        if r.ok and r.path:
            self.win.open_in_new_tab(r.path)
            self.changed.emit()
        else:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't connect {m.name}:\n{r.message}")

    def rclone_connect(self, remote: str) -> None:
        self.open_mount(cn.NetMount(remote, "", f"rclone:{remote}", False, "rclone"))

    def online_accounts(self) -> None:
        argv = cn.online_accounts_command()
        if not argv:
            QMessageBox.information(self.win, APP_NAME, "GNOME Settings isn't available. OneDrive/Google Drive "
                                                        "can also be added with rclone (sudo apt install rclone).")
            return
        system._spawn(argv)
        QMessageBox.information(
            self.win, APP_NAME,
            "In Settings > Online Accounts add Microsoft 365 (OneDrive) or Google and turn on “Files”.\n\n"
            "The drive then appears under Network in GSExplorer+'s sidebar and in Connect > Network drives.")

    def rclone_config(self) -> None:
        try:
            system.run_in_terminal("rclone config", os.path.expanduser("~"), self.win.cfg["terminal"])
        except OSError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))

    # ------------------------------------------------------------------ menu
    def fill_menu(self, m: QMenu) -> None:
        m.clear()
        m.addAction(self.win.a_connect)
        m.addAction("SSH terminal…", lambda: self.connect_dialog("ssh"))
        m.addAction("Remote desktop (RDP)…", lambda: self.connect_dialog("rdp"))
        m.addAction("VNC…", lambda: self.connect_dialog("vnc"))
        saved = self.saved()
        sm = m.addMenu("Saved connections")
        if not saved:
            a = sm.addAction("None yet — tick “Save” when connecting")
            a.setEnabled(False)
        for c in saved:
            sm.addAction(f"{c.title}  ({cn.PROTOCOLS[c.protocol]['label'].split(' (')[0]})",
                         lambda c=c: self.connect(c))
        if saved:
            sm.addSeparator()
            sm.addAction("Manage…", self.manage)
        m.addSeparator()
        nm = m.addMenu("Network drives")
        mounts = cn.list_gio_mounts()
        if not mounts:
            a = nm.addAction("No network drives connected")
            a.setEnabled(False)
        for mt in mounts:
            label = mt.name + ("" if mt.mounted else "  (not connected)")
            sub = nm.addMenu(label)
            sub.addAction("Open" if mt.mounted else "Connect", lambda mt=mt: self.open_mount(mt))
            if mt.mounted:
                sub.addAction("Disconnect", lambda mt=mt: self.disconnect(mt))
        if any(x.mounted for x in mounts):
            nm.addSeparator()
            nm.addAction("Disconnect all", self.disconnect_all)
        cm = m.addMenu("Cloud storage")
        cm.addAction("OneDrive / Google Drive (Online Accounts)…", self.online_accounts)
        if cn.rclone_available():
            cm.addSeparator()
            for r in cn.rclone_remotes():
                cm.addAction(f"{r} (rclone)", lambda r=r: self.rclone_connect(r))
            cm.addAction("Configure rclone remotes…", self.rclone_config)
        else:
            a = cm.addAction("rclone remotes (install rclone)")
            a.setEnabled(False)

    def manage(self) -> None:
        ManageDialog(self.win, self).exec()


class ManageDialog(QDialog):
    def __init__(self, win, ctl: ConnectController):
        super().__init__(win)
        self.ctl = ctl
        self.setWindowTitle(f"Saved connections — {APP_NAME}")
        self.resize(520, 360)
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Connect", self._connect), ("Edit…", self._edit), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self._fill()

    def _fill(self):
        self.list.clear()
        for c in self.ctl.saved():
            it = QListWidgetItem(f"{c.title}  —  {c.display_target()}"
                                 + ("   (password in keyring)" if c.remember_password else ""))
            it.setData(Qt.UserRole, c)
            self.list.addItem(it)

    def _cur(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _connect(self):
        c = self._cur()
        if c:
            self.accept()
            self.ctl.connect(c)

    def _edit(self):
        c = self._cur()
        if c:
            self.ctl.edit(c)
            self._fill()

    def _remove(self):
        c = self._cur()
        if c:
            self.ctl.delete(c)
            self._fill()
