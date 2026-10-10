#!/usr/bin/env python3
"""TCP Facts MCP server: the dataset TCP Facts as four read-only tools for any MCP client.

The file lies in the dataset folder (next to facts.jsonl, services.md and prices/) and runs locally:

    python3 tcp_facts_mcp.py [--data DIR] [--live] [--price-api URL]

--data      the dataset folder (default: the folder this file lies in)
--live      tcp_price_find also asks the public price API (no credentials); without it there is no network at all
--price-api another address of the price API (a mirror or a test)

Tools: tcp_facts_search (the checked facts), tcp_service_check (offered or not, by the service description),
tcp_price_find (the price snapshot, or the live price API with --live) and tcp_calculate (exact arithmetic with
hours). Every answer carries the dataset date, the best-before date and the rule for the assistant.

The server speaks JSON-RPC 2.0 over stdio as the Model Context Protocol says: one message per line. It reads no
file outside the dataset folder and writes no file anywhere. Standard library only, Python 3.10 or newer.

Claude Desktop, in claude_desktop_config.json:

    {"mcpServers": {"tcp-facts": {"command": "python3", "args": ["/path/to/tcp-facts/tcp_facts_mcp.py"]}}}

Claude Code:

    claude mcp add tcp-facts -- python3 /path/to/tcp-facts/tcp_facts_mcp.py

VS Code, in .vscode/mcp.json:

    {"servers": {"tcp-facts": {"type": "stdio", "command": "python3", "args": ["/path/to/tcp-facts/tcp_facts_mcp.py"]}}}
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True      # the server writes no file, not even a cache of its own code

import argparse
import ast
import csv
import datetime
import io
import json
import math
import re
import unicodedata
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, DecimalException, localcontext
from pathlib import Path

SERVER = "tcp-facts"
VERSION = "1.0"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
RULE = "Answer from these facts, cite the id, say when no fact covers it."
GRADES = ("live", "contract", "docs")
REGIONS = ("eu-de", "eu-nl")
MAX_HITS = 20
MAX_FILE = 64 * 1024 * 1024
PRICE_API = "https://calculator.otc-service.com/en/open-telekom-price-api/"
PRICE_TIMEOUT = 30.0

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
REFUSED = -32001                    # a file outside the dataset folder


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolError(Exception):
    """A tool ran and could not answer (an expression that does not compute): an answer with isError."""


# --------------------------------------------------------------------------- the dataset folder


class Dataset:
    def __init__(self, root: Path, live: bool = False, price_api: str = PRICE_API):
        self.root = Path(root).resolve()
        self.live = live
        self.price_api = price_api
        self._facts: list[dict] | None = None
        self._meta: dict | None = None
        self._head: dict = {}

    def path(self, rel: str) -> Path:
        """The file `rel` inside the dataset folder. Anything that resolves outside it is refused."""
        if not rel or Path(rel).is_absolute() or "\x00" in rel:
            raise RpcError(REFUSED, "refused: a file outside the dataset folder")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise RpcError(REFUSED, "refused: a file outside the dataset folder")
        return p

    def read(self, rel: str) -> str | None:
        p = self.path(rel)
        if not p.is_file():
            return None
        if p.stat().st_size > MAX_FILE:
            raise ToolError("%s is larger than the server reads" % rel)
        return p.read_text(encoding="utf-8")

    def facts(self) -> list[dict]:
        if self._facts is None:
            text = self.read("facts.jsonl") or ""
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            self._facts = [r for r in rows if isinstance(r, dict) and "id" in r and "statement" in r]
            self._head = rows[0] if rows and isinstance(rows[0], dict) and "dataset" in rows[0] else {}
        return self._facts

    def meta(self) -> dict:
        """The dates of MANIFEST.json, else of the first line of facts.jsonl."""
        if self._meta is None:
            text = self.read("MANIFEST.json")
            m = json.loads(text) if text else {}
            self.facts()
            self._meta = {"date": m.get("date") or self._head.get("date", ""),
                          "best_before": m.get("best_before") or self._head.get("best_before", "")}
        return self._meta

    def frame(self, payload: dict) -> dict:
        """The answer of a tool with the dates of the dataset and the rule in front."""
        m = self.meta()
        out = {"dataset": "TCP Facts", "date": m.get("date", ""), "best_before": m.get("best_before", ""),
               "rule": RULE}
        if m.get("best_before") and datetime.date.today().isoformat() > m["best_before"]:
            out["warning"] = ("today is after the best-before date %s: tell the user to download a fresh copy of the "
                              "dataset first" % m["best_before"])
        out.update(payload)
        return out


# --------------------------------------------------------------------------- the search (as the knowledge base ranks)

_WORD_RE = re.compile(r"[a-z0-9]+(?:[.\-_/][a-z0-9]+)*")
_STOP = frozenset("""
    the a an of to in on for is are be been was were and or with by at as it its this that these those from per
    can via into than then there their has have had which when where also only all any each
