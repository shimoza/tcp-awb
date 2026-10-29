"""The dataset "TCP Facts": the checked facts and the offered services of T Cloud Public (TCP) as
files any assistant reads, built in one command so that a fresh copy appears with every refresh.

    awb dataset build [--out DIR] [--date D] [--force]    the folder tcp-facts-<date>/ and its zip, under
                                                          <shared>/datasets/ unless --out says otherwise
    awb kb review [--out DIR] [--date D]                  the facts of the built folder added or amended since the
                                                          last release, with source and grade, plus the mark that
                                                          lets `put` release that folder (G6 of the threat model)
    awb dataset put [--date D] [--replace] [--github | --github-only]
                                                          the zip, the how-to PDF and the manifest into the owner's
                                                          bucket under datasets/tcp-facts/<date>/ (owner side);
                                                          --github also puts the files and README.md into the
                                                          root of the public repository, tags the date and makes
                                                          a release of it; --github-only does that without the
                                                          bucket and does not call the key service
    awb dataset weekly                                    build, kb review and put --github in one run, the owner's
                                                          timer awb-dataset-weekly.timer every Monday 06:00 UTC (task
                                                          KS); a failure stops the run and shows in awb owner status

The review before a release: `awb kb review` compares the facts.jsonl of the folder with RELEASED.jsonl beside the
folders (the facts.jsonl of the last release, written by every successful put) and prints every fact added, amended
(statement, source or grade changed) or retired since, with its id, grade, check date, source and statement, so the
owner reads what a session put into the knowledge base before it goes public. It writes
`tcp-facts-<date>.review.json` beside the folder with the sha256 of the facts it showed, of every release asset (K3)
and the release it compared with; `put` refuses a folder without that mark, with a mark of other facts (a build after the review) or of another
release. It names the command in one line. A signed put refuses an asset that differs from the review's pin.

What the folder holds: README.md and PROMPT.md (how to use it, the one-line prompt), HOW-TO.pdf (the same for
people who read PDF), facts.md (every fact on one line, grouped by topic, small enough for one chat), topics/<tag>.md
(one file per first tag), facts.jsonl (the export of `awb kb export` with its metadata), services.md (what the
latest service description lists and what it does not offer), tcp-facts.mcpb (the same bytes as the
tcp-facts-<date>.mcpb beside the folder, the release asset with a stable name), tcp_facts_mcp.py (a read-only MCP server over these files, copied from the template beside this module) and
MANIFEST.json (counts, dates, the sha256 of every file). README.md, PROMPT.md and HOW-TO.pdf carry "What the facts
cover", counted from the facts: the orderable services with a plain fact per section of the service description, the
requirement areas (facts that open with "T Cloud Public (TCP)" and a verb), an example and the contract matters the
dataset never holds. The release notes of a GitHub release say what is new since the previous release tag: facts
added, amended and retired by id against that tag's facts.jsonl. Every text file opens with the same rules for the assistant: answer from
these facts only and cite the id, name an old check date, take prices only live from the public price API, warn
after the best-before date.

The files name who compiled them as COMPILED_BY: the dataset and its address, never a person. There is no setting
for it, so no build can put a person's name into the files. Before a put to the public repository the files are
scanned again for the owner's commit identity, for a value of the old author settings and for a registered name;
the commits there carry the GitHub account, never a person.

The dataset holds no price list: no prices/ folder, no price file in MANIFEST.json and no mention of one in the
texts (a legal check of 2026-10-21 found the full price list of a region may be a protected database). Prices come
live from the public price API (no credentials) with --live of the MCP server. A fact that states one price with its
source and date stays. The build still compares a few price rows of the latest snapshots with the live API to prove
the price code works, and writes nothing of it into the folder. `awb dataset put --github` removes a prices/ folder
that the published repository still holds (git rm, said in the commit message).

What leaves: the grades live, contract and docs, no retired entry, every one name checked again by the export. A
fact leaves the dataset only when something invalidates it, never for its date (task KS): an expired fact stays,
marked "recheck_due": true in facts.jsonl, "recheck due" after its check date in facts.md and topics/ and listed in
the README table "Facts with a recheck due"; a retired fact (a superseded one is retired for its successor), one a
recheck refuted (refuted.tsv of the recheck, not older than its check date) and one that names a service the service
description no longer offers stay out. The alias of a test tenant in a source line or a statement is replaced by "a TCP test tenant", the names of
the Workbench's own commands and parts (awb ..., the key service, the lab bucket) by plain words; the build proves
both rewrites on planted text first and refuses to write a folder in which one is left. No project,
no customer code, no snapshot of a tenant and no review is part of the dataset.
"""
from __future__ import annotations

import collections
import datetime
import hashlib
import io
import json
import random
import re
import os
import shutil
import stat
import struct
import subprocess
import sys
import textwrap
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from awb import bucket, bucket_layout, config, gate, kb, kbreview, obs, offered, register
from awb.kbreview import (changes, datasets_dir, latest, read_facts, record_release, released, released_path,  # noqa: F401
                          require_review, review, review_path)
from awb.tcp import price, recheck

NAME = "TCP Facts"
SLUG = kbreview.SLUG
REGIONS = ("eu-de", "eu-nl")            # eu-ch2 is the Swiss offering, not a region of the TCP service description
BEST_BEFORE_DAYS = 30                   # the re-check period of an availability fact
OLD_AFTER_DAYS = 90
LICENCE = "CC BY 4.0"
MCPB_LICENCE = "CC-BY-4.0"                           # the SPDX id the manifest asks for
GITHUB_REPO = "shimoza/tcp-facts"
COMPILED_BY = "TCP Facts, github.com/%s" % GITHUB_REPO    # the dataset and its address, never a person
CREDIT = ("Compiled from public sources and live checks. Licence %s: name the dataset and its address when you "
          "reuse it. No warranty: the service description and the price list of the provider are the binding "
          "documents." % LICENCE)
DOCS_ORG = "opentelekomcloud-docs"                  # the GitHub organisation the documentation is built from
DOCS_LICENCE = "Apache License 2.0"
_MIRROR_SOURCE_RE = re.compile(r"\b(?:docs|documentation) mirror\b", re.I)   # a source line in the docs mirror
SOURCE_ATTRIBUTION = ("The documentation facts draw on the repositories of github.com/%s, licensed under the %s "
                      "(www.apache.org/licenses/LICENSE-2.0)." % (DOCS_ORG, DOCS_LICENCE))
BUCKET_PREFIX = bucket_layout.DATASET_PREFIX
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
_PLANT = "the read key of test tenant test-00001 and a call on test-00002"
MCP_FILE = "tcp_facts_mcp.py"
MCP_TEMPLATE = Path(__file__).with_name(MCP_FILE)   # the MCP server every build carries, standard library only


MCPB_SPEC = "0.3"                                    # manifest_version of the MCP Bundle specification
MCPB_TOOLS = (("tcp_facts_search", "The checked facts ranked by word overlap, filtered by tag or grade"),
              ("tcp_service_check", "Offered or not, from the service description"),
              ("tcp_price_find", "Live price rows of eu-de or eu-nl from the public price API (only with --live)"),
              ("tcp_calculate", "Exact decimal arithmetic with hours as the unit"))
