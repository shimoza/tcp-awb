"""owner-actions: the service behind the owner routes of the console (T9 step 2, DESIGN-owner-level.md section 4).

    python3 -I -m awb.tcp.web.owner_actions --peer awb-web [--socket PATH] [--status FILE] [--log DIR]

It runs as its own system user (awb-owner), never as the owner, on /run/awb-owner.sock (a socket unit: root, group
awb-web, 0660). It answers a connection only when the peer's uid is the gateway's (SO_PEERCRED) and reads one file,
the status the owner's timer writes (`awb owner status --write`): codes, numbers, fixed words and times. It opens
nothing else, holds no key, reaches no vault and runs no command.

    GET /api/owner/state      the vault and the key service (state and since), the deployed release, the page
    GET /api/owner/ui-runs    the newest runs of the UI queue, the one Publish would take marked
    GET /api/owner/intake     per customer code the candidates that wait for a review and the time of the report

Every answer carries `written` (when the owner's timer wrote the status) and `stale` (older than five minutes). A
log line per request goes to <log>/owner-YYYY-MM.tsv (600): time, level, route, result word, request id.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import socket
import socketserver
import struct
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SOCKET = "/run/awb-owner.sock"
STATUS = Path("/var/lib/awb-owner-status/status.json")
LOG_DIR = Path("/var/lib/awb-owner-actions/log")
MAX_STATUS = 256 * 1024
STALE = 300
REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
UNAVAILABLE = "The owner status is not available yet."
ROUTES = ("/api/owner/state", "/api/owner/ui-runs", "/api/owner/intake")


def read_status(path: Path, opener=None) -> dict | None:
    """The status file, checked field by field; None when it is missing, too large, a link or of another shape."""
    from awb import status_shape

    try:
        fd = (opener or os.open)(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_STATUS + 1)
    except OSError:
        return None
    if len(raw) > MAX_STATUS:
        return None
    try:
        data = json.loads(raw)
        status_shape.validate(data)
    except (ValueError, status_shape.StatusError, TypeError, AttributeError, KeyError):
        return None
    return data


def stale(written: str | None, now: float) -> bool:
    try:
        t = datetime.strptime(written or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return True
    return now - t > STALE


def answer(route: str, status: dict | None, now: float) -> tuple[int, dict]:
    if status is None:
        return 503, {"error": UNAVAILABLE}
    meta = {"written": status["written"], "stale": stale(status["written"], now)}
    if route == "/api/owner/state":
        return 200, {"vault": status["vault"], "keys": status["keys"], "deployed": status["deployed"],
                     "page": status["page"], **meta}
    if route == "/api/owner/ui-runs":
        return 200, {"runs": status["ui_runs"], **meta}
    return 200, {"customers": status["intake"], **meta}


class Handler(BaseHTTPRequestHandler):
    server_version = "AWB"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def reply(self, status: int, data: dict, word: str) -> None:
        raw = json.dumps(data, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.server.log(self.headers.get("X-AWB-Level", "-"), self.path.split("?")[0], word,
                        self.headers.get("X-AWB-Request", ""))

    def do_GET(self):
        route = self.path.split("?")[0]
        if route not in ROUTES:
            return self.reply(404, {"error": "Not found."}, "not-found")
        if self.headers.get("X-AWB-Level") != "owner":
            return self.reply(403, {"error": "Not allowed."}, "level")
        status, data = answer(route, read_status(self.server.status), self.server.clock())
        self.reply(status, data, "ok" if status == 200 else "unavailable")

    do_HEAD = do_GET

    def do_POST(self):
        self.reply(405, {"error": "Method not allowed."}, "method")

    def log_message(self, fmt, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The owner-actions server on a listening Unix socket: only the gateway's uid gets an answer."""
    daemon_threads = True

    def __init__(self, sock, peer: int, status: Path, log_dir: Path | None, clock=time.time):
        socketserver.BaseServer.__init__(self, sock.getsockname(), Handler)
        self.socket, self.peer, self.status, self.log_dir, self.clock = sock, peer, Path(status), log_dir, clock

    def verify_request(self, request, client_address):
        try:
            uid = struct.unpack("3i", request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                         struct.calcsize("3i")))[1]
        except OSError:
            uid = -1
        if uid != self.peer:
            self.log("-", "-", "peer", "")
            return False
        return True

    def log(self, level: str, route: str, word: str, request_id: str) -> None:
        if self.log_dir is None:
            return
        t = time.gmtime(self.clock())
        line = "\t".join((time.strftime("%Y-%m-%dT%H:%M:%SZ", t), level if level in ("owner", "reader") else "-",
                          route if route in ROUTES or route == "-" else "other", word,
                          request_id if REQUEST_ID_RE.match(request_id or "") else "-")) + "\n"
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.log_dir / ("owner-%s.tsv" % time.strftime("%Y-%m", t)),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="ascii") as fh:
                fh.write(line)
        except OSError:
            pass


def listener(path: str):
    """The socket systemd handed over, else one bound at `path` (a run without socket activation)."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    if os.path.exists(path):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(16)
    return sock


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="owner_actions")
    ap.add_argument("--peer", required=True, help="the user whose processes may connect (the gateway's)")
    ap.add_argument("--socket", default=SOCKET)
    ap.add_argument("--status", default=str(STATUS))
    ap.add_argument("--log", default=str(LOG_DIR))
    a = ap.parse_args(argv)
    try:
        peer = pwd.getpwnam(a.peer).pw_uid
    except KeyError:
        print("owner-actions: the user %s does not exist" % a.peer, flush=True)
        return 2
    server = Server(listener(a.socket), peer, Path(a.status), Path(a.log))
    print("owner-actions ready", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
