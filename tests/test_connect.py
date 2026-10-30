"""Connect backend tests.

Pure tests (URI building, validation, storage, desktop commands) always run.
Live tests run only when GX_LIVE=1 and a session bus + gvfsd are available, against servers
configured by the caller (defaults: SMB share //127.0.0.1/gxshare and SFTP 127.0.0.1, user gxtest).
"""
import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
H = tempfile.mkdtemp(prefix="gx-conn-")
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(H, ".config"))

from gsexplorer import connect as cn  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


# ---------------------------------------------------------------- pure
C = cn.Connection
check("smb uri with domain+user", cn.gio_uri(C(protocol="smb", host="nas", share="Team Docs", user="gus",
                                               domain="CORP")) == "smb://CORP;gus@nas/Team%20Docs/")
check("sftp uri custom port + path", cn.gio_uri(C(protocol="sftp", host="srv", port=2222, user="gus",
                                                  path="/var/www")) == "sftp://gus@srv:2222/var/www/")
check("default port omitted", cn.gio_uri(C(protocol="davs", host="cloud.example.com", port=443,
                                           path="remote.php/dav")) == "davs://cloud.example.com/remote.php/dav/")
check("ipv6 literal bracketed", cn.gio_uri(C(protocol="sftp", host="fe80::1")) == "sftp://[fe80::1]/")
check("validate rejects host with @", cn.validate(C(protocol="sftp", host="u@h")))
check("validate requires smb share", cn.validate(C(protocol="smb", host="nas")))
check("validate ok", cn.validate(C(protocol="sftp", host="srv.local", user="gus")) is None)
check("fuse dir -> uri", cn.fuse_dir_to_uri(os.path.join(cn.gvfs_dir(), "smb-share:server=nas,share=docs"))
      == "smb://nas/docs/")
check("ssh command", cn.ssh_command(C(protocol="ssh", host="h", port=2200, user="gus"))
      == ["ssh", "-p", "2200", "gus@h"])
cn.save_connections([C(name="NAS", protocol="smb", host="nas", share="d", user="gus", remember_password=True)])
st = os.stat(cn._store_path())
raw = open(cn._store_path()).read()
check("connections file is 0600 and has no password field",
      stat.S_IMODE(st.st_mode) == 0o600 and "password\"" not in raw.replace("remember_password", ""))
check("connections round-trip", cn.load_connections()[0].name == "NAS")
try:
    argv, term = cn.desktop_command(C(protocol="rdp", host="win10", user="gus", domain="CORP"))
    check("rdp command has no password", not any(a.startswith("/p:") for a in argv) and "win10" in " ".join(argv))
except OSError as e:
    check("rdp: no client installed -> clear message", "Install Remmina" in str(e))

# ---------------------------------------------------------------- live
if os.environ.get("GX_LIVE") == "1":
    pw = os.environ.get("GX_PW", "Secr3t!pw")
    smb = C(protocol="smb", host="127.0.0.1", share="gxshare", user="gxtest", domain="WORKGROUP")
    r = cn.mount(smb, None)
    check("smb without password asks for one", not r.ok and r.need_password)
    r = cn.mount(smb, "wrong-password")
    check("smb wrong password reported", not r.ok and r.need_password and "Wrong" in r.message)
    r = cn.mount(smb, pw)
    check("smb mount ok + FUSE path", r.ok and os.path.isfile(os.path.join(r.path, "remote.txt")))
    with open(os.path.join(r.path, "remote.txt")) as f:
        check("smb file readable", f.read().strip() == "hello from smb")
    mounts = cn.list_gio_mounts()
    check("mount listed", any(m.path == r.path and m.mounted for m in mounts))
    ok, msg = cn.unmount(r.path)
    import time
    for _ in range(30):                       # gvfs-fuse drops the folder asynchronously
        if not os.path.exists(r.path):
            break
        time.sleep(0.1)
    print("   unmount:", ok, msg)
    check("smb unmount", ok and not os.path.exists(r.path))

    sftp = C(protocol="sftp", host="127.0.0.1", user="gxtest", path="/srv/gxshare")
    r = cn.mount(sftp, pw)
    check("sftp mount ok + subpath", r.ok and os.path.isfile(os.path.join(r.path, "remote.txt")))
    ok, _ = cn.unmount(r.path)
    check("sftp unmount", ok)

    if cn.keyring_available():
        stored = cn.keyring_store(smb, pw)
        check("keyring store", stored)
        check("keyring lookup", cn.keyring_lookup(smb) == pw)
        cn.keyring_clear(smb)
        check("keyring clear", cn.keyring_lookup(smb) is None)

print(f"\n{sum(results)}/{len(results)} connect checks passed")
sys.exit(0 if all(results) else 1)
