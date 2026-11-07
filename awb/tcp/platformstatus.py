"""The status of the TCP platform for the board: the open incidents and the next maintenances (B-042).

    awb platform-status pull    read the public status page and write <shared>/platform-status/state.tsv
    awb platform-status show    print the section "TCP platform" of the board alone

The T-Cloud Status Dashboard answers without a key (knowledge base KB-GI4M): GET /v2/events (the last 50 events) and
GET /v2/components (which names the components of an event and so its regions). The pull is two GET calls over HTTPS
to the host fixed below, a time limit of 20 seconds for each, no redirect followed and at most 2 MB read. It sends no
cookie and no key. It runs as the work user under a oneshot unit every 15 minutes and in a working session.

state.tsv holds one header row, `pull`, with the time of the pull (UTC, ISO), `ok` or the one word of the failure
(timeout, http or json) and the time of the last good pull, then one `event` row for each event that is open now (an
incident without an end or with an end in the future whose status is not resolved or completed) or that starts within
the next 14 days: type, status, impact, start, end, regions, the title cut to 100 characters and the event id. Every
cell is matched against its form (awb/tcp/board.py, platform_row) before it is written and again when the board reads
it; an event with a type, status, impact, date or id outside its form is dropped, a region outside the list leaves the
list and a title outside its form (printable characters only, no | and no tab) leaves its cell empty. A failed pull
keeps the rows of the last good one and says so in the header. The file is replaced atomically.

Nothing the page says reaches the board unmatched: the titles pass the data check when the board is put together
(awb/tcp/web/projects_api.py, platform) although the page carries no customer data.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path

from awb.tcp import board

HOST = "status.otc-service.com"
EVENTS_PATH = "/v2/events"
COMPONENTS_PATH = "/v2/components"
USER_AGENT = "Architect-Workbench-platform-status/1"
TIMEOUT = 20.0
MAX_BYTES = 2 << 20
PLANNED_DAYS = 14
FOLDER = "platform-status"
FILE = "state.tsv"


class PullError(Exception):
    """A pull that failed; `word` is the one word of the header: timeout, http or json."""

    def __init__(self, word: str):
        super().__init__(word)
        self.word = word


def https_get(host: str, path: str, timeout: float = TIMEOUT, cap: int = MAX_BYTES) -> tuple[int, bytes]:
    """One GET over HTTPS on port 443 within `timeout` seconds in all: (status, body). The body is read up to `cap`
    bytes and a longer one is an error. Raises PullError."""
    deadline = time.monotonic() + timeout
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                           "Accept-Encoding": "identity"})
        response = conn.getresponse()
        chunks, size = [], 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PullError("timeout")
            if conn.sock is not None:
                conn.sock.settimeout(left)
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise PullError("http")
            chunks.append(chunk)
        return response.status, b"".join(chunks)
    except PullError:
        raise
    except (socket.timeout, TimeoutError):
        raise PullError("timeout") from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise PullError("http") from None
    finally:
        if conn is not None:
            conn.close()


def fetch_json(path: str, getter):
    """The JSON answer of GET `path` on the fixed host; PullError http for a status other than 200, json for a body
    that is not JSON."""
    status, body = getter(HOST, path, TIMEOUT, MAX_BYTES)
    if status != 200:
        raise PullError("http")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PullError("json") from None


def _list(data) -> list:
    """The records of an answer: a list or an object with the list under `data`."""
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise PullError("json")
    return data


def component_regions(components: list) -> dict[int, str]:
    """The region of each component id from the attributes of /v2/components ({name, value} pairs or an object)."""
    out = {}
    for c in components:
        if not isinstance(c, dict) or type(c.get("id")) is not int:
            continue
        attrs = c.get("attributes")
        if isinstance(attrs, list):
            attrs = {a.get("name"): a.get("value") for a in attrs if isinstance(a, dict)}
        if isinstance(attrs, dict) and isinstance(attrs.get("region"), str):
            out[c["id"]] = attrs["region"]
    return out


def utc_cell(value) -> str | None:
    """A date of the page as 2026-11-07T10:00:00Z (a time without a zone is UTC); None when it is none or no date."""
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    t = t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def event_cells(event: dict, regions: dict[int, str]) -> list[str]:
    """The eight cells of an event as the page gave them, not yet matched."""
    listed = event.get("components")
    ids = [c.get("id") if isinstance(c, dict) else c for c in (listed if isinstance(listed, list) else [])
           if isinstance(c, (int, dict))]
    found = sorted({regions[i] for i in ids if i in regions})
    end = event.get("end_date")

    def word(key: str) -> str:
        value = event.get(key)
        return value.strip().lower() if isinstance(value, str) else ""
    title = event.get("title")
    return [word("type"), word("status"), str(event["impact"]) if type(event.get("impact")) is int else "",
            utc_cell(event.get("start_date")) or "", (utc_cell(end) or "!") if end else "", ",".join(found),
            title[:board.PLATFORM_TITLE_MAX] if isinstance(title, str) else "",
            str(event["id"]) if type(event.get("id")) is int else ""]


def select(events: list, regions: dict[int, str], now: dt.datetime) -> tuple[list[dict], int]:
    """The rows to keep and the number of events that matched no form. Kept: an incident that is open now and an
    event of any type that starts within PLANNED_DAYS days and is not called off."""
    rows, dropped = [], 0
    horizon = now + dt.timedelta(days=PLANNED_DAYS)
    for event in events:
        if not isinstance(event, dict):
            dropped += 1
            continue
        row = board.platform_row(event_cells(event, regions))
        if row is None:
            dropped += 1
            continue
        start = board.platform_time(row["start"])
        end = board.platform_time(row["end"]) if row["end"] else None
        is_open = row["type"] == "incident" and row["status"] not in board.PLATFORM_CLOSED and (
            end is None or end > now)
        is_planned = now < start <= horizon and row["status"] not in board.PLATFORM_CALLED_OFF
        if is_open or is_planned:
            rows.append(row)
    return rows, dropped


def state_text(now: dt.datetime, result: str, good: str | None, rows: list[dict]) -> str:
    """state.tsv: the header and one event row each; every row matched once more before it is written."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = ["\t".join(("pull", stamp, result, good or ""))]
    for row in rows:
        again = board.platform_row(board.platform_cells(row))
        if again is not None:
            lines.append("\t".join(("event",) + tuple(board.platform_cells(again))))
    return "\n".join(lines) + "\n"


