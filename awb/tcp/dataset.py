"""The dataset "TCP Facts": the checked facts, the offered services and the price lists of T Cloud Public (TCP) as
files any assistant reads, built in one command so that a fresh copy appears with every refresh.

    awb dataset build [--out DIR] [--date D] [--force]    the folder tcp-facts-<date>/ and its zip, under
                                                          <shared>/datasets/ unless --out says otherwise
    awb dataset put [--date D] [--replace]                the zip, the how-to PDF and the manifest into the owner's
                                                          bucket under datasets/tcp-facts/<date>/ (owner side)

What the folder holds: README.md and PROMPT.md (how to use it, the one-line prompt), HOW-TO.pdf (the same for
people who read PDF), facts.md (every fact on one line, grouped by topic, small enough for one chat), topics/<tag>.md
(one file per first tag), facts.jsonl (the export of `awb kb export` with its metadata), services.md (what the
latest service description lists), prices/<region>.csv (the latest snapshot of each region) and MANIFEST.json
(counts, dates, the sha256 of every file). Every text file opens with the same rules for the assistant: answer from
these facts only and cite the id, name an old check date, treat prices as a snapshot, warn after the best-before
date.

The author named in the files is AWB_DATASET_AUTHOR or `dataset_author` of the host file (the account name
without either); the code carries no person's name.

Price rows of a service the service description withdrew or the hand-kept not-offered list names (BMS, DIS, VBS,
CSBS) are left out of the CSV files and named in their header, because the price API still serves them.

What leaves: the grades live, contract and docs, no retired and no expired entry, every one name checked again by
the export. The alias of a test tenant in a source line or a statement is replaced by "a TCP test tenant", the names of
the Workbench's own commands and parts (awb ..., the key service, the lab bucket) by plain words; the build proves
both rewrites on planted text first and refuses to write a folder in which one is left. No project,
no customer code, no snapshot of a tenant and no review is part of the dataset.
"""
from __future__ import annotations

import collections
import csv
import datetime
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import textwrap
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from awb import bucket, config, kb, obs, offered
from awb.tcp import price

NAME = "TCP Facts"
SLUG = "tcp-facts"
REGIONS = ("eu-de", "eu-nl")            # eu-ch2 is the Swiss offering, not a region of the TCP service description
BEST_BEFORE_DAYS = 30                   # the re-check period of an availability fact
OLD_AFTER_DAYS = 90
LICENCE = "CC BY 4.0"
DEFAULT_AUTHOR = "shimoza"              # the account; the owner's name comes from the setting, never from the code


def author() -> str:
    """Who the dataset names as its author: AWB_DATASET_AUTHOR, then dataset_author of the host file, then the
    account name. The repository never carries a person's name."""
    return os.environ.get("AWB_DATASET_AUTHOR") or config.host_conf().get("dataset_author") or DEFAULT_AUTHOR
BUCKET_PREFIX = "datasets/%s/" % SLUG
PRICE_COLUMNS = (("id", "id"), ("service", "productId"), ("product", "productName"), ("flavor", "opiFlavour"),
                 ("os", "osUnit"), ("vcpu", "vCpu"), ("ram", "ram"), ("unit", "unit"), ("currency", "currency"),
                 ("payg", "priceAmount"), ("reserved_12m", "R12"), ("reserved_24m", "R24"), ("reserved_36m", "R36"),
                 ("reserved_upfront_12m", "RU12"), ("reserved_upfront_24m", "RU24"),
                 ("reserved_upfront_36m", "RU36"))
_TENANT_RE = re.compile(r"\b(?:(?:the\s+)?test\s+tenant\s+)?test-\d{5}\b")
_LEFT_RE = re.compile(r"\btest-\d{5}\b")      # the scan after the build: its own pattern, so a weak rewrite cannot hide
_TOOL_RULES = (                                   # the Workbench's own commands and parts, named in source lines
    (re.compile(r"\bawb\s+[a-z]+(?:\s+[a-z]+)?(?:\s+--[a-z-]+(?:\s+[^\s,;.)]+)?)*"), "a scripted call"),
    (re.compile(r"\bthe key service\b", re.I), "a signed call"),
    (re.compile(r"\bkey service\b", re.I), "signing service"),
    (re.compile(r"\blab bucket\b", re.I), "test bucket"),
    (re.compile(r"\b(?:Architect )?Workbench\b"), "the tooling"),
)
_TOOL_LEFT_RE = re.compile(r"\bawb\b|key service|lab bucket|workbench", re.I)
_HOST_RE = re.compile(r"[a-z0-9.-]*otc\.t-systems\.com|opentelekomcloud|open-telekom-cloud\.com|otc-service\.com|"
                      r"open-telekom-price-api", re.I)