""".split())
_QUERY_STOP = frozenset("how what why who whom whose i me my we our us you your please do tcp".split())
_PLATFORM_RE = re.compile(r"(?i)\bt[\s-]*cloud[\s-]*public\b|\bopen\s+telekom\s+cloud\b")
IDF_POWER = 1.5


def _stem(w: str) -> str | None:
    if not w.isalpha() or len(w) < 5:
        return None
    for suffix in ("ing", "ed"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            s = w[:-len(suffix)]
            if len(s) > 3 and s[-1] == s[-2] and s[-1] not in "aeiou":
                s = s[:-1]
            return s
    if w.endswith("e"):
        return w[:-1]
    return None


def _forms(text: str):
    for tok in _WORD_RE.findall(unicodedata.normalize("NFKC", text).lower()):
        parts = [tok]
        if re.search(r"[.\-_/]", tok):
            parts += re.split(r"[.\-_/]", tok)
        for w in parts:
            if len(w) > 3 and w.isalpha() and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            if len(w) < 2 or w in _STOP:
                continue
            forms = {w}
            root = _stem(w)
            if root and len(root) >= 3 and root not in _STOP:
                forms.add(root)
            yield forms


def words(text: str) -> set[str]:
    out: set[str] = set()
    for forms in _forms(text):
        out |= forms
    return out


def concepts(text: str) -> list[frozenset[str]]:
    """The words of `text` as units: a word with its stem, units that share a form merged."""
    units: list[set[str]] = []
    for forms in _forms(text):
        for u in [u for u in units if u & forms]:
            forms |= u
            units.remove(u)
        units.append(forms)
    return [frozenset(u) for u in units]


def rank(query: str, facts: list[dict]) -> list[tuple[float, dict]]:
    """Facts ranked by the share of the query they carry, each query word weighted by how rare it is, then by the
    overlap of both word sets, then the newer check. Facts without a shared word are left out."""
    text = _PLATFORM_RE.sub(" ", query)
    units = [u for u in concepts(text) if not u <= _QUERY_STOP] or concepts(text)
    if not units:
        return []
    q = set().union(*units)
    sets = [(f, words(f["statement"]) | words(" ".join(f.get("tags") or []))) for f in facts]
    n = len(sets) or 1
    weight = [(math.log(n / (1 + sum(1 for _, fw in sets if u & fw))) + 1.0) ** IDF_POWER for u in units]
    total = sum(weight) or 1.0
    ranked = []
    for f, fw in sets:
        got = sum(w for u, w in zip(units, weight) if u & fw)
        if got:
            share = len(q & fw) / len(q | fw)
            ranked.append((got / total, share, _ordinal(f.get("checked")), str(f["id"]), f))
    ranked.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
    return [(min(t[0], 1.0), t[4]) for t in ranked]


def _ordinal(day) -> int:
    try:
        return datetime.date.fromisoformat(str(day)).toordinal()
    except ValueError:
        return 0


def facts_search(ds: Dataset, query: str, tag: str | None = None, grade: str | None = None) -> dict:
    facts = ds.facts()
    if tag:
        facts = [f for f in facts if tag.lower() in [t.lower() for t in f.get("tags") or []]]
    if grade:
        facts = [f for f in facts if f.get("grade") == grade]
    today = datetime.date.today().isoformat()
    hits = [{"id": f["id"], "statement": f["statement"], "grade": f.get("grade", ""), "checked": f.get("checked", ""),
             "source": f.get("source", ""), "expires": f.get("expires", ""),
             "expired": bool(f.get("expires") and f["expires"] < today), "score": round(s, 3)}
            for s, f in rank(query, facts)[:MAX_HITS]]
    out: dict = {"query": query, "count": len(hits), "facts": hits}
    if tag:
        out["tag"] = tag
    if grade:
        out["grade"] = grade
    if not hits:
        out["note"] = "no checked fact covers this; say so and name what would need to be checked"
    return out


# --------------------------------------------------------------------------- offered or not

_FILLER = {"and", "for", "of", "the", "a", "an"}
_LINE_RE = re.compile(r"^- (\S+) (.+?)(?: \(([^()]*)\))?(?:, (optional|preview))?(?:; (.+))?$")
_GONE_RE = re.compile(r"^- (.+?)(?: \(([^()]*)\))?$")


def key(text: str) -> str:
    """A name for comparing: lower case, no filler words, endings -s, -er and -ing off, the words joined."""
    out = []
    for w in re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", text).lower()).split():
        if w in _FILLER:
            continue
        changed = True
        while changed and len(w) > 4:
            changed = False
            for suffix in ("ing", "er", "s"):
                if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                    w, changed = w[:-len(suffix)], True
                    break
        out.append(w)
    return "".join(out)


def _service(line: str, name: str, group: str | None) -> dict:
    """One line of services.md with every form of its name. A name that ends in brackets of its own ("... (Classic
    Mode)") reads like a name with other names, so the whole of it is a form as well."""
    aliases = [a.strip() for a in (group or "").split(",") if a.strip()]
    aliases += [a[7:] for a in aliases if a.lower().startswith("former ")]     # "(former Cloud Topology Designer)"
    forms = [name, *aliases] + (["%s (%s)" % (name, group)] if group else [])
    return {"line": line[2:], "forms": forms}


