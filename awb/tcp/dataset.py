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

What leaves: the grades live, contract and docs, no retired and no expired entry, every one name checked again by
the export. The alias of a test tenant in a source line or a statement is replaced by "a TCP test tenant"; the
build proves that rewrite on a planted alias first and refuses to write a folder in which one is left. No project,
no customer code, no snapshot of a tenant and no review is part of the dataset.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
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
REGIONS = ("eu-de", "eu-nl", "eu-ch2")
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
_PLANT = "the read key of test tenant test-10491 and a call on test-10497"


class DatasetError(Exception):
    """A dataset step cannot be done. The message carries counts and dates, never a value of a file."""


def tenant_free(text: str) -> str:
    """The alias of a test tenant replaced by plain words."""
    return _TENANT_RE.sub("a TCP test tenant", text)


def _prove_rewrite() -> None:
    got = tenant_free(_PLANT)
    if "test-1049" in got or got.count("a TCP test tenant") != 2:
        raise DatasetError("the tenant rewrite failed its own test; nothing was built")


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
        ("p", "%d checked facts about T Cloud Public (TCP), the public cloud of Deutsche Telekom, with the list of "
              "orderable services and the public price list of three regions. Made to be dropped into any AI "
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
          force: bool = False) -> Built:
    """The folder tcp-facts-<date>/ and the zip beside it. Refused without facts, over an existing folder
    unless `force`, and when a tenant alias is left in any file."""
    _prove_rewrite()
    today = today or datetime.date.today()
    day = today.isoformat()
    best_before = (today + datetime.timedelta(days=BEST_BEFORE_DAYS)).isoformat()
    records, left = kb.export(p, today=today)
    if not records:
        raise DatasetError("no fact fit to leave: nothing to build")
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

    catalog = offered.load()
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
    head = header(day, best_before, fetched)

    parts = [head, "# %s %s" % (NAME, day), "",
             "%d checked facts about T Cloud Public (TCP), the public cloud of Deutsche Telekom. One line per fact, "
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
        with (root / "prices" / (region + ".csv")).open("w", encoding="utf-8", newline="") as f:
            f.write("# TCP price list %s, public price API, fetched %s. Prices per unit in EUR, net. payg = pay as "
                    "you go, reserved_* = reserved term per month, reserved_upfront_* = paid up front. The live "
                    "API is the source of truth before any quote.\n" % (region, meta.get("fetched_at") or day))
            w = csv.writer(f)
            w.writerow([c for c, _ in PRICE_COLUMNS])
            for rec in recs:
                w.writerow([str(rec.raw.get(k, "")).replace(" EUR", "") for _, k in PRICE_COLUMNS])

    blocks = _how_to_blocks(day, best_before, len(records), grades, len(svc), revision, counts)
    (root / "PROMPT.md").write_text(blocks_to_markdown(blocks), encoding="utf-8")
    (root / "HOW-TO.pdf").write_bytes(pdf_bytes(blocks, "%s %s: how to use it" % (NAME, day)))
    (root / "README.md").write_text(blocks_to_markdown(
        [("h1", "%s %s" % (NAME, day))] + blocks[1:3] + [("h2", "Use"), ("p", "Attach facts.md and type:"),
                                                          ("code", ONE_LINER), ("p", "PROMPT.md has the rest.")]
        + blocks[-5:]), encoding="utf-8")

    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".md", ".jsonl", ".csv"):
            if _LEFT_RE.search(path.read_text(encoding="utf-8")):
                shutil.rmtree(root)
                raise DatasetError("a tenant alias was left in %s; the folder was removed" % path.name)

    files = sorted(x for x in root.rglob("*") if x.is_file())
    manifest = {"dataset": NAME, "date": day, "best_before": best_before, "facts": len(records), "grades": grades,
                "topics": {t: len(bytag[t]) for t in tags}, "services": len(svc), "service_description": revision,
                "prices": {"fetched": fetched, "records": counts}, "licence": LICENCE, "author": author(),
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
                   "left out: " + ", ".join("%s %d" % kv for kv in left.items() if kv[1]),
                   "zip %s, %d bytes, best before %s" % (zpath.name, zpath.stat().st_size, best_before)]
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
            built = build(p, out=args.out, today=today, force=args.force)
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
