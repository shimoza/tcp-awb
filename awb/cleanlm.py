"""The second pass of `awb clean` by a local model, the owner's local corrections and the measure (task B-051).

    awb clean FILE... --llm [URL]
    awb clean teach keep WORD
    awb clean teach wipe WORD [--kind person|company|place]
    awb clean teach list
    awb clean measure [--llm [URL]] [--only MODULE ...]

The rules run first and unchanged. With `--llm` the cleaned text of a docs run then goes in pieces (about 2,000
characters, cut at a sentence) to a local model behind an OpenAI-compatible API (GPT4All serves one on
http://localhost:4891/v1/chat/completions; AWB_CLEAN_LLM_URL and AWB_CLEAN_LLM_MODEL name another URL and model,
AWB_CLEAN_LLM_TIMEOUT another deadline than 120 s per piece). A fixed prompt asks for a JSON list of spans
{text, kind: person|company|place} that are still real names. A span is taken only when it stands literally in its
piece and is no entry of the lists of public words, technical terms or allowed terms (rules/*.txt) and no keep word
of the owner; it becomes a token by the rules of wipe mode and goes into the map of the run. The model never
rewrites the text: it only points at spans.

The URL must be loopback (127.0.0.1, ::1 or localhost; localhost must resolve to loopback only): any other host
is refused with one line before anything is sent. The call never goes through a proxy (HTTPS_PROXY and the like are
ignored). A model that does not answer, times out or answers no valid JSON costs nothing but the piece: the run goes
on with the rules alone and the report counts the missed pieces. No model is trained or fine-tuned here: the model is
used as it ships; what improves is the rules, the word lists and the local corrections below.

The local corrections live in ~/awb-laptop/vault/teach.tsv (600) and every run on the laptop reads them: a keep word
is never a candidate (the keep list of wipe mode, compared with the whole value as the run wiped it), a wipe word is
matched like a registered form of no customer and becomes a token of its kind. Nothing of teach.tsv goes to the
bucket, a report or an update; `awb clean teach list` prints counts per kind.

`awb clean measure` runs the wipe pack (calibration/redteam/wipe/, invented cases only, through clean_compare.py of
the pack) with the rules alone and with `--llm` also with the model pass. It prints leaks and losses per case
module for both. Nothing here prints, logs or raises with a value.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from awb import laptop

DEFAULT_URL = "http://localhost:4891/v1/chat/completions"
URL_ENV = "AWB_CLEAN_LLM_URL"
MODEL_ENV = "AWB_CLEAN_LLM_MODEL"
TIMEOUT_ENV = "AWB_CLEAN_LLM_TIMEOUT"
DEFAULT_MODEL = "Llama 3 8B Instruct"
DEFAULT_TIMEOUT = 120.0
PIECE = 2000
MAX_ANSWER = 1024 * 1024
KINDS = ("person", "company", "place")
LOOPBACK_NAMES = ("localhost",)
REFUSED = "--llm takes a loopback URL only (127.0.0.1, ::1 or localhost): nothing was sent"
PUBLIC_LISTS = ("common-words.txt", "tech-terms.txt", "allowed-terms.txt", "vendor-names.txt", "standards.txt",
                "source-platforms.txt")
"""The lists whose entries a proposed span may not be: public words, technical terms and allowed terms."""

PROMPT = (
    "You check a text from which names were already replaced. Find the names of real persons, companies and places "
    "that are still in it. Tokens in square brackets such as [person 1], [company 2], [place 1], [name 1] or "
    "[secret] and codes such as CUST-Q7M4 are replaced already: never return them. Products, services, technical "
    "terms, teams, roles and job titles are no names. Copy each name exactly as it stands in the text. Answer with a "
    "JSON list and nothing else, one object per name: [{\"text\": \"...\", \"kind\": \"person\"}], the kind is person, "
    "company or place. When there is no name, answer [].")


class LLMError(Exception):
    """The model pass cannot start (a URL that is not loopback). The message carries no value."""


# --------------------------------------------------------------------------- the URL and the call


def check_url(url: str, resolve=socket.getaddrinfo) -> str:
    """`url` when it is http or https to a loopback host; else LLMError with the one line of REFUSED. localhost must
    resolve to loopback addresses only."""
    try:
        u = urllib.parse.urlsplit(url or "")
        host = (u.hostname or "").casefold()
        port = u.port
    except ValueError:
        raise LLMError(REFUSED) from None
    if u.scheme not in ("http", "https") or not host or u.username or u.password:
        raise LLMError(REFUSED)
    if host in LOOPBACK_NAMES:
        try:
            addrs = {a[4][0] for a in resolve(host, port or 80, proto=socket.IPPROTO_TCP)}
        except OSError:
            raise LLMError(REFUSED) from None
        if not addrs or not all(_loopback(a) for a in addrs):
            raise LLMError(REFUSED)
        return url
    if host not in ("127.0.0.1", "::1"):
        raise LLMError(REFUSED)
    return url


def _loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


class Model:
    """The local model of one call of `awb clean`: the URL (checked), the model name and the deadline per piece.
    `sent` and `missed` count the pieces over every file."""

    def __init__(self, url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.url = check_url(url or os.environ.get(URL_ENV) or DEFAULT_URL)
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        if timeout is None:
            try:
                timeout = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        self.timeout = timeout
        # no proxy for this call: an empty ProxyHandler, whatever HTTPS_PROXY and HTTP_PROXY say
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.sent = 0
        self.missed = 0

    def ask(self, piece: str) -> list[dict] | None:
        """The spans the model proposes for one piece; None when it did not answer, timed out or gave no valid
        JSON."""
        self.sent += 1
        body = json.dumps({"model": self.model, "temperature": 0, "max_tokens": 1024,
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": piece}]}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                data = resp.read(MAX_ANSWER + 1)
            if len(data) > MAX_ANSWER:
                raise ValueError("answer too large")
            content = json.loads(data.decode("utf-8"))["choices"][0]["message"]["content"]
            spans = parse_spans(content)
        except Exception:  # noqa: BLE001 - a model that fails costs the piece, never the run
            spans = None
        if spans is None:
            self.missed += 1
        return spans


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_spans(content) -> list[dict] | None:
    """The spans of an answer: a JSON list of {text, kind} (also inside a code fence or under "spans" or "names" of
    an object); None when the answer is no such JSON. Items of another shape or kind are left out."""
    if not isinstance(content, str):
        return None
    text = _FENCE_RE.sub("", content.strip())
    try:
        value = json.loads(text)
    except ValueError:
        a, b = text.find("["), text.rfind("]")
        if a < 0 or b <= a:
            return None
        try:
            value = json.loads(text[a:b + 1])
        except ValueError:
            return None
    if isinstance(value, dict):
        value = value.get("spans", value.get("names"))
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        t, kind = item.get("text"), str(item.get("kind") or "").casefold()
        if isinstance(t, str) and t.strip() and kind in KINDS:
            out.append({"text": t.strip(), "kind": kind})
    return out


# --------------------------------------------------------------------------- the pass over one text

_CUT_RE = re.compile(r"[.!?…](?=\s)|\n")


def pieces(text: str, size: int = PIECE) -> list[str]:
    """`text` in pieces of about `size` characters, cut after a sentence or a line where one ends in reach, else at
    a space; every piece a stretch of `text`. Pieces of white space only are left out."""
    out: list[str] = []
    start, n = 0, len(text)
    while start < n:
        if n - start <= size:
            end = n
        else:
            end = 0
            for m in _CUT_RE.finditer(text, start, start + size):
                end = m.end()
            if end <= start + size // 4:
                space = text.rfind(" ", start + size // 4, start + size)
                end = space + 1 if space > start else start + size
        piece = text[start:end]
        if piece.strip():
            out.append(piece)
        start = end
    return out


def public_terms() -> frozenset[str]:
    """The entries of the lists of public words, technical terms and allowed terms, of every section, case folded
    with white space runs as one space. A missing list gives no entry."""
    from awb import patterns

    terms: set[str] = set()
    for name in PUBLIC_LISTS:
        try:
            lines = (patterns.RULES_DIR / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        terms.update(_key(line) for line in lines if line.strip() and not line.lstrip().startswith("#"))
    return frozenset(terms)


_BRACKET_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")


def _protected(text: str) -> list[tuple[int, int]]:
    """Tokens, codes and bracket marks of the cleaned text: no span of the model reaches into them."""
    from awb.intake import wipe

    spans = [(s.start, s.end) for s in wipe._token_spans(text)]
    return spans + [(m.start(), m.end()) for m in _BRACKET_RE.finditer(text)]


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _occurrences(text: str, value: str, protected) -> list[tuple[int, int]]:
    rx = re.compile(r"(?<![\w@.-])%s(?![\w@-])" % re.escape(value))
    return [(m.start(), m.end()) for m in rx.finditer(text)
            if not any(a < m.end() and m.start() < b for a, b in protected)]


def model_pass(engine, body: str, where: str, model: Model, taken: dict[str, str], *, public=None,
               keep=()) -> tuple[str, Counter, Counter]:
    """The second pass over the cleaned `body` of one file: its pieces to the model, the spans it proposes checked,
    the ones taken (and those taken from earlier files of the run, in `taken`: value -> kind) replaced by the token
    of their kind in wipe mode and recorded in the engine (the map of the run). Returns the body, the counts per
    class of what was replaced and the counts of the pass (sent, missed, proposed/taken/public/absent per kind)."""
    from awb.intake.wipe import runstate
    from awb.matcher import Span

    public = public_terms() if public is None else public
    kept = {_key(k) for k in keep}
    stats: Counter = Counter()
    for piece in pieces(body):
        proposed = model.ask(piece)
        stats["sent"] += 1
        if proposed is None:
            stats["missed"] += 1
            continue
        seen: set[tuple[str, str]] = set()
        guard = _protected(piece)
        for item in proposed:
            value, kind = item["text"], item["kind"]
            if (value, kind) in seen:
                continue
            seen.add((value, kind))
            stats["proposed " + kind] += 1
            key = _key(value)
            if key in public or key in kept:
                stats["public " + kind] += 1
                continue
            if not re.search(r"[^\W\d_]{2}", value) or not _occurrences(piece, value, guard):
                stats["absent " + kind] += 1
                continue
            stats["taken " + kind] += 1
            taken.setdefault(value, kind)
    counts: Counter = Counter()
    for value in sorted(taken, key=lambda v: (-len(v), v)):
        kind = taken[value]
        hits = _occurrences(body, value, _protected(body))
        if not hits:
            continue
        token = runstate.token_for_key(engine.wipe, (kind, runstate._canon(value)))
        for a, b in hits:
            engine.record(where, Span(a, b, kind), value, token)
        parts, pos = [], 0
        for a, b in hits:
            parts += [body[pos:a], token]
            pos = b
        body = "".join(parts) + body[pos:]
        counts[kind] += len(hits)
    return body, counts, stats


def stats_line(stats: Counter) -> str:
    """The cell of the report for one file: counts only, never a value."""
    if not stats:
        return "-"

    def kinds(what: str) -> str:
        return ", ".join("%s %d" % (k, stats[what + " " + k]) for k in KINDS if stats[what + " " + k]) or "0"

    return "pieces sent %d, missed %d; proposed %s; taken %s; dropped as public words %s; not in the piece %s" % (
        stats["sent"], stats["missed"], kinds("proposed"), kinds("taken"), kinds("public"), kinds("absent"))


# --------------------------------------------------------------------------- the local corrections


def teach_path() -> Path:
    return laptop.vault() / "teach.tsv"


def _valid_word(word: str) -> str:
    word = " ".join((word or "").split())
    if not word or len(word) > 100 or not re.search(r"[^\W\d_]{2}", word):
        raise LLMError("a word to teach has two letters or more and at most 100 characters")
    return word


def teach_entries() -> list[tuple[str, str, str]]:
    """(keep|wipe, kind or -, word) of teach.tsv; an empty list without the file. Lines of another shape are left
    out."""
    try:
        text = teach_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != 3 or not cells[2].strip():
            continue
        if cells[0] == "keep" or (cells[0] == "wipe" and cells[1] in KINDS):
            out.append((cells[0], cells[1], cells[2]))
    return out


def teach(action: str, word: str, kind: str | None = None) -> None:
    """One correction into teach.tsv (600): `keep` a word the wipe replaced, `wipe` a name that got through (kind
    person by default). A newer entry of the same word replaces the older one."""
    if action not in ("keep", "wipe"):
        raise LLMError("teach takes keep, wipe or list")
    word = _valid_word(word)
    if action == "keep":
        kind = "-"
    elif (kind or "person") not in KINDS:
        raise LLMError("--kind is person, company or place")
    else:
        kind = kind or "person"
    entries = [e for e in teach_entries() if _key(e[2]) != _key(word)] + [(action, kind, word)]
    laptop.init()
    path = teach_path()
    tmp = path.with_name(".teach.tsv.tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join("%s\t%s\t%s\n" % e for e in entries))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def teach_counts() -> Counter:
    c: Counter = Counter()
    for action, kind, _ in teach_entries():
        c["keep" if action == "keep" else "wipe " + kind] += 1
    return c


def teach_lines() -> list[str]:
    c = teach_counts()
    parts = ["keep %d" % c["keep"]] + ["wipe %s %d" % (k, c["wipe " + k]) for k in KINDS]
    return ["teach: %s (%s, this laptop only)" % (", ".join(parts), "~/awb-laptop/vault/teach.tsv")]


def keep_words() -> tuple[str, ...]:
    return tuple(w for a, _, w in teach_entries() if a == "keep")


def wipe_words() -> list[tuple[str, str]]:
    return [(w, k) for a, k, w in teach_entries() if a == "wipe"]


def engine(entries, wipe=()):
    """The engine of a run: that of the intake; with taught wipe words also a matcher of them whose hits are spans
    of their kind (code L:kind:value, so the wipe mode gives them their token)."""
    from awb import register
    from awb.intake.engine import _Engine
    from awb.intake.wipe import runstate
    from awb.matcher import Matcher, Span

    if not wipe:
        return _Engine(entries)
    pairs = sorted({(v, "L:%s:%s" % (kind, runstate._canon(word))) for word, kind in wipe
                    for v in register._variants(word)}, key=lambda p: (-len(p[0]), p[0], p[1]))
    taught = Matcher(pairs)

    class _Taught(_Engine):
        def raw_spans(self, text):
            names, structured = super().raw_spans(text)
            known = names + structured
            extra = [Span(s.start, s.end, s.code.split(":", 2)[1], s.code) for s in taught.find(text)
                     if not any(s.start < k.end and k.start < s.end for k in known)]
            return names + extra, structured

    return _Taught(entries)


# --------------------------------------------------------------------------- awb clean measure


def pack_dir() -> Path:
    from awb import patterns

    return Path(patterns.RULES_DIR).parent / "calibration" / "redteam" / "wipe"


def _modules(pack: Path, only) -> list[tuple[str, Path]]:
    found = {p.stem[len("cases_"):]: p for p in sorted(pack.glob("cases_*.py"))}
    if only:
        missing = [m for m in only if m not in found]
        if missing:
            raise LLMError("no case module %s in the pack" % ", ".join(missing))
        return [(m, found[m]) for m in only]
    return sorted(found.items())


def measure(url: str | None = None, *, use_model: bool = False, only=None, pack: Path | None = None,
            say=print, model: Model | None = None) -> dict:
    """`awb clean measure`: every case of the wipe pack through `awb clean` (docs) in a throw-away laptop with the
    invented register, with the rules alone and, with `use_model`, with the model pass. Prints a line per case
    module with leaks and losses (cases) for both and returns the counts."""
    if use_model and model is None:
        model = Model(url)
    root = pack_dir().parents[2]
    pack = Path(pack) if pack is not None else pack_dir()
    if not (pack_dir() / "clean_compare.py").is_file():
        raise LLMError("the wipe pack is not in this install: measure needs the clone with calibration/")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from calibration.redteam.wipe import clean_compare as cc
        from calibration.redteam.wipe import harness as wh
    except ImportError:
        raise LLMError("the wipe pack is not in this install: measure needs the clone with calibration/") from None
    modules = _modules(pack, only)
    saved = os.environ.get(laptop.HOME_ENV)
    work = Path(tempfile.mkdtemp(prefix="awb-measure-"))
    say("measure: the wipe pack through awb clean (docs), invented cases only; leaks and losses count cases")
    say("%-14s %5s | %-11s | %-11s" % ("module", "cases", "rules", "with model" if model else "model"))
    say("%-14s %5s | %5s %5s | %5s %5s" % ("", "", "leak", "loss", "leak", "loss"))
    result: dict = {"modules": {}, "rules": Counter(), "model": Counter() if model else None}
    try:
        for name, path in modules:
            cases = wh.load_cases(path)
            row = {"cases": len(cases), "rules": Counter(), "model": Counter() if model else None}
            for case in cases:
                for col, llm in (("rules", None), ("model", model)):
                    if col == "model" and model is None:
                        continue
                    rec = cc.clean_once(case, work, "docs", llm=llm)
                    row[col]["leak"] += bool(rec["leaks"])
                    row[col]["loss"] += bool(rec["losses"])
                    row[col]["error"] += bool(rec["error"])
            result["modules"][name] = row
            result["rules"].update(row["rules"])
            if model:
                result["model"].update(row["model"])
            say(_measure_line(name, row))
        say(_measure_line("all", {"cases": sum(r["cases"] for r in result["modules"].values()),
                                  "rules": result["rules"], "model": result["model"]}))
        if model:
            say("model: %d pieces sent, %d missed" % (model.sent, model.missed))
        errors = result["rules"]["error"] + (result["model"]["error"] if model else 0)
        if errors:
            say("%d case runs ended in an error (a case that needs python-docx or reportlab on this laptop)" % errors)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if saved is None:
            os.environ.pop(laptop.HOME_ENV, None)
        else:
            os.environ[laptop.HOME_ENV] = saved
    return result


def _measure_line(name: str, row: dict) -> str:
    m = row["model"]
    model = "%5d %5d" % (m["leak"], m["loss"]) if m is not None else "%5s %5s" % ("-", "-")
    return "%-14s %5d | %5d %5d | %s" % (name, row["cases"], row["rules"]["leak"], row["rules"]["loss"], model)


# --------------------------------------------------------------------------- the commands


def teach_main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean teach", description="Local corrections of this laptop in "
                    "~/awb-laptop/vault/teach.tsv (600), read by every later run: keep WORD (a word the wipe "
                    "replaced that must stay), wipe WORD [--kind] (a name that got through), list (counts per kind). "
                    "Nothing of it is ever sent.")
    ap.add_argument("action", choices=("keep", "wipe", "list"))
    ap.add_argument("word", nargs="?", default=None, metavar="WORD", help="the word or the phrase as it stood")
    ap.add_argument("--kind", default=None, choices=KINDS, help="the kind of a wipe word (default person)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if args.action == "list":
            if args.word or args.kind:
                raise LLMError("teach list takes no word")
        else:
            if not args.word:
                raise LLMError("teach %s takes a WORD" % args.action)
            if args.action == "keep" and args.kind:
                raise LLMError("--kind goes with teach wipe")
            teach(args.action, args.word, args.kind)
    except LLMError as err:
        print("awb clean teach: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb clean teach: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    print("\n".join(teach_lines()))
    return 0


def measure_main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean measure", description="The wipe pack (invented cases only) through awb clean "
                    "with the rules alone and, with --llm, with the pass of the local model: leaks and losses per "
                    "case module for both.")
    ap.add_argument("--llm", nargs="?", const="", default=None, metavar="URL", help="also with the local model "
                    "(default %s, else AWB_CLEAN_LLM_URL); loopback only" % DEFAULT_URL)
    ap.add_argument("--only", nargs="+", default=None, metavar="MODULE", help="these case modules only "
                    "(transcripts, meetings ...)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        measure(args.llm or None, use_model=args.llm is not None, only=args.only)
    except LLMError as err:
        print("awb clean measure: %s" % err, file=sys.stderr)
        return 2
    return 0
