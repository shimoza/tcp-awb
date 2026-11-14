"""`awb owner status`: one list of everything that waits on the owner (UX review 2026-10-24, change 4).

The sources, read on the owner's side only:

- the owner items of OPEN.md of every active project (a parked project is a count, nothing more);
- the rows of presentations/work-profile/queue.tsv of the owner's repository whose who is owner
  (the file may be missing: then the rows are skipped);
- export-* folders in the owner's home with a file younger than 30 days and no file in the import history;
- the build runs that wait on him (asked, red, held, green), with the commands of `awb build nudge`;
- the last weekly publish of the dataset TCP Facts (task KS, awb-dataset-weekly.timer, Monday 06:00 UTC): a failed
  one is the item "dataset: weekly publish failed: <reason>" until a publish succeeds; a successful one is a note
  "dataset tcp-facts-<date>: N added, M amended, K retired" for 24 hours, so the 08:00 mail after it carries it.
- the last weekly refresh (awb-refresh-weekly.timer, Saturday 17:00 UTC, before the read tier of 19:00): a failed
  one is the item "refresh: weekly run failed: <reason>" until a run succeeds.
- the lab runs of the projects (task LR, awb/labruns.py) open for more than two hours or ended with failed
  deletes, with what is left (kinds only, never an id).
- the customer tenants in write mode (task TW, awb/tenantmode.py: the tenant_write line of keys.conf) with the day
  and the note of the switch, for as long as they stay there, and the command that switches each back.
- the last put of the codes file of his report workbook (`awb report --codes --put`, awb-report-codes.timer, every
  hour): a failed one is the item "reports: ..." with its reason until a put succeeds; it says
  "projects.csv older than 24 h" when the last good put is that old or there was none.
- the open windows of his Windows lab machines (task WL, awb/winlab.py: winlab.json beside keys.conf) with the
  project and the end, until the window ends by itself or he closes it, and the command that closes each; and every
  registered lab machine that is online (its tunnel answers on its loopback port with an SSH banner), one line each.

Each item is one line: the project code or the label, a few words, where it is done (project session, his terminal,
steering session) and a ready command block where one exists. Codes only: every text passes the name check as
`projects.waiting` does; a text with a hit is withheld, and when the check cannot run every text is withheld and the
mail is not sent.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from awb import config

WHERE_SESSION = "project session"
WHERE_TERMINAL = "his terminal"
WHERE_STEERING = "steering session"
QUEUE_REL = Path("presentations/work-profile/queue.tsv")
TEXT_MAX = 110
PER_PROJECT = 5               # owner items shown per project; the others are counted
FIRST = 3                     # items `awb` with no argument prints
_DEV_START = ("owner (tooling)", "(tooling)", "steering:")
_DONE_STATES = ("stopped: by_owner", "stopped: success")
_WAITING_STATE = re.compile(r"^\s*(waiting|asked)\b|\bwaits? on (him|you|the owner)\b", re.IGNORECASE)
_SEPARATOR = re.compile(r"^\|?[\s:|-]+\|?$")
_TASK_ID = re.compile(r"B-[0-9]{3,4}")
_BUILD_WAITING = ("asked", "red", "held", "green", "hung", "stopped", "interrupted", "stale")
DATASET_DIR = Path("/var/lib/awb-dataset-weekly")   # StateDirectory= of awb-dataset-weekly.service, the owner's (700)
DATASET_ENV = "AWB_DATASET_WEEKLY"                   # another folder for the tests
DATASET_FILE = "last.json"
NOTE_HOURS = 24
REFRESH_DIR = Path("/var/lib/awb-refresh-weekly")   # StateDirectory= of awb-refresh-weekly.service, the owner's (700)
REFRESH_ENV = "AWB_REFRESH_WEEKLY"                   # another folder for the tests
REPORT_DIR = Path("/var/lib/awb-report-codes")   # StateDirectory= of awb-report-codes.service, the owner's (700)
REPORT_ENV = "AWB_REPORT_CODES"                  # another folder for the tests
REPORT_STATE = "last.json"


@dataclass
class Item:
    label: str
    text: str
    where: str            # empty: the item renders as one line "label: text"
    block: str | None = None
    kind: str = ""        # owner, kept, done, dev or more: what `decide` does with the item; empty: shown as it is
    extra: str = ""       # the tenant of a kept create
    item: int | None = None  # original projects._owner_items index, before filtering
    has_default: bool = False
    app: str = ""
    source: str = ""      # full owner item for the name check, never serialized


def queue_file() -> Path:
    """The owner's TSV queue, with the same override as awb queue."""
    from awb import workqueue

    return workqueue.queue_file()


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
        app = project_app(p, r.code, Path(r.path))
        for index, item in enumerate(projects._owner_items(text)):
            cmd = projects.item_command(item)
            parts = re.split(r"\bDefault:\s*", item.replace("**", "").replace("`", ""), maxsplit=1, flags=re.I)
            out.append(Item(r.code, _short(item), WHERE_TERMINAL if cmd else WHERE_SESSION, cmd, "owner",
                            item=index, has_default=len(parts) == 2 and bool(parts[1].strip()), app=app,
                            source=item))
    return out, parked


