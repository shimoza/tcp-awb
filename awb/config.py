"""Where the Workbench keeps things. Three places, kept apart on purpose.

    ~/tcp-awb/        the repository: code, rules, design. Pushable.
    ~/tcp-shared/     what working sessions share. Codes only, never a name.
      projects.tsv      the project register
      ledger/           activity records
      outbox/<CODE>/    sanitised Markdown copies and the public intake report, waiting to go into a project
    ~/tcp-vault/      the intake side. A working session never reads it.
      register.tsv      the only place where a written form and its code meet
      inbox/            where incoming material is dropped before intake
      originals/<CODE>/ files exactly as they arrived, under file ids
      reports/<CODE>/   private intake reports (real strings, positions, candidates)
      log/              log of name checks
    ~/tcp-<code>/     one folder per project, listed in projects.tsv

The vault is its own place so that it can move to another system user or another server without touching the
rest (design document 05, T-90).

Order of precedence for every path: a function argument, then the environment (AWB_SHARED, AWB_VAULT,
AWB_PROJECTS, AWB_KB, AWB_CHECK_SOCKET, AWB_ADMIN_SOCKET), then the host file /etc/awb/paths.conf (written by
seal/setup.sh; lines "key = value", keys shared, vault, projects, kb, check_socket, admin_socket, work_user),
then the defaults below. AWB_CONF points at another host file (tests do). Tests set the environment.

The work user of a sealed host is the exception: its paths come from the root-owned host file alone. The
environment and AWB_CONF are left out for it, because a session can change the shell profile of the work user and
must not point the hooks at another check socket, knowledge base or vault that way.

    ~/tcp-kb/          the knowledge base of the TCP scope (its own git repository)
    check socket       /run/awb/check.sock, the name check of the vault daemon for working sessions
    admin socket       <vault>/admin.sock, for the owner's commands only
"""
from __future__ import annotations

import os
import pwd
import re
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    shared: Path
    vault: Path
    projects_root: Path
    kb: Path = Path("~/tcp-kb").expanduser()
    check_socket: Path = Path("/run/awb/check.sock")
    admin_socket: Path | None = None

    # vault side
    @property
    def register(self) -> Path:
        return self.vault / "register.tsv"

    @property
    def register_encrypted(self) -> Path:
        return self.vault / "register.tsv.gpg"

    @property
    def keep_list(self) -> Path:
        """Phrases he reviewed as not a name. Vault side, because a reviewed phrase can still point at a customer."""
        return self.vault / "keep.tsv"

    @property
    def admin_sock(self) -> Path:
        return self.admin_socket or (self.vault / "admin.sock")

    @property
    def inbox(self) -> Path:
        return self.vault / "inbox"

    @property
    def originals(self) -> Path:
        return self.vault / "originals"

    @property
    def private_reports(self) -> Path:
        return self.vault / "reports"

    @property
    def check_log(self) -> Path:
        return self.vault / "log"

    # shared side
    @property
    def outbox(self) -> Path:
        return self.shared / "outbox"

    @property
    def projects_register(self) -> Path:
        return self.shared / "projects.tsv"

    @property
    def ledger(self) -> Path:
        return self.shared / "ledger"


HOST_CONF = "/etc/awb/paths.conf"


_WORK_USER_RE = re.compile(r"(?m)^[ \t]*work_user[ \t]*=[ \t]*(\S+)[ \t]*$")


def work_user() -> str | None:
    """The work user the root host file (HOST_CONF) names; None without one. AWB_CONF never counts here: a file
    a session can write must not decide who is the work user."""
    try:
        text = Path(HOST_CONF).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _WORK_USER_RE.search(text)
    return m.group(1) if m else None


def is_work_user() -> bool:
    """True when this process runs as the work user of a sealed host."""
    want = work_user()
    if not want:
        return False
    try:
        return pwd.getpwuid(os.geteuid()).pw_name == want
    except KeyError:
        return False


KNOWN_MODES = ("sealed",)
"""The host modes this release knows. The private mode is designed (presentations/modes/DESIGN.md), not built."""
_MODE_RE = re.compile(r"(?m)^[ \t]*mode[ \t]*=[ \t]*(.*?)[ \t]*$")


