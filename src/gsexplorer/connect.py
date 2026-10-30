# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Connect: network file systems, remote shells, remote desktops and cloud drives.

Backends (all standard Ubuntu components; GSExplorer+ itself contains no network code):
  * SMB, SFTP, FTP/FTPS, WebDAV(S), NFS  -> GVFS (`gio mount`), the same layer Nautilus uses.
    Mounted shares appear as normal folders under $XDG_RUNTIME_DIR/gvfs (FUSE), so every
    GSExplorer+ feature (copy, sync, preview...) works on them.
  * OneDrive / Google Drive / Nextcloud  -> GNOME Online Accounts volumes (mounted via gio),
    or rclone remotes if rclone is installed (`rclone mount`).
  * SSH terminal -> system `ssh` in your terminal emulator.
  * RDP / VNC    -> Remmina, FreeRDP (xfreerdp3/xfreerdp), GNOME Connections or vncviewer.

Credential handling:
  * Passwords are never put on a command line or in an environment variable; they are written
    to the child's stdin and dropped. Optional "remember" stores them in the desktop keyring
    via `secret-tool` (libsecret) — never in GSExplorer+'s files.
  * Unknown SSH host keys are shown to the user before trusting; a CHANGED host key is refused.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import quote, unquote

PROTOCOLS: dict[str, dict] = {
    "smb": {"label": "Windows share / NAS (SMB)", "kind": "mount", "port": 445},
    "sftp": {"label": "SSH file transfer (SFTP)", "kind": "mount", "port": 22},
    "ftp": {"label": "FTP", "kind": "mount", "port": 21},
    "ftps": {"label": "FTP over TLS (FTPS)", "kind": "mount", "port": 21},
    "davs": {"label": "WebDAV over HTTPS (Nextcloud, SharePoint…)", "kind": "mount", "port": 443},
    "dav": {"label": "WebDAV (unencrypted HTTP)", "kind": "mount", "port": 80},
    "nfs": {"label": "NFS export", "kind": "mount", "port": 2049},
    "ssh": {"label": "SSH terminal", "kind": "terminal", "port": 22},
    "rdp": {"label": "Remote Desktop (RDP)", "kind": "desktop", "port": 3389},
    "vnc": {"label": "VNC remote desktop", "kind": "desktop", "port": 5900},
}
INSECURE = {"ftp", "dav"}


@dataclass
class Connection:
    name: str = ""
    protocol: str = "smb"
    host: str = ""
    port: int = 0                      # 0 = protocol default
    share: str = ""                    # SMB share
    path: str = ""                     # remote folder / export path
    user: str = ""
    domain: str = ""                   # SMB / RDP domain
    remember_password: bool = False    # stored in system keyring, never in our files
    identity_file: str = ""            # SSH/SFTP private key (optional)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, d: dict) -> "Connection":
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k):
                setattr(c, k, v)
        if c.protocol not in PROTOCOLS:
            c.protocol = "smb"
        c.port = int(c.port or 0)
        return c

    @property
    def kind(self) -> str:
        return PROTOCOLS[self.protocol]["kind"]

    @property
    def title(self) -> str:
        return self.name or self.display_target()

    def display_target(self) -> str:
        who = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port else ""
        tail = f"/{self.share}" if self.share else ""
        tail += ("/" + self.path.strip("/")) if self.path.strip("/") else ""
        return f"{self.protocol}://{who}{self.host}{port}{tail}"


def validate(c: Connection) -> str | None:
    if not c.host.strip():
        return "Enter a server name or address."
    if re.search(r"[\s/@?#]", c.host.strip()):
        return "The server must be a host name or IP address (no spaces, slashes or @)."
    if c.port and not 0 < c.port < 65536:
        return "Port must be between 1 and 65535."
    if c.protocol == "smb" and not c.share.strip() and c.kind == "mount":
        return "Enter the share name (for example: Documents)."
    if c.user and re.search(r"[\s/:@]", c.user):
        return "The user name can't contain spaces, “/”, “:” or “@”."
    return None