def project_app(p: config.Paths, code: str, root: Path) -> str:
    """UX18's configured work host and project root, checked with the item before export."""
    from awb import codes

    conf = config.host_conf()
    host, parent = conf.get("work_host", ""), Path(conf.get("projects", ""))
    if (not codes.is_project_code(code) or root.name != code or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host) or
            not parent.is_absolute() or parent != p.projects_root or root.parent != parent):
        return "app: destination unavailable."
    return "app: SSH host %s, folder %s" % (host, root)


def page_items(p: config.Paths) -> list[dict]:
    """Only the checked project decisions of the normal owner status view, with stable source indexes."""
    from awb import codes

    items, _ = collect(p)
    return [{"code": it.label, "item": it.item, "text": it.text,
             "has_default": it.has_default, "app": it.app}
            for it in items if it.kind == "owner" and it.item is not None and codes.is_project_code(it.label)]


def validate_status(status: dict) -> None:
    """Validate the optional Decisions extension and the original status shape; accept old timer files."""
    from awb import codes, status_shape

    status_shape.validate({k: v for k, v in status.items() if k != "decisions"})
    rows = status.get("decisions", [])
    keys = {"code", "item", "text", "has_default", "app"}
    seen = set()
    if not isinstance(rows, list):
        raise status_shape.StatusError("the status field decisions has an unexpected shape")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != keys or not isinstance(row["code"], str) or
                not codes.is_project_code(row["code"]) or type(row["item"]) is not int or row["item"] < 0 or
                not isinstance(row["has_default"], bool) or
                not isinstance(row["text"], str) or not 0 < len(row["text"]) <= TEXT_MAX or
                not row["text"].isprintable() or not isinstance(row["app"], str) or
                not 0 < len(row["app"]) <= 4096 or not row["app"].isprintable() or
                (row["code"], row["item"]) in seen):
            raise status_shape.StatusError("the status field decisions has an unexpected shape")
        seen.add((row["code"], row["item"]))


def queue_items(path: Path | None = None) -> list[Item]:
    """Owner rows with their next steps. Explicit Markdown paths retain the legacy reader."""
    from awb import ledger, projects, workqueue

    path = path or queue_file()
    if path.suffix != ".md":
        try:
            rows = workqueue.load(path)
        except (workqueue.QueueError, ledger.LedgerError, OSError, UnicodeError):
            return [Item("queue", "queue unavailable; run awb queue list", WHERE_TERMINAL)]
        out = []
        for row in rows:
            if row["who"] != "owner":
                continue
            cmd = projects.item_command(row["next"])
            out.append(Item(row["id"], "%s; next: %s" % (_short(row["what"]), row["next"]),
                            WHERE_TERMINAL if cmd else WHERE_STEERING, cmd))
        return out

    try:
        text = path.read_text(encoding="utf-8")
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