def mode() -> str | None:
    """The mode of a sealed host as the root host file (HOST_CONF) says it: the word of its `mode` line, "sealed"
    when a host file that names a work user has no `mode` line (every host sealed before the line existed), None
    without a work user. Read for every uid from the root file alone, never AWB_CONF. A word that is not in
    KNOWN_MODES comes back as it is: the session-start receipt refuses it."""
    try:
        text = Path(HOST_CONF).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not _WORK_USER_RE.search(text):
        return None
    found = _MODE_RE.findall(text)
    return found[-1] if found else "sealed"


def host_conf() -> dict[str, str]:
    """Key and value pairs of the host file; an empty dict when there is none or it cannot be read. The work
    user reads the root host file only, never the one AWB_CONF names."""
    f = Path(HOST_CONF) if is_work_user() else Path(os.environ.get("AWB_CONF") or HOST_CONF)
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _resolve(value, env: str | None, conf_key: str, conf: dict, default: str) -> Path:
    if value is None:
        value = (os.environ.get(env) if env else None) or conf.get(conf_key) or default
    return Path(value).expanduser().resolve()


def paths(shared=None, vault=None, projects_root=None) -> Paths:
    """Arguments win, then the environment, then /etc/awb/paths.conf, then ~/tcp-shared, ~/tcp-vault, ~. For the
    work user of a sealed host the environment is left out (see the module text)."""
    conf = host_conf()
    sealed = is_work_user()

    def env(name: str) -> str | None:
        return None if sealed else name

    admin = (None if sealed else os.environ.get("AWB_ADMIN_SOCKET")) or conf.get("admin_socket")
    return Paths(
        shared=_resolve(shared, env("AWB_SHARED"), "shared", conf, "~/tcp-shared"),
        vault=_resolve(vault, env("AWB_VAULT"), "vault", conf, "~/tcp-vault"),
        projects_root=_resolve(projects_root, env("AWB_PROJECTS"), "projects", conf, "~"),
        kb=_resolve(None, env("AWB_KB"), "kb", conf, "~/tcp-kb"),
        check_socket=_resolve(None, env("AWB_CHECK_SOCKET"), "check_socket", conf, "/run/awb/check.sock"),
        admin_socket=Path(admin).expanduser().resolve() if admin else None,
    )


def make_dir(path: Path, mode: int, *, shared: bool = False) -> Path:
    """Create a folder (and missing parents) and give it `mode`.

    On a sealed host the shared side belongs to the work user and is a group-shared tree: seal/setup.sh leaves
    tcp-shared with the setgid bit (2750) and the outbox open to the group (2770). The owner's intake writes into
    it as a member of that group. So, for a folder of the shared side (`shared=True`):

    - a new folder under a setgid parent gets the setgid bit and the owner and group bits of its parent, so that
      what the intake writes keeps the group and the work user can read and move it;
    - an existing folder that carries the setgid bit or that another user owns is left as it is (the owner
      may not change it and setup.sh chose its mode).

    Everywhere else and for every folder of the vault side the folder gets exactly `mode`, as before.
    """
    path = Path(path)
    # a link where a folder should be, or on the way to it: on the shared side the work user could plant one
    # and let the owner chmod or write through it (the review of 2026-09-27). The shared root is resolved by
    # paths(), so every link below it is planted. Nobody plants links inside the vault.
    if os.path.islink(path) or (shared and os.path.realpath(path) != os.path.abspath(path)):
        raise FileExistsError("a link stands where a folder should be")
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    want = mode
    if shared:
        st = os.stat(path)
        if not created and (st.st_uid != os.geteuid() or st.st_mode & stat.S_ISGID):
            return path
        try:
            parent = os.stat(path.parent)
        except OSError:
            parent = None
        if parent is not None and parent.st_mode & stat.S_ISGID:
            want = stat.S_ISGID | (parent.st_mode & 0o770)
    chmod_dir(path, want)
    return path


def chmod_dir(path: Path, mode: int) -> None:
    """chmod a folder through a handle opened without following links, so that a link planted between the
    check and the change reaches nothing."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def ensure_layout(p: Paths) -> Paths:
    """Create both places. Everything on the vault side is mode 700, the shared side 750 (see make_dir for the
    group-shared tree of a sealed host)."""
    for d in (p.shared, p.outbox, p.ledger):
        make_dir(d, 0o750, shared=True)
    for d in (p.vault, p.inbox, p.originals, p.private_reports, p.check_log):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    p.projects_root.mkdir(parents=True, exist_ok=True)
    return p
