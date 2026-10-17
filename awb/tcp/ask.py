"""`awb ask`: questions in plain words, answered by a model that may only use the Workbench's checked sources.

    awb ask serve [--port 8081]      the Ask page on 127.0.0.1, run as its own service user (awb-ask)
    awb ask "a question"             the same answer in the terminal

The model gets three read-only tools: the knowledge base (`kb_find`), the live price API (`price_find`) and what
runs on the test tenants (`tenant_now`). The system prompt tells it to answer from what the tools return, to name
the entry of every fact (KB-XXXX) and the time of every price, and to say so when no checked fact answers the
question. It never guesses a price.

Before a question leaves the machine it goes through the name check (`check.screen`, through the vault daemon on a
sealed host): a registered name, structured data or a secret stops it, the way the prompt hook stops a prompt. The
model loop is the one Ask engine of the project chat (`chat_service.engine`): the tool requests, the tool answers
and the answer pass the same check. The API key is read from KEY_FILE, which only the service user can read: the
work user's sessions never see it. A daily budget of questions and tokens (USAGE_FILE, counted from the usage every
answer reports, an unreadable file refuses) stops the page before the credit runs out. The page answers GET (the
form) and POST (a question, only from its own origin); it logs no question.

Runtime code is the standard library: the Messages API is called over HTTPS with urllib.
"""
from __future__ import annotations

import datetime
import html
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from awb import config

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MODEL = os.environ.get("AWB_ASK_MODEL") or "claude-opus-5-5"
"""On this model thinking is always on. The answers come from the tools, the model only reads and phrases them."""
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _effort() -> str:
    value = os.environ.get("AWB_ASK_EFFORT") or "medium"
    if value not in EFFORTS:
        raise AskError("AWB_ASK_EFFORT must be one of %s" % ", ".join(EFFORTS))
    return value


def _max_tokens() -> int:
    raw = os.environ.get("AWB_ASK_MAX_TOKENS") or "16000"
    if not raw.isdigit() or int(raw) < 1000:
        raise AskError("AWB_ASK_MAX_TOKENS must be a whole number of 1000 or more")
    return int(raw)


def check_settings() -> None:
    """Refuse a bad AWB_ASK_EFFORT or AWB_ASK_MAX_TOKENS when the service starts."""
    _effort()
    _max_tokens()


MAX_TOKENS = 16000          # thinking counts against it; AWB_ASK_MAX_TOKENS overrides, read at each question
MAX_ROUNDS = 4
MAX_QUESTION = 1000
KEY_FILE = Path(os.environ.get("AWB_ASK_KEY_FILE") or "/etc/awb/anthropic.key")
USAGE_FILE = Path(os.environ.get("AWB_ASK_USAGE") or "/var/lib/awb-ask/usage.json")
DAILY_QUESTIONS = 40
DAILY_TOKENS = 300_000
HOST = "127.0.0.1"
DEFAULT_PORT = 8081

SYSTEM = """You answer questions of cloud architects about T Cloud Public (TCP), the public cloud of Deutsche Telekom.

Answer only from what your tools return:
- kb_find searches checked facts. Every fact has a grade (live, contract, docs, said, assumed) and a check date. Name
  the entry of every fact you use, like (KB-XXXX). A fact graded "said" or "assumed" is a lead, not proof: say so.
- price_find reads the live public price API. Give every price with its unit and the time it was fetched. Never state
  a price that did not come from this tool.
- tenant_now shows what runs on a test tenant.
Call kb_find first for every question about features, flavors, limits or availability; call price_find only for
prices. Use the words of the tool results, never the words of the question: when a tool returns something other
than what was asked (cluster flavors for a question about node flavors, another region, another service), say so
plainly and do not relabel it.
When the tools do not answer the question, say that no checked fact covers it and what would need to be checked.
Never guess. Never name a customer.

Write plain text for a web page: no Markdown, no asterisks, no headings. Short sentences, the answer in the first
line, one fact per line where a list helps, no dashes between clauses, no filler."""

TOOLS = [
    {"name": "kb_find", "description": "Search the checked facts about TCP. Returns the best matching entries with "
     "their id, grade, class, check date and whether they expired.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "a few key words, "
                      "such as 'cce node flavors' or 'rds backup retention'"}}, "required": ["query"]}},
    {"name": "price_find", "description": "Live prices from the public TCP price API for one service in one region.",
     "input_schema": {"type": "object", "properties": {
         "service": {"type": "string", "description": "the service short name, such as ecs, evs, rds, cce, obs"},
         "region": {"type": "string", "enum": ["eu-de", "eu-nl", "eu-ch2"]},
         "flavor": {"type": "string", "description": "an exact flavor, optional"},
         "grep": {"type": "string", "description": "part of the flavor or product name, optional"}},
         "required": ["service"]}},
    {"name": "tenant_now", "description": "What runs now on a test tenant, from its last snapshot. Without an alias "
     "it lists the tenants.", "input_schema": {"type": "object", "properties": {"alias": {"type": "string"}}}},
]


