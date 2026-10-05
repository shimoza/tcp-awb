"""The services a platform offers: what its latest service description lists, and nothing else.

The lists live in the repository, so every process reads the same ones without reaching a mirror:
rules/services.tsv (the services of the current revision, with the short names the text gives them and the date
from which the text marks a service no longer available or bookable) and rules/services-not-offered.txt (services
that are not offered, kept by hand: the documentation or an API may still know them). `awb service update` on the
platform side (awb/tcp/services.py) rebuilds the first one from the mirror of the service description.

The chats get the list in their instructions and a note under an answer that names a service which is not offered;
the review refuses such a deliverable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from awb import patterns

CATALOG = patterns.RULES_DIR / "services.tsv"
NOT_OFFERED = patterns.RULES_DIR / "services-not-offered.txt"
FILLER = {"and", "for", "of", "the", "a", "an"}


@dataclass
class Service:
    number: str
    name: str
    area: str
    status: str                       # offered, optional or preview: the chapter it stands in
    aliases: list[str] = field(default_factory=list)
    ends: str = ""                    # YYYY-MM-DD from which the text marks it no longer available or bookable
    end_kind: str = ""                # available or bookable

    def forms(self) -> list[str]:
        return [self.name] + self.aliases

    def withdrawn(self, today: str | None = None) -> bool:
        return bool(self.ends) and self.ends <= (today or date.today().isoformat())


@dataclass
class Catalog:
    revision: str
    services: list[Service]
    not_offered: list[list[str]]      # each a name with its other names, confirmed by hand

    def find(self, name: str) -> list[Service]:
        k = key(name)
        return [s for s in self.services if k in {key(f) for f in s.forms()}]


def norm(text: str) -> str:
    """A name for comparing: lower case, the ligatures and dashes of a PDF made plain, punctuation as spaces."""
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("‐", "-").replace("‑", "-")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def key(text: str) -> str:
    """The comparing form of a name: no filler words, endings such as -s, -er and -ing taken off, the words joined,
    so that "Elastic Load Balancing" meets "Elastic Load Balancer" and "Function Graph" meets "FunctionGraph"."""
    out = []
    for w in norm(text).split():
        if w in FILLER:
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


def load() -> Catalog:
    """The lists of the repository."""
    revision, services = "", []
    for line in CATALOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            m = re.search(r"revision (\d{4}-\d{2}-\d{2})", line)
            revision = m.group(1) if m else revision
            continue
        if line.strip():
            number, name, area, status, aliases, ends, end_kind = (line.split("\t") + [""] * 7)[:7]
            services.append(Service(number, name, area, status, [a.strip() for a in aliases.split("|") if a.strip()],
                                    ends, end_kind))
    rows = []
    if NOT_OFFERED.exists():
        for line in NOT_OFFERED.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                rows.append([f.strip() for f in line.split("|") if f.strip()])
    return Catalog(revision, services, rows)


def _dmy(iso: str) -> str:
    y, m, d = (iso.split("-") + ["", "", ""])[:3]
    return "%s.%s.%s" % (d, m, y) if d else iso


def revision_label(catalog: Catalog) -> str:
    return _dmy(catalog.revision)


@dataclass
class Verdict:
    name: str
    offered: bool
    services: list[Service]
    note: str


def _describe(s: Service, rev: str, today: str) -> str:
    where = "section %s of the service description of %s" % (s.number, rev)
    if s.withdrawn(today):
        return "%s: no longer %s since %s (%s). Do not propose it for new work." % (s.name, s.end_kind, _dmy(s.ends),
                                                                                    where)
    tail = "; no longer %s from %s" % (s.end_kind, _dmy(s.ends)) if s.ends else ""
    what = {"offered": "offered", "optional": "offered as an optional service", "preview": "offered as a preview"}
    return "%s: %s, %s%s" % (s.name, what[s.status], where, tail)


def check(name: str, catalog: Catalog | None = None, today: str | None = None) -> Verdict:
    """Offered or not, by the service description alone."""
    catalog = catalog or load()
    today = today or date.today().isoformat()
    rev = revision_label(catalog)
    found = catalog.find(name)
    if found:
        live = [s for s in found if not s.withdrawn(today)]
        return Verdict(name, bool(live), found, " | ".join(_describe(s, rev, today) for s in found))
    confirmed = any(key(name) in {key(f) for f in row} for row in catalog.not_offered)
    return Verdict(name, False, [], "%s: not offered. The service description of %s does not list it%s. Do not "
                   "propose it." % (name, rev, "; the documentation may still describe it, which is no offer"
                                    if confirmed else ""))


def mentions(text: str, catalog: Catalog | None = None, today: str | None = None) -> list[str]:
    """The services `text` names that are not offered: the hand-kept list and the sections the service description
    marks no longer available or bookable, by name or short name, each once. A short name an offered service also
    uses is left alone."""
    catalog = catalog or load()
    today = today or date.today().isoformat()
    live_forms = {key(f) for s in catalog.services if not s.withdrawn(today) for f in s.forms()}
    rows = list(catalog.not_offered) + [s.forms() for s in catalog.services if s.withdrawn(today)]
    plain = " %s " % norm(text)
    found = []
    for row in rows:
        for form in row:
            if key(form) in live_forms:
                continue
            if form.isupper() and len(form) >= 3:
                hit = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(form), text) is not None
            else:
                hit = (" %s " % norm(form)) in plain
            if hit:
                if row[0] not in found:
                    found.append(row[0])
                break
    return found


def prompt_lines(catalog: Catalog | None = None, today: str | None = None) -> str:
    """The offered services and the rule, for the instructions of a model."""
    catalog = catalog or load()
    today = today or date.today().isoformat()
    def label(s):
        short = next((a for a in s.aliases if a.isupper() and 2 <= len(a) <= 6), "")
        return s.name + (" (%s)" % short if short else "")
    offered = [label(s) for s in catalog.services if s.status != "preview" and not s.withdrawn(today)]
    previews = [label(s) for s in catalog.services if s.status == "preview" and not s.withdrawn(today)]
    gone = ["%s (no longer %s since %s)" % (s.name, s.end_kind, _dmy(s.ends)) for s in catalog.services
            if s.withdrawn(today)]
    others = [row[0] for row in catalog.not_offered]
    return ("Offered on TCP is exactly what the service description of %s lists: %s. Previews: %s. Not for new work: "
            "%s. Not offered at all: %s. Never recommend or propose a service that is not offered, even when the "
            "documentation, an API or a price record knows it; when asked about one, say it is not offered."
            % (revision_label(catalog), "; ".join(offered), "; ".join(previews) or "none", "; ".join(gone) or "none",
               "; ".join(others) or "none"))


def note_for(text: str, catalog: Catalog | None = None, today: str | None = None) -> str:
    """One line to put under an answer that names a service which is not offered, or empty."""
    catalog = catalog or load()
    found = mentions(text, catalog, today)
    if not found:
        return ""
    return ("Not offered on TCP: %s (the service description of %s)." % (", ".join(found), revision_label(catalog)))
