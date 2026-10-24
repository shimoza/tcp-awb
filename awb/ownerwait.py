"""`awb owner status`: one list of everything that waits on the owner (UX review 2026-10-24, change 4).

Four sources, read on the owner's side only:

- the owner items of OPEN.md of every active project (a parked project is a count, nothing more);
- the rows of presentations/work-profile/QUEUE.md of the owner's repository whose state says it waits on him
  (the file may be missing: then the rows are skipped);
- the build runs that wait on him (asked, red, held, green), with the commands of `awb build nudge`.

Each item is one line: the project code or the label, a few words, where it is done (project session, his terminal,
steering session) and a ready command block where one exists. Codes only: every text passes the name check as
`projects.waiting` does; a text with a hit is withheld, and when the check cannot run every text is withheld and the
mail is not sent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from awb import config

WHERE_SESSION = "project session"
WHERE_TERMINAL = "his terminal"
WHERE_STEERING = "steering session"
QUEUE_REL = Path("presentations/work-profile/QUEUE.md")
TEXT_MAX = 110
_WAITING_STATE = re.compile(r"^\s*(waiting|asked)\b|\bwaits? on (him|you|the owner)\b", re.IGNORECASE)
_SEPARATOR = re.compile(r"^\|?[\s:|-]+\|?$")
_TASK_ID = re.compile(r"B-[0-9]{3,4}")
_BUILD_WAITING = ("asked", "red", "held", "green", "hung", "stopped", "interrupted", "stale")


@dataclass
class Item:
    label: str
    text: str
    where: str
    block: str | None = None


def queue_file() -> Path:
    """The owner's QUEUE.md: ~/tcp-awb/presentations/work-profile/QUEUE.md."""
    return Path("~/tcp-awb").expanduser() / QUEUE_REL


def _short(text: str) -> str:
    from awb import projects

    text = projects._plain(text).replace("`", "").replace("**", "")
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 3].rstrip() + "..."


