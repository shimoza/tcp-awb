"""`awb owner status`: one list of everything that waits on the owner (UX review 2026-10-24, change 4).

Six sources, read on the owner's side only:

- the owner items of OPEN.md of every active project (a parked project is a count, nothing more);
- the rows of presentations/work-profile/QUEUE.md of the owner's repository whose state says it waits on him
  (the file may be missing: then the rows are skipped);
- the build runs that wait on him (asked, red, held, green), with the commands of `awb build nudge`;
- the last weekly publish of the dataset TCP Facts (task KS, awb-dataset-weekly.timer, Monday 06:00 UTC): a failed
  one is the item "dataset: weekly publish failed: <reason>" until a publish succeeds; a successful one is a note
  "dataset tcp-facts-<date>: N added, M amended, K retired" for 24 hours, so the 08:00 mail after it carries it.
- the lab runs of the projects (task LR, awb/labruns.py) open for more than two hours or ended with failed
  deletes, with what is left (kinds only, never an id).
- the last put of the codes file of his report workbook (`awb report --codes --put`, awb-report-codes.timer, every
  hour): a failed one is the item "reports: ..." with its reason until a put succeeds; it says
  "projects.csv older than 24 h" when the last good put is that old or there was none.

Each item is one line: the project code or the label, a few words, where it is done (project session, his terminal,
steering session) and a ready command block where one exists. Codes only: every text passes the name check as
`projects.waiting` does; a text with a hit is withheld, and when the check cannot run every text is withheld and the
mail is not sent.
"""
from __future__ import annotations

import datetime
import json
import os
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
DATASET_DIR = Path("/var/lib/awb-dataset-weekly")   # StateDirectory= of awb-dataset-weekly.service, the owner's (700)
DATASET_ENV = "AWB_DATASET_WEEKLY"                   # another folder for the tests
DATASET_FILE = "last.json"
NOTE_HOURS = 24
REPORT_DIR = Path("/var/lib/awb-report-codes")   # StateDirectory= of awb-report-codes.service, the owner's (700)
REPORT_ENV = "AWB_REPORT_CODES"                  # another folder for the tests
REPORT_STATE = "last.json"


@dataclass
class Item:
    label: str
    text: str
    where: str            # empty: the item renders as one line "label: text"
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


def dataset_state_path() -> Path:
    """The file the weekly publish writes after each run: <state folder>/last.json."""
    return Path(os.environ.get(DATASET_ENV) or DATASET_DIR) / DATASET_FILE