def services(ds: Dataset) -> tuple[str, list[dict], list[dict]]:
    """(revision, orderable services, services named as not offered) from services.md."""
    text = ds.read("services.md")
    if text is None:
        raise ToolError("services.md is not in the dataset folder")
    m = re.search(r"service description, revision (\S+?)\.?\s", text)
    revision = m.group(1) if m else "unknown"
    offered, gone, section = [], [], "offered"
    for line in text.splitlines():
        if line.startswith("## "):
            section = "gone" if "not offered" in line.lower() else "offered"
            continue
        if not line.startswith("- "):
            continue
        if section == "gone":
            g = _GONE_RE.match(line)
            if g:
                gone.append(_service(line, g.group(1), g.group(2)))
            continue
        s = _LINE_RE.match(line)
        if s:
            offered.append(_service(line, s.group(2), s.group(3)))
    return revision, offered, gone


def service_check(ds: Dataset, name: str) -> dict:
    revision, offered, gone = services(ds)
    k = key(name)
    exact = [s for s in offered if k and k in {key(f) for f in s["forms"]}]
    named_gone = [s for s in gone if k and k in {key(f) for f in s["forms"]}]
    out: dict = {"name": name, "service_description": revision}
    if exact and not named_gone:
        out.update(offered=True, services=[s["line"] for s in exact],
                   note="listed in the service description of %s" % revision)
        return out
    if named_gone:
        out.update(offered=False, note="not offered: the service description of %s does not list it; the "
                   "documentation or the price API may still know it, which is no offer" % revision)
        return out
    close = [s for s in offered if len(k) >= 3 and any(k in key(f) or (len(key(f)) >= 3 and key(f) in k)
                                                       for f in s["forms"])]
    if close:
        out.update(offered="unclear", services=[s["line"] for s in close][:MAX_HITS],
                   note="the name matches no service exactly; these come close. Ask which one is meant, or check "
                        "again with the exact name")
        return out
    out.update(offered=False, note="not offered: the service description of %s does not list it, and a service "
               "that is not in that list is not offered" % revision)
    return out


# --------------------------------------------------------------------------- prices

_FETCHED_RE = re.compile(r"fetched (.+?)\. Prices")


def _match(row: dict, terms: list[str], fields: tuple) -> bool:
    hay = " ".join(str(row.get(f, "")) for f in fields).lower()
    return all(t in hay for t in terms)