def _export_hash(path: Path) -> str:
    """Hash bytes without decoding content, following file links or opening special files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not a regular file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _import_hashes(p: config.Paths) -> set[str] | None:
    """Hash originals recorded by intake reports. Unknown history never means 'not imported'.

    The existing import log is the private report's Files table, not a digest index. Only its recorded originals
    count, including sealed originals opened through the owner's vault daemon. No report text leaves this reader.
    """
    import base64

    from awb import candidates, vault_client

    found: set[str] = set()
    seen: set[Path] = set()
    try:
        for report in p.private_reports.glob("*/*.md*"):
            if report.is_symlink() or report.suffix not in (".md", ".gpg"):
                continue
            text = candidates.read_report(report)
            if "- mode: stopped for the review" in text:
                continue
            inside = False
            for line in text.splitlines():
                if line.startswith("## "):
                    inside = line == "## Files"
                if not inside or not line.startswith("| F-"):
                    continue
                cells = candidates._cells(line)
                if len(cells) != 6 or cells[5] in ("", "not moved"):
                    continue
                original = Path(cells[5])
                # Encrypting an existing vault seals the file without rewriting older reports.
                if original.suffix != ".gpg" and not original.exists():
                    original = original.with_name(original.name + ".gpg")
                if not original.resolve().is_relative_to(p.originals.resolve()):
                    return None
                if original in seen:
                    continue
                seen.add(original)
                if original.suffix == ".gpg":
                    answer = vault_client.admin_call("open_file", p.admin_sock, path=str(original))
                    digest = hashlib.sha256(base64.b64decode(answer["data"], validate=True)).hexdigest()
                else:
                    digest = _export_hash(original)
                found.add(digest)
    except (OSError, UnicodeError, ValueError, KeyError, candidates.ReviewError, vault_client.VaultError):
        return None
    return found


def _export_code(folder: Path, rows: list[dict[str, str]]) -> str | None:
    from awb import codes

    # Match the whole folder token, never export-1 inside export-10. Conflicting rows need an owner decision.
    mention = re.compile(r"(?<![\w.-])%s(?![\w.-])" % re.escape(folder.name))
    queued = {r["project"] for r in rows if r["project"] and
              any(mention.search(t) for t in (r["what"], r["next"]))}
    choices = queued or set(codes.PROJECT_RE.findall(folder.name))
    return next(iter(choices)) if len(choices) == 1 else None


def export_items(p: config.Paths, queue: Path | None = None) -> list[Item]:
    """Recent, wholly unimported exports. File names and content never enter an item, only folder and count."""
    from awb import ledger, workqueue

    queue = queue or queue_file()
    try:
        rows = workqueue.load(queue) if queue.suffix != ".md" else []
    except (workqueue.QueueError, ledger.LedgerError, OSError, UnicodeError):
        rows = []
    cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - 30 * 86400
    imported = None
    out = []
    # the owner's home; AWB_EXPORTS_ROOT names another folder (the tests keep the host's own exports out)
    root = Path(os.environ.get("AWB_EXPORTS_ROOT") or Path.home())
    for folder in sorted(root.glob("export-*")):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            files = []

            def failed(error):
                raise error

            for root, dirs, names in os.walk(folder, onerror=failed, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
                for name in names:
                    path = Path(root) / name
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        files.append((path, info.st_mtime))
            if not files or max(mtime for _, mtime in files) <= cutoff:
                continue
            if imported is None:
                imported = _import_hashes(p)
                if imported is None:
                    return []
            if any(_export_hash(path) in imported for path, _ in files):
                continue
        except OSError:
            continue
        try:
            unsafe = ledger.screen(folder.name, ledger.register_path(p), candidates=False)
        except ledger.LedgerError:
            unsafe = True
        if unsafe or any(ord(c) < 32 or ord(c) == 127 for c in str(folder)):
            out.append(Item("?", ledger.WITHHELD, WHERE_TERMINAL, kind="export"))
            continue
        code = _export_code(folder, rows)
        directory = shlex.quote(str(folder))
        command = ("awb import %s --trusted %s" % (code, directory) if code else
                   'awb start project --from %s --trusted --goal "..."' % directory)
        out.append(Item("export " + folder.name, "%d files, not imported" % len(files), WHERE_TERMINAL,
                        command, "export"))
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
                            "\n".join(build_commands(tid, word)), "done" if state in _DONE_STATES else ""))
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


def refresh_state_path() -> Path:
    """The file the weekly refresh writes after each run: <state folder>/last.json."""
    return Path(os.environ.get(REFRESH_ENV) or REFRESH_DIR) / DATASET_FILE


def refresh_items(path: Path | None = None) -> list[Item]:
    """The failed weekly refresh as one item; nothing when the last run went through or none ran."""
    state = _dataset_state(path or refresh_state_path())
    if state.get("state") != "failed":
        return []
    return [Item("refresh", "weekly run failed: %s" % _short(str(state.get("reason") or "unknown")), "")]


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


def doc_host_lines() -> list[str]:
    """One line when documentation hosts were added on the host (`awb doc hosts add`) and are not in the repository
    list yet, so they are committed in one batch."""
    def lines(f: Path | None) -> list[str]:
        try:
            text = f.read_text(encoding="utf-8") if f else ""
        except (OSError, UnicodeDecodeError):
            return []
        return [x for x in (ln.split("#", 1)[0].strip().lower() for ln in text.splitlines()) if x]

    # Read here, not through awb.tcp.doc: the core never imports the platform package. The same two files as
    # doc.extra_hosts and doc.allowed_hosts: the file the paths.conf line doc_hosts names, rules/allow-domains.txt.
    value = config.host_conf().get("doc_hosts", "").strip()
    extra = lines(Path(value) if value.startswith("/") else None)
    repo = set(lines(Path(__file__).resolve().parent.parent / "rules" / "allow-domains.txt"))
    n = len({h for h in extra if h not in repo})
    return ["%d host%s added on the host, not in the repository" % (n, "" if n == 1 else "s")] if n else []


def notes(path: Path | None = None, now: datetime.datetime | None = None, p: config.Paths | None = None
          ) -> list[str]:
    """The lines that tell him something without waiting on him: the weekly publish of the last 24 hours, after the
    name check (a line with a hit or a check that cannot run gives nothing), and the documentation hosts added on
    the host."""
    return _publish_notes(path, now, p) + doc_host_lines()


def _publish_notes(path: Path | None, now: datetime.datetime | None, p: config.Paths | None) -> list[str]:
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
    each with its leftovers by kind and the command the project session runs; then every create kept on purpose
    within labruns.KEPT_DAYS days (task LK) with its tenant, kind, the first 8 characters of its id and the reason."""
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
    try:
        kept = labruns.kept(p, now)
    except Exception:
        kept = []
    for k in kept:
        text = "lab run on %s kept %s %s: %s" % (k["tenant"], k["label"], str(k["id"])[:8], k["reason"])
        out.append(Item(str(k.get("project") or "?"), _short(text), WHERE_SESSION, None, "kept", str(k["tenant"])))
    return out