class AskError(Exception):
    """A question was not answered. The message carries no key and no question."""


# --------------------------------------------------------------------------- tools


def run_tool(p: config.Paths, name: str, args: dict) -> str:
    if name == "kb_find":
        from awb import kb

        hits = [e for _, e in kb.find(str(args.get("query", ""))[:200], where=p) if not e.is_retired][:6]
        try:
            hits, _ = kb.screen(hits, p.register)     # a name registered after the add never reaches the model
        except kb.KBError:
            return json.dumps({"error": "the name check cannot run, so no fact is handed out"})
        today = datetime.date.today().isoformat()
        return json.dumps([{"id": e.id, "fact": e.statement, "grade": e.grade, "class": e.cls, "checked": e.checked,
                            "expired": bool(e.expires and e.expires < today)} for e in hits]) or "[]"
    if name == "price_find":
        from awb.tcp import price

        region = args.get("region") if args.get("region") in ("eu-de", "eu-nl", "eu-ch2") else "eu-de"
        got = price.fetch(str(args.get("service", ""))[:40].lower(), region)
        if got.listing.state != "list":
            return json.dumps({"error": "the price API gave no usable answer for this service"})
        recs = price.select(got.records, flavor=args.get("flavor") or None, grep=args.get("grep") or None)
        return json.dumps({"source": got.source_line(), "count": len(recs), "records": [
            {"flavor": r.flavor, "name": r.name, "os": r.os, "unit": r.unit, "currency": r.currency,
             "prices": {t: (str(v) if v is not None else None) for t, v in r.prices.items()}} for r in recs[:12]]})
    if name == "tenant_now":
        from awb.tcp import tenants

        today = datetime.date.today()
        alias = str(args.get("alias") or "")
        if alias and alias in [t.alias for t in tenants.load(p)]:
            return "\n".join(tenants.now_lines(p, alias, today))
        return "\n".join(tenants.list_lines(p, today))
    return json.dumps({"error": "no such tool"})


# --------------------------------------------------------------------------- the API


def read_key() -> str:
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        raise AskError("the API key is not readable here") from None
    if not key:
        raise AskError("the API key file is empty")
    return key