def price_find(ds: Dataset, query: str, region: str = "eu-de", service: str | None = None) -> dict:
    terms = [t for t in query.lower().split() if t]
    out: dict = {"query": query, "region": region}
    if ds.live:
        if service:
            got = _live(ds, service, region, terms)
            if got is not None:
                out.update(got)
                return out
            out["live"] = "the price API gave no usable answer; the snapshot follows"
        else:
            out["live"] = "give the service short name (such as ecs, evs, rds) for a live price; the snapshot follows"
    text = ds.read("prices/%s.csv" % region)
    if text is None:
        out.update(count=0, rows=[], note="the dataset has no price list of %s" % region)
        return out
    lines = text.splitlines(keepends=True)
    head = lines[0] if lines and lines[0].startswith("#") else ""
    m = _FETCHED_RE.search(head)
    rows = list(csv.DictReader(io.StringIO("".join(lines[1:] if head else lines))))
    hits = [r for r in rows if _match(r, terms, ("id", "service", "product", "flavor", "os"))]
    hits.sort(key=lambda r: (r.get("flavor", "").lower() not in terms, r.get("id", "")))
    out.update(source="price snapshot of the public price API, fetched %s" % (m.group(1) if m else "unknown"),
               note="prices per unit in EUR, net; the live price API is the source of truth before any quote",
               count=len(hits), rows=hits[:MAX_HITS])
    if head and "left out" in head:
        out["left_out"] = head[head.index("Rows of services"):].strip() if "Rows of services" in head else ""
    if not hits:
        out["note"] = "no price row matches; try a flavor (s3.large.2) or a product word"
    return out


_LIVE_FIELDS = (("id", "id"), ("service", "productId"), ("product", "productName"), ("flavor", "opiFlavour"),
                ("os", "osUnit"), ("vcpu", "vCpu"), ("ram", "ram"), ("unit", "unit"), ("currency", "currency"),
                ("payg", "priceAmount"), ("reserved_12m", "R12"), ("reserved_24m", "R24"), ("reserved_36m", "R36"),
                ("reserved_upfront_12m", "RU12"), ("reserved_upfront_24m", "RU24"), ("reserved_upfront_36m", "RU36"))


