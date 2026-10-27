"""The laptop edition (task lc-laptop-clean, the owner's yes of 2026-10-22): `awb clean` on a plain Linux, his work
laptop with Ubuntu under WSL, with no vault daemon, no key service and no work user.

One folder holds everything the laptop keeps, `AWB_LAPTOP_HOME` or `~/awb-laptop`:

    vault/            700  only on the laptop, never copied anywhere
      register.tsv    600  his customers' written forms (the format of awb/register.py)
      issued.txt      600  the customer codes registered without a form, one per line
      maps/<tcp-xxxx>/<stamp>.tsv
                      600  token, class and value of every replacement of one run of awb clean
    tmp/              700  the temporary folders of the readers (awb/extract) and of the uploads of the page
    project                the current project of `awb clean use tcp-xxxx` (task LC4)
    last-run/<tcp-xxxx>    the reports of the last run of `awb clean` in that project, for `awb clean put`

The work folders of the projects are `AWB_WORK_HOME` or `~/work`, one `tcp-xxxx` folder each.

Nothing here opens a network connection. Nothing here prints a form or a value: lines carry codes and counts.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from awb import codes, register

HOME_ENV = "AWB_LAPTOP_HOME"
DEFAULT_HOME = "~/awb-laptop"
ISSUED = "issued.txt"
WORK_ENV = "AWB_WORK_HOME"
DEFAULT_WORK = "~/work"
CURRENT = "project"
LAST_RUN = "last-run"
PUT_ENV = ("AWB_OBS_AK", "AWB_OBS_SK")
"""The environment variables of `awb clean --put`: the access key and the secret key of his own IAM user."""
PUT_OPTIONAL_ENV = ("AWB_OBS_BUCKET", "AWB_OBS_REGION")
"""Optional: the bucket (default awb) and the region (default eu-de)."""


class LaptopError(Exception):
    """A step of the laptop edition cannot run. The message carries codes and counts, never a form or a value."""


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or DEFAULT_HOME).expanduser()


def _dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def vault() -> Path:
    return home() / "vault"


def register_path() -> Path:
    return vault() / "register.tsv"


def tmp_dir() -> Path:
    return _dir(home() / "tmp")


def init() -> list[str]:
    """The folders of the laptop and an empty register; a second run leaves what is there."""
    _dir(home())
    _dir(vault())
    _dir(vault() / "maps")
    tmp_dir()
    if not register_path().exists():
        register.save(register_path(), [])
    entries = register.load(register_path())
    return ["laptop: %s" % home(), "vault: %s (700, only on this laptop)" % vault(),
            "register: %d forms of %d codes" % (len(entries), len(register.codes(entries))),
            "for --put set %s and %s (his own key; optional %s)" % (PUT_ENV[0], PUT_ENV[1],
                                                                   " and ".join(PUT_OPTIONAL_ENV))]


def entries() -> list[register.Entry]:
    if not register_path().exists():
        return []
    return register.load(register_path())


def customer_of(project: str) -> str:
    """CUST- and the four characters of the project code: the one code per project of the host."""
    if not codes.is_project_code(project or "") or not project.startswith("tcp-"):
        raise LaptopError("a project code reads tcp-xxxx")
    return "CUST-" + project.split("-", 1)[1].upper()


def work_root() -> Path:
    return Path(os.environ.get(WORK_ENV) or DEFAULT_WORK).expanduser()


def work_dir(project: str) -> Path:
    customer_of(project)
    return work_root() / project


def use(project: str) -> Path:
    """`awb clean use tcp-xxxx`: the current project of this laptop and its work folder (made when missing)."""
    customer_of(project)
    init()
    folder = work_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(home() / CURRENT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(project + "\n")
    return folder


def current() -> str | None:
    try:
        text = (home() / CURRENT).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if codes.is_project_code(text) and text.startswith("tcp-") else None


def project_of_dir(cwd: Path | None = None) -> str | None:
    """The project whose work folder holds `cwd` (the folder itself or one below it)."""
    try:
        rel = Path(os.path.abspath(cwd or Path.cwd())).relative_to(os.path.abspath(work_root()))
    except ValueError:
        return None
    first = rel.parts[0] if rel.parts else ""
    return first if codes.is_project_code(first) and first.startswith("tcp-") else None


def resolve_project(given: str | None, cwd: Path | None = None) -> tuple[str, str]:
    """The project of a run and where it came from: --project, the work folder of the current directory, or the
    project of `awb clean use`. Raises when there is none."""
    if given:
        customer_of(given)
        return given, "--project"
    here = project_of_dir(cwd)
    if here:
        return here, "the folder"
    saved = current()
    if saved:
        return saved, "awb clean use"
    raise LaptopError("no project: name it once with awb clean use tcp-xxxx (or --project tcp-xxxx)")


def record_run(project: str, reports: list[Path]) -> None:
    """The reports of the last run of `project`, for `awb clean put`."""
    folder = _dir(home() / LAST_RUN)
    fd = os.open(folder / project, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join("%s\n" % os.path.abspath(r) for r in reports))


def last_run(project: str) -> list[Path]:
    try:
        text = (home() / LAST_RUN / project).read_text(encoding="utf-8")
    except OSError:
        return []
    return [Path(line) for line in text.splitlines() if line.strip()]


def _issued() -> set[str]:
    try:
        return {line.strip() for line in (vault() / ISSUED).read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def known(customer: str) -> bool:
    """The customer has an active form in the laptop register or was registered without one."""
    return customer in _issued() or any(e.code == customer and e.status == "active" for e in entries())


def ask_forms(customer: str, read=None, tty=None, say=print) -> int:
    """`awb clean register CUST-XXXX`: the written forms typed on the terminal once, one per line, an empty line
    ends; an empty first line registers the code without a form. Nothing is echoed back. Returns the forms added."""
    if not (codes.is_code(customer) and codes.kind_of(customer) == "CUST" and customer.count("-") == 1):
        raise LaptopError("a customer code reads CUST-XXXX")
    if not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())):
        raise LaptopError("awb clean register asks for the forms on the terminal: run it in your own terminal")
    init()
    read = read or (lambda: sys.stdin.readline())
    say("Type the written forms of %s, one per line: the full name, the short name, an acronym, the domain, other "
        "spellings; an empty line ends. An empty first line registers it without a form." % customer)
    added = 0
    while True:
        line = read()
        if not line or not line.strip():
            break
        try:
            register.add(register_path(), customer, "CUST", line.rstrip("\r\n"))
            added += 1
        except register.RegisterError:
            say("that line cannot go into the register (brackets, pipes, hashes or a form it holds already); type "
                "it again or end with an empty line")
    if not added and customer not in _issued():
        fd = os.open(vault() / ISSUED, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(customer + "\n")
    say("registered %d form(s) of %s" % (added, customer) if added else "%s registered without a form" % customer)
    return added


def write_map(project: str, rows: list[tuple[str, str, str, str]]) -> Path | None:
    """The mapping of one run (token, class, value, output file) into vault/maps/<project>/<stamp>.tsv, mode 600.
    None when the run replaced nothing."""
    if not rows:
        return None
    folder = _dir(_dir(vault() / "maps") / project)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path, n = folder / ("%s.tsv" % stamp), 1
    while path.exists():
        n += 1
        path = folder / ("%s-%d.tsv" % (stamp, n))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("token\tclass\tvalue\tfile\n")
        for row in rows:
            fh.write("\t".join(" ".join(str(c).split()) for c in row) + "\n")
    return path