def tenant_mode_items(path: Path | None = None) -> list[Item]:
    """Every customer tenant in write mode (task TW, the tenant_write line of keys.conf), with its day and note, for as
    long as it stays there: there is no expiry, the owner switches it back."""
    from awb import tenantmode

    out: list[Item] = []
    for alias, e in sorted(tenantmode.read(path).items()):
        text = "customer tenant in write mode since %s: %s" % (e["since"], e.get("note") or "no note")
        out.append(Item(alias, _short(text), WHERE_TERMINAL, "awb tenant mode %s readonly" % alias))
    return out


def winlab_items(path: Path | None = None, now: datetime.datetime | None = None) -> list[Item]:
    """Every open window of a lab machine (task WL) with its project and end; an ended window is not listed."""
    from awb import winlab

    return [Item(name, _short("lab machine window open for %s until %s" % (w["project"], winlab._shown(w["end"]))),
                 WHERE_TERMINAL, "awb winlab close %s" % name) for name, w in winlab.windows(path, now)]


def winlab_online_items(path: Path | None = None, probe=None) -> list[Item]:
    """Every registered lab machine that is online now: its tunnel answers on its loopback port (task WL part 4)."""
    from awb import winlab

    return [Item(name, _short("lab machine online, its tunnel answers on port %d" % m["port"]), "")
            for name, m in winlab.online_machines(path, probe)]


def decide(items: list[Item], view: str = "decisions", code: str | None = None) -> list[Item]:
    """The items of one view. "all": every item (of project CODE when given). "dev": the tooling and steering items
    only. "decisions": no task that the owner stopped himself or that ended with success, no tooling or steering
    item, the creates kept by lab runs as one line per project, and at most PER_PROJECT owner items per project (the
    oldest first, the order of OPEN.md) with one line that counts the others."""
    if view == "all":
        return [it for it in items if code is None or it.label == code]
    if view == "dev":
        return [it for it in items if it.kind == "dev"]
    out: list[Item] = []
    kept: dict[str, Item] = {}
    shown: dict[str, int] = {}
    hidden: dict[str, int] = {}
    for it in items:
        if it.kind in ("done", "dev"):
            continue
        if it.kind == "kept":
            line = kept.get(it.label)
            if line is None:
                line = kept[it.label] = Item(it.label, "", WHERE_SESSION, None, "kept")
                out.append(line)
            line.extra = (line.extra + "," if line.extra else "") + it.extra
            line.block = str(int(line.block or 0) + 1)
            continue
        if it.kind == "owner":
            shown[it.label] = shown.get(it.label, 0) + 1
            if shown[it.label] > PER_PROJECT:
                hidden[it.label] = hidden.get(it.label, 0) + 1
                continue
        out.append(it)
    for line in kept.values():
        n = int(line.block or 0)
        tenants = ", ".join(sorted(set(line.extra.split(","))))
        line.text, line.block = "%d create%s kept on %s, listed in RESOURCES.md" % (n, "" if n == 1 else "s", tenants), None
    for label, n in hidden.items():
        last = max(i for i, it in enumerate(out) if it.kind == "owner" and it.label == label)
        out.insert(last + 1, Item("", "and %d more: awb owner status --all %s" % (n, label), "", None, "more"))
    return out