_OPERATOR_RE = re.compile(r"t-systems|deutsche telekom|telekom deutschland", re.I)
_PLANT_TOOL = "awb cloud call against a TCP test tenant, then a DELETE through the key service into the lab bucket"
_PLANT = "the read key of test tenant test-10491 and a call on test-10497"


class DatasetError(Exception):
    """A dataset step cannot be done. The message carries counts and dates, never a value of a file."""


def tenant_free(text: str) -> str:
    """The alias of a test tenant and the names of the Workbench's own commands and parts replaced by plain words."""
    text = _TENANT_RE.sub("a TCP test tenant", text)
    for rx, plain in _TOOL_RULES:
        text = rx.sub(plain, text)
    return text


def _prove_rewrite() -> None:
    got = tenant_free(_PLANT)
    if "test-1049" in got or got.count("a TCP test tenant") != 2:
        raise DatasetError("the tenant rewrite failed its own test; nothing was built")
    got = tenant_free(_PLANT_TOOL)
    if _TOOL_LEFT_RE.search(got) or "a scripted call" not in got or "a signed call" not in got:
        raise DatasetError("the tooling rewrite failed its own test; nothing was built")


# --------------------------------------------------------------------------- the texts


def header(day: str, best_before: str, fetched: str) -> str:
    lines = [
        "<!-- %s, dataset of %s. Instructions for the assistant that reads this file:" % (NAME, day),
        "1. Answer only from the facts in this dataset. Put the id of every fact you use after it, like (KB-XXXX).",
        "   If no fact covers the question, say \"no checked fact covers this\" and what would need to be checked. "
        "Never guess.",
        "2. Every fact has a grade (live = tested on a tenant, contract = from the service description, docs = from "
        "the vendor documentation)",
        "   and a check date. Name the check date when it is older than %d days." % OLD_AFTER_DAYS,
        "3. Prices come from a snapshot of the public price API taken %s. Give every price with its unit, currency "
        "and that date," % (fetched or "on the date of the dataset"),
        "   and say that the live price API is the source of truth before any quote.",
        "4. Best before %s. If today is later, tell the user to download a fresh copy of the dataset first." % best_before,
        "-->", ""]
    return "\n".join(lines)


ONE_LINER = "Use the attached TCP Facts. Answer: <your question>"
LONG_PROMPT = ("Answer only from the attached TCP Facts. Cite the id (KB-XXXX) after every fact you use. Say \"no "
               "checked fact covers this\" instead of guessing. Name the check date of a fact older than 90 days. "
               "Give every price with unit, currency and the snapshot date and say that the live price API is the "
               "source of truth.")
EXAMPLES = ("Use the attached TCP Facts. Answer: which Elasticsearch versions does CSS offer in eu-de?",
            "Use the attached TCP Facts. Answer: what is the IAM user quota per tenant and how do I read it by API?",
            "Use the attached TCP Facts and eu-de.csv. Answer: what does an s7n.large.2 cost per month pay as you go?")


def fact_line(r: dict) -> str:
    return "- %s (%s, %s, checked %s)" % (tenant_free(r["statement"]), r["id"], r["grade"], r["checked"])