def open_items(p: config.Paths) -> tuple[list[Item], int]:
    """(owner items of every active project's OPEN.md, count of parked projects)."""
    from awb import projects

    out: list[Item] = []
    parked = 0
    for r in projects.load(p):
        if r.state == "parked":
            parked += 1
            continue
        if r.state != "active":
            continue
        try:
            text = (Path(r.path) / "OPEN.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for item in projects._owner_items(text):
            cmd = projects.item_command(item)
            out.append(Item(r.code, _short(item), WHERE_TERMINAL if cmd else WHERE_SESSION, cmd))
    return out, parked


def queue_items(path: Path | None = None) -> list[Item]:
    """The rows of QUEUE.md whose state waits on the owner; nothing when the file is missing or unreadable."""
    from awb import projects

    try:
        text = (path or queue_file()).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[Item] = []
    cols: dict[str, int] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            cols = {} if not line.strip() else cols
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if _SEPARATOR.match(line.strip()):
            continue
        names = [c.lower() for c in cells]
        if "state" in names and ("step" in names or "id" in names):
            cols = {n: i for i, n in enumerate(names)}
            continue
        if not cols or "state" not in cols or cols["state"] >= len(cells) or not cells[0]:
            continue
        if not _WAITING_STATE.search(cells[cols["state"]]):
            continue
        what = cells[cols["what"]] if cols.get("what", 99) < len(cells) else ""
        nxt = cells[cols["next"]] if cols.get("next", 99) < len(cells) else ""
        cmd = projects.item_command(nxt) if nxt else None
        words = _short(what or nxt or cells[cols["state"]])
        out.append(Item(_short(cells[0])[:20], words, WHERE_TERMINAL if cmd else WHERE_STEERING, cmd))
    return out


def build_commands(tid: str, word: str) -> list[str]:
    """His next commands for a build task that waits on him: fixed words and the id, nothing of the agent's side
    (the same words as `awb build nudge`)."""
    if word == "asked":
        return ["awb build show %s" % tid, 'awb build answer %s "..."' % tid]
    if word == "red":
        return ["awb build retry %s" % tid, "awb build widen %s PATH..." % tid]
    if word == "green":
        return ["sudo awb deploy --task %s" % tid]
    return ["awb build retry %s" % tid]


def build_items(runs: Path | None = None) -> list[Item]:
    """The build tasks whose newest run waits on the owner, with the commands `awb build nudge` mails. The run
    folders are read directly (runs/B-NNN/N/state, `<state>\\t<time>`): this module stays out of awb/build.py, so a
    change there does not restart the owner timer."""
    import os

    runs = Path(runs) if runs else Path(os.environ.get("AWB_BUILD_RUNS") or config.build_root() / "runs")
    try:
        ids = sorted(n for n in os.listdir(runs) if _TASK_ID.fullmatch(n))
    except OSError:
        return []
    out: list[Item] = []
    for tid in ids:
        try:
            nums = sorted(int(n) for n in os.listdir(runs / tid) if n.isdigit() and not n.startswith("0"))
            raw = (runs / tid / str(nums[-1]) / "state").read_text(encoding="utf-8")[:512]
        except (OSError, UnicodeDecodeError, IndexError):
            continue
        state = raw.split("\t", 1)[0].strip()
        word = state.split(":", 1)[0]
        if word in _BUILD_WAITING and re.fullmatch(r"[a-z0-9: ._-]{1,80}", state):
            out.append(Item(tid, "build task is %s" % _short(state), WHERE_TERMINAL,
                            "\n".join(build_commands(tid, word))))
    return out


def collect(p: config.Paths | None = None, queue: Path | None = None, runs: Path | None = None
            ) -> tuple[list[Item], int]:
    """All items after the name check, and the count of parked projects."""
    from awb import ledger

    p = p or config.paths()
    items, parked = open_items(p)
    items += queue_items(queue)
    items += build_items(runs)
    texts = []
    for it in items:
        texts += [it.text, it.block or "", it.label]
    try:
        hidden = ledger.hit_indexes(texts, ledger.register_path(p))
    except ledger.LedgerError:
        hidden = set(range(len(texts)))
    for n, it in enumerate(items):
        if {3 * n, 3 * n + 1, 3 * n + 2} & hidden:
            it.text, it.block = ledger.WITHHELD, None
            if 3 * n + 2 in hidden:
                it.label = "?"
    return items, parked


def check_ran(p: config.Paths | None = None) -> bool:
    """True when the name check can run now (the mail is sent only then)."""
    from awb import ledger

    p = p or config.paths()
    try:
        ledger.hit_indexes(["probe"], ledger.register_path(p))
    except ledger.LedgerError:
        return False
    return True


def render(items: list[Item], parked: int) -> list[str]:
    """The list: one line per item, the block indented under it."""
    if not items:
        head = ["nothing waits on you"]
    else:
        head = ["%d item%s wait on you" % (len(items), "" if len(items) == 1 else "s")]
    lines = list(head)
    for it in items:
        lines.append("%s  %s  [%s]" % (it.label, it.text, it.where))
        for ln in (it.block or "").splitlines():
            lines.append("    %s" % ln)
    if parked:
        lines.append("%d parked project%s not listed" % (parked, "" if parked == 1 else "s"))
    return lines


def mail(p: config.Paths | None = None, send=None, queue: Path | None = None, runs: Path | None = None) -> str:
    """The daily mail (the owner's timer, 08:00 Europe/Berlin): the list through the key service's notify, the way
    `mail_owner_commands` sends. Nothing is sent for an empty list or when the name check cannot run. Returns a word:
    "sent", "empty" or "not sent: why"."""
    p = p or config.paths()
    if not check_ran(p):
        return "not sent: the name check cannot run"
    items, parked = collect(p, queue, runs)
    if not items:
        return "empty"
    if send is None:
        return "not sent: no mail path"      # `awb owner status --mail` (awb/tcp/owner.py) passes the key service's
    body = "\n".join(render(items, parked)) + "\n"
    try:
        word = send("Workbench: %d item%s wait on you" % (len(items), "" if len(items) == 1 else "s"), body)
    except Exception as err:
        return "not sent: %s" % type(err).__name__
    return "sent" if word == "sent" or word is True else "not sent: %s" % (word if isinstance(word, str) else "refused")