def post(body: dict, key: str, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": API_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise AskError("the API answered HTTP %d" % err.code) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise AskError("the API could not be reached (%s)" % type(err).__name__) from None


# --------------------------------------------------------------------------- budget

_LOCK = threading.Lock()


def _usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def budget_left(today: str | None = None) -> tuple[int, int]:
    today = today or datetime.date.today().isoformat()
    day = _usage().get(today, {})
    return DAILY_QUESTIONS - day.get("questions", 0), DAILY_TOKENS - day.get("tokens", 0)


def spend(tokens: int, questions: int = 0, today: str | None = None) -> None:
    today = today or datetime.date.today().isoformat()
    with _LOCK:
        data = _usage()
        day = data.setdefault(today, {"questions": 0, "tokens": 0})
        day["questions"] += questions
        day["tokens"] += tokens
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_name("." + USAGE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, USAGE_FILE)


# --------------------------------------------------------------------------- one question


def screen(question: str, p: config.Paths) -> None:
    """Refuse a question with a registered name, structured data, a secret or a home path before it leaves the
    machine (check.screen, every class refuses)."""
    from awb import check

    try:
        found = check.screen(question, check.checker(p.register)).found
    except Exception:
        raise AskError("the name check did not run, so the question was not sent") from None
    if "name" in found:
        raise AskError("the question carries a registered name: write the code instead")
    if set(found) & set(check.SECRET_CLASSES):
        raise AskError("the question carries a secret, a token, a key or a home path: leave it out")
    if found:
        raise AskError("the question carries structured data (such as an address or a number): leave it out")


def ask(question: str, p: config.Paths | None = None, key: str | None = None, sender=None) -> dict:
    """{"answer": text, "tools": [names used], "tokens": n}. Raises AskError. The model loop is the one Ask engine of
    the project chat (chat_service.engine) with the names of check.checker; the stops keep the words of this page."""
    from awb import check
    from awb.tcp.web import chat_service as chat

    p = p or config.paths()
    question = (question or "").strip()
    if not question:
        raise AskError("an empty question")
    if len(question) > MAX_QUESTION:
        raise AskError("the question is longer than %d characters" % MAX_QUESTION)
    check_settings()
    screen(question, p)
    if sender is None:          # the real API needs the key; a stand-in (tests) does not
        key, sender = key or read_key(), post
    words = {chat.BUDGET_SPENT: "the daily budget of the Ask page is used up; it resets at midnight",
             chat.SHORT_BUDGET: "the daily budget of the Ask page is used up; it resets at midnight",
             chat.DECLINED: "the model declined this question",
             chat.CUT_ANSWER: "the answer did not fit in the token limit (thinking counts against it): "
                              "ask a narrower question or raise AWB_ASK_MAX_TOKENS",
             chat.TOOL_LIMIT: "no answer after %d tool rounds" % MAX_ROUNDS}
    try:
        names = check.checker(p.register)
    except Exception:
        raise AskError("the name check did not run, so the question was not sent") from None
    turn = chat.Turn()
    try:
        chat.reserve_question()
        answer = chat.engine(p, [{"role": "user", "content": question}], sender, key, names=names, turn=turn,
                             usage_required=False)
    except chat.ChatError as err:
        if str(err) != chat.EMPTY_ANSWER:
            raise AskError(words.get(str(err), str(err))) from None
        answer = "(no answer)"
    return {"answer": answer, "tools": turn.used, "tokens": turn.tokens}


# --------------------------------------------------------------------------- the page

# The page template of the old portal (awb/tcp/portal.py, removed in F1 step 10) lives on here: the Ask page is
# the one server-rendered page left. Every value that reaches it is HTML-escaped.
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
input,select,textarea{font:inherit;padding:6px 8px;border:1px solid #c5ccd5;border-radius:4px}
button{font:inherit;padding:6px 14px;border:0;border-radius:4px;background:#E20074;color:#fff}
.muted{color:#5B6878;font-size:90%}.expired{color:#b00020}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> str:
    """A whole page: the header with the links to the console and to Ask, the title and the body."""
    nav = '<header><b>Architect Workbench</b><a href="/">Console</a><a href="/ask">Ask</a></header>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><style>%s</style></head>'
            '<body>%s<main><h1>%s</h1>%s</main></body></html>' % (_e(title), _CSS, nav, _e(title), body))


def _form(question: str = "") -> str:
    return ('<form method="post" action="/ask"><textarea name="q" rows="4" cols="90" maxlength="%d" '
            'placeholder="for example: which CCE node flavors are there in eu-de, and what does s3.large.2 cost per '
            'hour?">%s</textarea><br><button>Ask</button></form>' % (MAX_QUESTION, _e(question)))


def ask_page(answer: dict | None = None, question: str = "", error: str = "") -> str:
    left_q, _ = budget_left()
    body = ('<p>Answers come only from the checked facts, the live price API and the tenant snapshots, with the '
            'entry of every fact. No customer data: a registered name is stopped before anything is sent.</p>'
            + _form(question))
    if error:
        body += '<p class="expired">%s</p>' % _e(error)
    if answer:
        body += ('<h2>Answer</h2><div style="white-space:pre-wrap">%s</div><p class="muted">sources used: %s</p>'
                 % (_e(answer["answer"]), _e(", ".join(sorted(set(answer["tools"]))) or "none")))
    body += '<p class="muted">%d questions left today</p>' % max(left_q, 0)
    return page("Ask", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "awb-ask"
    sys_version = ""
    paths: config.Paths | None = None
    sender = None

    def _send(self, status: int, text: str):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path != "/ask":
            self._send(404, page("Not found", "<p>No such page.</p>"))
            return
        self._send(200, ask_page())

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/ask":
            self._send(404, page("Not found", "<p>No such page.</p>"))
            return
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            self._send(403, page("Not allowed", "<p>A question comes from this page only.</p>"))
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8 * MAX_QUESTION)
        except ValueError:
            length = 0
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        question = (form.get("q") or [""])[0]
        try:
            answer = ask(question, self.paths, sender=self.sender)
            self._send(200, ask_page(answer, question))
        except AskError as err:
            self._send(200, ask_page(None, question, str(err)))
        except Exception as err:
            self._send(500, ask_page(None, question, "the answer could not be built (%s)" % type(err).__name__))

    def log_message(self, fmt, *args):
        line = self.requestline.split(" ")
        path = urllib.parse.urlsplit(line[1]).path if len(line) > 1 else "-"
        sys.stderr.write("%s %s %s\n" % (line[0] if line else "-", path, args[1] if len(args) > 1 else "-"))


def make_server(port: int = DEFAULT_PORT, paths: config.Paths | None = None, sender=None) -> ThreadingHTTPServer:
    check_settings()
    handler = type("AskHandler", (Handler,), {"paths": paths, "sender": staticmethod(sender) if sender else None})
    return ThreadingHTTPServer((HOST, port), handler)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        try:
            check_settings()
        except AskError as err:
            print("awb ask: %s" % err, file=sys.stderr)
            return 2
        port = DEFAULT_PORT
        if len(argv) == 3 and argv[1] == "--port" and argv[2].isdigit():
            port = int(argv[2])
        server = make_server(port)
        print("awb ask: serving on http://%s:%d/ask" % (HOST, port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    usage = "usage: awb ask serve [--port N] | awb ask QUESTION"
    if not argv or argv[0] in ("-h", "--help"):
        if argv:
            print(usage + "\n\n" + __doc__.strip().splitlines()[0])
            return 0
        print(usage, file=sys.stderr)
        return 2
    try:
        res = ask(" ".join(argv))
    except AskError as err:
        print("awb ask: %s" % err, file=sys.stderr)
        return 1
    print(res["answer"])
    print("\n(sources: %s; %d tokens)" % (", ".join(sorted(set(res["tools"]))) or "none", res["tokens"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