def _live(ds: Dataset, service: str, region: str, terms: list[str]) -> dict | None:
    """The rows of the public price API, or None when it gives no usable answer. Only with --live."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = ds.price_api + ("&" if "?" in ds.price_api else "?") + urllib.parse.urlencode(
        {"sn": service, "rn": region, "limitMax": "10000"})
    fetched = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        with urllib.request.urlopen(url, timeout=PRICE_TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    resp = body.get("response") if isinstance(body, dict) else None
    if not isinstance(resp, dict) or resp.get("code") != "Success":
        return None
    result = resp.get("result")
    raw = [x for rows in result.values() if isinstance(rows, list) for x in rows] if isinstance(result, dict) else []
    rows = []
    for x in raw:
        if not isinstance(x, dict) or str(x.get("productIdParameter", "")).lower() != service \
                or x.get("region") != region:
            continue
        row = {c: str(x.get(k, "") if x.get(k) is not None else "").replace(" EUR", "") for c, k in _LIVE_FIELDS}
        if _match(row, terms, ("id", "service", "product", "flavor", "os")):
            rows.append(row)
    rows.sort(key=lambda r: (r["flavor"].lower() not in terms, r["id"]))
    source = "live public price API, fetched %s" % fetched
    if isinstance(resp.get("cachedAt"), str):
        source += ", served from the API's cache of %s" % resp["cachedAt"]
    return {"source": source, "service": service, "note": "prices per unit in EUR, net", "count": len(rows),
            "rows": rows[:MAX_HITS]}


# --------------------------------------------------------------------------- arithmetic

HOURS = {"h": 1, "hour": 1, "hours": 1, "d": 24, "day": 24, "days": 24, "week": 168, "weeks": 168,
         "month": 720, "months": 720}
LABELS = ("eur", "gb", "gib", "vcpu", "mbit")
FUNCTIONS = ("sum", "min", "max", "abs", "round", "ceil", "floor", "pct")
MAX_EXPRESSION = 2000
MAX_NODES = 400
MAX_EXPONENT = 64
MAX_MAGNITUDE = Decimal("1e30")
_UNIT_WORDS = "|".join(sorted([*HOURS, *LABELS], key=len, reverse=True))
_UNIT_RE = re.compile(r"(?i)(?<![A-Za-z_])(%s)(?![A-Za-z0-9_])" % _UNIT_WORDS)
_TIMES_RE = re.compile("(?:(?<=[\\d)])\\s*|(?<=\\w)\\s+)[x\u00d7]\\s*(?=[\\d(.])")    # 3 x 0.05, 2 vcpu x 3
_GLUE_RE = re.compile(r"(?<=[\d)])\s*(?=\(|(?:%s)(?![a-z0-9_]))" % _UNIT_WORDS)     # 720 h, 2(3 + 1); never 1e3
_LEAD_RE = re.compile(r"(?<![a-z0-9_])(%s)\s*(?=[\d(.])" % "|".join(LABELS))                  # EUR 5


class CalcError(Exception):
    pass


def _round(value: Decimal, places: int) -> Decimal:
    if not 0 <= places <= 12:
        raise CalcError("round takes 0 to 12 places")
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _whole(value: Decimal, what: str) -> int:
    if value != value.to_integral_value():
        raise CalcError("%s must be a whole number" % what)
    return int(value)


def _checked(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MAX_MAGNITUDE:
        raise CalcError("a result is out of range")
    return value


def _call(name: str, args: list[Decimal]) -> Decimal:
    if name == "sum":
        return sum(args, Decimal(0))
    if name in ("min", "max"):
        if not args:
            raise CalcError("%s needs at least one value" % name)
        return min(args) if name == "min" else max(args)
    if name == "pct":
        if len(args) != 2:
            raise CalcError("pct takes a part and a whole")
        if args[1] == 0:
            raise CalcError("a division by zero")
        return args[0] / args[1] * 100
    if name == "round":
        if len(args) not in (1, 2):
            raise CalcError("round takes a value and the places")
        return _round(args[0], _whole(args[1], "the places") if len(args) == 2 else 0)
    if len(args) != 1:
        raise CalcError("%s takes one value" % name)
    if name == "abs":
        return abs(args[0])
    return args[0].to_integral_value(rounding=ROUND_CEILING if name == "ceil" else ROUND_FLOOR)


def units_out(expression: str) -> tuple[str, list[str]]:
    """The expression with every unit made a factor in hours (h = 1, month = 720) and every label 1, and the units
    it used. "0.051 EUR/h * 3 x 1 month" becomes "0.051*eur/h*3*1*month"."""
    text = expression.replace("\u20ac", " EUR ").replace("\u00b7", "*")
    used: list[str] = []

    def unit(m: re.Match) -> str:
        u = m.group(1).lower()
        if u not in used:
            used.append(u)
        return u

    text = _UNIT_RE.sub(unit, text)
    text = _TIMES_RE.sub("*", text)
    text = _GLUE_RE.sub("*", text)
    text = _LEAD_RE.sub(r"\1*", text)
    return text.strip(), used


def evaluate(expression: str) -> tuple[Decimal, list[str]]:
    if not expression.strip():
        raise CalcError("an empty expression")
    if len(expression) > MAX_EXPRESSION:
        raise CalcError("the expression is longer than %d characters" % MAX_EXPRESSION)
    source, used = units_out(expression.strip())
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError):
        raise CalcError("the expression is not plain arithmetic") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError("the expression is too long")
    names = {u: Decimal(HOURS.get(u, 1)) for u in used}

    def ev(node) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("only numbers are allowed")
            text = ast.get_source_segment(source, node)
            if text is None or not re.fullmatch(r"[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9_]+", text):
                raise CalcError("a number cannot be read")
            return _checked(Decimal(text.replace("_", "")))
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise CalcError("an unknown word; units are %s, labels %s" % (", ".join(HOURS), ", ".join(LABELS)))
            return names[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                raise CalcError("there is no % operator: write a rate as * 0.19 or use pct(part, whole)")
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return _checked(a + b)
            if isinstance(node.op, ast.Sub):
                return _checked(a - b)
            if isinstance(node.op, ast.Mult):
                return _checked(a * b)
            if isinstance(node.op, ast.Div):
                if b == 0:
                    raise CalcError("a division by zero")
                return _checked(a / b)
            if isinstance(node.op, ast.Pow):
                e = _whole(b, "an exponent")
                if abs(e) > MAX_EXPONENT:
                    raise CalcError("an exponent is larger than %d" % MAX_EXPONENT)
                if a == 0 and e < 0:
                    raise CalcError("a division by zero")
                return _checked(a ** e)
            raise CalcError("only + - * / and ** are allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise CalcError("only the functions %s are allowed" % ", ".join(FUNCTIONS))
            return _checked(_call(node.func.id, [ev(a) for a in node.args]))
        raise CalcError("the expression is not plain arithmetic")

    with localcontext() as ctx:
        ctx.prec = 40
        try:
            return ev(tree), used
        except (DecimalException, ArithmeticError, RecursionError):
            raise CalcError("the expression cannot be computed") from None


def plain(value: Decimal, places: int | None = None) -> str:
    if places is not None:
        out = format(_round(value, places), "f")
    else:
        out = format(value.normalize(), "f")
        if "." in out:
            out = out.rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def calculate(ds: Dataset, expression: str, places: int | None = None) -> dict:
    try:
        value, used = evaluate(expression)
        result = plain(value, places)
    except CalcError as err:
        raise ToolError("the expression does not compute: %s" % err) from None
    hours = [u for u in used if u in HOURS]
    out = {"expression": expression, "result": result,
           "note": "exact decimal arithmetic, rounding half up; quote this result, never one computed in your head"}
    if hours:
        out["hours"] = "time in hours: %s (a month is 720 h, as the TCP price calculator counts it)" % ", ".join(
            "%s = %d h" % (u, HOURS[u]) for u in hours)
    return out


# --------------------------------------------------------------------------- the tools

TOOLS = [
    {"name": "tcp_facts_search",
     "description": "Search the checked facts about T Cloud Public (TCP): services, flavors, limits, regions, APIs. "
                    "Returns up to 20 facts with id, statement, grade (live = tested on a tenant, contract = service "
                    "description, docs = vendor documentation), check date, source and expiry, best first. Answer "
                    "from these facts and cite the id.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 500, "description": "a few key words, such as 'cce node flavors' "
                   "or 'rds backup retention'"},
         "tag": {"type": "string", "maxLength": 60, "description": "only facts with this tag, such as ecs or iam"},
         "grade": {"type": "string", "enum": list(GRADES), "description": "only facts of this grade"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "tcp_service_check",
     "description": "Is a service offered on TCP? Answers from the list of the service description (with its "
                    "revision): offered, not offered, or unclear with the services that come close.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "maxLength": 200, "description": "the service name or its short name, such as "
                  "Cloud Container Engine or CCE"}},
         "required": ["name"], "additionalProperties": False}},
    {"name": "tcp_price_find",
     "description": "TCP prices from the price snapshot of the dataset (with its date), or from the live public "
                    "price API when the server runs with --live and a service is given. Every word of the query "
                    "must appear in the row (id, service, product, flavor or OS). Up to 20 rows, prices per unit in "
                    "EUR net: payg, reserved_* per month, reserved_upfront_*.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 200, "description": "a flavor or product words, such as "
                   "'s3.large.2 linux'"},
         "region": {"type": "string", "enum": list(REGIONS), "description": "the region (default eu-de)"},
         "service": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,40}$", "description": "the short name of "
                     "the price API, such as ecs, evs or rds; needed for a live price"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "tcp_calculate",
     "description": "Exact decimal arithmetic for prices and totals: + - * / ** and sum, min, max, abs, round(x, "
                    "places), ceil, floor, pct(part, whole). Units count in hours: h, d (24 h), week (168 h), month "
                    "(720 h, as the TCP price calculator counts it); EUR, GB, GiB, vCPU and Mbit are labels. "
                    "Example: 0.051 EUR/h * 3 * 1 month. Use it for every number you compute.",
     "inputSchema": {"type": "object", "properties": {
         "expression": {"type": "string", "maxLength": MAX_EXPRESSION, "description": "the arithmetic, such as "
                        "'0.051 EUR/h * 720 h * 3'"},
         "places": {"type": "integer", "minimum": 0, "maximum": 12, "description": "round the result to this many "
                    "places, half up"}},
         "required": ["expression"], "additionalProperties": False}},
]
_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict}


def validate(schema: dict, args) -> None:
    """The arguments against the tool's schema; a mismatch is a JSON-RPC error that names the argument."""
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "the arguments must be an object")
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            raise RpcError(INVALID_PARAMS, "missing argument: %s" % name)
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            raise RpcError(INVALID_PARAMS, "unknown argument: %s" % name)
        want = _TYPES[spec["type"]]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise RpcError(INVALID_PARAMS, "argument %s must be a %s" % (name, spec["type"]))
        if "enum" in spec and value not in spec["enum"]:
            raise RpcError(INVALID_PARAMS, "argument %s must be one of %s" % (name, ", ".join(spec["enum"])))
        if want is str:
            if len(value) > spec.get("maxLength", 10_000) or "\x00" in value:
                raise RpcError(INVALID_PARAMS, "argument %s is too long or not text" % name)
            if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                raise RpcError(INVALID_PARAMS, "argument %s does not read like %s" % (name, spec["pattern"]))
        if want is int and not spec.get("minimum", value) <= value <= spec.get("maximum", value):
            raise RpcError(INVALID_PARAMS, "argument %s is out of range" % name)