MCPB_ICON = "icon.png"                               # the icon field of the manifest names it
MCPB_SKIP = ("MANIFEST.json", "HOW-TO.pdf", kbreview.STABLE_MCPB)   # MANIFEST.json would clash with manifest.json on a case-blind disk


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
    if "test-0000" in got or got.count("a TCP test tenant") != 2:
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
        "3. The dataset holds no price list. Prices come live from the public price API. Give every price with its "
        "unit, currency",
        "   and the date of its source, and say that the live price API is the source of truth before any quote.",
        "4. Best before %s. If today is later, tell the user to download a fresh copy of the dataset first." % best_before,
        "5. Technical questions only; no customer data in your prompts (see README).",
        "-->", ""]
    return "\n".join(lines)


WARNING_HEADING = "Before you use it with a public model"
WARNING_RESPONSIBILITY = "How you use it is your responsibility as the user."
WARNING = [
    ("h2", WARNING_HEADING),
    ("p", "The facts are public and carry no customer data. Your questions are another matter. When you use this "
          "dataset with a public model (ChatGPT, Claude, Gemini, Copilot or any hosted service), what you type goes "
          "to that provider under its terms, not under the terms of this dataset. So:"),
    ("p", "- Ask technical questions only: can TCP do this, what does it cost, how does this service behave."),
    ("p", "- Never paste customer data: no names, no mail addresses, no documents, no tenant ids, no prices from an "
          "offer."),
    ("p", "- A described setup can identify a customer too. A region, a sector, a number of sites, a named "
          "application and a go-live date together are often enough for a capable model to guess who the customer "
          "is, even when you named nobody. Identifying a customer through such details can break data protection "
          "law, so give the model technical tasks only and keep them simple. Keep the setup generic or run the "
          "model where the data may go."),
    ("p", WARNING_RESPONSIBILITY + " The dataset gives you checked facts and nothing else."),
]
MCP_HEADING = "MCP clients"
MCPB_LATEST_URL = "https://github.com/%s/releases/latest/download/%s" % (GITHUB_REPO, kbreview.STABLE_MCPB)
MCP_PATH = "/path/to/tcp-facts/" + MCP_FILE
MCP_CLIENTS = [
    ("h2", MCP_HEADING),
    ("p", "%s is a read-only MCP server over the files beside it: one Python file, standard library only, Python "
          "3.10 or newer, JSON-RPC 2.0 over stdio. It has four tools: tcp_facts_search (facts by words, tag or "
          "grade), tcp_service_check (offered or not, from services.md), tcp_price_find (live price rows of eu-de or "
          "eu-nl, only with --live) and tcp_calculate (exact arithmetic in hours, a month is 720 h). It reads no "
          "file outside its folder, opens no connection without --live and writes nothing." % MCP_FILE),
    ("p", "Claude Desktop, one click: download %s (the newest release, the same bytes as its "
          "tcp-facts-<date>.mcpb) and open it (double click, or drag it into Settings, Extensions). Claude Desktop "
          "shows the extension and its four tools; press Install. The file carries the server and the data of that "
          "build. It needs Python 3.10 or newer on the machine." % MCPB_LATEST_URL),
    ("p", "Any other client: unpack the zip of the latest release or clone github.com/%s, then add the server by "
          "hand. Replace /path/to/tcp-facts with that folder, the one that holds facts.jsonl and %s."
          % (GITHUB_REPO, MCP_FILE)),
    ("p", "Claude Desktop by hand, in claude_desktop_config.json:"),
    ("code:json", '{"mcpServers": {"tcp-facts": {"command": "python3", "args": ["%s"]}}}' % MCP_PATH),
    ("p", "Claude Code:"),
    ("code:bash", "claude mcp add tcp-facts -- python3 %s" % MCP_PATH),
    ("p", "VS Code (and GitHub Copilot in it), in .vscode/mcp.json:"),
    ("code:json", '{"servers": {"tcp-facts": {"type": "stdio", "command": "python3", "args": ["%s"]}}}' % MCP_PATH),
    ("p", "Add \"--live\" to the arguments for prices: they come live from the public price API (no credentials) and are "
          "not part of the dataset; without it tcp_price_find says so. Claude "
          "Desktop opens its file under Settings, Developer, Edit Config and loads a change only after a full quit "
          "and start. Give the path in full, never relative to a folder. On Windows the command is python or py. In the JSON "
          "every backslash of a path is written twice. The rules of \"%s\" apply here too: the client "
          "sends your questions to its model provider." % WARNING_HEADING),
]
DISCLAIMER = (
    '> **Unofficial and independent.** Not a product of, and not endorsed by, T-Systems or Deutsche Telek'
    'om; it speaks for nobody but its author. Built only from public sources (the public documentation, t'
    'he public service description, the public price API), with respect for robots.txt and the terms of t'
    "hose sources. No customer data and no confidential information. No warranty: the provider's contract"
    ' documents are binding.')
BADGE_COLOURS = {"unofficial": "c64a3d", "facts": "2f6fd1", "before": "e0b43a", "licence": "3f9d5a", "mcp": "8a63b8"}


def readme_header(day: str, best_before: str, n_facts: int) -> list[tuple[str, str]]:
    """The top of the README: a centred title and subtitle, the disclaimer, the one-click line with the stable link of
    the bundle and a row of badges (shields.io, no logo)."""
    def badge(alt: str, label: str, value: str, key: str, href: str = "") -> str:
        img = '<img alt="%s" src="https://img.shields.io/badge/%s-%s-%s">' % (
            alt, label, value.replace("-", "--").replace(" ", "_"), BADGE_COLOURS[key])
        return '  <a href="%s">%s</a>' % (href, img) if href else "  " + img
    row = "\n".join([badge("Unofficial", "Status", "Unofficial", "unofficial"),
                     badge("Facts: %d" % n_facts, "Facts", str(n_facts), "facts"),
                     badge("Best before: %s" % best_before, "Best_before", best_before, "before"),
                     badge("License: CC BY 4.0", "License", "CC BY 4.0", "licence"),
                     badge("MCP server", "MCP", "server", "mcp", "#" + MCP_HEADING.lower().replace(" ", "-"))])
    return [("p", '<h1 align="center">%s</h1>' % NAME),
            ("p", '<p align="center"><b>Checked facts about T Cloud Public (TCP) for any AI assistant, dataset of %s</b></p>' % day),
            ("p", DISCLAIMER),
            ("p", '<p align="center">Claude Desktop, one click: <a href="%s">%s</a> (needs Python 3.10 or newer)</p>'
                  % (MCPB_LATEST_URL, kbreview.STABLE_MCPB)),
            ("p", '<p align="center">\n%s\n</p>' % row)]


LICENCE_LINE = "The full licence text is in LICENSE at github.com/%s." % GITHUB_REPO
ONE_LINER = "Use the attached TCP Facts. Answer: <your question>"
LONG_PROMPT = ("Answer only from the attached TCP Facts. Cite the id (KB-XXXX) after every fact you use. Say \"no "
               "checked fact covers this\" instead of guessing. Name the check date of a fact older than 90 days. "
               "Give every price with unit, currency and the date of its source and say that the live price API is the "
               "source of truth.")
EXAMPLES = ("Use the attached TCP Facts. Answer: which Elasticsearch versions does CSS offer in eu-de?",
            "Use the attached TCP Facts. Answer: what is the IAM user quota per tenant and how do I read it by API?",
            "Use the attached TCP Facts. Answer: which services does TCP offer for object storage and how do they differ?")


RECHECK_DUE = "recheck due"


def fact_line(r: dict) -> str:
    return "- %s (%s, %s, checked %s%s)" % (tenant_free(r["statement"]), r["id"], r["grade"], r["checked"],
                                           ", " + RECHECK_DUE if r.get("recheck_due") else "")