def write_state(folder: Path, text: str) -> None:
    """<folder>/state.tsv replaced atomically: a file beside it written and synced, then renamed, through a folder
    handle opened without following a link."""
    from awb import config

    config.make_dir(folder, 0o750, shared=True)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp = ".%s.%d.tmp" % (FILE, os.getpid())
        try:
            os.unlink(tmp, dir_fd=fd)
        except FileNotFoundError:
            pass
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=fd)
        with os.fdopen(out, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, FILE, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def pull(p, *, getter=None, now: dt.datetime | None = None) -> tuple[str, int, int]:
    """One pull: (result, rows written, events dropped). The result is ok or the word of the failure; a failed pull
    writes the header with that word and the rows of the last good pull again. Raises OSError when the file cannot be
    written."""
    getter = getter or https_get
    now = now or dt.datetime.now(dt.timezone.utc)
    before = board.platform_state(p)
    try:
        events = _list(fetch_json(EVENTS_PATH, getter))
        try:
            regions = component_regions(_list(fetch_json(COMPONENTS_PATH, getter)))
        except PullError:
            regions = {}     # the regions are a name, not the fact: an incident without them is still shown
        rows, dropped = select(events, regions, now)
    except PullError as err:
        kept = before["rows"] if before else []
        write_state(p.shared / FOLDER, state_text(now, err.word, before["pulled"] if before else None, kept))
        return err.word, len(kept), 0
    write_state(p.shared / FOLDER, state_text(now, "ok", now.strftime("%Y-%m-%dT%H:%M:%SZ"), rows))
    return "ok", len(rows), dropped


def main(argv: list[str] | None = None) -> int:
    from awb import config
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb platform-status", description="The open incidents and the next maintenances of the "
                                                            "TCP platform for the board.")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("pull", help="read the public status page and write the state file of the board")
    sub.add_parser("show", help="print the section TCP platform of the board")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command not in ("pull", "show"):
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    if args.command == "pull":
        try:
            result, rows, dropped = pull(p)
        except OSError as err:
            print("awb platform-status: the state file cannot be written (%s)" % type(err).__name__, file=sys.stderr)
            return 1
        if result != "ok":
            print("awb platform-status: the pull failed (%s); %d row(s) of the last good pull kept" % (result, rows),
                  file=sys.stderr)
            return 1
        print("awb platform-status: %d row(s) written%s" % (rows, ", %d event(s) dropped" % dropped if dropped else ""))
        return 0
    from awb.tcp.web import projects_api

    line = board.locked(p)
    if line:
        print(line, flush=True)
    try:
        data = projects_api.platform(p)
    except projects_api.Unavailable as err:
        print("awb platform-status: %s" % err, file=sys.stderr)
        return 1
    print("\n".join(board.platform_lines(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