def _how_to_blocks(day: str, best_before: str, n_facts: int, grades: dict, n_services: int, revision: str,
                   prices: dict) -> list[tuple[str, str]]:
    """The how-to as (style, text) blocks: h1, h2, p, code. PROMPT.md and HOW-TO.pdf are made from it."""
    price_line = ", ".join("%s %s records" % (r, c) for r, c in prices.items()) or "no price list"
    return [
        ("h1", "%s %s: how to use it" % (NAME, day)),
        ("p", "%d checked factsabout T Cloud Public (TCP) with the list of "
              "orderable services and the public price list of two regions. Made to be dropped into any AI "
              "assistant." % n_facts),
        ("p", "%d facts were tested live on a TCP tenant, %d come from the vendor documentation and %d from the "
              "service description (revision %s). No fact from hearsay, no expired fact, no customer, no project, "
              "no credential." % (grades.get("live", 0), grades.get("docs", 0), grades.get("contract", 0), revision)),
        ("h2", "Three steps"),
        ("p", "1. Take facts.md (all facts, one line each). For one topic only, take its file from topics/. For a "
              "price question add the CSV of the region from prices/."),
        ("p", "2. Attach the file to your assistant: ChatGPT, Claude, Gemini, Copilot or a local model. In a "
              "ChatGPT or Claude project the file stays attached for every chat."),
        ("p", "3. Type the prompt and your question. The file carries its own rules in its first lines, so the "
              "prompt stays short:"),
        ("code", ONE_LINER),
        ("p", "If the assistant ignores the rules in the file, paste this once before the question:"),
        ("code", LONG_PROMPT),
        ("h2", "Examples"),
        *[("code", e) for e in EXAMPLES],
        ("h2", "Files"),
        ("p", "facts.md: all facts, grouped by topic. facts.jsonl: the same facts with source, tags, class, expiry "
              "and the evidence of negatives, for RAG and tools. topics/<topic>.md: one file per topic. "
              "services.md: the %d services orderable per the service description of %s. prices/<region>.csv: "
              "%s. MANIFEST.json: counts, dates and the sha256 of every file." % (n_services, revision, price_line)),
        ("h2", "Dates and licence"),
        ("p", "Built %s. Best before %s: facts about availability are re-checked every %d days. Prices are a "
              "snapshot; the live price API is the source of truth before any quote." % (day, best_before,
                                                                                         BEST_BEFORE_DAYS)),
        ("p", "Licence %s. Author %s. Checked as stated, without warranty. The service description and the price "
              "list of the provider are the binding documents." % (LICENCE, author())),
    ]


def blocks_to_markdown(blocks: list[tuple[str, str]]) -> str:
    out = []
    for style, text in blocks:
        if style == "h1":
            out.append("# " + text)
        elif style == "h2":
            out.append("\n## " + text)
        elif style == "code":
            out.append("```\n%s\n```" % text)
        else:
            out.append(text)
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- a PDF without a library

_PAGE_W, _PAGE_H, _MARGIN = 595, 842, 56
_STYLES = {"h1": ("F2", 17, 24, 88), "h2": ("F2", 12, 17, 88), "p": ("F1", 10, 14, 92), "code": ("F3", 9, 13, 84)}


def _pdf_escape(text: str) -> str:
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_bytes(blocks: list[tuple[str, str]], title: str) -> bytes:
    """A PDF of the blocks: Helvetica for text, Helvetica-Bold for headings, Courier for a prompt. A4, as many
    pages as the text needs. Standard fonts only, so no library and no font file."""
    pages: list[list[str]] = [[]]
    y = _PAGE_H - _MARGIN

    def line(font: str, size: int, text: str) -> None:
        nonlocal y
        pages[-1].append("BT /%s %d Tf %d %d Td (%s) Tj ET" % (font, size, _MARGIN, int(y), _pdf_escape(text)))

    for style, text in blocks:
        font, size, lead, width = _STYLES[style]
        wrapped = textwrap.wrap(text, width) or [""]
        if y - lead * (len(wrapped) + 1) < _MARGIN and pages[-1]:
            pages.append([])
            y = _PAGE_H - _MARGIN
        y -= lead if style in ("h1", "h2") else 0
        for w in wrapped:
            y -= lead
            if y < _MARGIN:
                pages.append([])
                y = _PAGE_H - _MARGIN - lead
            line(font, size, w)
        y -= lead * 0.5
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    fonts = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier"}
    font_ids = {k: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % v.encode())
                for k, v in fonts.items()}
    pages_id = len(objs) + 1 + 2 * len(pages)
    page_ids = []
    for content in pages:
        stream = "\n".join(content).encode("latin-1")
        cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        res = b"<< /Font << " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in font_ids.items()) + b" >> >>"
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R >>"
                            % (pages_id, _PAGE_W, _PAGE_H, res, cid)))
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>"
               % len(page_ids)) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info = add(b"<< /Title (%s) /Author (%s) >>" % (_pdf_escape(title).encode("latin-1"),
                                                     _pdf_escape(author()).encode("latin-1")))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offsets:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, catalog, info, xref))
    return out.getvalue()