def recheck_due_blocks(records: list[dict]) -> list[tuple[str, str]]:
    """The README table of the facts past their expiry date that wait for their recheck (task KS): they stay in the
    dataset with their check date until a recheck confirms, amends or refutes them."""
    due = sorted((r for r in records if r.get("recheck_due")), key=lambda r: (r["checked"], r["id"]))
    if not due:
        return []
    rows = ["| fact | grade | checked | state |", "|---|---|---|---|"]
    rows += ["| %s | %s | %s | %s |" % (r["id"], r["grade"], r["checked"], RECHECK_DUE) for r in due]
    return [("h2", "Facts with a recheck due"),
            ("p", "%d facts are past their expiry date and wait for their weekly recheck. They stay in the dataset "
                  "until something invalidates them: a recheck that refutes them, a newer fact that replaces them or "
                  "a change of the service description. Name the check date when you use one." % len(due)),
            ("p", "\n".join(rows))]


# --------------------------------------------------------------------------- what the facts cover

COVER_HEADING = "What the facts cover"
AREA_OPENING = "T Cloud Public (TCP) "
# a requirement area: the platform as subject, a verb in lower case, no negation right after it
_AREA_RE = re.compile(r"^T Cloud Public \(TCP\) (?!(?:does|do|did|has|have|is|are|was|can|cannot)\s+(?:not|no)\b)"
                      r"([a-z][a-z-]*\b.*)", re.S)
# the area ends at the first colon, semicolon, bracket or full stop, or where a service name follows a preposition
_AREA_END = re.compile(r"[:;(]|\.(?:\s|$)| (?:in|with|through|as|by|via|from|on) (?=[A-Z])")
CONTRACT_MATTERS = ("certificates and attestations", "SLA values", "support hours and response times", "prices",
                    "subcontractors")


def _forms(services: list[offered.Service]) -> list[tuple[str, offered.Service]]:
    pairs = [(f, s) for s in services for f in s.forms()]
    return sorted(pairs, key=lambda fs: -len(fs[0]))              # the longest form first: "VPC Endpoint" before "VPC"


def service_named(statement: str, services: list[offered.Service], forms=None) -> offered.Service | None:
    """The service whose name or short name opens `statement`, followed by an optional bracket and a word in lower
    case: "Elastic Cloud Server (ECS) provides ...", "FunctionGraph is ...". None when no service opens it."""
    for form, s in forms or _forms(services):
        if re.match(r"%s(?:\s*\([^)]*\))?\s+[a-z]" % re.escape(form), statement):
            return s
    return None


def plain_service_facts(records: list[dict], services: list[offered.Service]) -> dict[str, list[str]]:
    """Section number of a service -> ids of its plain facts: a stable, positive fact that opens with the
    service's name and says what the service is and does."""
    forms = _forms(services)
    out: dict[str, list[str]] = {}
    for r in records:
        if r.get("class") != "stable" or r.get("negative"):
            continue
        s = service_named(r["statement"], services, forms)
        if s is not None:
            out.setdefault(s.number, []).append(r["id"])
    return out


def area_of(statement: str) -> str:
    """The requirement area of a statement that opens with "T Cloud Public (TCP)" and a verb, as its first words
    up to the service that covers it: "offers managed relational databases". Empty for any other statement."""
    m = _AREA_RE.match(statement)
    if not m:
        return ""
    rest = m.group(1)
    end = _AREA_END.search(rest)
    return " ".join((rest[:end.start()] if end else rest).split())


def area_facts(records: list[dict]) -> list[tuple[str, str]]:
    """(area, id) of every stable, positive fact that states a requirement area, sorted by area."""
    out = []
    for r in records:
        if r.get("class") == "stable" and not r.get("negative"):
            area = area_of(r["statement"])
            if area:
                out.append((area, r["id"]))
    return sorted(out)


def _sections(services: list[offered.Service]) -> list[str]:
    out: list[str] = []
    for s in services:
        if s.area not in out:
            out.append(s.area)
    return out


def coverage_blocks(records: list[dict], services: list[offered.Service], revision: str) -> list[tuple[str, str]]:
    """The section "What the facts cover", counted from the facts and the services of the build."""
    plain = plain_service_facts(records, services)
    have = [s for s in services if s.number in plain]
    blocks = [("h2", COVER_HEADING),
              ("p", "%d of the %d services orderable per the service description of %s have at least one plain fact "
                    "that says what the service is and does: a statement that opens with the name of the service. "
                    "By section of the service description:" % (len(have), len(services), revision))]
    for area in _sections(services):
        members = [s for s in services if s.area == area]
        blocks.append(("p", "- %s: %d of %d" % (area, len([s for s in members if s.number in plain]), len(members))))
    missing = [s for s in services if s.number not in plain]
    if missing and have:
        blocks.append(("p", "Without such a fact: %s." % ", ".join("%s (%s)" % (s.name, s.number) for s in missing)))
    areas = area_facts(records)
    if areas:
        blocks.append(("p", "%d facts state what the platform covers for a requirement area. Each opens with \"%s\" "
                            "and names the services that cover it:" % (len(areas), AREA_OPENING.strip())))
        blocks += [("p", "- %s (%s)" % a) for a in areas]
    if areas:
        example, cited = "TCP " + areas[0][0], "(%s) for this one" % areas[0][1]
    elif have:
        example = "TCP offers %s" % have[0].name
        cited = "(%s) for this one" % plain[have[0].number][0]
    else:
        example, cited = "<the requirement>", ""
    blocks += [("p", "Example: to check whether TCP covers a requirement, attach facts.md and ask:"),
               ("code", "Use the attached TCP Facts. Answer: is this requirement covered: \"%s\"? Cite the id of "
                        "every fact that backs the answer." % example),
               ("p", "A good answer quotes the fact and its id%s, so a reader finds it in facts.md. When no fact "
                     "covers the requirement it says \"no checked fact covers this\"." % (", for example " + cited
                                                                                       if cited else "")),
               ("p", "What the dataset does not hold and never will: commitments on %s and %s. These are contract "
                     "matters: check them in the contract. A fact that touches one of them is background from a "
                     "public source, not a commitment." % (", ".join(CONTRACT_MATTERS[:-1]), CONTRACT_MATTERS[-1]))]
    return blocks


def whats_new(previous: list[dict], records: list[dict], services: list[offered.Service], since: str) -> str:
    """The changes since the previous published build: facts added, amended (same id, changed statement or source)
    and retired, and the requirement areas and service sections the added facts touch."""
    added, amended, retired = changes(previous, records)
    text = "## What is new since %s\n\n%d facts added, %d amended (same id, changed statement or source), %d retired." \
           % (since, len(added), len(amended), len(retired))
    areas = sorted({area_of(r["statement"]) for r in added} - {""})
    if areas:
        text += "\n\nRequirement areas of the added facts: %s." % "; ".join(areas)
    forms = _forms(services)
    touched = collections.Counter()
    for r in added:
        s = service_named(r["statement"], services, forms)
        if s is not None:
            touched[s.area] += 1
    if touched:
        text += "\n\nService sections of the added facts: %s." % ", ".join(
            "%s (%d)" % (a, touched[a]) for a in _sections(services) if a in touched)
    return text


