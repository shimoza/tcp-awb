"""`awb portal serve`: a read-only web page over the Workbench commands that need no model.

    awb portal serve [--port 8080]

Five pages: the knowledge base (`/kb?q=...`), live prices (`/price?service=ecs&flavor=...`), the projects
(`/projects`), the review state of their deliverables (`/reviews`) and what runs on the test tenants (`/tenants`). No page writes anything, no page calls a
model and no page takes a file.

The server listens on 127.0.0.1 only. It is reached through a tunnel that puts an access check in front of it (a
Cloudflare Tunnel with Cloudflare Access, or `ssh -L`), never directly from the network. It answers GET and HEAD
only, sends a strict content security policy and keeps no log of what was searched: the log line carries the
method, the page and the status, never the query.

Runtime code is the standard library. Every value that reaches a page is HTML-escaped.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from awb import config

HOST = "127.0.0.1"
DEFAULT_PORT = 8080
MAX_QUERY = 200
MAX_HITS = 30
MAX_PRICE_ROWS = 60
PRICE_TERMS = ("PAYG", "R12", "R36")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_REGIONS = ("eu-de", "eu-nl", "eu-ch2")

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_CSS = """
body{font-family:Calibri,Arial,sans-serif;margin:0;color:#1B2430;background:#fff}
header{background:#1B2430;color:#fff;padding:14px 24px}
header a{color:#fff;margin-right:18px;text-decoration:none}
header b{color:#E20074;margin-right:28px}
main{padding:20px 24px;max-width:1100px}
input,select{font:inherit;padding:6px 8px;border:1px solid #c5ccd5;border-radius:4px}
button{font:inherit;padding:6px 14px;border:0;border-radius:4px;background:#E20074;color:#fff}
table{border-collapse:collapse;margin-top:14px;width:100%}
th{background:#1B2430;color:#fff;text-align:left;padding:6px 8px}
td{padding:6px 8px;border-bottom:1px solid #e6e9ee;vertical-align:top}
.muted{color:#5B6878;font-size:90%}.grade{font-weight:bold;color:#E20074}.expired{color:#b00020}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> str:
    nav = ('<header><b>Architect Workbench</b><a href="/">Home</a><a href="/kb">Knowledge</a>'
           '<a href="/price">Prices</a><a href="/projects">Projects</a><a href="/reviews">Reviews</a><a href="/tenants">Tenants</a><a href="/ask">Ask</a></header>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><style>%s</style></head>'
            '<body>%s<main><h1>%s</h1>%s</main></body></html>' % (_e(title), _CSS, nav, _e(title), body))


# --------------------------------------------------------------------------- pages


def home(p: config.Paths, q: dict) -> str:
    body = ('<p>Checked facts about the platform, live prices and the state of the projects. Read only.</p>'
            '<form action="/kb"><input name="q" size="50" placeholder="search the knowledge, for example cce flavors">'
            ' <button>Search</button></form>'
            '<p class="muted">Every fact carries its grade (live, contract, docs, said, assumed) and the date it was '
            'checked. Prices come from the public price API at the moment you ask.</p>')
    return page("Architect Workbench", body)


def knowledge(p: config.Paths, q: dict) -> str:
    from awb import kb

    query = q.get("q", "")[:MAX_QUERY].strip()
    form = ('<form action="/kb"><input name="q" size="50" value="%s"> <button>Search</button></form>' % _e(query))
    if not query:
        return page("Knowledge", form)
    hits = [e for _, e in kb.find(query, where=p) if not e.is_retired][:MAX_HITS]
    if not hits:
        return page("Knowledge", form + "<p>No checked fact carries these words.</p>")
    rows = "".join(
        '<tr><td>%s</td><td class="grade">%s</td><td>%s</td><td>%s%s</td><td class="muted">%s</td></tr>'
        % (_e(e.statement), _e(e.grade), _e(e.cls), _e(e.checked),
           ' <span class="expired">expired</span>' if e.expires and e.expires < _today() else "", _e(e.id))
        for e in hits)
    table = ("<table><tr><th>fact</th><th>grade</th><th>class</th><th>checked</th><th>entry</th></tr>%s</table>"
             % rows)
    return page("Knowledge", form + "<p class=\"muted\">%d facts, best match first</p>" % len(hits) + table)


def prices(p: config.Paths, q: dict) -> str:
    from awb.tcp import price

    service = q.get("service", "").strip().lower()[:40]
    flavor = q.get("flavor", "").strip()[:60]
    grep = q.get("grep", "").strip()[:60]
    region = q.get("region", "eu-de")
    region = region if region in _REGIONS else "eu-de"
    options = "".join('<option%s>%s</option>' % (" selected" if r == region else "", r) for r in _REGIONS)
    form = ('<form action="/price"><input name="service" size="10" placeholder="ecs" value="%s"> '
            '<input name="flavor" size="18" placeholder="flavor, exact" value="%s"> '
            '<input name="grep" size="18" placeholder="part of the name" value="%s"> '
            '<select name="region">%s</select> <button>Look up</button></form>'
            % (_e(service), _e(flavor), _e(grep), options))
    if not service:
        return page("Prices", form + '<p class="muted">A service such as ecs, evs, rds or cce.</p>')
    if not _SERVICE_RE.match(service):
        return page("Prices", form + "<p>A service name reads like ecs or rds.</p>")
    got = price.fetch(service, region)
    if got.listing.state != "list":
        return page("Prices", form + "<p>The price API gave no usable answer for this service right now.</p>")
    records = price.select(got.records, flavor=flavor or None, grep=grep or None)
    shown = records[:MAX_PRICE_ROWS]
    head = "".join("<th>%s</th>" % t for t in PRICE_TERMS)
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s</tr>"
        % (_e(r.flavor), _e(r.name), _e(r.os), _e(r.unit),
           "".join("<td>%s</td>" % _e(_money(r.prices.get(t), r.currency)) for t in PRICE_TERMS))
        for r in shown)
    note = '<p class="muted">%d records, %s</p>' % (len(records), _e(got.source_line()))
    if len(records) > len(shown):
        note += '<p class="muted">The first %d are shown: narrow it with a flavor or a part of the name.</p>' % len(shown)
    return page("Prices", form + note + "<table><tr><th>flavor</th><th>name</th><th>os</th><th>unit</th>%s</tr>%s</table>"
                % (head, rows))


def project_list(p: config.Paths, q: dict) -> str:
    from awb import projects

    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (_e(x.code), _e(x.kind), _e(x.customer), _e(x.state), _e(x.created))
                   for x in projects.load(p))
    return page("Projects", "<table><tr><th>project</th><th>kind</th><th>customer</th><th>state</th>"
                            "<th>created</th></tr>%s</table>" % rows)


def reviews(p: config.Paths, q: dict) -> str:
    from awb import projects, review

    rows = []
    for x in projects.load(p):
        if x.state != "active":
            continue
        try:
            states = [s["state"] for s in review.status(Path(x.path))]
        except OSError:
            continue
        if not states:
            continue
        rows.append("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td></tr>"
                    % (_e(x.code), states.count("valid"), states.count("stale"),
                       len(states) - states.count("valid") - states.count("stale")))
    body = ("<table><tr><th>project</th><th>reviewed</th><th>changed since the review</th><th>not reviewed</th>"
            "</tr>%s</table>" % "".join(rows)) if rows else "<p>No project has a deliverable yet.</p>"
    return page("Reviews", body)


def tenant_page(p: config.Paths, q: dict) -> str:
    import datetime

    from awb.tcp import tenants

    today = datetime.date.today()
    alias = q.get("t", "").strip()[:32]
    known = [t.alias for t in tenants.load(p)]
    body = "<pre>%s</pre>" % _e("\n".join(tenants.list_lines(p, today)))
    if known:
        body += "<p>%s</p>" % " ".join('<a href="/tenants?t=%s">%s</a>' % (_e(a), _e(a)) for a in known)
    from awb.tcp import throttle

    body += "<h2>Calls to the TCP API this month</h2><pre>%s</pre>" % _e(
        "\n".join(throttle.usage_lines(p.shared, today.strftime("%Y-%m"))))
    if alias in known:
        body += "<h2>%s: now</h2><pre>%s</pre>" % (_e(alias), _e("\n".join(tenants.now_lines(p, alias, today))))
        body += "<h2>%s: history</h2><pre>%s</pre>" % (_e(alias), _e("\n".join(tenants.history_lines(p, alias))))
    return page("Tenants", body)


ROUTES = {"/": home, "/kb": knowledge, "/price": prices, "/projects": project_list, "/reviews": reviews,
          "/tenants": tenant_page}


def _today() -> str:
    import datetime

    return datetime.date.today().isoformat()


def _money(value, currency: str) -> str:
    return "" if value is None else "%s %s" % (value, currency)


# --------------------------------------------------------------------------- the server


class Handler(BaseHTTPRequestHandler):
    server_version = "awb-portal"
    sys_version = ""
    paths: config.Paths | None = None

    def _send(self, status: int, text: str, content_type: str = "text/html; charset=utf-8", body: bool = True):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def _answer(self, body: bool) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            self._send(200, "ok\n", "text/plain; charset=utf-8", body)
            return
        route = ROUTES.get(url.path)
        if route is None:
            self._send(404, page("Not found", "<p>No such page.</p>"), body=body)
            return
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        try:
            text = route(self.paths or config.paths(), query)
        except Exception as err:     # never a traceback or a value on a page
            self._send(500, page("Error", "<p>The page could not be built (%s).</p>" % _e(type(err).__name__)),
                       body=body)
            return
        self._send(200, text, body=body)

    def do_GET(self):
        self._answer(True)

    def do_HEAD(self):
        self._answer(False)

    def _refuse(self):
        self._send(405, page("Not allowed", "<p>This page is read only.</p>"))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    def log_message(self, fmt, *args):
        # the method, the page and the status, never the query: a search may carry a word nobody should keep
        line = self.requestline.split(" ")
        path = urllib.parse.urlsplit(line[1]).path if len(line) > 1 else "-"
        status = args[1] if len(args) > 1 else "-"
        sys.stderr.write("%s %s %s\n" % (line[0] if line else "-", path, status))


def make_server(port: int = DEFAULT_PORT, paths: config.Paths | None = None) -> ThreadingHTTPServer:
    handler = type("PortalHandler", (Handler,), {"paths": paths})
    return ThreadingHTTPServer((HOST, port), handler)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb portal", description="A read-only web page over the Workbench commands.")
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("serve", help="serve the portal on 127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command != "serve":
        ap.print_usage(sys.stderr)
        return 2
    server = make_server(args.port)
    print("awb portal: serving on http://%s:%d (read only)" % (HOST, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