def not_offered(catalog: offered.Catalog, today: str, *names: str) -> bool:
    """True when one of the names is a service the service description withdrew or the hand-kept list of services
    not offered names. A name the catalogue simply does not know (a billing name such as a flavor family) is not
    judged: the price API's product names are not service names."""
    banned = {offered.key(f) for row in catalog.not_offered for f in row}
    for n in names:
        if not n:
            continue
        if offered.key(n) in banned:
            return True
        v = offered.check(n, catalog, today)
        if v.services and not v.offered:
            return True
    return False


# --------------------------------------------------------------------------- the checks of a build

_NEG_RE = re.compile(r"not offered|no longer|ramped down|withdrawn|not orderable|not be ordered|cannot be ordered|"
                     r"end of life|no entry|does not list|not listed|not in the service description|discontinued|"
                     r"retired|dropped|drops |removed|no proof|is not offered|absent", re.I)


def operator_named(records: list[dict]) -> list[str]:
    """Ids of the facts whose statement, source or tried texts name the operator outside a host name or the
    provider's package name. Reported, not refused: the owner decides the wording."""
    out = []
    for r in records:
        text = _HOST_RE.sub("", " ".join([r["statement"], r["source"], *r.get("tried", [])]))
        if _OPERATOR_RE.search(text):
            out.append(r["id"])
    return out


def facts_check(records: list[dict], catalog: offered.Catalog, day: str) -> tuple[list[dict], list[tuple[str, list[str]]]]:
    """(kept, flagged). A fact that names a service the service description does not offer, without saying that it
    is not offered, is flagged: the documentation and the API still know such services, the dataset must not read
    as if they could be ordered."""
    kept, flagged = [], []
    for r in records:
        named = offered.mentions(r["statement"], catalog, day)
        if named and not (r.get("negative") or _NEG_RE.search(r["statement"])):
            flagged.append((r["id"], named))
        else:
            kept.append(r)
    return kept, flagged


def _prove_facts_check(catalog: offered.Catalog, day: str) -> None:
    plants = [{"id": "KB-PLNT", "statement": "Bare Metal Server (BMS) is offered in eu-de with three flavors.",
               "negative": False},
              {"id": "KB-PLN2", "statement": "ECS flavor s3.large.2 is offered in eu-de.", "negative": False},
              {"id": "KB-PLN3", "statement": "Bare Metal Server (BMS) is ramped down on TCP.", "negative": False}]
    _, flagged = facts_check(plants, catalog, day)
    if [f[0] for f in flagged] != ["KB-PLNT"]:
        raise DatasetError("the offered check over the facts failed its own test; nothing was built")


def _prove_price_filter(catalog: offered.Catalog, day: str) -> None:
    if not not_offered(catalog, day, "bms", "BARE METAL") or not_offered(catalog, day, "ecs", "ELASTIC CLOUD SERVER"):
        raise DatasetError("the price filter failed its own test; nothing was built")


def price_check(kept: dict[str, list], *, sample: int, seed: str, fetch=None) -> list[str]:
    """A few rows of every region fetched again from the live price API and compared by record id and PAYG price,
    after the comparison proved on a planted wrong price that it can fail. Raises DatasetError on a mismatch or a
    row the API no longer serves; returns one line per region."""
    fetch = fetch or price.fetch
    rng = random.Random(seed)
    lines = []
    for region, recs in kept.items():
        picks = rng.sample(recs, min(sample, len(recs)))
        if not picks:
            continue
        by_service: dict[str, list] = {}
        for r in picks:
            by_service.setdefault(r.service, []).append(r)
        live: dict[str, price.Record] = {}
        for service in by_service:
            got = fetch(service, region)
            live.update({x.id: x for x in got.records})

        def mismatches(rows) -> list[str]:
            out = []
            for r in rows:
                want = r.price("PAYG")
                got_r = live.get(r.id)
                if got_r is None:
                    out.append("%s not served live" % r.id)
                elif got_r.price("PAYG") != want:
                    out.append("%s snapshot %s live %s" % (r.id, want, got_r.price("PAYG")))
            return out

        planted = price.Record.from_raw(dict(picks[0].raw, priceAmount="9.999999 EUR"))
        if not mismatches([planted]):
            raise DatasetError("the live price check failed its own test; nothing was built")
        bad = mismatches(picks)
        if bad:
            raise DatasetError("live price check of %s: %s; take a new price snapshot and build again"
                               % (region, "; ".join(bad)))
        lines.append("live price check %s: %d rows of %d services match the API" % (region, len(picks), len(by_service)))
    return lines