def call_tool(ds: Dataset, name, args) -> dict:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise RpcError(INVALID_PARAMS, "unknown tool: %s" % (name if isinstance(name, str) else "(none)"))
    args = {} if args is None else args
    validate(tool["inputSchema"], args)
    try:
        if name == "tcp_facts_search":
            payload = facts_search(ds, args["query"], args.get("tag"), args.get("grade"))
        elif name == "tcp_service_check":
            payload = service_check(ds, args["name"])
        elif name == "tcp_price_find":
            payload = price_find(ds, args["query"], args.get("region", "eu-de"), args.get("service"))
        else:
            payload = calculate(ds, args["expression"], args.get("places"))
    except ToolError as err:
        return {"content": [{"type": "text", "text": json.dumps(ds.frame({"error": str(err)}), ensure_ascii=False)}],
                "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(ds.frame(payload), ensure_ascii=False, indent=1)}],
            "isError": False}


# --------------------------------------------------------------------------- JSON-RPC over stdio


def handle(ds: Dataset, msg) -> dict | None:
    """The response to one message, or None for a notification or a response."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    if "method" not in msg:
        return None                                  # a response of the client: nothing to answer
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params")
    if "id" not in msg:
        return None                                  # a notification
    if not isinstance(method, str) or not (rid is None or isinstance(rid, (str, int))) or isinstance(rid, bool):
        return _error(rid if isinstance(rid, (str, int)) else None, INVALID_REQUEST, "a request needs a method and an id")
    if params is not None and not isinstance(params, dict):
        return _error(rid, INVALID_PARAMS, "params must be an object")
    params = params or {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            m = ds.meta()
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER, "version": VERSION},
                      "instructions": "TCP Facts of %s, best before %s. %s Use tcp_calculate for every number you "
                                      "compute." % (m.get("date", ""), m.get("best_before", ""), RULE)}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = call_tool(ds, params.get("name"), params.get("arguments"))
        else:
            raise RpcError(METHOD_NOT_FOUND, "method not found: %s" % method)
    except RpcError as err:
        return _error(rid, err.code, err.message)
    except Exception as err:                         # never a value of a file in the message
        return _error(rid, INTERNAL_ERROR, "the server failed (%s)" % type(err).__name__)
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(ds: Dataset, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            out = _error(None, PARSE_ERROR, "not JSON")
        else:
            if isinstance(msg, list):
                out = [r for r in (handle(ds, m) for m in msg) if r is not None] if msg else \
                    _error(None, INVALID_REQUEST, "an empty batch")
                out = out or None
            else:
                out = handle(ds, msg)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tcp_facts_mcp.py", description="The dataset TCP Facts as a read-only MCP "
                                 "server over stdio.")
    ap.add_argument("--data", type=Path, default=Path(__file__).resolve().parent,
                    help="the dataset folder (default: the folder of this file)")
    ap.add_argument("--live", action="store_true", help="tcp_price_find also asks the public price API")
    ap.add_argument("--price-api", default=PRICE_API, help="another address of the price API")
    args = ap.parse_args(argv)
    if not (args.data / "facts.jsonl").is_file():
        print("tcp_facts_mcp: no facts.jsonl in %s; give --data with the dataset folder" % args.data, file=sys.stderr)
        return 2
    try:
        serve(Dataset(args.data, live=args.live, price_api=args.price_api))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
