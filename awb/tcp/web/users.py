"""The logins of the console (F4, owner side).

    sudo awb web user add LOGIN [--level reader|owner]
    sudo awb web user reset|remove LOGIN
    sudo awb web user list

The gateway reads `/etc/awb-web/users.json` (root, group awb-web, mode 640): per login a salt and a PBKDF2 digest,
the level (reader, the default, or owner), a generation that grows with every reset and, for an owner entry, a TOTP
secret (T9, the owner host signs in with the password and a code from his phone). `add` and `reset` make a random
password of 20 characters, print it once to his terminal and store only the digest; for an owner entry they also
make a new TOTP secret and print its enrolment once (the key, the otpauth line and, when qrencode is on the host, a
QR code in the terminal). The command refuses inside an assistant session, so no session ever sees a password or a
secret. While the file
does not exist the single login of `auth.json` works; `add` creates the file and ends it. `remove` of the last login
leaves an empty file: nobody signs in, the single login does not come back. The gateway reads the file again on
every change and ends the sessions of a removed login and of a login with a new password.

For Publish (T9 step 3) owner-actions checks the code again against its own copy of the owner entries' secrets,
`/etc/awb/owner-publish.json` (awb-owner, 600): `add`, `reset` and `remove` of an owner entry keep it in step.

AWB_WEB_USERS points the commands at another file; such a file needs no sudo (the tests use it). AWB_OWNER_PUBLISH
does the same for the copy of the secrets.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import base64
import secrets
import shutil
import string
import subprocess
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path

from awb.tcp.web.gateway import DEFAULT_ROUNDS, LEVELS, LOGIN_RE

USERS = Path("/etc/awb-web/users.json")
OWNER_PUBLISH = Path("/etc/awb/owner-publish.json")
OWNER_ACTIONS_USER = "awb-owner"
GROUP = "awb-web"
LENGTH = 20
ALPHABET = string.ascii_letters + string.digits


class Refused(Exception):
    """The command is refused; the text names the reason, never a password."""


def users_path(environ: dict) -> Path:
    return Path(environ.get("AWB_WEB_USERS") or USERS)


def load(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "users": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("users"), dict):
            raise ValueError
        return data
    except (OSError, ValueError, AttributeError):
        raise Refused("the users file cannot be read") from None


def save(path: Path, data: dict, as_root: bool) -> None:
    """Write the file atomically: mode 640, owned by root and the gateway's group when root writes it."""
    tmp = path.with_name(".%s.new-%d" % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fchmod(fh.fileno(), 0o640)
            os.fsync(fh.fileno())
        if as_root:
            os.chown(tmp, 0, grp.getgrnam(GROUP).gr_gid)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def publish_path(environ: dict) -> Path:
    """The copy of the owner secrets: AWB_OWNER_PUBLISH, else next to a users file of AWB_WEB_USERS, else the host's."""
    if environ.get("AWB_OWNER_PUBLISH"):
        return Path(environ["AWB_OWNER_PUBLISH"])
    if environ.get("AWB_WEB_USERS"):
        return Path(environ["AWB_WEB_USERS"]).with_name("owner-publish.json")
    return OWNER_PUBLISH


def sync_publish(users_file: Path, path: Path, as_root: bool) -> None:
    """Write owner-actions' copy of the TOTP secrets of the owner entries (login: secret), mode 600, owned by
    awb-owner when root writes it."""
    secrets_of = {login: u["totp"] for login, u in load(users_file)["users"].items()
                  if u.get("level") == "owner" and u.get("totp")}
    tmp = path.with_name(".%s.new-%d" % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(secrets_of, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
        if as_root:
            import pwd
            try:
                entry = pwd.getpwnam(OWNER_ACTIONS_USER)
                os.chown(tmp, entry.pw_uid, entry.pw_gid)
            except KeyError:
                pass        # owner-actions is not installed yet: root keeps the file until install.sh made the user
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def entry(password: str, rounds: int = DEFAULT_ROUNDS, level: str = "reader", generation: int = 1) -> dict:
    salt = secrets.token_bytes(16)
    out = {"salt": salt.hex(), "rounds": rounds,
           "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds).hex(),
           "changed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "level": level, "generation": generation}
    if level == "owner":
        out["totp"] = new_totp_secret()
    return out


def change(path: Path, action: str, login: str, as_root: bool, rounds: int = DEFAULT_ROUNDS,
           level: str | None = None) -> str | None:
    """Apply add, reset or remove. Returns the new password (add, reset) or None (remove). `level` is for add
    (reader when left out); a reset keeps the level and raises the generation."""
    if not LOGIN_RE.fullmatch(login):
        raise Refused("a login is 2 to 32 characters: a small letter first, then small letters, digits, . _ -")
    data = load(path)
    users = data["users"]
    if action == "add" and login in users:
        raise Refused("the login exists: use reset for a new password")
    if action in ("reset", "remove") and login not in users:
        raise Refused("no such login")
    if level is not None and (action != "add" or level not in LEVELS):
        raise Refused("--level goes with add and is reader or owner")
    password = None
    if action == "remove":
        del users[login]
    else:
        password = new_password()
        old = users.get(login, {})
        users[login] = entry(password, rounds, level or old.get("level", "reader"),
                             int(old.get("generation", 0)) + 1)
    save(path, data, as_root)
    return password


def enrolment(login: str, secret: str, issuer: str = "AWB") -> str:
    """The otpauth line an authenticator app reads (RFC 6238 defaults: SHA1, 6 digits, 30 seconds)."""
    return "otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (
        quote(issuer), quote(login), secret, quote(issuer))


def qr_lines(text: str, runner=subprocess.run) -> list[str]:
    """`text` as a QR code for the terminal when qrencode is on the host (the text on standard input, never in an
    argument or a file); no lines without it."""
    tool = shutil.which("qrencode")
    if not tool:
        return []
    try:
        res = runner([tool, "-t", "ANSIUTF8", "-o", "-"], input=text.encode(), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return res.stdout.decode("utf-8", "replace").splitlines() if res.returncode == 0 else []


def main(a, environ: dict, geteuid=os.geteuid, say=print) -> int:
    from awb import vault

    path = users_path(environ)
    as_root = geteuid() == 0
    if a.action == "list":
        users = load(path)["users"]
        say("logins: %s" % (", ".join("%s (%s)" % (k, users[k].get("level", "reader")) for k in sorted(users))
                            if users else "none")
            + ("" if path.exists() else " (the single login of auth.json works)"))
        return 0
    if vault.in_assistant_session():
        raise Refused("never inside an assistant session: run it in your own terminal")
    if path == USERS and not as_root:
        raise Refused("run it with sudo: sudo awb web user %s LOGIN" % a.action)
    before = load(path)["users"].get(a.login, {}).get("level")
    password = change(path, a.action, a.login, as_root, level=getattr(a, "level", None))
    if "owner" in (before, getattr(a, "level", None)) or publish_path(environ).exists():
        sync_publish(path, publish_path(environ), as_root)
    if password is None:
        say("removed %s; its sessions end" % a.login)
        return 0
    say("%s %s. Password (shown once, not stored): %s" % ("added" if a.action == "add" else "new password for",
                                                         a.login, password))
    secret = load(path)["users"][a.login].get("totp")
    if secret:
        say("Owner entry: add this key to the authenticator app on your phone now (shown once): %s"
            % " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
        say(enrolment(a.login, secret))
        for line in qr_lines(enrolment(a.login, secret)):
            say(line)
        say("Never use the vault passphrase as a console password.")
    return 0