# --------------------------------------------------------------------------- the build


@dataclass
class Built:
    folder: Path
    zip: Path
    day: str
    facts: int
    grades: dict
    topics: int
    services: int
    prices: dict
    left: dict
    lines: list[str] = field(default_factory=list)


def datasets_dir(p: config.Paths) -> Path:
    return p.shared / "datasets"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(p: config.Paths, *, out: Path | None = None, today: datetime.date | None = None,
          force: bool = False, live: bool = True, sample: int = 3) -> Built:
    """The folder tcp-facts-<date>/ and the zip beside it. Refused without facts, over an existing folder
    unless `force`, and when a tenant alias is left in any file."""
    _prove_rewrite()
    today = today or datetime.date.today()
    day = today.isoformat()
    best_before = (today + datetime.timedelta(days=BEST_BEFORE_DAYS)).isoformat()
    records, left = kb.export(p, today=today)
    if not records:
        raise DatasetError("no fact fit to leave: nothing to build")
    catalog = offered.load()
    _prove_facts_check(catalog, day)
    _prove_price_filter(catalog, day)
    records, flagged = facts_check(records, catalog, day)
    left["names a service not offered"] = len(flagged)
    if not records:
        raise DatasetError("no fact left after the offered check: nothing to build")
    for r in records:
        r["statement"] = tenant_free(r["statement"])
        r["source"] = tenant_free(r["source"])
        r["tried"] = [tenant_free(t) for t in r.get("tried", [])]
    records.sort(key=lambda r: ((r["tags"] or ["zzz"])[0], r["id"]))
    grades: dict[str, int] = {}
    bytag: dict[str, list[dict]] = {}
    for r in records:
        grades[r["grade"]] = grades.get(r["grade"], 0) + 1
        bytag.setdefault((r["tags"] or ["other"])[0], []).append(r)
    tags = sorted(bytag, key=lambda t: (-len(bytag[t]), t))

    revision = offered.revision_label(catalog) or "unknown"
    snapshots: dict[str, tuple[dict, list[price.Record]]] = {}
    for region in REGIONS:
        path = price.latest_snapshot(p, region)
        if path is not None:
            snapshots[region] = price.load_snapshot(path)
    fetched = ""
    for meta, _ in snapshots.values():
        fetched = max(fetched, str(meta.get("fetched_at") or ""))
    counts = {r: len(recs) for r, (_, recs) in snapshots.items()}

    base = out or config.make_dir(datasets_dir(p), 0o2775, shared=True)
    root = base / ("%s-%s" % (SLUG, day))
    if root.exists():
        if not force:
            raise DatasetError("the folder of %s exists; give --force to build it again" % day)
        shutil.rmtree(root)
    (root / "topics").mkdir(parents=True)
    (root / "prices").mkdir()
    try:
        return _write(root, p, day, best_before, fetched, records, flagged, left, grades, bytag, tags, catalog, revision,
                      snapshots, counts, live, sample)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)      # a refused build leaves no half-written folder behind
        raise