def write_dataset_state(state: dict, path: Path | None = None) -> Path | None:
    """The result of one weekly publish into the state file, atomically; None when its folder is missing or not
    writable (a run by hand outside the unit): the publish itself is not changed by that."""
    path = path or dataset_state_path()
    state = dict(state, at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    tmp = path.with_name(".%s.new" % path.name)
    try:
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def _dataset_state(path: Path | None) -> dict:
    try:
        state = json.loads((path or dataset_state_path()).read_text(encoding="utf-8")[:4096])
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def dataset_line(state: dict) -> str:
    """The success line of a weekly publish."""
    return "dataset %s: %d added, %d amended, %d retired" % (
        state.get("dataset", "?"), int(state.get("added", 0)), int(state.get("amended", 0)),
        int(state.get("retired", 0)))


def dataset_items(path: Path | None = None) -> list[Item]:
    """The failed weekly publish as one item; nothing when the last run succeeded or none ran."""
    state = _dataset_state(path)
    if state.get("state") != "failed":
        return []
    return [Item("dataset", "weekly publish failed: %s" % _short(str(state.get("reason") or "unknown")), "")]


def report_dir() -> Path:
    """The state folder of `awb report --codes`: the codes file, its sidecar and last.json."""
    return Path(os.environ.get(REPORT_ENV) or REPORT_DIR)


def report_state_path() -> Path:
    return report_dir() / REPORT_STATE


def report_items(path: Path | None = None, now: datetime.datetime | None = None) -> list[Item]:
    """The failed put of the codes file as one item; nothing when the last put succeeded or none ran. The state is
    read here, not through awb/reportcodes.py, so the owner's timer never imports the bucket client."""
    state = _dataset_state(path or report_state_path())
    if state.get("state") != "failed":
        return []
    reason = _short(str(state.get("reason") or "unknown"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        last = datetime.datetime.strptime(str(state.get("last_put", "")), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        old = now - last > datetime.timedelta(hours=NOTE_HOURS)
    except ValueError:
        old = True
    if old:
        return [Item("reports", "projects.csv older than 24 h, the last put failed: %s" % reason, "")]
    return [Item("reports", "the last put of projects.csv failed: %s" % reason, "")]


def notes(path: Path | None = None, now: datetime.datetime | None = None, p: config.Paths | None = None
          ) -> list[str]:
    """The lines that tell him something without waiting on him: the weekly publish of the last 24 hours, after the
    name check (a line with a hit or a check that cannot run gives nothing)."""
    from awb import ledger

    state = _dataset_state(path)
    if state.get("state") != "published":
        return []
    try:
        at = datetime.datetime.strptime(str(state.get("at", "")), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        line = dataset_line(state)
    except (ValueError, TypeError):
        return []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not datetime.timedelta(0) <= now - at <= datetime.timedelta(hours=NOTE_HOURS):
        return []
    try:
        if ledger.hit_indexes([line], ledger.register_path(p or config.paths())):
            return []
    except ledger.LedgerError:
        return []
    return [line]


def lab_run_items(p: config.Paths, now: float | None = None) -> list[Item]:
    """The lab runs that need him: open for more than two hours or ended with failed deletes (labruns.attention),
    each with its leftovers by kind and the command the project session runs."""
    from awb import labruns

    try:
        found = labruns.attention(p, now)
    except Exception:
        return []
    out: list[Item] = []
    for run in found:
        left = run.get("left") or []
        kinds = ", ".join(sorted({str(c.get("label") or "resource") for c in left})) or "nothing"
        if run["why"] == "open":
            text = "lab run on %s open %d h, %d left: %s" % (run["tenant"], run.get("hours", 2), len(left), kinds)
        else:
            failed = sum(1 for c in left if c.get("state") == "failed")
            text = "lab run on %s ended with %d failed delete%s: %s" % (run["tenant"], failed,
                                                                       "" if failed == 1 else "s", kinds)
        out.append(Item(str(run.get("project") or "?"), _short(text), WHERE_SESSION,
                        "awb lab run end --tenant %s" % run["tenant"]))
    return out


def collect(p: config.Paths | None = None, queue: Path | None = None, runs: Path | None = None
            ) -> tuple[list[Item], int]:
    """All items after the name check, and the count of parked projects."""
    from awb import ledger

    p = p or config.paths()
    items, parked = open_items(p)
    items += queue_items(queue)
    items += build_items(runs)
    items += dataset_items()
    items += report_items()
    items += lab_run_items(p)
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


def render(items: list[Item], parked: int, note_lines: list[str] | tuple = ()) -> list[str]:
    """The list: one line per item, the block indented under it, then the notes."""
    if not items:
        head = ["nothing waits on you"]
    else:
        head = ["%d item%s wait on you" % (len(items), "" if len(items) == 1 else "s")]
    lines = list(head)
    for it in items:
        lines.append("%s  %s  [%s]" % (it.label, it.text, it.where) if it.where else "%s: %s" % (it.label, it.text))
        for ln in (it.block or "").splitlines():
            lines.append("    %s" % ln)
    if parked:
        lines.append("%d parked project%s not listed" % (parked, "" if parked == 1 else "s"))
    lines += list(note_lines)
    return lines


def mail(p: config.Paths | None = None, send=None, queue: Path | None = None, runs: Path | None = None) -> str:
    """The daily mail (the owner's timer, 08:00 Europe/Berlin): the list through the key service's notify, the way
    `mail_owner_commands` sends. Nothing is sent for an empty list or when the name check cannot run. Returns a word:
    "sent", "empty" or "not sent: why"."""
    p = p or config.paths()
    if not check_ran(p):
        return "not sent: the name check cannot run"
    items, parked = collect(p, queue, runs)
    note_lines = notes(p=p)
    if not items and not note_lines:
        return "empty"
    if send is None:
        return "not sent: no mail path"      # `awb owner status --mail` (awb/tcp/owner.py) passes the key service's
    body = "\n".join(render(items, parked, note_lines)) + "\n"
    subject = ("Workbench: %d item%s wait on you" % (len(items), "" if len(items) == 1 else "s") if items
               else "Workbench: nothing waits on you, %d note%s" % (len(note_lines), "" if len(note_lines) == 1 else "s"))
    try:
        word = send(subject, body)
    except Exception as err:
        return "not sent: %s" % type(err).__name__
    return "sent" if word == "sent" or word is True else "not sent: %s" % (word if isinstance(word, str) else "refused")