def gio_uri(c: Connection) -> str:
    """GVFS URI for a mount-type connection. Credentials are never embedded (except user name)."""
    host = c.host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                                     # IPv6 literal
    port = f":{c.port}" if c.port and c.port != PROTOCOLS[c.protocol]["port"] else ""
    path = "/".join(quote(p) for p in c.path.strip("/").split("/") if p)
    if c.protocol == "smb":
        who = ""
        if c.user:
            who = (quote(c.domain) + ";" if c.domain else "") + quote(c.user) + "@"
        return f"smb://{who}{host}{port}/{quote(c.share.strip('/'))}/" + (path + "/" if path else "")
    if c.protocol == "nfs":
        return f"nfs://{host}{port}/{path}/"
    who = quote(c.user) + "@" if c.user else ""
    return f"{c.protocol}://{who}{host}{port}/" + (path + "/" if path else "")


# ----------------------------------------------------------------------------- storage

def _store_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "gsexplorer-plus", "connections.json")


def load_connections() -> list[Connection]:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            return [Connection.from_dict(d) for d in json.load(f).get("connections", [])]
    except (OSError, ValueError, AttributeError):
        return []


def save_connections(conns: list[Connection]) -> None:
    p = _store_path()
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".conn.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "connections": [asdict(c) for c in conns]}, f, indent=2, ensure_ascii=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ----------------------------------------------------------------------------- keyring (libsecret)

def keyring_available() -> bool:
    return shutil.which("secret-tool") is not None


def keyring_store(c: Connection, password: str) -> bool:
    if not keyring_available():
        return False
    try:
        p = subprocess.run(["secret-tool", "store", "--label", f"GSExplorer+: {c.title}",
                            "application", "gsexplorer-plus", "connection", c.id],
                           input=password, text=True, capture_output=True, timeout=15)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def keyring_lookup(c: Connection) -> str | None:
    if not keyring_available():
        return None
    try:
        for app_attr in ("gsexplorer-plus", "gexplorer"):   # "gexplorer": passwords saved before 26.8.0
            p = subprocess.run(["secret-tool", "lookup", "application", app_attr, "connection", c.id],
                               capture_output=True, text=True, timeout=15)
            if p.returncode == 0 and p.stdout:
                return p.stdout
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def keyring_clear(c: Connection) -> None:
    if keyring_available():
        for app_attr in ("gsexplorer-plus", "gexplorer"):
            subprocess.run(["secret-tool", "clear", "application", app_attr, "connection", c.id],
                           capture_output=True, timeout=15)


# ----------------------------------------------------------------------------- GVFS mounts

def gvfs_dir() -> str:
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(rt, "gvfs")


@dataclass
class MountResult:
    ok: bool
    path: str = ""                     # local FUSE path to open
    message: str = ""
    need_password: bool = False
    host_key_question: str = ""        # non-empty: ask user whether to trust this host key
    trust_choice: str = ""
    host_key_changed: bool = False


def _gio() -> str | None:
    return shutil.which("gio")


def _gvfs_entries() -> set[str]:
    try:
        return set(os.listdir(gvfs_dir()))
    except OSError:
        return set()


def _fuse_path_for(c: Connection, before: set[str]) -> str:
    """Locate the FUSE directory GVFS created for this mount."""
    after = _gvfs_entries()
    new = sorted(after - before)
    host = c.host.strip().lower()
    candidates = new or sorted(after)
    for name in candidates:
        n = name.lower()
        if f"server={host}" in n or f"host={host}" in n:
            if c.protocol == "smb" and c.share and f"share={c.share.lower()}" not in n:
                continue
            base = os.path.join(gvfs_dir(), name)
            sub = c.path.strip("/")
            return os.path.join(base, sub) if sub and os.path.isdir(os.path.join(base, sub)) else base
    return os.path.join(gvfs_dir(), new[0]) if new else ""