def sources_text(n_docs: int, n_mirror: int) -> str:
    """Where the facts come from: the documentation facts from the docs mirror (counted by their source line), the
    sources never used and the tenants of the live checks."""
    return ("%d of the %d documentation facts come from the open source repositories of the GitHub organisation %s, "
            "published under the %s. The public source of each of the others is in facts.jsonl. The documentation "
            "website is not crawled, its robots.txt disallows all robots. Sources whose terms forbid automated "
            "analysis are not used, so the community portal and internal documentation are left out. The live checks "
            "ran on the compiler's own TCP test tenants." % (n_mirror, n_docs, DOCS_ORG, DOCS_LICENCE))


def mirror_count(records: list[dict]) -> int:
    """The documentation facts whose source line names the docs mirror."""
    return sum(1 for r in records if r.get("grade") == "docs" and _MIRROR_SOURCE_RE.search(r.get("source", "")))


def _how_to_blocks(day: str, best_before: str, n_facts: int, grades: dict, n_services: int, revision: str,
                   cover: list[tuple[str, str]] = (), n_mirror: int = 0,
                   check: list[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    """The how-to as (style, text) blocks: h1, h2, p, code (code:json, code:bash for the fence). PROMPT.md and
    HOW-TO.pdf are made from it."""
    return [
        ("h1", "%s %s: how to use it" % (NAME, day)),
        ("p", "%d checked facts about T Cloud Public (TCP) with the list of "
              "orderable services. Made to be dropped into any AI assistant. Prices are not part of the dataset: "
              "they come live from the public price API with --live." % n_facts),
        ("p", "%d facts were tested live on a TCP tenant, %d come from the vendor documentation and %d from the "
              "service description (revision %s). No fact from hearsay, no expired fact, no customer, no project, "
              "no credential." % (grades.get("live", 0), grades.get("docs", 0), grades.get("contract", 0), revision)),
        ("p", sources_text(grades.get("docs", 0), n_mirror)),
        ("h2", "Three steps"),
        ("p", "1. Take facts.md (all facts, one line each). For one topic only, take its file from topics/."),
        ("p", "2. Attach the file to your assistant: ChatGPT, Claude, Gemini, Copilot or a local model. In a "
              "ChatGPT or Claude project the file stays attached for every chat."),
        ("p", "3. Type the prompt and your question. The file carries its own rules in its first lines, so the "
              "prompt stays short:"),
        ("code", ONE_LINER),
        ("p", "If the assistant ignores the rules in the file, paste this once before the question:"),
        ("code", LONG_PROMPT),
        ("h2", "Examples"),
        *[("code", e) for e in EXAMPLES],
        *cover,
        *WARNING,
        *MCP_CLIENTS,
        ("h2", "Files"),
        ("p", "facts.md: all facts, grouped by topic. facts.jsonl: the same facts with source, tags, class, expiry "
              "and the evidence of negatives, for RAG and tools. topics/<topic>.md: one file per topic. "
              "services.md: the %d services orderable per the service description of %s. %s: a read-only MCP server over these files for Claude, VS Code or any MCP client; its first "
              "lines say how to add it. MANIFEST.json: counts, dates and the sha256 of every file."
              % (n_services, revision, MCP_FILE)),
        *check,
        ("h2", "Dates and licence"),
        ("p", "Built %s. Best before %s: facts about availability are re-checked every %d days. Prices are not "
              "part of the dataset: the live price API is the source of truth before any quote." % (day, best_before,
                                                                                         BEST_BEFORE_DAYS)),
        ("p", "Dataset: %s." % COMPILED_BY),
        ("p", CREDIT),
        ("p", SOURCE_ATTRIBUTION),
        ("p", LICENCE_LINE),
    ]


def blocks_to_markdown(blocks: list[tuple[str, str]]) -> str:
    out = []
    for style, text in blocks:
        if style == "h1":
            out.append("# " + text)
        elif style == "h2":
            out.append("\n## " + text)
        elif style.startswith("code"):         # code or code:<language> for the fence
            out.append("```%s\n%s\n```" % (style[5:], text))
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
        font, size, lead, width = _STYLES[style.split(":")[0]]
        wrapped = textwrap.wrap(text, width, break_on_hyphens=False) or [""]   # a link stays whole on its line
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
                                                     _pdf_escape(COMPILED_BY).encode("latin-1")))
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


def service_line(s: offered.Service) -> str:
    """One line of services.md: the section, the name, its other names, the chapter when it is not "offered" and the
    date from which the service description marks it no longer available or bookable."""
    other = [a for a in s.aliases if a not in s.name]
    return "- %s %s%s%s%s" % (s.number, s.name, " (%s)" % ", ".join(other) if other else "",
                              "" if s.status == "offered" else ", %s" % s.status,
                              "; no longer %s from %s" % (s.end_kind, offered._dmy(s.ends)) if s.ends else "")


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
    # task KS: a fact leaves only when something invalidates it, never for its date. An expired fact stays with
    # recheck_due; retired (and so superseded) facts never come out of the export, a refuted one is left out here
    # and one that names a service the service description no longer offers by facts_check below.
    records, left = kb.export(p, today=today, include_expired=True)
    refutes = recheck.refuted()
    left["contradicted by a recheck"] = 0
    kept = []
    for r in records:
        if refutes.get(r["id"], "") >= r["checked"]:
            left["contradicted by a recheck"] += 1
            continue
        r["recheck_due"] = bool(r.get("expired"))
        kept.append(r)
    records = kept
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
    try:
        return _write(root, p, day, best_before, fetched, records, flagged, left, grades, bytag, tags, catalog, revision,
                      snapshots, counts, live, sample)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)      # a refused build leaves no half-written folder behind
        raise


