"""The board: the status of every active project for management, on demand.

    awb board write [--out DIR]   write board-<date>-<time>.md and .html into DIR (default: presentations/board/
                                  of the repository) and print the two paths
    awb board show                the same as Markdown on the terminal

The same data as GET /api/board of the portal (awb/tcp/web/projects_api.py, board()): the recorded status of each
active project (the Status: and Next: lines of STATE.md) with its time and the commits newer than it, what the
project waits on, its first open items, its deliverables by review state and its live resources. A project whose
status lags behind its commits or has no status line yet is marked, never updated: the session of the project does
that. Codes only, every text after the data check. The HTML prints on A4 as it is, for a PDF from the browser.
Both carry the line of the last wipe measurement of the refresh: "wipe: L leaks, M losses of N (date)", and the build
line of the self-build from <shared>/build/state.tsv, which the build runner writes (awb/buildrun.py): "build: B-012
done 14:02, B-013 running since 14:10; live eb928d2, main 1a2b3c4, 1 not deployed". Every cell of that file is
matched against a fixed form or vocabulary before it reaches the line; anything else is dropped or cut to its word.
A task unit that failed while no runner watched (the oneshot awb-task-failed@.service, SB5) adds "; no runner
watched: B-012 failed 14:02 (timeout)" until the runner writes the file again.
While the vault is locked both commands open with "locked since <time>" (T3); the project texts wait for the
unlock, because the data check cannot run.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "presentations" / "board"
ZONE = "Europe/Berlin"


def when(value: str | None) -> str:
    """A time of the board as the readers in Germany read it: 06.10.2026 14:11 CEST."""
    if not value:
        return "unknown"
    t = dt.datetime.fromisoformat(value)
    try:
        from zoneinfo import ZoneInfo

        t = t.astimezone(ZoneInfo(ZONE))
    except Exception:
        t = t.astimezone(dt.timezone.utc)
    return t.strftime("%d.%m.%Y %H:%M ") + (t.tzname() or "UTC")


def _customer(p: dict) -> str:
    return "internal" if p.get("customer") in (None, "none") else p["customer"]


def _as_of(p: dict) -> str:
    s = p["status"]
    if not s.get("summary"):
        return "no status line yet"
    text = when(s.get("updated"))
    if s.get("behind"):
        text += ", %d change(s) since: may be out of date" % s["behind"]
    return text


def _deliverables(p: dict) -> str:
    d = p["deliverables"]
    if d["status"] != "available":
        return "cannot be read"
    if not d["counts"]:
        return "none"
    return ", ".join("%d %s" % (n, state) for state, n in sorted(d["counts"].items()))


def _more(block: dict) -> str:
    rest = block["total"] - len(block["items"])
    return " (and %d more)" % rest if rest > 0 else ""


def markdown(board: dict) -> str:
    s = board["summary"]
    out = ["# Project status, %s" % when(board["generated_at"]), "",
           "%d active project(s): %d with a current status, %d lagging behind their work, %d without a status "
           "line yet." % (s["projects"], s["current"], s["lagging"], s["without_status"]), ""]
    if board.get("wipe"):
        out += [board["wipe"], ""]
    if board.get("build"):
        out += [board["build"], ""]
    out += ["| Project | Customer | Status | Next | Status as of | Open | Live |", "|---|---|---|---|---|---|---|"]

    def cell(text) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")
    for p in board["projects"]:
        st = p["status"]
        out.append("| %s (%s) | %s | %s | %s | %s | %s | %s |" % (
            p["code"], p["kind"], _customer(p), cell(st.get("summary") or "-"), cell(st.get("next") or "-"),
            cell(_as_of(p)), "-" if p["open_items"] is None else p["open_items"],
            "-" if p["live_resources"] is None else p["live_resources"]))
    for p in board["projects"]:
        st = p["status"]
        out += ["", "## %s: %s" % (p["code"], p["goal"] or "no goal recorded"), "",
                "- Customer: %s, kind %s, created %s" % (_customer(p), p["kind"], p["created"]),
                "- Status: %s" % (st.get("summary") or "none recorded yet"),
                "- Next: %s" % (st.get("next") or "none recorded yet"),
                "- Status as of: %s" % _as_of(p)]
        if p["waiting_on"]["items"]:
            out.append("- Waiting on%s:" % _more(p["waiting_on"]))
            out += ["  - %s" % x for x in p["waiting_on"]["items"]]
        if p["open"]["items"]:
            out.append("- Open items, %d in all%s:" % (p["open"]["total"], _more(p["open"]) and ", the first five"))
            out += ["  - %s" % x for x in p["open"]["items"]]
        out += ["- Deliverables: %s" % _deliverables(p),
                "- Live resources in the cloud: %s" % ("unknown" if p["live_resources"] is None
                                                       else p["live_resources"])]
    return "\n".join(out) + "\n"


STYLE = """
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1d1d1f;margin:32px auto;max-width:1100px;
padding:0 16px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:28px 0 6px;border-top:1px solid #ddd;padding-top:14px}
p.lead{color:#555;margin:0 0 18px}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #e3e3e3;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f5f5f7}.late{color:#9a5b00;font-weight:600}.none{color:#888}ul{margin:4px 0 0 18px;padding:0}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}dt{color:#666}dd{margin:0}
.wrap{overflow-x:auto}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}section{break-inside:avoid}th{background:#eee}}
@page{size:A4;margin:14mm}
"""


def page(board: dict) -> str:
    e = html.escape
    s = board["summary"]

    def as_of(p: dict) -> str:
        text = e(_as_of(p))
        if not p["status"].get("summary"):
            return '<span class="none">%s</span>' % text
        return '<span class="late">%s</span>' % text if p["status"].get("behind") else text

    rows = "".join(
        "<tr><td>%s<br><span class=\"none\">%s</span></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%s</td><td>%s</td></tr>" % (
            e(p["code"]), e(p["kind"]), e(_customer(p)), e(p["status"].get("summary") or "-"),
            e(p["status"].get("next") or "-"), as_of(p), "-" if p["open_items"] is None else p["open_items"],
            "-" if p["live_resources"] is None else p["live_resources"]) for p in board["projects"])

    def items(block: dict) -> str:
        return "<ul>%s</ul>%s" % ("".join("<li>%s</li>" % e(x) for x in block["items"]), e(_more(block)))
    sections = []
    for p in board["projects"]:
        st = p["status"]
        rows_dl = [("Customer", e("%s, kind %s, created %s" % (_customer(p), p["kind"], p["created"]))),
                   ("Status", e(st.get("summary") or "none recorded yet")),
                   ("Next", e(st.get("next") or "none recorded yet")),
                   ("Status as of", as_of(p))]
        if p["waiting_on"]["items"]:
            rows_dl.append(("Waiting on", items(p["waiting_on"])))
        if p["open"]["items"]:
            rows_dl.append(("Open items (%d)" % p["open"]["total"], items(p["open"])))
        rows_dl += [("Deliverables", e(_deliverables(p))),
                    ("Live resources", "unknown" if p["live_resources"] is None else str(p["live_resources"]))]
        sections.append("<section><h2>%s: %s</h2><dl>%s</dl></section>" % (
            e(p["code"]), e(p["goal"] or "no goal recorded"),
            "".join("<dt>%s</dt><dd>%s</dd>" % (k, v) for k, v in rows_dl)))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>Project status %s</title><style>%s</style></head>"
            "<body><h1>Project status, %s</h1><p class=\"lead\">%d active project(s): %d with a current status, "
            "%d lagging behind their work, %d without a status line yet.</p>%s<div class=\"wrap\"><table><thead><tr>"
            "<th>Project</th><th>Customer</th><th>Status</th><th>Next</th><th>Status as of</th><th>Open</th>"
            "<th>Live</th></tr></thead><tbody>%s</tbody></table></div>%s</body></html>\n" % (
                e(when(board["generated_at"])), STYLE, e(when(board["generated_at"])), s["projects"], s["current"],
                s["lagging"], s["without_status"],
                "".join('<p class="lead">%s</p>' % e(board[k]) for k in ("wipe", "build") if board.get(k)), rows,
                "".join(sections)))


def write(board: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromisoformat(board["generated_at"]).astimezone(dt.timezone.utc).strftime("%Y-%m-%d-%H%M")
    md, page_path = out / ("board-%s.md" % stamp), out / ("board-%s.html" % stamp)
    md.write_text(markdown(board), encoding="utf-8")
    page_path.write_text(page(board), encoding="utf-8")
    return md, page_path


WIPE_COLUMNS = ("date", "cases", "leaks", "losses", "leaked_values", "lost_terms", "withheld", "errors", "release",
                "by_module")


def wipe_tsv(p) -> Path:
    return p.shared / "refresh" / "wipe.tsv"


def wipe_history(p) -> list[dict]:
    """The lines of <shared>/refresh/wipe.tsv (the wipe part of awb refresh writes them) as dicts, oldest first; a
    line that does not parse is skipped."""
    try:
        text = wipe_tsv(p).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != len(WIPE_COLUMNS) or cells[0] == "date":
            continue
        row = dict(zip(WIPE_COLUMNS, cells))
        try:
            for key in ("cases", "leaks", "losses", "leaked_values", "lost_terms", "withheld", "errors"):
                row[key] = int(row[key])
        except ValueError:
            continue
        rows.append(row)
    return rows


def wipe_line(p) -> str:
    """The line of the board: "wipe: L leaks, M losses of N (date)" from the last line of wipe.tsv."""
    rows = wipe_history(p)
    if not rows:
        return "wipe: not measured yet"
    r = rows[-1]
    return "wipe: %d leaks, %d losses of %d (%s)" % (r["leaks"], r["losses"], r["cases"], r["date"])


# --------------------------------------------------------------------------- the build line (self-build SB4)

BUILD_TSV_MAX = 256 << 10
DONE_SHOWN = 3
_ID = re.compile(r"B-[0-9]{3,4}")                        # awb.build.ID_RE
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}):\d{2}")    # awb.build._STAMP
_SHORT = re.compile(r"[0-9a-f]{7}")
BUILD_WORDS = ("queued", "running", "green", "done", "asked", "answered", "widened", "interrupted", "stale", "hung",
               "red", "stopped", "held")
"""The state words of awb.build.STATES; a reason after `word: ` shows only when it is in BUILD_REASONS."""
GATE_CLASSES = ("name", "secret", "private-key", "token", "homepath", "blocklist", "identifier", "opaque",
                "attribution")                          # awb.gate.CLASSES
BUILD_REASONS = {
    "hung": ("suite",),
    "red": ("no commit", "second branch", "not on main", "too many commits", "merge commit", "dirty tree", "identity",
            "outside paths", "link or submodule", "changelog", "tests.json", "rules list lost lines", "suite",
            "tests lost", "check") + tuple("gate %s" % c for c in GATE_CLASSES),
    "stopped": ("by_owner", "no_result", "session_gone", "error", "error_max_turns", "error_during_execution",
                "error_max_budget_usd"),
    "held": ("checks pending", "push refused", "push pending", "prompt", "prepare", "model endpoint", "no main",
             "baseline", "gate selftest", "login missing"),
}
"""The fixed phrases of the runner (a test reads them out of awb/buildrun.py)."""
QUEUE_HELD = ("vault locked", "no vault daemon", "key service not loaded", "no key service", "gate selftest",
              "build root full", "build root missing", "no identity", "model key", "systemd-run as the build user",
              "build model", "login missing", "model endpoint")
"""The reasons the runner holds the whole queue for (awb/buildrun.py, preconditions)."""
UNIT_RESULTS = ("success", "exit-code", "signal", "core-dump", "timeout", "watchdog", "start-limit-hit", "resources",
                "oom-kill", "protocol", "exec-condition", "start-failed", "unknown")
"""awb.buildrun.RESULTS and `unknown`: the result of a task unit that failed while no runner watched (SB5)."""
_TASK_UNIT = re.compile(r"awb-task-(B-[0-9]{3,4}(?:-(?:prep|hand|suite))?)")    # awb.buildrun.UNIT_RE, the probe out


def build_tsv(p) -> Path:
    return p.shared / "build" / "state.tsv"


def _read_small(path: Path, cap: int) -> str | None:
    """A regular file, never through a link, under a cap, UTF-8; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > cap:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_state_word(state: str) -> str | None:
    """The state as the line may print it: the word, with its reason only when the reason is a fixed phrase."""
    word, sep, reason = state.partition(": ")
    if word not in BUILD_WORDS:
        return None
    return "%s: %s" % (word, reason) if sep and reason in BUILD_REASONS.get(word, ()) else word


def build_state(p) -> dict | None:
    """The rows of state.tsv, each cell matched before use; None without a readable file."""
    text = _read_small(build_tsv(p), BUILD_TSV_MAX)
    if text is None:
        return None
    out = {"queue": None, "tasks": [], "live": "unknown", "main": "unknown", "not_deployed": None, "behind": [],
           "failed": []}
    seen = set()
    for row in text.splitlines():
        cells = row.split("\t")
        kind = cells[0]
        if kind == "queue" and len(cells) == 3 and cells[1] == "held":
            out["queue"] = cells[2] if cells[2] in QUEUE_HELD else "held"
        elif kind == "task" and len(cells) == 7:
            tid, n, state, since, commit, mail = cells[1:]
            word = build_state_word(state)
            m = _STAMP.fullmatch(since)
            if not _ID.fullmatch(tid) or tid in seen or not n.isdigit() or word is None:
                continue
            seen.add(tid)
            out["tasks"].append({"id": tid, "state": word, "since": since if m else "", "time": m.group(1) if m else "",
                                 "commit": commit if _SHORT.fullmatch(commit) else "",
                                 "mail_not_sent": mail == "not_sent"})
        elif kind in ("live", "main") and len(cells) == 2 and _SHORT.fullmatch(cells[1]):
            out[kind] = cells[1]
        elif kind == "not_deployed" and len(cells) == 2 and cells[1].isdigit() and len(cells[1]) <= 4:
            out["not_deployed"] = int(cells[1])
        elif kind == "daemons" and len(cells) == 2:
            out["behind"] = [d for d in ("vault", "keys") if d in cells[1].split(",")]
        elif kind == "failed" and len(cells) == 4:
            m_unit, m = _TASK_UNIT.fullmatch(cells[1]), _STAMP.fullmatch(cells[3])
            if m_unit and m and cells[2] in UNIT_RESULTS:
                out["failed"].append({"unit": m_unit.group(1), "result": cells[2], "time": m.group(1)})
    return out


def build_line(p) -> str:
    """The board's build line: the tasks that are not done, the newest done ones, live / main / not deployed, the
    daemon code behind and a mail that was not sent."""
    data = build_state(p)
    if data is None:
        return "build: no runner state yet"
    tasks = sorted(data["tasks"], key=lambda t: (t["since"], t["id"]))
    done = [t for t in tasks if t["state"] == "done"]
    shown = [t for t in tasks if t["state"] != "done" or t in done[-DONE_SHOWN:]]
    parts = []
    if data["queue"]:
        parts.append("queue held" if data["queue"] == "held" else "queue held: %s" % data["queue"])
    for t in shown:
        if t["state"] == "done":
            parts.append("%s done %s" % (t["id"], t["time"] or "unknown"))
        elif t["state"] == "running":
            parts.append("%s running since %s" % (t["id"], t["time"] or "unknown"))
        else:
            parts.append("%s %s" % (t["id"], t["state"]))
    if len(done) > DONE_SHOWN:
        parts.append("%d more done" % (len(done) - DONE_SHOWN))
    line = "build: " + (", ".join(parts) or "no task yet")
    nd = data["not_deployed"]
    line += "; live %s, main %s, %s not deployed" % (data["live"], data["main"], "unknown" if nd is None else nd)
    if data["behind"]:
        line += "; daemon code behind: %s" % ", ".join(data["behind"])
    unsent = [t["id"] for t in tasks if t["mail_not_sent"]]
    if unsent:
        line += "; mail not sent: %s" % " ".join(unsent)
    if data["failed"]:
        line += "; no runner watched: %s" % ", ".join(
            "%s failed %s (%s)" % (f["unit"], f["time"], f["result"]) for f in data["failed"][-DONE_SHOWN:])
    return line


def locked(p) -> str | None:
    """"locked since <time>" while the vault daemon says it is locked, else None (also without a daemon)."""
    from awb import vault

    try:
        state = vault.ping_state(p.check_socket)
    except (vault.VaultError, OSError):
        return None
    return vault.locked_line(state["since"]) if state["state"] == "locked" else None


def main(argv: list[str] | None = None) -> int:
    from awb import config
    from awb.cli import SafeParser
    from awb.tcp.web import projects_api

    ap = SafeParser(prog="awb board", description="The status of every active project for management.")
    sub = ap.add_subparsers(dest="command")
    w = sub.add_parser("write", help="write the board as Markdown and HTML")
    w.add_argument("--out", default=str(DEFAULT_OUT), help="the folder for the two files")
    sub.add_parser("show", help="print the board as Markdown")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command not in ("write", "show"):
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    line = locked(p)
    if line:
        print(line, flush=True)
    try:
        board = projects_api.board(p)
    except projects_api.Unavailable as err:
        print("awb board: %s" % err, file=sys.stderr)
        return 1
    board["wipe"] = wipe_line(p)
    board["build"] = build_line(p)
    if args.command == "show":
        from awb import hooks

        print(markdown(board), end="")
        print("\n%s" % hooks.refused_line(p))
        return 0
    md, page_path = write(board, Path(args.out).expanduser())
    s = board["summary"]
    print("awb board: %d active project(s), %d current, %d lagging, %d without a status line" % (
        s["projects"], s["current"], s["lagging"], s["without_status"]))
    print(md)
    print(page_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