def mount(c: Connection, password: str | None = None, trust_choice: str = "", timeout: int = 60) -> MountResult:
    """Mount via `gio mount`, answering its prompts through stdin (never argv)."""
    gio = _gio()
    if not gio:
        return MountResult(False, message="`gio` not found. Install the gvfs and gvfs-backends packages.")
    uri = gio_uri(c)
    before = _gvfs_entries()
    lines: list[str] = []
    if trust_choice:
        lines.append(trust_choice)                     # answer to the host-key question
    if c.protocol == "smb":
        lines += [c.user or "guest", c.domain or "WORKGROUP", password or ""]
    elif password is not None:
        if not c.user and c.protocol not in ("sftp",):
            lines.append("")                          # user prompt (FTP/WebDAV may ask)
        lines.append(password)
    feed = "\n".join(lines) + "\n" if lines else ""
    argv = [gio, "mount", uri]
    try:
        p = subprocess.run(argv, input=feed, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return MountResult(False, message="The server didn't answer in time.")
    except OSError as e:
        return MountResult(False, message=str(e))
    out = p.stdout + "\n" + p.stderr
    clean = "\n".join(l for l in out.splitlines() if "WARNING" not in l and "KEYRING" not in l).strip()
    if re.search(r"has changed|POSSIBLE.*ATTACK|identity.*changed", out, re.I):
        return MountResult(False, host_key_changed=True,
                           message="The server's host key has CHANGED since you last connected. This can "
                                   "mean someone is intercepting the connection. GSExplorer+ will not connect. "
                                   "If the change is expected, remove the old key with "
                                   f"`ssh-keygen -R {c.host}` and try again.")
    if p.returncode == 0 or "already mounted" in out.lower():
        path = ""
        deadline = time.monotonic() + 8          # gvfs-fuse can publish the folder a moment later
        while time.monotonic() < deadline:
            path = _fuse_path_for(c, before)
            if path and os.path.isdir(path):
                break
            time.sleep(0.1)
        if not path or not os.path.isdir(path):
            return MountResult(True, "", "Connected, but the FUSE folder wasn't found. Install gvfs-fuse "
                                         "to browse network locations as folders.")
        return MountResult(True, path, "Connected.")
    m = re.search(r"\[(\d+)\]\s*(Log In Anyway|Connect Anyway|Yes)", out)
    if re.search(r"identity of the remote (computer|host)|can.t verify the identity|is unknown", out, re.I) and m:
        msg = re.sub(r"\[\d+\].*", "", clean).strip()
        return MountResult(False, host_key_question=msg, trust_choice=m.group(1),
                           message="Unknown server identity.")
    if re.search(r"Password:|password required|Authentication Required|Permission denied|"
                 r"Access denied|LOGON_FAILURE|incorrect", out, re.I):
        return MountResult(False, need_password=True,
                           message="Wrong user name or password." if password else "Password required.")
    last = [l for l in clean.splitlines() if l.strip()]
    return MountResult(False, message=(last[-1] if last else f"gio mount failed (exit {p.returncode})"))


def unmount(uri_or_path: str) -> tuple[bool, str]:
    gio = _gio()
    if not gio:
        return False, "gio not found"
    target = uri_or_path
    if uri_or_path.startswith(gvfs_dir() + "/"):
        # GIO maps a FUSE path back to its mount exactly (URIs would need the same user/domain)
        top = os.path.relpath(uri_or_path, gvfs_dir()).split("/", 1)[0]
        target = os.path.join(gvfs_dir(), top)
    p = subprocess.run([gio, "mount", "-u", target], capture_output=True, text=True, timeout=30)
    msg = "\n".join(l for l in (p.stdout + p.stderr).splitlines() if "WARNING" not in l).strip()
    if p.returncode == 0 and target.startswith(gvfs_dir()):
        # gvfs-fuse removes the folder asynchronously; wait so an immediate reconnect can't race it
        deadline = time.monotonic() + 5
        while os.path.exists(target) and time.monotonic() < deadline:
            time.sleep(0.05)
    return p.returncode == 0, msg or "Disconnected."


def fuse_dir_to_uri(path: str) -> str:
    """'…/gvfs/smb-share:server=nas,share=docs' -> 'smb://nas/docs/' (best effort)."""
    rel = os.path.relpath(path, gvfs_dir()).split("/", 1)[0]
    m = re.match(r"([a-z0-9-]+):(.*)", rel)
    if not m:
        return ""
    scheme, kv = m.group(1), dict(x.split("=", 1) for x in m.group(2).split(",") if "=" in x)
    host = unquote(kv.get("server") or kv.get("host") or "")
    port = f":{kv['port']}" if "port" in kv else ""
    user = unquote(kv.get("user", ""))
    who = f"{user}@" if user else ""
    if scheme == "smb-share":
        dom = unquote(kv.get("domain", ""))
        who = (f"{dom};" if dom and user else "") + who
        return f"smb://{who}{host}{port}/{kv.get('share', '')}/"
    if scheme in ("sftp", "ftp", "ftps", "dav", "davs", "nfs"):
        ssl = "s" if kv.get("ssl") == "true" and scheme == "dav" else ""
        return f"{scheme}{ssl}://{who}{host}{port}/"
    return ""


@dataclass
class NetMount:
    name: str
    path: str                          # FUSE path ('' if not available)
    uri: str
    mounted: bool
    source: str                        # gvfs | online-account | rclone


def _pretty(fuse_name: str) -> str:
    """'smb-share:server=nas,share=docs' -> 'docs on nas'."""
    m = re.match(r"([a-z0-9-]+):(.*)", fuse_name)
    if not m:
        return fuse_name
    kv = dict(x.split("=", 1) for x in m.group(2).split(",") if "=" in x)
    host = unquote(kv.get("server") or kv.get("host") or "?")
    if m.group(1) == "smb-share":
        return f"{unquote(kv.get('share', ''))} on {host}"
    user = unquote(kv.get("user", ""))
    return f"{user + '@' if user else ''}{host} ({m.group(1)})"


def _target_key(uri: str) -> tuple[str, str]:
    m = re.match(r"[a-z0-9+-]+://(?:[^@/]*@)?([^/:]+)(?::\d+)?/?([^/]*)", uri.lower())
    return (m.group(1), m.group(2)) if m else (uri.lower(), "")


def list_gio_mounts() -> list[NetMount]:
    """Mounted network locations plus not-yet-mounted online-account volumes (OneDrive, Google)."""
    out: list[NetMount] = []
    for name in sorted(_gvfs_entries()):
        full = os.path.join(gvfs_dir(), name)
        out.append(NetMount(_pretty(name), full, fuse_dir_to_uri(full) or name, True, "gvfs"))
    have = {_target_key(m.uri) for m in out}
    gio = _gio()
    text = ""
    if gio:
        try:
            text = subprocess.run([gio, "mount", "-l", "-i"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
    cur_vol = None
    for line in text.splitlines():
        ms = re.match(r"\s*Mount\(\d+\):\s*(.+?)\s*->\s*(\S+)", line)
        if ms:
            uri = ms.group(2)
            if re.match(r"(smb|sftp|ftps?|davs?|nfs|google-drive|onedrive|afp)://", uri) \
                    and _target_key(uri) not in have:
                out.append(NetMount(ms.group(1), "", uri, True, "gvfs"))     # no FUSE (gvfs-fuse missing)
                have.add(_target_key(uri))
            continue
        mv = re.match(r"\s*Volume\(\d+\):\s*(.+)", line)
        if mv:
            cur_vol = mv.group(1).strip()
            continue
        ma = re.match(r"\s*activation_root=(\S+)", line)
        if ma and cur_vol and re.match(r"(google-drive|onedrive|davs?|smb|sftp)://", ma.group(1)) \
                and _target_key(ma.group(1)) not in have:
            out.append(NetMount(cur_vol, "", ma.group(1), False, "online-account"))
            cur_vol = None
    return out + rclone_mounts()


def mount_uri(uri: str, timeout: int = 90) -> MountResult:
    """Mount an existing GVFS volume (e.g. a GNOME Online Accounts OneDrive/Google Drive)."""
    gio = _gio()
    if not gio:
        return MountResult(False, message="gio not found")
    before = _gvfs_entries()
    try:
        p = subprocess.run([gio, "mount", uri], capture_output=True, text=True, timeout=timeout, input="")
    except (OSError, subprocess.SubprocessError) as e:
        return MountResult(False, message=str(e))
    if p.returncode != 0 and "already mounted" not in (p.stdout + p.stderr).lower():
        msg = [l for l in (p.stdout + p.stderr).splitlines() if l.strip() and "WARNING" not in l]
        return MountResult(False, message=msg[-1] if msg else "mount failed")
    new = sorted(_gvfs_entries() - before)
    return MountResult(True, os.path.join(gvfs_dir(), new[0]) if new else "", "Connected.")


# ----------------------------------------------------------------------------- rclone (optional)

def rclone_available() -> bool:
    return shutil.which("rclone") is not None


def rclone_mount_root() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "gsexplorer-plus", "mnt")


def rclone_remotes() -> list[str]:
    if not rclone_available():
        return []
    try:
        p = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
        return [r.rstrip(":") for r in p.stdout.split() if r.endswith(":")]
    except (OSError, subprocess.SubprocessError):
        return []


def rclone_mounts() -> list[NetMount]:
    out = []
    root = rclone_mount_root()
    mounted = set()
    try:
        with open("/proc/self/mounts", "rb") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2 and parts[2] == b"fuse.rclone":
                    mounted.add(os.fsdecode(parts[1]))
    except OSError:
        pass
    for mp in sorted(mounted):
        if mp.startswith(root + "/"):
            name = os.path.basename(mp)
            out.append(NetMount(f"{name} (rclone)", mp, f"rclone:{name}", True, "rclone"))
    return out


def rclone_mount(remote: str) -> MountResult:
    if not rclone_available():
        return MountResult(False, message="rclone is not installed (sudo apt install rclone).")
    if not re.match(r"^[\w .-]+$", remote):
        return MountResult(False, message="Invalid remote name.")
    if not (shutil.which("fusermount3") or shutil.which("fusermount")):
        return MountResult(False, message="FUSE tools missing (sudo apt install fuse3).")
    mp = os.path.join(rclone_mount_root(), remote)
    os.makedirs(mp, mode=0o700, exist_ok=True)
    if os.path.ismount(mp):
        return MountResult(True, mp, "Already connected.")
    p = subprocess.run(["rclone", "mount", f"{remote}:", mp, "--vfs-cache-mode", "writes", "--daemon",
                        "--daemon-wait", "30s"], capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not os.path.ismount(mp):
        return MountResult(False, message=(p.stderr or p.stdout).strip()[-400:] or "rclone mount failed")
    return MountResult(True, mp, "Connected.")


def rclone_unmount(path: str) -> tuple[bool, str]:
    exe = shutil.which("fusermount3") or shutil.which("fusermount")
    if not exe:
        return False, "fusermount not found"
    p = subprocess.run([exe, "-u", path], capture_output=True, text=True, timeout=30)
    return p.returncode == 0, (p.stderr or "Disconnected.").strip()


# ----------------------------------------------------------------------------- shells / desktops

def ssh_command(c: Connection) -> list[str]:
    argv = ["ssh"]
    if c.port and c.port != 22:
        argv += ["-p", str(c.port)]
    if c.identity_file:
        argv += ["-i", os.path.expanduser(c.identity_file)]
    argv.append(f"{c.user}@{c.host}" if c.user else c.host)
    return argv


RDP_CLIENTS = ("remmina", "xfreerdp3", "xfreerdp", "gnome-connections")
VNC_CLIENTS = ("remmina", "gnome-connections", "vncviewer", "xtigervncviewer")


def desktop_client(protocol: str) -> str | None:
    for exe in (RDP_CLIENTS if protocol == "rdp" else VNC_CLIENTS):
        if shutil.which(exe):
            return exe
    return None


def desktop_command(c: Connection) -> tuple[list[str], bool]:
    """Return (argv, needs_terminal). Passwords are never passed: the client prompts for them."""
    client = desktop_client(c.protocol)
    if client is None:
        raise OSError("No remote-desktop client found. Install Remmina (sudo apt install remmina "
                      "remmina-plugin-rdp remmina-plugin-vnc) or FreeRDP (sudo apt install freerdp3-x11).")
    port = c.port or PROTOCOLS[c.protocol]["port"]
    if client == "remmina":
        who = f"{quote(c.user)}@" if c.user else ""
        return ["remmina", "-c", f"{c.protocol}://{who}{c.host}:{port}"], False
    if client in ("xfreerdp3", "xfreerdp"):
        argv = [client, f"/v:{c.host}:{port}", "+clipboard", "+dynamic-resolution", "/cert:tofu"]
        if c.user:
            argv.append(f"/u:{c.user}")
        if c.domain:
            argv.append(f"/d:{c.domain}")
        return argv, True                        # FreeRDP asks for the password on the terminal
    if client == "gnome-connections":
        return ["gnome-connections", f"{c.protocol}://{c.host}:{port}"], False
    return [client, f"{c.host}::{port}"], False   # TigerVNC syntax


def online_accounts_command() -> list[str] | None:
    if shutil.which("gnome-control-center"):
        return ["gnome-control-center", "online-accounts"]
    return None
