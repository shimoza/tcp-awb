"""The board: the status of every active project for management, on demand.

    awb board write [--out DIR]   write board-<date>-<time>.md and .html into DIR (default: presentations/board/
                                  of the repository) and print the two paths
    awb board show                the same as Markdown on the terminal

The same data as GET /api/board of the portal (awb/tcp/web/projects_api.py, board()): the recorded status of each
active project (the Status: and Next: lines of STATE.md) with its time and the commits newer than it, what the
project waits on, its first open items, its deliverables by review state and its live resources. A project whose
status lags behind its commits or has no status line yet is marked, never updated: the session of the project does
that. Codes only, every text after the data check. The HTML prints on A4 as it is, for a PDF from the browser.
"""
from __future__ import annotations

import datetime as dt
import html
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
           "line yet." % (s["projects"], s["current"], s["lagging"], s["without_status"]), "",
           "| Project | Customer | Status | Next | Status as of | Open | Live |", "|---|---|---|---|---|---|---|"]

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
            "%d lagging behind their work, %d without a status line yet.</p><div class=\"wrap\"><table><thead><tr>"
            "<th>Project</th><th>Customer</th><th>Status</th><th>Next</th><th>Status as of</th><th>Open</th>"
            "<th>Live</th></tr></thead><tbody>%s</tbody></table></div>%s</body></html>\n" % (
                e(when(board["generated_at"])), STYLE, e(when(board["generated_at"])), s["projects"], s["current"],
                s["lagging"], s["without_status"], rows, "".join(sections)))


def write(board: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromisoformat(board["generated_at"]).astimezone(dt.timezone.utc).strftime("%Y-%m-%d-%H%M")
    md, page_path = out / ("board-%s.md" % stamp), out / ("board-%s.html" % stamp)
    md.write_text(markdown(board), encoding="utf-8")
    page_path.write_text(page(board), encoding="utf-8")
    return md, page_path


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
    try:
        board = projects_api.board(config.paths())
    except projects_api.Unavailable as err:
        print("awb board: %s" % err, file=sys.stderr)
        return 1
    if args.command == "show":
        print(markdown(board), end="")
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