def _write(root: Path, p: config.Paths, day: str, best_before: str, fetched: str, records: list[dict], flagged,
           left: dict, grades: dict, bytag: dict, tags: list[str], catalog, revision: str, snapshots: dict,
           counts: dict, live: bool, sample: int) -> Built:
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

    orderable = [s for s in catalog.services if not s.ends or s.ends > day]
    svc = [service_line(s) for s in orderable]
    gone = ["- %s%s" % (row[0], " (%s)" % ", ".join(row[1:]) if row[1:] else "") for row in catalog.not_offered]
    (root / "services.md").write_text(
        head + "# Services orderable on TCP\n\nFrom the T Cloud Public service description, revision %s. %d "
        "services. A service that is not in this list is not offered, whatever the documentation says.\n\n%s\n"
        % (revision, len(svc), "\n".join(svc))
        + ("\n## Not offered\n\nThe documentation or the price API still knows these, the service description does "
           "not offer them.\n\n%s\n" % "\n".join(gone) if gone else ""), encoding="utf-8")

    for region, (meta, recs) in snapshots.items():
        # only the live price check reads these rows; none of them is written into the folder
        keep = [rec for rec in recs
                if not not_offered(catalog, day, rec.raw.get("productIdParameter", ""), rec.raw.get("productId", ""))]
        kept_rows[region] = keep
        counts[region] = len(keep)

    check_lines = price_check(kept_rows, sample=sample, seed=day) if live and kept_rows else \
        ["live price check skipped"]
    named = operator_named(records)
    check_lines.append("operator named in prose: %s" % (", ".join(named) if named else "none"))
    cover = coverage_blocks(records, orderable, revision)
    blocks = _how_to_blocks(day, best_before, len(records), grades, len(svc), revision, cover,
                            mirror_count(records), check_blocks(sign_settings()))
    (root / "PROMPT.md").write_text(blocks_to_markdown(blocks), encoding="utf-8")
    (root / "HOW-TO.pdf").write_bytes(pdf_bytes(blocks, "%s %s: how to use it" % (NAME, day)))
    (root / "README.md").write_text(blocks_to_markdown(
        readme_header(day, best_before, len(records)) + [("p", "Build of %s. A later build of the same day replaces this one under the same "
                                              "name; the manifest carries the build time." % built_at)]
        + blocks[1:4] + [("h2", "Use"), ("p", "Attach facts.md and type:"),
                                                          ("code", ONE_LINER), ("p", "PROMPT.md has the rest.")]
        + cover + recheck_due_blocks(records) + WARNING + MCP_CLIENTS + blocks[blocks.index(("h2", "Files")):]), encoding="utf-8")

    shutil.copyfile(MCP_TEMPLATE, root / MCP_FILE)
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".md", ".jsonl", ".csv", ".py"):
            text = path.read_text(encoding="utf-8")
            if _LEFT_RE.search(text):
                raise DatasetError("a tenant alias was left in %s; the folder was removed" % path.name)
            if _TOOL_LEFT_RE.search(text):
                raise DatasetError("a name of the tooling was left in %s; the folder was removed" % path.name)

    files = sorted(x for x in root.rglob("*") if x.is_file())
    manifest = {"dataset": NAME, "date": day, "built_at": built_at, "best_before": best_before, "facts": len(records),
                "grades": grades, "recheck_due": sum(1 for r in records if r.get("recheck_due")),
                "topics": {t: len(bytag[t]) for t in tags}, "services": len(svc), "service_description": revision,
                                "facts_left_out": [{"id": i, "names": n} for i, n in flagged], "licence": LICENCE, "compiled_by": COMPILED_BY,
                "files": {str(x.relative_to(root)): {"bytes": x.stat().st_size, "sha256": _sha(x)} for x in files}}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    zpath = root.parent / (root.name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for x in sorted(root.rglob("*")):
            if x.is_file():
                z.write(x, "%s/%s" % (root.name, x.relative_to(root)))
    pack_mcpb(root, day, best_before, len(records))
    built = Built(root, zpath, day, len(records), grades, len(tags), len(svc), counts, left)
    built.lines = ["%s: %d facts (%s), %d topics, %d services of revision %s, prices %s"
                   % (root.name, len(records), ", ".join("%s %d" % kv for kv in sorted(grades.items())), len(tags),
                      len(svc), revision, ", ".join("%s %d" % kv for kv in counts.items()) or "none"),
                   "left out: " + ", ".join("%s %d" % kv for kv in left.items() if kv[1])
                   + ("; facts naming a service not offered: %s" % ", ".join("%s (%s)" % (i, ", ".join(n)) for i, n in flagged)
                      if flagged else ""),
                   "coverage: %d of %d services have a plain fact, %d requirement area facts"
                   % (len(plain_service_facts(records, orderable)), len(orderable), len(area_facts(records))),
                   *check_lines,
                   "zip %s, %d bytes, built %s, best before %s" % (zpath.name, zpath.stat().st_size, built_at,
                                                                   best_before),
                   "extension %s.mcpb, one click in Claude Desktop" % root.name]
    return built


def mcpb_manifest(day: str, best_before: str, facts: int) -> dict:
    """manifest.json of the MCP Bundle: the server file at the root and the data files beside it, so the server
    finds its dataset folder where it lies."""
    version = ".".join(str(int(x)) for x in day.split("-"))          # semver allows no leading zero
    entry = {"command": "python3", "args": ["${__dirname}/" + MCP_FILE]}
    return {"manifest_version": MCPB_SPEC, "name": SLUG, "display_name": NAME, "version": version,
            "description": "%d checked facts about T Cloud Public as a read-only MCP server. Best before %s."
                           % (facts, best_before),
            "long_description": "Four read-only tools over the dataset %s of %s: facts, offered or not, live prices "
                                "(only with --live) and exact arithmetic. No network without --live, nothing "
                                "written."
                                % (NAME, day),
            "author": {"name": NAME}, "repository": {"type": "git", "url": "https://github.com/%s.git" % GITHUB_REPO},
            "homepage": "https://github.com/%s" % GITHUB_REPO, "license": MCPB_LICENCE,
            "icon": MCPB_ICON,
            "server": {"type": "python", "entry_point": MCP_FILE,
                       "mcp_config": {**entry, "platform_overrides": {"win32": {"command": "python",
                                                                                 "args": entry["args"]}}}},
            "tools": [{"name": n, "description": d} for n, d in MCPB_TOOLS],
            "keywords": ["t-cloud-public", "facts"],
            "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.10"}}}


