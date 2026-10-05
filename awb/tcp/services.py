"""Offered services of T Cloud Public (TCP): what the latest service description lists, and nothing else.

    awb service check NAME...    offered or not, with the section and the revision; exit 1 when one is not offered
    awb service list             every service of the current revision with its section
    awb service update           rebuild rules/services.tsv from the mirror (owner side, after a new revision)

The service description is the source of truth for "is it offered". `awb mirror sd` keeps it under
<mirror root>/service-description/<revision>/ (CURRENT names the latest). Its chapter 3 lists the services: 3.1 to 3.9
by area, 3.11 the optional services, 3.12 the previews. A service that is not there is not offered, whatever the
documentation (GitHub), an API that still answers, a price record or a memory says. How a service works and what it
is made of comes from the docs, the API, the knowledge base and experience, for the services that are offered.

`awb service update` rebuilds rules/services.tsv from the current revision (the services with the short names the
text gives them and the "No longer available from" marks) and the block of service names in rules/stop-words.txt.
rules/services-not-offered.txt is kept by hand: the update only prints the documented services the service
description does not know, to check each before it goes there. Reading the lists, the check and the notes for the
chats and the review live in the core (awb/offered.py).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from awb import patterns
from awb.offered import (CATALOG, FILLER, NOT_OFFERED, Catalog, Service, Verdict, _dmy, check, key, load,  # noqa: F401
                         mentions, norm, note_for, prompt_lines, revision_label)

STOP_WORDS = patterns.RULES_DIR / "stop-words.txt"
BLOCK_START = "# --- the services of the T Cloud Public service description and the ones not offered (awb service update)"
BLOCK_END = "# --- end of the services"
STATUS = {"optional": "an optional service", "preview": "a preview", "offered": "offered"}
_TOC_RE = re.compile(r"^\s*(3\.(\d+)(?:\.(\d+))?)\s+(.+?)\s{2,}\d+\s*$", re.M)
_SPLIT_RE = re.compile(r"\s+and\s+")
# documentation folders that are no service of their own
_NOT_SERVICES = {"api-usage", "adr", "docsportal", "umn", "doc-exports", "otc-api", "otc-metadata", "templates"}

# short names the text of the service description does not write in brackets and initials cannot give
EXTRA = {"3.6.1": ["CES"], "3.4.2": ["EIP"], "3.4.7": ["NAT"], "3.5.2": ["APIG", "API Gateway"], "3.9.7": ["DBSS"],
         "3.9.10": ["CFW"], "3.8.1": ["MRS"], "3.4.8": ["VPCEP"], "3.1.2": ["DeH"], "3.7.2": ["SWR"],
         "3.6.9": ["EPS"], "3.7.4": ["UCS"], "3.7.1": ["CCE", "CCE Turbo"], "3.6.4": ["RMS"],
         "3.4.3": ["ELB", "Elastic Load Balancing"], "3.4.10": ["VPN"], "3.1.4": ["IMS"], "3.2.2": ["DCS"],
         "3.3.1": ["OBS"], "3.3.2": ["EVS"], "3.3.5": ["SFS", "SFS Turbo"], "3.4.1": ["VPC"], "3.6.5": ["IAM"],
         "3.6.6": ["LTS"], "3.6.2": ["CTS"], "3.5.3": ["SMN"], "3.5.1": ["DMS", "Kafka"], "3.9.2": ["KMS"],
         "3.9.8": ["HSS"], "3.8.2": ["DWS"], "3.8.4": ["DLI"], "3.2.4": ["CSS"], "3.2.5": ["DRS"],
         "3.2.8": ["DDM"], "3.4.9": ["ER"], "3.3.7": ["CBR"], "3.3.4": ["CSBS"], "3.3.3": ["VBS"],
         "3.3.6": ["SDRS"], "3.1.3": ["AS"], "3.7.3": ["ASM"], "3.6.3": ["TMS"], "3.6.7": ["RFS"],
         "3.5.4": ["AOM"], "3.5.5": ["APM"], "3.9.9": ["CSMS"], "3.4.4": ["DNS"], "3.4.5": ["DC"],
         "3.9.3": ["WAF Classic", "WAF"], "3.9.4": ["WAF Cloud", "WAF"], "3.9.5": ["WAF Dedicated", "WAF"]}
_WITHDRAWN_RE = re.compile(r"No longer (available|bookable) from (\d{2})\.(\d{2})\.(\d{4}):\s*(3\.\d+\.\d+)\s")




def _clean(title: str) -> str:
    return " ".join(title.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("‐", "-").split())


# --------------------------------------------------------------------------- building (owner side)


def parse(text: str) -> list[Service]:
    """The services of chapter 3 of a service description text (pdftotext -layout): the table of contents, the
    short names the text writes in brackets, initials and EXTRA, and the "No longer available from DATE:" marks."""
    services: list[Service] = []
    areas: dict[str, str] = {}
    for m in _TOC_RE.finditer(text[:80000]):
        number, area, sub, title = m.group(1), m.group(2), m.group(3), _clean(m.group(4))
        if sub is None:
            areas[area] = title
            continue
        if area == "10" or norm(title).startswith(("special conditions", "general provisions")):
            continue
        if any(s.number == number for s in services):
            continue
        status = "optional" if area == "11" else "preview" if area == "12" else "offered"
        services.append(Service(number, title, areas.get(area, ""), status, _aliases(title, text) + EXTRA.get(number, [])))
    flat = _clean(text)
    for m in _WITHDRAWN_RE.finditer(flat):
        for s in services:
            if s.number == m.group(5) and not s.ends:
                s.ends, s.end_kind = "%s-%s-%s" % (m.group(4), m.group(3), m.group(2)), m.group(1)
    initials: dict[str, list[Service]] = {}
    for s in services:
        short = _initials(re.sub(r"\s*\([^)]*\)", "", s.name))
        if len(short) >= 2:
            initials.setdefault(short.upper(), []).append(s)
    for short, owners in initials.items():
        for s in owners:
            if len(owners) == 1 and key(short) not in {key(f) for f in s.forms()}:
                s.aliases.append(short)
    for s in services:
        s.aliases = list(dict.fromkeys(a for a in s.aliases if key(a) != key(s.name)))
    return services


def _aliases(title: str, text: str) -> list[str]:
    """Other names of a service: the parts of "A and B", the name before "(dedicated)" or "(former X)" and X, and every
    short form the text writes after one of these names in brackets, such as "Elastic Cloud Server (ECS)"."""
    names = []
    former = re.search(r"\(former ([^)]+)\)", title)
    base = re.sub(r"\s*\([^)]*\)", "", title).strip()
    if base != title:
        names.append(base)
    if former:
        names.append(former.group(1).strip())
    parts = [p.strip() for p in _SPLIT_RE.split(base) if p.strip()]
    if len(parts) > 1 and all(len(p.split()) >= 2 for p in parts):     # two names, never "Modes and Metrics"
        names += parts
    flat = _clean(text)
    for name in [title, base] + names:
        for m in re.finditer(re.escape(name) + r"\s*\(([A-Z][A-Za-z0-9]{1,9})\)", flat):
            if sum(c.isupper() for c in m.group(1)) >= 2:      # a short name, never a word such as (Dedicated)
                names.append(m.group(1))
    return [n for n in dict.fromkeys(names) if n and n != title]


def _initials(name: str) -> str:
    words = [w for w in norm(name).split() if w not in FILLER]
    return "".join(w[0] for w in words) if len(words) > 1 else ""


def documented(docs: Path) -> list[str]:
    """The services the documentation mirror describes: one folder per service, its name in words."""
    if not docs.is_dir():
        return []
    return sorted(" ".join(w.capitalize() for w in d.name.split("-")) for d in docs.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and d.name not in _NOT_SERVICES)


def update(root: Path | None = None) -> tuple[Catalog, list[str]]:
    """Rebuild rules/services.tsv from the current revision of the mirror (owner side). The hand-kept list of
    services that are not offered is left as it is; the documented services the catalog does not know come back
    as candidates to check before one is added there."""
    from awb.tcp import mirror

    base = (root or mirror.root())
    sd = base / mirror.SD
    revision = (sd / mirror.CURRENT).read_text(encoding="utf-8").strip()
    text = (sd / revision / "service-description.txt").read_text(encoding="utf-8")
    services = parse(text)
    if len(services) < 20:
        raise SystemExit("awb service update: the service description gave only %d services; nothing was written"
                         % len(services))
    catalog = Catalog(revision, services, load().not_offered if CATALOG.exists() else [])
    lines = ["# The services of the T Cloud Public service description, revision %s (awb service update)." % revision,
             "# number\tname\tarea\tstatus\tother names\tno longer from\tno longer what"]
    lines += ["\t".join((s.number, s.name, s.area, s.status, " | ".join(s.aliases), s.ends, s.end_kind))
              for s in services]
    CATALOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_stop_block(catalog)
    known = {key(f) for row in catalog.not_offered for f in row}
    candidates = [d for d in documented(base / mirror.DOCS) if not catalog.find(d) and key(d) not in known]
    return catalog, candidates


def mirror_revision() -> str:
    """The revision CURRENT names in the mirror of the service description, or empty when there is none."""
    from awb.tcp import mirror

    sd = mirror.current_sd(mirror.root())
    return sd.parent.name if sd is not None else ""


def write_stop_block(catalog: Catalog) -> None:
    """The service names are product names, never a name of a customer or a person: the intake's list of
    words and phrases that are never a name candidate keeps them in a block of its own, rewritten here."""
    forms = {f for s in catalog.services for f in s.forms() if not f.isupper()}
    forms |= {f for row in catalog.not_offered for f in row if not f.isupper()}
    forms |= {" ".join(re.sub(r"[()]", " ", f).split()) for f in forms if "(" in f}     # words without brackets
    names = sorted(forms, key=str.lower)
    text = STOP_WORDS.read_text(encoding="utf-8") if STOP_WORDS.exists() else ""
    if BLOCK_START in text:
        head, _, rest = text.partition(BLOCK_START)
        tail = rest.partition(BLOCK_END)[2].lstrip("\n")
        text = head.rstrip("\n") + "\n" + (("\n" + tail) if tail.strip() else "")
    block = [BLOCK_START + ", revision %s" % catalog.revision] + names + [BLOCK_END]
    STOP_WORDS.write_text(text.rstrip("\n") + "\n" + "\n".join(block) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- command


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb service", description="Offered services: the latest service description, nothing else.")
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("check", help="offered or not, with the section and the revision")
    s.add_argument("names", nargs="+")
    sub.add_parser("list", help="every service of the current revision")
    sub.add_parser("update", help="rebuild rules/services.tsv from the mirror (owner side, after a new revision)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command == "check":
        catalog = load()
        verdicts = [check(n, catalog) for n in args.names]
        for v in verdicts:
            print(v.note)
        newer = mirror_revision()
        if newer and newer != catalog.revision:
            print("note: the mirror holds the service description of %s, the lists are of %s: the owner runs "
                  "awb service update before this answer counts" % (_dmy(newer), revision_label(catalog)))
        return 0 if all(v.offered for v in verdicts) else 1
    if args.command == "list":
        catalog = load()
        print("service description of %s: %d services" % (revision_label(catalog), len(catalog.services)))
        for s in catalog.services:
            state = "" if s.status == "offered" else s.status
            if s.ends:
                state = (state + " " if state else "") + "no longer %s from %s" % (s.end_kind, _dmy(s.ends))
            print("%-7s %-62s %s" % (s.number, s.name + (" (%s)" % ", ".join(s.aliases) if s.aliases else ""), state))
        return 0
    if args.command == "update":
        catalog, candidates = update()
        print("awb service: %d services of the revision %s written to rules/services.tsv"
              % (len(catalog.services), catalog.revision))
        if candidates:
            print("documented in the mirror, unknown to the service description (check each before adding it to "
                  "rules/services-not-offered.txt): %s" % "; ".join(candidates))
        return 0
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