def collect(p: config.Paths | None = None, queue: Path | None = None, runs: Path | None = None,
            view: str = "decisions", code: str | None = None) -> tuple[list[Item], int]:
    """The items of VIEW (see `decide`) after the name check, and the count of parked projects."""
    from awb import devask, ledger, projects

    p = p or config.paths()
    items, parked = open_items(p)
    items += queue_items(queue)
    items += export_items(p, queue)
    commits = projects.note_commits()
    items += [Item(n["project"], "held note %s after %s" % (n["file"], n["after"]), "", kind="dev")
              for n in projects.held_notes(p) if n["after"] not in commits]
    items += build_items(runs)
    items += dataset_items()
    items += refresh_items()
    items += report_items()
    items += lab_run_items(p)
    items += tenant_mode_items()
    items += winlab_items()
    items += winlab_online_items()
    items += devask.status_items(p, view)
    for it in items:
        if it.kind != "done" and it.text.lower().startswith(_DEV_START):
            it.kind = "dev"
    texts = []
    for it in items:
        texts += [it.text, it.block or "", it.label, it.app, it.source]
    try:
        hidden = ledger.hit_indexes(texts, ledger.register_path(p))
    except ledger.LedgerError:
        hidden = set(range(len(texts)))
    for n, it in enumerate(items):
        if {5 * n, 5 * n + 1, 5 * n + 2, 5 * n + 4} & hidden:
            it.text, it.block = ledger.WITHHELD, None
            it.has_default = False
            if 5 * n + 2 in hidden:
                it.label = "?"
        if 5 * n + 3 in hidden:
            it.app = ledger.WITHHELD
    return decide(items, view, code), parked


def check_ran(p: config.Paths | None = None) -> bool:
    """True when the name check can run now (the mail is sent only then)."""
    from awb import ledger

    p = p or config.paths()
    try:
        ledger.hit_indexes(["probe"], ledger.register_path(p))
    except ledger.LedgerError:
        return False
    return True


def count(items: list[Item]) -> int:
    """The items without the lines that only count others."""
    return sum(1 for it in items if it.kind != "more")


def render(items: list[Item], parked: int, note_lines: list[str] | tuple = ()) -> list[str]:
    """The list: one line per item, the block indented under it, then the notes."""
    if not items:
        head = ["nothing waits on you"]
    else:
        head = ["%d item%s wait on you" % (count(items), "" if count(items) == 1 else "s")]
    lines = list(head)
    for it in items:
        if it.kind in ("more", "dev-overdue"):
            lines.append(it.text)
            continue
        if it.kind == "export":
            lines.append("%s: %s  [%s]" % (it.label, it.text, it.where))
        else:
            lines.append("%s  %s  [%s]" % (it.label, it.text, it.where) if it.where else "%s: %s" % (it.label, it.text))
        for ln in (it.block or "").splitlines():
            lines.append("    %s" % ln)
    if parked:
        lines.append("%d parked project%s not listed" % (parked, "" if parked == 1 else "s"))
    lines += list(note_lines)
    return lines


MAIL_MAX = 4000
"""Characters of one owner mail: the key service's notify refuses a longer message (NOTIFY_MAX of awb/tcp/keys.py)."""


def fit(lines: list[str], limit: int = MAIL_MAX) -> str:
    """The mail body within LIMIT: whole items from the top, then one line that counts the items left out."""
    body = "\n".join(lines) + "\n"
    if len(body) <= limit:
        return body
    starts = [i for i, ln in enumerate(lines) if i and not ln.startswith("    ")]
    for cut in reversed(starts):
        left = sum(1 for i in starts if i >= cut)
        tail = "... %d more line%s left out, the console lists them all\n" % (left, "" if left == 1 else "s")
        body = "\n".join(lines[:cut]) + "\n" + tail
        if len(body) <= limit:
            return body
    return body[:limit]


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
    body = fit(render(items, parked, note_lines))
    subject = ("Workbench: %d item%s wait on you" % (count(items), "" if count(items) == 1 else "s") if items
               else "Workbench: nothing waits on you, %d note%s" % (len(note_lines), "" if len(note_lines) == 1 else "s"))
    try:
        word = send(subject, body)
    except Exception as err:
        return "not sent: %s" % type(err).__name__
    return "sent" if word == "sent" or word is True else "not sent: %s" % (word if isinstance(word, str) else "refused")
