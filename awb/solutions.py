"""Solution packages: build, status, release and the release screen (presentations/solution-packages/DESIGN.md 4, 5.6).

A package is a folder of the solutions project, `packages/<slug>/`:

    PACKAGE.md      the manifest: `key: value` lines, then a line `---` and free notes
    *.md            the parts the manifest lists, in its order; risks.md never leaves
    SOURCES.tsv     every KB and PAT id the text rests on, with its check date and hash at build time
    RELEASES.tsv    one row per release: version, date, the hash of SOURCES.tsv
    out/vN/         the built files: <slug>.md, .html, .pdf, SOURCES.tsv and PRICES.tsv

Manifest keys: slug (the folder name), kind (offer | guide | kit), audience (one line), tags (rules/tags.txt),
parts (a comma list of the files, in order), services (a comma list of service names of the service description,
each checked at build), best-before (days, default 90) and, written by hand when it is over, state: retired.

A price never stands in a part. A part writes `{{price SERVICE FLAVOR [os=TEXT] [term=PAYG|R12|...]
[region=REGION]}}` and the build fills the line live from the price API with term, region and date, and keeps the
amount in PRICES.tsv, so a later status sees when it moved. Every service of the manifest goes through the check of
`awb service check` (the latest service description alone); the build refuses one that is not offered, an id that
is not in the knowledge base or is retired, a part that is missing and risks.md as a part.

Version rule (4.3): the first build is v1; after a release a build is vN.M+1 and `--major` makes it vN+1. A built
version that is not released yet is built again in place; a released one is never written again.

    awb solutions build SLUG [--major] [--project DIR]
    awb solutions status [SLUG] [--offline] [--project DIR]
    awb solutions screen SLUG [--version V] [--project DIR]
    awb solutions screen --selftest
    awb solutions release SLUG [--project DIR]

`status` prints one line per package: version, state and the counts of the staleness reasons of 4.5: changed
(a source's statement or check date moved), retired, expired, new (an entry or card of the package's tags or slug
checked after the release that SOURCES.tsv does not hold), price (a filled price moved; `--offline` leaves it
out), service (a named service is no longer offered) and age (past best-before). Counts only, never a text.

The release screen (5.6) runs on every text file of `out/V/` but the HTML and the PDF (both rendered from the same
Markdown, so their words are the Markdown's) and refuses
with file, line and class, never a value and never a project:

1. export: `projectfacts.export_hits`, the name check, the gate's detectors and the short-text candidate rule (the
   KB and PAT ids are masked for it: they are drawn at random and read as unknown words);
2. code: a register, customer or project code, a lab run id or a K number;
3. fingerprint: the hashed strings of every project (`<shared>/patterns/fingerprints/`, DESIGN 5.3), matched
   against the words and numbers of each line; the dates of the build day are the build's own;
4. support: a card that states something about customers (signal, question, objection, risk) with support below
   2, and a source that is retired.

This is the one screen that runs on our own output, by the owner's decision P2 of 2026-11-19: a package mixes
what several projects taught and goes to third parties. `release` runs it, writes the RELEASES.tsv row, commits
the package folder and tags the commit `<slug>/<version>`. `--selftest` plants a fingerprint, a code and a weak
card in a temporary package and proves the screen finds each of them and passes the clean text.

Exit codes: 0 ok, 1 refused or findings, 2 usage or error.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from awb import codes, config, kb, kbpattern

PACKAGES = "packages"
MANIFEST = "PACKAGE.md"
SOURCES = "SOURCES.tsv"
RELEASES = "RELEASES.tsv"
PRICES = "PRICES.tsv"
OUT = "out"
KINDS = ("offer", "guide", "kit")
KEYS = ("slug", "kind", "audience", "tags", "parts", "services", "best-before", "region", "state")
REQUIRED = ("slug", "kind", "audience", "tags", "parts")
BEST_BEFORE = 90
NEVER_LEAVES = ("risks.md",)
SOURCE_COLUMNS = ("id", "type", "kind", "checked", "expires", "support", "hash")
PRICE_COLUMNS = ("service", "flavor", "os", "term", "region", "amount", "currency", "unit")
REASONS = ("changed", "retired", "expired", "new", "price", "service", "age")
_PRICE_RE = re.compile(r"\{\{price ([^{}]*)\}\}")
_ID_RE = re.compile(r"\b(?:%s|%s)\b" % (codes.KB, kbpattern.ID))
_VERSION_RE = re.compile(r"v([1-9][0-9]{0,3})(?:\.([1-9][0-9]{0,3}))?")
_LAB_RUN_RE = re.compile(r"run-[0-9]{8}-[0-9]{6}-[a-z0-9]{4}")
_SCREENED = (".md", ".txt", ".tsv", ".csv", ".sh", ".ps1", ".py", ".json", ".yaml", ".yml", ".tf")


class SolutionsError(kb.KBError):
    """A package cannot be built, read or released. Messages carry slugs, ids, files, lines and classes."""


@dataclass
class Manifest:
    slug: str
    kind: str
    audience: str
    tags: tuple[str, ...]
    parts: tuple[str, ...]
    services: tuple[str, ...] = ()
    best_before: int = BEST_BEFORE
    region: str = "eu-de"
    state: str = ""


@dataclass
class Finding:
    file: str
    line: int
    cls: str

    def __str__(self) -> str:
        return "%s:%d %s" % (self.file, self.line, self.cls)


@dataclass
class Status:
    slug: str
    version: str
    state: str
    counts: dict[str, int] = field(default_factory=dict)
    built: str = ""

    def line(self) -> str:
        tail = ", ".join("%s %d" % (r, self.counts.get(r, 0)) for r in REASONS if r in self.counts)
        return "  ".join(x for x in (self.slug, self.version or "-", self.state, tail,
                                     "built %s" % self.built if self.built else "") if x)


# --------------------------------------------------------------------------- the package folder


def project_root(start: Path | None = None) -> Path:
    return kbpattern.project_root(start)[1]


def package_dir(proj: Path, slug: str) -> Path:
    if not kbpattern.SLUG_RE.fullmatch(slug or ""):
        raise SolutionsError("a package slug is lower case letters, digits and dashes (2 to 48)")
    d = proj / PACKAGES / slug
    if not (d / MANIFEST).is_file():
        raise SolutionsError("package %s has no %s" % (slug, MANIFEST))
    return d


def slugs(proj: Path) -> list[str]:
    d = proj / PACKAGES
    if not d.is_dir():
        return []
    return sorted(x.name for x in d.iterdir() if kbpattern.SLUG_RE.fullmatch(x.name) and (x / MANIFEST).is_file())


def _list(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in value.split(",") if x.strip())


def read_manifest(d: Path) -> Manifest:
    head: dict[str, str] = {}
    for i, line in enumerate(kb._read_text(d / MANIFEST).split("\n"), start=1):
        if line.strip() == kb.SEPARATOR:
            break
        if not line.strip() or line.startswith("#"):
            continue
        k, sep, v = line.partition(":")
        k = k.strip().lower()
        if not sep or k not in KEYS:
            raise SolutionsError("%s line %d is not a known key (%s)" % (MANIFEST, i, ", ".join(KEYS)))
        head[k] = v.strip()
    missing = [k for k in REQUIRED if not head.get(k)]
    if missing:
        raise SolutionsError("%s lacks %s" % (MANIFEST, ", ".join(missing)))
    if head["slug"] != d.name:
        raise SolutionsError("the slug of %s is not its folder name" % MANIFEST)
    if head["kind"] not in KINDS:
        raise SolutionsError("kind is one of %s" % ", ".join(KINDS))
    tags, bad = kb._clean_tags(_list(head["tags"]))
    if bad:
        raise SolutionsError("; ".join(bad))
    try:
        best = int(head.get("best-before") or BEST_BEFORE)
    except ValueError:
        raise SolutionsError("best-before is a number of days") from None
    return Manifest(slug=head["slug"], kind=head["kind"], audience=head["audience"], tags=tuple(tags),
                    parts=_list(head["parts"]), services=_list(head.get("services", "")), best_before=best,
                    region=head.get("region") or "eu-de", state=head.get("state", ""))


def releases(d: Path) -> list[tuple[str, str, str]]:
    """(version, date, sources hash) of every release, oldest first."""
    try:
        text = kb._read_text(d / RELEASES)
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) == 3 and _VERSION_RE.fullmatch(cells[0]):
            out.append((cells[0], cells[1], cells[2]))
    return out


def _version_key(v: str) -> tuple[int, int]:
    m = _VERSION_RE.fullmatch(v)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def next_version(released: Sequence[str], major: bool = False) -> str:
    if not released:
        return "v1"
    n, m = max(_version_key(v) for v in released)
    return "v%d" % (n + 1) if major else "v%d.%d" % (n, m + 1)


def built_versions(d: Path) -> list[str]:
    o = d / OUT
    if not o.is_dir():
        return []
    return sorted((x.name for x in o.iterdir() if _VERSION_RE.fullmatch(x.name) and x.is_dir()), key=_version_key)


# --------------------------------------------------------------------------- sources


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def source_rows(ids: Sequence[str], where=None) -> tuple[list[dict], list[str]]:
    """The rows of SOURCES.tsv for `ids` and the refusals (an id not in the knowledge base or retired)."""
    entries = {e.id: e for e in kb.load(where)}
    cards = {c.id: c for c in kbpattern.load(where)}
    rows, problems = [], []
    for i in sorted(set(ids)):
        if i in entries:
            e = entries[i]
            if e.is_retired:
                problems.append("%s is retired" % i)
            rows.append({"id": i, "type": "fact", "kind": e.grade, "checked": e.checked, "expires": e.expires,
                         "support": "-", "hash": _hash(e.statement)})
        elif i in cards:
            c = cards[i]
            if c.is_retired:
                problems.append("%s is retired" % i)
            rows.append({"id": i, "type": "card", "kind": c.kind, "checked": c.checked, "expires": "-",
                         "support": str(c.support), "hash": _hash(c.statement)})
        else:
            problems.append("%s is not in the knowledge base" % i)
    return rows, problems


def render_tsv(columns: Sequence[str], rows: Sequence[dict]) -> str:
    return "\t".join(columns) + "\n" + "".join("\t".join(str(r[c]) for c in columns) + "\n" for r in rows)


def read_tsv(path: Path) -> list[dict]:
    try:
        lines = kb._read_text(path).splitlines()
    except FileNotFoundError:
        return []
    if not lines:
        return []
    head = lines[0].split("\t")
    return [dict(zip(head, line.split("\t"))) for line in lines[1:] if line.strip()]


# --------------------------------------------------------------------------- live prices and services


def _price_fetcher():
    from awb.tcp import price

    return price.fetch


@dataclass
class PriceAsk:
    service: str
    flavor: str
    os: str = ""
    term: str = "PAYG"
    region: str = "eu-de"


def parse_price(spec: str, region: str) -> PriceAsk:
    words = spec.split()
    plain = [w for w in words if "=" not in w]
    opts = dict(w.split("=", 1) for w in words if "=" in w)
    if len(plain) != 2 or set(opts) - {"os", "term", "region"}:
        raise SolutionsError("a price line reads {{price SERVICE FLAVOR [os=TEXT] [term=TERM] [region=REGION]}}")
    return PriceAsk(plain[0].lower(), plain[1], opts.get("os", ""), opts.get("term", "PAYG"),
                    opts.get("region", region))


def live_price(ask: PriceAsk, fetcher: Callable, cache: dict) -> dict:
    """One price row for PRICES.tsv, from the price API, or SolutionsError when it does not name one record."""
    key = (ask.service, ask.region)
    if key not in cache:
        cache[key] = fetcher(ask.service, ask.region)
    got = cache[key]
    recs = [r for r in got.records if r.flavor.lower() == ask.flavor.lower()
            and (not ask.os or ask.os.lower() in r.os.lower())]
    if len(recs) != 1:
        raise SolutionsError("the price of %s %s in %s: %d records match%s" % (
            ask.service, ask.flavor, ask.region, len(recs), ", give os=" if len(recs) > 1 else ""))
    amount = recs[0].price(ask.term)
    if amount is None:
        raise SolutionsError("the price of %s %s has no term %s" % (ask.service, ask.flavor, ask.term))
    return {"service": ask.service, "flavor": recs[0].flavor, "os": recs[0].os or "-", "term": ask.term,
            "region": ask.region, "amount": str(amount), "currency": recs[0].currency or "EUR",
            "unit": recs[0].unit or "-"}


def price_text(row: dict, today: date) -> str:
    unit = row["unit"] if row["unit"] != "-" else "unit"
    return "%s %s per %s (%s, %s, live price API of %s)" % (row["amount"], row["currency"], unit.lower(), row["term"],
                                                           row["region"], today.isoformat())


def service_problems(services: Sequence[str], today: date) -> list[str]:
    from awb import offered

    cat = offered.load()
    return ["service %d of the manifest is not offered by the latest service description" % i
            for i, s in enumerate(services, start=1) if not offered.check(s, cat, today.isoformat()).offered]


# --------------------------------------------------------------------------- build


def _text_of(d: Path, m: Manifest) -> str:
    texts = []
    for part in m.parts:
        if part in NEVER_LEAVES or "/" in part or not part.endswith(".md"):
            raise SolutionsError("part %s may not be built (a .md file of the package, never risks.md)" % part)
        try:
            texts.append(kb._read_text(d / part).rstrip("\n"))
        except FileNotFoundError:
            raise SolutionsError("part %s is missing" % part) from None
    return "\n\n".join(texts) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kb._write_atomic(path, text)


def build(slug: str, *, project: Path | None = None, major: bool = False, fetcher: Callable | None = None,
          today: date | None = None, where=None, pdf: bool = True) -> tuple[str, Path, dict]:
    """Build `out/<version>/` of a package. Returns (version, folder, counts of sources and prices)."""
    today = today or date.today()
    proj = project_root(project)
    d = package_dir(proj, slug)
    m = read_manifest(d)
    if m.state == "retired":
        raise SolutionsError("package %s is retired" % slug)
    released = [v for v, _, _ in releases(d)]
    version = next_version(released, major)
    text = _text_of(d, m)
    rows, problems = source_rows(_ID_RE.findall(text), where)
    problems += service_problems(m.services, today)
    if problems:
        raise SolutionsError("; ".join(problems))
    fetcher = fetcher or _price_fetcher()
    cache: dict = {}
    prices: list[dict] = []

    def fill(match: re.Match) -> str:
        row = live_price(parse_price(match.group(1), m.region), fetcher, cache)
        prices.append(row)
        return price_text(row, today)

    filled = _PRICE_RE.sub(fill, text)
    out = d / OUT / version
    _write(out / (slug + ".md"), filled)
    from awb import bucket

    _write(out / (slug + ".html"), bucket.md_to_html(filled))
    sources = render_tsv(SOURCE_COLUMNS, rows)
    _write(out / SOURCES, sources)
    _write(d / SOURCES, sources)
    _write(out / PRICES, render_tsv(PRICE_COLUMNS, prices))
    _write(out / "BUILT", "%s\n" % today.isoformat())
    if pdf:
        from awb import pdf as pdfmod

        try:
            pdfmod.convert(out / (slug + ".md"), start=proj)
        except pdfmod.PdfError as err:
            raise SolutionsError("the PDF was not written: %s" % err) from None
    return version, out, {"sources": len(rows), "prices": len(prices)}


# --------------------------------------------------------------------------- status


def status(slug: str, *, project: Path | None = None, offline: bool = False, fetcher: Callable | None = None,
           today: date | None = None, where=None) -> Status:
    today = today or date.today()
    proj = project_root(project)
    d = package_dir(proj, slug)
    m = read_manifest(d)
    rel = releases(d)
    built = built_versions(d)
    unreleased = [v for v in built if v not in {r[0] for r in rel}]
    if m.state == "retired":
        return Status(slug, rel[-1][0] if rel else "", "retired")
    if not rel:
        return Status(slug, "", "draft", built=unreleased[-1] if unreleased else "")
    version, released_on, _ = rel[-1]
    out = d / OUT / version
    counts = {r: 0 for r in REASONS}
    sources = read_tsv(out / SOURCES)
    held = {r["id"] for r in sources}
    entries = {e.id: e for e in kb.load(where)}
    cards = {c.id: c for c in kbpattern.load(where)}
    for r in sources:
        now = entries.get(r["id"]) or cards.get(r["id"])
        if now is None or now.is_retired:
            counts["retired"] += 1
            continue
        if _hash(now.statement) != r.get("hash") or now.checked != r.get("checked"):
            counts["changed"] += 1
        if isinstance(now, kb.Entry) and now.is_expired(today):
            counts["expired"] += 1
    tags = set(m.tags)
    for e in entries.values():
        if not e.is_retired and e.id not in held and tags & set(e.tags) and e.checked > released_on:
            counts["new"] += 1
    for c in cards.values():
        if not c.is_retired and c.id not in held and (slug in c.packages or tags & set(c.tags)) \
                and c.checked > released_on:
            counts["new"] += 1
    if offline:
        del counts["price"]
    else:
        fetcher = fetcher or _price_fetcher()
        cache: dict = {}
        for r in read_tsv(out / PRICES):
            try:
                now_row = live_price(PriceAsk(r["service"], r["flavor"], "" if r["os"] == "-" else r["os"],
                                              r["term"], r["region"]), fetcher, cache)
                moved = Decimal(now_row["amount"]) != Decimal(r["amount"])
            except (SolutionsError, ArithmeticError, KeyError):
                moved = True
            counts["price"] += int(moved)
    counts["service"] = len(service_problems(m.services, today))
    counts["age"] = int(today > date.fromisoformat(released_on) + timedelta(days=m.best_before))
    state = "stale" if any(counts.values()) else "released"
    return Status(slug, version, state, counts, built=unreleased[-1] if unreleased else "")


# --------------------------------------------------------------------------- the release screen (5.6)


def _code_classes(line: str) -> list[str]:
    out = []
    n_reg, n_proj = codes.count_codes(line)
    if n_reg or codes.CUST_RE.search(line):
        out.append("code (register or customer)")
    if n_proj:
        out.append("code (project)")
    if _LAB_RUN_RE.search(line):
        out.append("code (lab run)")
    if codes.CALC_RE.search(line):
        out.append("code (K number)")
    return out


def screen_texts(files: Sequence[tuple[str, str]], *, sources: Sequence[dict], prints: dict[str, str],
                 key: bytes | None, register_path: Path | None, own_dates: Sequence[str] = ()) -> list[Finding]:
    """Findings of the release screen over (name, text) pairs. `prints` maps a salted hash to its class; `key` is
    the salt. With `register_path` None the export check (class 1) is left out, which only the selftest does."""
    found: list[Finding] = []
    own = {"@" + d for d in own_dates}
    lines: list[tuple[str, int, str]] = []
    for name, text in files:
        for n, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                lines.append((name, n, line))
    if register_path is not None and lines:
        from awb import projectfacts

        try:
            # the ids of the knowledge base are drawn at random and read as unknown words to the candidate rule:
            # they are the build's own, so they are masked for this check only
            masked = [_ID_RE.sub(lambda m: "fact" if m.group(0).startswith("KB") else "card", l) for _, _, l in lines]
            hits = projectfacts.export_hits(masked, register_path)
        except Exception:
            raise SolutionsError("the export check cannot run; nothing is released") from None
        found += [Finding(lines[i][0], lines[i][1], "export (name, secret or candidate)") for i in sorted(hits)]
    for name, n, line in lines:
        found += [Finding(name, n, cls) for cls in _code_classes(line)]
        if prints and key is not None:
            classes = set()
            for k in kbpattern.candidates(line):
                if k in own:
                    continue
                cls = prints.get(kbpattern.hashed(k, key))
                if cls:
                    classes.add(cls)
            found += [Finding(name, n, "fingerprint (%s)" % cls) for cls in sorted(classes)]
    for r in sources:
        if r.get("type") == "card" and r.get("kind") in kbpattern.CUSTOMER_KINDS:
            try:
                weak = int(r.get("support", "0")) < 2
            except ValueError:
                weak = True
            if weak:
                found.append(Finding(SOURCES, 0, "support (%s states something about customers, support below 2)"
                                     % r["id"]))
    found.sort(key=lambda f: (f.file, f.line, f.cls))
    return found


def screen(slug: str, *, project: Path | None = None, version: str | None = None, where=None,
           p: config.Paths | None = None, register_path: Path | None = None) -> tuple[str, list[Finding]]:
    proj = project_root(project)
    d = package_dir(proj, slug)
    built = built_versions(d)
    version = version or (built[-1] if built else "")
    out = d / OUT / version
    if not version or not out.is_dir():
        raise SolutionsError("package %s has no built version%s; run awb solutions build first"
                             % (slug, " " + version if version else ""))
    files = []
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.suffix.lower() in _SCREENED:
            files.append((str(f.relative_to(d)), kb._read_text(f)))
    sources = read_tsv(out / SOURCES)
    # a card's support may have risen since the build: the screen reads it now
    cards = {c.id: c for c in kbpattern.load(where)}
    for r in sources:
        if r.get("id") in cards:
            r["support"] = str(cards[r["id"]].support)
            if cards[r["id"]].is_retired:
                r["kind"], r["type"] = "retired", "retired"
    found = [Finding(SOURCES, 0, "source %s is retired" % r["id"]) for r in sources if r.get("type") == "retired"]
    own = []
    try:
        own = [kb._read_text(out / "BUILT").strip()]
    except FileNotFoundError:
        pass
    key = kbpattern.salt(p)
    found += screen_texts(files, sources=sources, prints=kbpattern.load_fingerprints(p), key=key,
                          register_path=kb.register_of(register_path), own_dates=own)
    return version, found


def selftest() -> list[str]:
    """Problems of the screen itself: empty when every planted case is found and the clean text passes."""
    key = bytes(32)
    planted_token = "qzvx-cadhost-09"
    prints = {kbpattern.hashed(planted_token, key): "resource", kbpattern.hashed("#618546", key): "total",
              kbpattern.hashed("@2026-03-14", key): "date"}
    clean = "# CAD on TCP\n\nAn 8 GB professional card maps to g7v.2xlarge.8 (KB-AAAA).\nBuilt on 2026-11-19.\n"
    cases = {
        "fingerprint (resource)": "Join %s.example to the domain.\n" % planted_token,
        "fingerprint (total)": "The base tier is 6,185.46 a month.\n",
        "fingerprint (date)": "The PoC ran on 14.03.2026.\n",
        "code (project)": "See tcp-" + "q7m4 for the details.\n",
        "code (K number)": "The total is K-" + "12 of the calc.\n",
    }
    problems = []
    if screen_texts([("clean.md", clean)], sources=[], prints=prints, key=key, register_path=None,
                    own_dates=["2026-11-19"]):
        problems.append("the clean text does not pass")
    for cls, text in cases.items():
        got = screen_texts([("planted.md", clean + text)], sources=[], prints=prints, key=key, register_path=None,
                           own_dates=["2026-11-19"])
        if not any(f.cls == cls for f in got):
            problems.append("the planted %s is not found" % cls)
        if any(planted_token in str(f) or "6185" in str(f) for f in got):
            problems.append("a finding carries the value")
    weak = [{"id": "PAT-AAAA", "type": "card", "kind": "objection", "support": "1"}]
    if not any(f.cls.startswith("support") for f in screen_texts([], sources=weak, prints=prints, key=key,
                                                                    register_path=None)):
        problems.append("a customer card with support 1 is not found")
    strong = [{"id": "PAT-AAAA", "type": "card", "kind": "objection", "support": "2"}]
    if screen_texts([], sources=strong, prints=prints, key=key, register_path=None):
        problems.append("a customer card with support 2 is refused")
    with tempfile.TemporaryDirectory() as tmp:
        if kbpattern.load_fingerprints(config.Paths(shared=Path(tmp), vault=Path(tmp), projects_root=Path(tmp))):
            problems.append("an empty fingerprint folder is not empty")
    return problems


# --------------------------------------------------------------------------- release


def _git(proj: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(proj), *args], capture_output=True, text=True, timeout=600,
                         stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise SolutionsError("git %s failed in the solutions project (exit %d)" % (args[0], out.returncode))
    return out.stdout


def release(slug: str, *, project: Path | None = None, today: date | None = None, where=None,
            p: config.Paths | None = None, register_path: Path | None = None) -> tuple[str, list[Finding]]:
    """Screen the newest built version and, when it is clean, record it as released, commit the package folder and
    tag the commit. Returns (version, findings); nothing is recorded when there are findings."""
    today = today or date.today()
    proj = project_root(project)
    d = package_dir(proj, slug)
    rel = releases(d)
    built = [v for v in built_versions(d) if v not in {r[0] for r in rel}]
    if not built:
        raise SolutionsError("package %s has no built version that is not released yet" % slug)
    version = built[-1]
    _, found = screen(slug, project=proj, version=version, where=where, p=p, register_path=register_path)
    if found:
        return version, found
    digest = _hash(kb._read_text(d / OUT / version / SOURCES))
    rows = rel + [(version, today.isoformat(), digest)]
    _write(d / RELEASES, "".join("%s\t%s\t%s\n" % r for r in rows))
    rel_path = str(d.relative_to(proj))
    _git(proj, "add", "--", rel_path)
    _git(proj, "commit", "-q", "-m", "Release %s %s" % (slug, version), "--", rel_path)
    _git(proj, "tag", "%s/%s" % (slug, version))
    return version, []


# --------------------------------------------------------------------------- commands


def _print_findings(found: Sequence[Finding]) -> None:
    by: dict[str, int] = {}
    for f in found:
        print("  %s" % f)
        c = f.cls.split(" ")[0]
        by[c] = by.get(c, 0) + 1
    print("screen: %d finding%s (%s)" % (len(found), "" if len(found) == 1 else "s",
                                         ", ".join("%s %d" % kv for kv in sorted(by.items()))))


def _cmd_build(args) -> int:
    version, out, n = build(args.slug, project=args.project, major=args.major)
    print("built %s %s: %d sources, %d price lines filled live, in %s" % (
        args.slug, version, n["sources"], n["prices"], out.relative_to(out.parent.parent.parent)))
    return 0


def _cmd_status(args) -> int:
    proj = project_root(args.project)
    names = [args.slug] if args.slug else slugs(proj)
    if not names:
        print("no packages")
        return 0
    for s in names:
        print(status(s, project=proj, offline=args.offline).line())
    return 0


def _cmd_screen(args) -> int:
    if args.selftest:
        problems = selftest()
        for pr in problems:
            print("selftest: %s" % pr, file=sys.stderr)
        print("screen selftest: %s" % ("failed" if problems else "ok"))
        return 1 if problems else 0
    if not args.slug:
        print("awb solutions screen: give SLUG or --selftest", file=sys.stderr)
        return 2
    version, found = screen(args.slug, project=args.project, version=args.version)
    if found:
        _print_findings(found)
        return 1
    print("screen %s %s: clean" % (args.slug, version))
    return 0


def _cmd_release(args) -> int:
    version, found = release(args.slug, project=args.project)
    if found:
        print("not released: the release screen found something in %s" % version)
        _print_findings(found)
        return 1
    print("released %s %s, tagged %s/%s" % (args.slug, version, args.slug, version))
    return 0


def _parser():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb solutions", description="Solution packages: build, status, screen, release.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("build", help="fill the price lines live, check the sources and services, write out/vN/")
    s.add_argument("slug")
    s.add_argument("--major", action="store_true", help="vN+1 instead of vN.M+1 (a claim, a tier or a step changed)")
    s.add_argument("--project", type=Path, default=None)
    s.set_defaults(func=_cmd_build)
    s = sub.add_parser("status", help="one line per package: version, state, staleness counts")
    s.add_argument("slug", nargs="?")
    s.add_argument("--offline", action="store_true", help="leave the live price comparison out")
    s.add_argument("--project", type=Path, default=None)
    s.set_defaults(func=_cmd_status)
    s = sub.add_parser("screen", help="the release screen of a built version: file, line and class only")
    s.add_argument("slug", nargs="?")
    s.add_argument("--version", default=None)
    s.add_argument("--selftest", action="store_true", help="prove the screen finds planted cases")
    s.add_argument("--project", type=Path, default=None)
    s.set_defaults(func=_cmd_screen)
    s = sub.add_parser("release", help="screen, record, commit and tag the newest built version")
    s.add_argument("slug")
    s.add_argument("--project", type=Path, default=None)
    s.set_defaults(func=_cmd_release)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return 2
    try:
        return func(args)
    except kb.KBError as err:
        print("awb solutions: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb solutions: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