def icon_png() -> bytes:
    """64 by 64 PNG, two flat colours (a light disc on a dark square), deterministic bytes."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    dark, light = b"\x1f\x3a\x5f", b"\xf2\xb7\x05"
    rows = b"".join(b"\x00" + b"".join(light if (x - 31.5) ** 2 + (y - 31.5) ** 2 <= 20 ** 2 else dark
                                       for x in range(64)) for y in range(64))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def pack_mcpb(root: Path, day: str, best_before: str, facts: int) -> Path:
    """tcp-facts-<date>.mcpb beside the folder: a zip with manifest.json, the server and the data files. Its copy
    tcp-facts.mcpb (the same bytes) goes into the folder, the release asset with a stable name."""
    path = root.parent / (root.name + ".mcpb")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(mcpb_manifest(day, best_before, facts), indent=2) + "\n")
        z.writestr(MCPB_ICON, icon_png())
        for x in sorted(root.rglob("*")):
            if x.is_file() and x.name not in MCPB_SKIP:
                z.write(x, x.relative_to(root).as_posix())
    shutil.copyfile(path, root / kbreview.STABLE_MCPB)
    return path


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


# --------------------------------------------------------------------------- the public repository (owner side)

# into the root, README.md over the one there; LICENSE is the repository's own, the put never touches it
GITHUB_FILES = ("README.md", "facts.md", "facts.jsonl", "services.md", "topics", MCP_FILE)
STALE_PRICES = "prices"                     # a folder of an older release; the put removes it from the repository
GITHUB_ASSETS = ("HOW-TO.pdf", "MANIFEST.json")                                    # with the zip, on the release
PERSON_ENV = "AWB_DATASET_AUTHOR"           # the old settings: gone, but a value still set is searched for
PERSON_CONF = "dataset_author"
GIT_TIMEOUT = 120


SIGN_CONF = "dataset_sign"                      # yes: sign the release tag and SHA256SUMS (host file)
SIGN_KEY_CONF = "dataset_sign_key"              # path of the ssh signing key
SIGN_IDENTITY_CONF = "dataset_sign_identity"    # the identity in the allowed_signers file
SIGN_SIGNERS_CONF = "dataset_sign_allowed_signers"   # path of that file as a reader writes it; default below
SIGN_SIGNERS_DEFAULT = "allowed_signers"
SUMS_FILE = "SHA256SUMS"


def sign_settings() -> dict[str, str] | None:
    """None unless the host file says dataset_sign = yes. Then the key, the identity and the allowed_signers path;
    refused when the key path is missing."""
    conf = config.host_conf()
    if conf.get(SIGN_CONF, "").lower() != "yes":
        return None
    key = conf.get(SIGN_KEY_CONF, "")
    if not key:
        raise DatasetError("dataset_sign = yes needs dataset_sign_key = <path of the signing key> in the host file")
    return {"key": key, "identity": conf.get(SIGN_IDENTITY_CONF, "") or NAME,
            "signers": conf.get(SIGN_SIGNERS_CONF, "") or SIGN_SIGNERS_DEFAULT}


def check_text(sign: dict[str, str]) -> str:
    """The block "check a download": the sums, then the signature of the sums file."""
    return ("sha256sum -c %s\nssh-keygen -Y verify -f %s -I %s -n file -s %s.sig < %s"
            % (SUMS_FILE, sign["signers"], sign["identity"], SUMS_FILE, SUMS_FILE))


def check_blocks(sign: dict[str, str] | None) -> list[tuple[str, str]]:
    if not sign:
        return []
    return [("h2", "Check a download"),
            ("p", "The release holds %s with the sha256 of every asset and %s.sig, its signature. Download both "
                  "next to the assets and run:" % (SUMS_FILE, SUMS_FILE)),
            ("code", check_text(sign))]


def clone_dir(p: config.Paths) -> Path:
    return datasets_dir(p) / (SLUG + ".git")


# K1 (review of 2026-10-20): the clone lies in the work user's tree, so no setting of the clone may run a program as
# the owner: no hooks, no fsmonitor, and the signing program is ssh-keygen whatever the clone's config says. Every git
# call of _run gets them through GIT_CONFIG_COUNT, the scope of `git -c` above every config file.
GIT_FIXED = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"), ("gpg.ssh.program", "ssh-keygen"))


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_KEY_")
           and not k.startswith("GIT_CONFIG_VALUE_")}
    env["GIT_CONFIG_COUNT"] = str(len(GIT_FIXED))
    for i, (key, value) in enumerate(GIT_FIXED):
        env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i] = key, value
    return env


def _git(clone: Path, *args: str) -> list[str]:
    """A git command on the clone that names the clone as a safe directory, so git needs no global setting; only that
    path, never every directory. _run adds the settings of GIT_FIXED."""
    return ["git", "-c", "safe.directory=%s" % Path(clone).resolve(), *args]


def check_clone(clone: Path) -> None:
    """Refused unless the clone, its .git and everything in .git belong to this user, are no link and are writable
    by nobody else: the owner runs git and the signing key there, the work user may write the folder around it."""
    uid = os.geteuid()
    git = clone / ".git"
    entries = [clone, git]
    if git.is_dir() and not git.is_symlink():
        for root, dirs, files in os.walk(git):                  # does not follow links: a link is listed and refused
            entries += [Path(root) / n for n in dirs + files]
    for x in entries:
        try:
            st = os.lstat(x)
        except OSError:
            st = None
        if (st is None or stat.S_ISLNK(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022
                or (x == git and not stat.S_ISDIR(st.st_mode))):
            raise DatasetError("the clone %s is not yours alone (a link, another owner or writable by others); "
                               "nothing was published: remove it, the next put clones it again" % clone.name)


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """One git or gh call. A failure names the command and its exit code, never its output."""
    shown = args[3:] if args[1:2] == ["-c"] else args[1:]          # the safe.directory setting names a path
    try:
        # umask 022: what git writes into the clone stays writable by the owner alone (check_clone), whatever the
        # owner's own umask
        res = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT, umask=0o022,
                             env=_git_env() if args[0] in ("git", "gh") else None)
    except FileNotFoundError:
        raise DatasetError("%s is not installed" % args[0]) from None
    except subprocess.TimeoutExpired:
        raise DatasetError("%s %s took longer than %d seconds" % (args[0], shown[0], GIT_TIMEOUT)) from None
    if check and res.returncode != 0:
        raise DatasetError("%s %s failed (exit %d)" % (args[0], " ".join(shown[:2]), res.returncode))
    return res


def person_values() -> list[str]:
    """Every value that could carry a person into the files: the old author settings, should one still be set,
    and the commit identity of the user and of the checkout this code runs from."""
    out = [os.environ.get(PERSON_ENV, ""), config.host_conf().get(PERSON_CONF, "")]
    for key in ("user.name", "user.email"):
        out.append(_run(["git", "config", "--global", key], check=False).stdout)
        out.append(_run(["git", "config", key], cwd=Path(__file__).resolve().parent, check=False).stdout)
    return sorted({v.strip() for v in out if v and v.strip()})


def _account_identity(clone: Path) -> None:
    """The commits of the public repository carry the GitHub account and its noreply address, never a person."""
    login = _run(["gh", "api", "user", "-q", ".login"], cwd=clone).stdout.strip()
    uid = _run(["gh", "api", "user", "-q", ".id"], cwd=clone).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or not uid.isdigit():
        raise DatasetError("gh api user names no account; run gh auth login")
    _run(_git(clone, "config", "user.name", login), cwd=clone)
    _run(_git(clone, "config", "user.email", "%s+%s@users.noreply.github.com" % (uid, login)), cwd=clone)


def _person_re(values: list[str]) -> "re.Pattern[str] | None":
    words = set(values)
    for v in values:
        if "@" not in v:
            words.update(w for w in re.split(r"\s+", v) if len(w) >= 4)
    return re.compile("|".join(r"(?<!\w)%s(?!\w)" % re.escape(w) for w in sorted(words)), re.I) if words else None


def scan_people(files: list[Path], values: list[str], register_path: Path | None) -> list[str]:
    """Why `files` may not go public: a person value of `person_values` or a registered name in any of them, as
    messages that name the file and the class, never the value."""
    rx = _person_re(values)
    problems = []
    for f in files:
        if rx is not None and rx.search(f.read_bytes().decode("utf-8", "replace")):
            problems.append("%s: a person (commit identity or an old author setting)" % f.name)
    try:
        findings = gate.scan_files(files, register_path)
    except register.RegisterError as err:   # check.CheckUnavailable included
        raise DatasetError("the name check cannot run (%s); nothing was published" % type(err).__name__) from None
    problems += sorted({"%s: %s" % (Path(f.file).name, f.cls) for f in findings if f.cls in ("name", "blocklist",
                                                                                          "homepath")})
    return problems


def release_notes(manifest: dict, sums: dict[str, str] | None = None, new: str = "",
                  sign: dict[str, str] | None = None) -> str:
    """The counts, what is new since the previous release (`new`, from `whats_new`, empty for the first release),
    then the sha256 of each release asset, one line each. `sums` maps an asset name to its sum; the zip and
    MANIFEST.json come from the files (the manifest is inside the zip and cannot hold its own sum), every other
    asset from the manifest."""
    g = manifest.get("grades", {})
    text = ("%s %s: %d facts (live %d, docs %d, contract %d), %d left out because they name a service that is not "
            "offered. Best before %s.\n\n%s%s" % (NAME, manifest["date"], manifest["facts"], g.get("live", 0),
                                                   g.get("docs", 0), g.get("contract", 0),
                                                   len(manifest.get("facts_left_out", [])), manifest["best_before"],
                                                   new + "\n\n" if new else "", CREDIT))
    sums = dict(sums or {})
    for name in GITHUB_ASSETS:
        if name in manifest.get("files", {}):
            sums[name] = manifest["files"][name]["sha256"]
    names = sorted(sums, key=lambda n: (n in GITHUB_ASSETS, GITHUB_ASSETS.index(n) if n in GITHUB_ASSETS else 0))
    if names:
        text += "\n\nsha256 of the assets, check a download with `sha256sum -c`:\n\n```\n%s```" % "".join(
            "%s  %s\n" % (sums[n], n) for n in names)
    if sign:
        text += "\n\nCheck a download (%s and %s.sig are release assets):\n\n```\n%s\n```" % (
            SUMS_FILE, SUMS_FILE, check_text(sign))
    return text


def previous_release(clone: Path, day: str) -> tuple[str, list[dict]] | None:
    """The newest release tag before `day` in the clone and the facts of its facts.jsonl; None when there is no
    earlier release or it holds no facts.jsonl."""
    tags = [t for t in _run(_git(clone, "tag", "--list"), cwd=clone).stdout.split()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) and t < day]
    if not tags:
        return None
    prev = max(tags)
    res = _run(_git(clone, "show", "refs/tags/%s:facts.jsonl" % prev), cwd=clone, check=False)
    if res.returncode != 0:
        return None
    return prev, read_facts(res.stdout)


def _sign_sums(folder: Path, zpath: Path, sign: dict[str, str], pinned: dict[str, str] | None = None) -> list[Path]:
    """SHA256SUMS (one line per release asset) and its signature into the release folder; both come back. With
    `pinned` (the asset sums of the review mark) refused when an asset differs from what the review pinned, so the
    key vouches only for the files the owner reviewed (K3 of the review of 2026-10-20). The sums file is a new file,
    never written through a link."""
    mpath = folder.parent / (folder.name + ".mcpb")
    if not mpath.is_file():
        raise DatasetError("the folder of %s has no %s; run awb dataset build --force" % (folder.name, mpath.name))
    stable = folder / kbreview.STABLE_MCPB
    if not stable.is_file() or _sha(stable) != _sha(mpath):
        raise DatasetError("the folder of %s has no %s equal to %s; run awb dataset build --force"
                           % (folder.name, stable.name, mpath.name))
    assets = kbreview.release_assets(folder)
    sums = [(_sha(a), a.name) for a in assets]
    if pinned is not None and any(pinned.get(name) != sha for sha, name in sums):
        raise DatasetError("an asset of %s changed since the review; nothing was signed, %s"
                           % (folder.name, kbreview.REVIEW_HINT.replace("first", "again")))
    path = folder / SUMS_FILE
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join("%s  %s\n" % pair for pair in sums))
    (folder / (SUMS_FILE + ".sig")).unlink(missing_ok=True)
    _run(["ssh-keygen", "-Y", "sign", "-f", sign["key"], "-n", "file", SUMS_FILE], cwd=folder)
    if not (folder / (SUMS_FILE + ".sig")).is_file():
        raise DatasetError("ssh-keygen wrote no %s.sig" % SUMS_FILE)
    return [folder / SUMS_FILE, folder / (SUMS_FILE + ".sig")]


def _remote_ref(clone: Path) -> str:
    branch = _run(_git(clone, "symbolic-ref", "--short", "HEAD"), cwd=clone).stdout.strip()
    return "origin/" + branch


def sync_clone(clone: Path) -> bool:
    """Fetch the remote fresh (branches and tags, the tags as the remote has them now) and bring the clone to the
    remote branch: a fast-forward when the clone is behind or level, a hard reset to the remote branch when the
    local history diverged (the owner rewrote the published history). Returns True when it had to reset. Nothing
    of an old local history survives to be committed on or pushed back."""
    _run(_git(clone, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*",
              "--prune"), cwd=clone)
    ref = _remote_ref(clone)
    ahead_ok = _run(_git(clone, "merge-base", "--is-ancestor", "HEAD", ref), cwd=clone, check=False).returncode == 0
    if ahead_ok:
        _run(_git(clone, "merge", "-q", "--ff-only", ref), cwd=clone)
    else:
        _run(_git(clone, "reset", "-q", "--hard", ref), cwd=clone)
    _run(_git(clone, "clean", "-fdq"), cwd=clone)
    return not ahead_ok


def _push_branch(clone: Path) -> None:
    """Push HEAD to the remote branch, never forced: fetch again, refuse unless the remote branch is an ancestor of
    HEAD (a non-fast-forward push), then a plain push."""
    _run(_git(clone, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"), cwd=clone)
    if _run(_git(clone, "merge-base", "--is-ancestor", _remote_ref(clone), "HEAD"), cwd=clone,
            check=False).returncode:
        raise DatasetError("the published history moved or was rewritten since the clone was synced; nothing was "
                           "pushed: run the put again")
    _run(_git(clone, "push", "-q", "origin", "HEAD"), cwd=clone)


PUBLIC_STATEMENTS = (("unofficial", re.compile(r"(?i)\bunofficial\b.{0,200}\bnot a product of\b", re.S)),
                     ("public sources and robots.txt",
                      re.compile(r"(?i)\bonly from public sources\b.{0,300}\brobots\.txt\b", re.S)))
"""The two statements every public README carries at its top (the owner's rule of 2026-10-22): it is not an official
product, and it is built only from public sources with respect for robots.txt and similar restrictions."""


def public_statements_missing(readme: str, head: int = 3000) -> list[str]:
    """The names of the public statements missing from the first `head` characters of a README."""
    top = readme[:head]
    return [name for name, rx in PUBLIC_STATEMENTS if not rx.search(top)]


def github_put(p: config.Paths, folder: Path, *, replace: bool = False, clone: Path | None = None,
               pinned: dict[str, str] | None = None) -> list[str]:
    """The files of a build into the root of the public repository, committed as the GitHub account, and a release
    of its date with the zip, the how-to PDF and the manifest. Refused when that date is tagged already unless `replace`, and when a person
    or a registered name is in any file that would go public."""
    day = folder.name[len(SLUG) + 1:]
    zpath = folder.parent / (folder.name + ".zip")
    if not (zpath.is_file() and all((folder / n).exists() for n in GITHUB_ASSETS + GITHUB_FILES)):
        raise DatasetError("the folder of %s is not a complete build; run awb dataset build" % day)
    missing = public_statements_missing((folder / "README.md").read_text(encoding="utf-8"))
    if missing:
        raise DatasetError("the README of %s lacks the statement %s; nothing was published" % (day, " and ".join(missing)))
    clone = clone or clone_dir(p)
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        _run(["gh", "repo", "clone", GITHUB_REPO, str(clone)])
        check_clone(clone)
    else:
        check_clone(clone)
        sync_clone(clone)
    _account_identity(clone)
    tagged = bool(_run(_git(clone, "tag", "--list", day), cwd=clone).stdout.strip())
    if tagged and not replace:
        raise DatasetError("the repository holds the dataset of %s already; give --replace" % day)
    sign = sign_settings()
    if sign and SUMS_FILE + ".sig" not in (folder / "PROMPT.md").read_text(encoding="utf-8"):
        raise DatasetError("the how-to of %s has no \"check a download\" block; run awb dataset build --force" % day)
    prev = previous_release(clone, day)            # read before the files of this build land in the clone
    stale = (clone / STALE_PRICES).exists() or bool(
        _run(_git(clone, "ls-files", "--", STALE_PRICES), cwd=clone).stdout.strip())
    if stale:
        _run(_git(clone, "rm", "-r", "-q", "-f", "--ignore-unmatch", "--", STALE_PRICES), cwd=clone)
        shutil.rmtree(clone / STALE_PRICES, ignore_errors=True)
    for name in GITHUB_FILES:
        dst = clone / name
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if (folder / name).is_dir():
            shutil.copytree(folder / name, dst)
        else:
            shutil.copy2(folder / name, dst)
    public = sorted(x for x in clone.rglob("*") if x.is_file() and ".git" not in x.relative_to(clone).parts)
    public += [folder / n for n in ("PROMPT.md", "MANIFEST.json")]    # inside the zip and the release
    problems = scan_people(public, person_values(), p.register)
    if problems:
        _run(_git(clone, "checkout", "--", "."), cwd=clone, check=False)
        _run(_git(clone, "clean", "-fdq"), cwd=clone, check=False)
        raise DatasetError("refused, nothing was published: " + "; ".join(problems))
    _run(_git(clone, "add", "-A", "--", *GITHUB_FILES), cwd=clone)
    if _run(_git(clone, "diff", "--cached", "--quiet"), cwd=clone, check=False).returncode:
        _run(_git(clone, "commit", "-q", "-m", "%s %s%s" % (
            NAME, day, "; removed prices/ (the dataset holds no price list)" if stale else "")), cwd=clone)
    signed: list[Path] = []
    check_clone(clone)                                             # again before the key and the push
    if sign:
        signed = _sign_sums(folder, zpath, sign, pinned)
        tag = ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + sign["key"], "tag", "-s", "-m", "%s %s" % (NAME, day)]
        _run(_git(clone, *tag, *(["-f"] if replace else []), day), cwd=clone)
    else:
        _run(_git(clone, "tag", "-f", day) if replace else _git(clone, "tag", day), cwd=clone)
    _push_branch(clone)
    if tagged:
        _run(["gh", "release", "delete", day, "--repo", GITHUB_REPO, "--yes"], cwd=clone, check=False)
        _run(_git(clone, "push", "-q", "origin", ":refs/tags/" + day), cwd=clone, check=False)   # a tag moves by delete and push, never --force
    _run(_git(clone, "push", "-q", "origin", "refs/tags/" + day), cwd=clone)
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    if _sha(folder / "HOW-TO.pdf") != manifest["files"]["HOW-TO.pdf"]["sha256"]:
        raise DatasetError("HOW-TO.pdf differs from its sum in MANIFEST.json; run awb dataset build --force")
    mpath = folder.parent / (folder.name + ".mcpb")
    if not mpath.is_file():
        raise DatasetError("the folder of %s has no %s; run awb dataset build --force" % (day, mpath.name))
    stable = folder / kbreview.STABLE_MCPB
    if not stable.is_file() or _sha(stable) != _sha(mpath):
        raise DatasetError("the folder of %s has no %s equal to %s; run awb dataset build --force"
                           % (day, stable.name, mpath.name))
    sums = {zpath.name: _sha(zpath), "MANIFEST.json": _sha(folder / "MANIFEST.json")}
    new = ""
    if prev is not None:
        catalog = offered.load()
        new = whats_new(prev[1], read_facts(folder / "facts.jsonl"),
                        [s for s in catalog.services if not s.ends or s.ends > day], prev[0])
    _run(["gh", "release", "create", day, str(zpath), *(str(folder / n) for n in GITHUB_ASSETS), str(mpath), str(stable),
          *map(str, signed), "--repo",
          GITHUB_REPO, "--verify-tag", "--title", "%s %s" % (NAME, day), "--notes", release_notes(manifest, sums, new, sign)],
         cwd=clone)
    return ["github.com/%s commit and tag %s" % (GITHUB_REPO, day),
            "%s, the same bytes as %s, for %s" % (stable.name, mpath.name, MCPB_LATEST_URL),
            "github.com/%s/releases/tag/%s with %s" % (GITHUB_REPO, day, ", ".join([zpath.name, *GITHUB_ASSETS, mpath.name, *(x.name for x in signed)]))]


def put_folder(p: config.Paths, folder: Path, *, github: bool = False, github_only: bool = False,
               replace: bool = False) -> list[str]:
    """`awb dataset put` of one built folder: its review mark first, then the bucket (unless `github_only`), then
    the public repository (with `github` or `github_only`), then RELEASED.jsonl. The keys and pages it wrote."""
    client = None if github_only else bucket.client()
    mark = require_review(folder)               # G6: nothing leaves that the owner has not read
    keys = [] if github_only else put(client, folder, replace=replace)
    if github or github_only:
        keys += github_put(p, folder, replace=replace, pinned=mark["assets"])
    record_release(folder)
    return keys


_WEEKLY_ERRORS = (DatasetError, kbreview.ReviewError, kb.KBError, price.PriceError, obs.OBSError, bucket.BucketError,
                  ValueError)


def weekly(p: config.Paths, *, today: datetime.date | None = None, state: Path | None = None) -> int:
    """The weekly publish of the owner's timer (task KS, Monday 06:00 UTC after the Saturday recheck): awb dataset
    build of today (again over a folder of today), awb kb review of that folder (the owner's mark; refused for the
    work user as before) and awb dataset put --github. A step that fails stops the run: nothing after it runs, a
    failed build leaves no folder and the put keeps its own checks (the review mark, the two README statements, no
    price data, the person scan). The result goes into the state file that `awb owner status` reads: a failure is
    the item "dataset: weekly publish failed: <reason>", a success the note "dataset tcp-facts-<date>: N added, M
    amended, K retired" of the next 08:00 mail. Exit 0 published, 1 failed, 2 refused for the work user."""
    from awb import ownerwait

    if config.is_work_user():
        print("awb dataset: the weekly publish is the owner's; the work user may not review or publish",
              file=sys.stderr)
        return 2
    step = "build"
    try:
        built = build(p, today=today, force=True)
        for line in built.lines:
            print("awb dataset build: " + line)
        day = built.day
        step = "review"
        print("awb kb review: " + review(p, day=day)[0])
        mark = require_review(built.folder)
        step = "put"
        for key in put_folder(p, built.folder, github=True):
            print("awb dataset put: " + key)
    except _WEEKLY_ERRORS as err:
        reason = "%s: %s" % (step, err)
    except OSError as err:
        reason = "%s: %s (operating system error)" % (step, type(err).__name__)
    else:
        result = {"state": "published", "dataset": built.folder.name, "added": mark.get("added", 0),
                  "amended": mark.get("amended", 0), "retired": mark.get("retired", 0)}
        ownerwait.write_dataset_state(result, state)
        print("awb dataset weekly: " + ownerwait.dataset_line(result))
        return 0
    ownerwait.write_dataset_state({"state": "failed", "reason": reason}, state)
    print("awb dataset weekly: failed, %s" % reason, file=sys.stderr)
    return 1


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
    u.add_argument("--github", action="store_true", help="also commit, tag and release it in github.com/%s" % GITHUB_REPO)
    u.add_argument("--github-only", action="store_true",
                   help="only commit, tag and release it in github.com/%s, without the bucket" % GITHUB_REPO)
    sub.add_parser("weekly", help="build, review and put --github in one run, the owner's weekly timer (task KS)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    if args.command == "weekly":
        return weekly(p)
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
        if args.github and args.github_only:
            raise DatasetError("give --github (bucket and GitHub) or --github-only, not both")
        folder = latest(p, args.out, args.date)
        if folder is None:
            raise DatasetError("no built dataset%s; run awb dataset build" % (" of that date" if args.date else ""))
        keys = put_folder(p, folder, github=args.github, github_only=args.github_only, replace=args.replace)
        for key in keys:
            print("awb dataset put: " + key)
        return 0
    except (DatasetError, kbreview.ReviewError, kb.KBError, price.PriceError, obs.OBSError, bucket.BucketError,
            ValueError) as err:
        print("awb dataset: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb dataset: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