def _write(root: Path, p: config.Paths, day: str, best_before: str, fetched: str, records: list[dict], flagged,
           left: dict, grades: dict, bytag: dict, tags: list[str], catalog, revision: str, snapshots: dict,
           counts: dict, live: bool, sample: int) -> Built:
    left_out: dict[str, dict[str, int]] = {}
    kept_rows: dict[str, list] = {}
    built_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = header(day, best_before, fetched)

    parts = [head, "# %s %s" % (NAME, day), "",
             "%d checked facts about T Cloud Public (TCP). One line per fact, "
             "grouped by topic. Read the comment at the top before answering." % len(records)]
    for t in tags:
        parts += ["", "## %s (%d)" % (t, len(bytag[t])), ""]
        parts += [fact_line(r) for r in bytag[t]]
        (root / "topics" / (t + ".md")).write_text(
            head + "# %s %s: %s\n\n" % (NAME, day, t) + "\n".join(fact_line(r) for r in bytag[t]) + "\n",
            encoding="utf-8")
    (root / "facts.md").write_text("\n".join(parts) + "\n", encoding="utf-8")

    with (root / "facts.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"dataset": NAME, "date": day, "best_before": best_before, "count": len(records),
                            "instructions": "Answer only from these facts, cite the id of every fact used, name "
                                            "the check date when older than %d days, say so when no fact covers "
                                            "the question." % OLD_AFTER_DAYS}) + "\n")
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    svc = ["- %s %s%s%s" % (s.number, s.name, " (%s)" % s.aliases[0] if s.aliases and s.aliases[0] not in s.name
                              else "", "" if s.status == "offered" else ", %s" % s.status)
           for s in catalog.services if not s.ends or s.ends > day]
    (root / "services.md").write_text(
        head + "# Services orderable on TCP\n\nFrom the T Cloud Public service description, revision %s. %d "
        "services. A service that is not in this list is not offered, whatever the documentation says.\n\n%s\n"
        % (revision, len(svc), "\n".join(svc)), encoding="utf-8")

    for region, (meta, recs) in snapshots.items():
        keep, gone = [], collections.Counter()
        for rec in recs:
            if not_offered(catalog, day, rec.raw.get("productIdParameter", ""), rec.raw.get("productId", "")):
                gone[str(rec.raw.get("productId", "")) or rec.service] += 1
            else:
                keep.append(rec)
        left_out[region] = dict(gone)
        kept_rows[region] = keep
        counts[region] = len(keep)
        with (root / "prices" / (region + ".csv")).open("w", encoding="utf-8", newline="") as f:
            f.write("# TCP price list %s, public price API, fetched %s. Prices per unit in EUR, net. payg = pay as "
                    "you go, reserved_* = reserved term per month, reserved_upfront_* = paid up front. The live "
                    "API is the source of truth before any quote.%s\n"
                    % (region, meta.get("fetched_at") or day,
                       " Rows of services the service description of %s does not offer are left out, although the "
                       "API still serves them: %s." % (revision, ", ".join("%s (%d)" % kv for kv in sorted(gone.items())))
                       if gone else ""))
            w = csv.writer(f)
            w.writerow([c for c, _ in PRICE_COLUMNS])
            for rec in keep:
                w.writerow([str(rec.raw.get(k, "")).replace(" EUR", "") for _, k in PRICE_COLUMNS])

    check_lines = price_check(kept_rows, sample=sample, seed=day) if live and kept_rows else \
        ["live price check skipped"]
    named = operator_named(records)
    check_lines.append("operator named in prose: %s" % (", ".join(named) if named else "none"))
    blocks = _how_to_blocks(day, best_before, len(records), grades, len(svc), revision, counts)
    (root / "PROMPT.md").write_text(blocks_to_markdown(blocks), encoding="utf-8")
    (root / "HOW-TO.pdf").write_bytes(pdf_bytes(blocks, "%s %s: how to use it" % (NAME, day)))
    (root / "README.md").write_text(blocks_to_markdown(
        [("h1", "%s %s" % (NAME, day)), ("p", "Build of %s. A later build of the same day replaces this one under the same "
                                              "name; the manifest carries the build time." % built_at)]
        + blocks[1:3] + [("h2", "Use"), ("p", "Attach facts.md and type:"),
                                                          ("code", ONE_LINER), ("p", "PROMPT.md has the rest.")]
        + blocks[-5:]), encoding="utf-8")

    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".md", ".jsonl", ".csv"):
            text = path.read_text(encoding="utf-8")
            if _LEFT_RE.search(text):
                raise DatasetError("a tenant alias was left in %s; the folder was removed" % path.name)
            if _TOOL_LEFT_RE.search(text):
                raise DatasetError("a name of the tooling was left in %s; the folder was removed" % path.name)

    files = sorted(x for x in root.rglob("*") if x.is_file())
    manifest = {"dataset": NAME, "date": day, "built_at": built_at, "best_before": best_before, "facts": len(records),
                "grades": grades,
                "topics": {t: len(bytag[t]) for t in tags}, "services": len(svc), "service_description": revision,
                "prices": {"fetched": fetched, "records": counts, "left_out": left_out},
                "facts_left_out": [{"id": i, "names": n} for i, n in flagged], "licence": LICENCE, "author": author(),
                "files": {str(x.relative_to(root)): {"bytes": x.stat().st_size, "sha256": _sha(x)} for x in files}}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    zpath = root.parent / (root.name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for x in sorted(root.rglob("*")):
            if x.is_file():
                z.write(x, "%s/%s" % (root.name, x.relative_to(root)))
    built = Built(root, zpath, day, len(records), grades, len(tags), len(svc), counts, left)
    built.lines = ["%s: %d facts (%s), %d topics, %d services of revision %s, prices %s"
                   % (root.name, len(records), ", ".join("%s %d" % kv for kv in sorted(grades.items())), len(tags),
                      len(svc), revision, ", ".join("%s %d" % kv for kv in counts.items()) or "none"),
                   "left out: " + ", ".join("%s %d" % kv for kv in left.items() if kv[1])
                   + ("; facts naming a service not offered: %s" % ", ".join("%s (%s)" % (i, ", ".join(n)) for i, n in flagged)
                      if flagged else ""),
                   *check_lines,
                   "zip %s, %d bytes, built %s, best before %s" % (zpath.name, zpath.stat().st_size, built_at,
                                                                   best_before)]
    return built


def latest(p: config.Paths, out: Path | None = None, day: str | None = None) -> Path | None:
    base = out or datasets_dir(p)
    if day:
        folder = base / ("%s-%s" % (SLUG, day))
        return folder if folder.is_dir() else None
    folders = sorted(base.glob("%s-????-??-??" % SLUG))
    return folders[-1] if folders else None


# --------------------------------------------------------------------------- the bucket (owner side)


def put(c: obs.Client, folder: Path, *, replace: bool = False) -> list[str]:
    """The zip, HOW-TO.pdf and MANIFEST.json of a built folder into datasets/tcp-facts/<date>/ and the date
    into datasets/tcp-facts/LATEST. Refused when that date is there already unless `replace`."""
    day = folder.name[len(SLUG) + 1:]
    zpath = folder.parent / (folder.name + ".zip")
    if not (zpath.is_file() and (folder / "HOW-TO.pdf").is_file() and (folder / "MANIFEST.json").is_file()):
        raise DatasetError("the folder of %s is not a complete build; run awb dataset build" % day)
    prefix = "%s%s/" % (BUCKET_PREFIX, day)
    if not replace and c.head(prefix + zpath.name) is not None:
        raise DatasetError("the bucket holds the dataset of %s already; give --replace" % day)
    keys = []
    for src, ctype in ((zpath, "application/zip"), (folder / "HOW-TO.pdf", "application/pdf"),
                       (folder / "MANIFEST.json", "application/json")):
        key = prefix + src.name
        c.put_file(key, src, ctype)
        keys.append(key)
    c.put_bytes(BUCKET_PREFIX + "LATEST", (day + "\n").encode(), "text/plain")
    return keys


# --------------------------------------------------------------------------- the command


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = SafeParser(prog="awb dataset", description="The dataset TCP Facts: build it, put it into the bucket.")
    sub = ap.add_subparsers(dest="command")
    b = sub.add_parser("build", help="the folder tcp-facts-<date>/ and its zip")
    b.add_argument("--out", type=Path, default=None, help="where the folder goes (default: <shared>/datasets)")
    b.add_argument("--date", default=None, help="the date of the dataset (default: today)")
    b.add_argument("--force", action="store_true", help="build again over the folder of that date")
    b.add_argument("--no-live", action="store_true", help="skip the live price check (offline)")
    b.add_argument("--price-sample", type=int, default=3, help="rows per region checked against the live API")
    u = sub.add_parser("put", help="the zip, the how-to PDF and the manifest into the owner's bucket")
    u.add_argument("--out", type=Path, default=None, help="where the folders are (default: <shared>/datasets)")
    u.add_argument("--date", default=None, help="the dataset of that date (default: the newest)")
    u.add_argument("--replace", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    try:
        if args.date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            raise DatasetError("the date reads YYYY-MM-DD")
        if args.command == "build":
            today = datetime.date.fromisoformat(args.date) if args.date else None
            built = build(p, out=args.out, today=today, force=args.force, live=not args.no_live,
                          sample=max(1, args.price_sample))
            for line in built.lines:
                print("awb dataset build: " + line)
            return 0
        folder = latest(p, args.out, args.date)
        if folder is None:
            raise DatasetError("no built dataset%s; run awb dataset build" % (" of that date" if args.date else ""))
        keys = put(bucket.client(), folder, replace=args.replace)
        for key in keys:
            print("awb dataset put: " + key)
        return 0
    except (DatasetError, kb.KBError, price.PriceError, obs.OBSError, bucket.BucketError, ValueError) as err:
        print("awb dataset: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb dataset: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
