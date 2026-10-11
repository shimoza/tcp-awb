"""The price workbook of an estimate: built from a specification, priced live, compared between versions.

    awb pricebook build SPEC [--out FILE] [--lang en|de] [--against OLD] [--no-record] [--project DIR]
    awb pricebook diff OLD NEW [--lang en|de] [--no-prices]

A specification is a plain text file (or JSON) that a person can read and change. One estimate, one file. The text
form has a few settings at the top and one block per line item, headed by the name of the item in square brackets:

    title: Sizing for CUST-Q7M4
    region: eu-de
    language: en

    [web servers]
    service: ecs
    flavor: s3.large.2
    os: Open Linux
    quantity: 4
    hours: 720
    mode: payg
    assumption: Two zones and all day.
    assumption.de: Zwei Zonen und rund um die Uhr.

    [data disks]
    service: evs
    flavor: vss.ssd
    quantity: 2000
    mode: payg
    assumption: 500 GB per server.

Keys of a line item: service (as the service description or the price API names it), api (the name of the family in
the price API when it differs from the service, for example ecsflex under ECS), flavor, os, id (the id of a price
record, instead of flavor), region (eu-de, eu-nl or eu-ch2; the setting at the top is the default), quantity, hours
(hours per month, for an hourly rate only; 720 when it is left out, as the public calculator counts), mode (payg, r12,
r24, r36, ru12, ru24 or ru36; also written pay as you go, reserved 12, up front 12) and assumption (free text;
assumption.de and assumption.en for a workbook in that language). A line that continues on a line starting with a
blank belongs to the key above. A line starting with # is a comment. In JSON the same keys stand in a list `lines`
(each with a `label`) and `assumption_de` replaces `assumption.de`.

`build` refuses a service the service description of the repository does not offer (awb/offered.py), then asks the
public price API once per region (awb/tcp/price.py) and prices every line from the record it found, never from
memory. A flavor or id the API does not hold, a flavor that fits several records and a reserved term without a rate
are refused with the reason. Nothing is written unless every line is priced. The arithmetic is exact decimal
arithmetic (awb/calc.py): the monthly figure of a line is the rate times quantity times hours, rounded half up to the
cent (reserved rates are flat monthly values: rate times quantity); the yearly figure is the monthly figure times 12.

The workbook is written with the standard library (awb/xlsxlite.py). The sheet of the lines holds the unit price,
the quantity, the hours, the monthly and the yearly figure of each line as formulas with their stored results, the
assumption of each line in its own column and a block of totals that holds the months per year and the default
hours as cells the formulas use. The sheet Sources holds the time of the price API snapshot, the region, the
revision of the service description and the section of each service. With `--against OLD` (a workbook or a
specification) a third sheet lists what changed. Next to the workbook goes a Markdown summary for a mail: the
totals, the three biggest lines and the assumptions. When the specification lies in a project, the two totals are
recorded with awb calc, so the review accepts them with `calc:K-N` as evidence.

`diff` lists what changed between two versions line by line and in the totals. A specification is priced now, both
versions against the same snapshot, unless `--no-prices` is given (then only the settings of the lines are
compared). Exit codes: 0 done or no difference, 1 findings (a refused line, a difference), 2 unknown (the price API
did not answer) or an error.

This module belongs to one platform and sits below the neutral core, which never imports it (T-88).
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Callable

from awb import calc, jobs, offered, xlsxlite
from awb.tcp import price

LANGS = ("en", "de")
REGIONS = price.SNAPSHOT_REGIONS
DEFAULT_HOURS = price.CALCULATOR_HOURS
MAX_HOURS = Decimal(744)
MONTHS = Decimal(12)
MAX_LINES = 120
MAX_SPEC_BYTES = 1_000_000
MAX_TEXT = 500
MAX_LABEL = 80
SPEC_SUFFIXES = (".txt", ".json", ".spec", ".pbk")

_LABEL_HEAD_RE = re.compile(r"^\[([^\[\]]+)\]\s*$")
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]{0,30})\s*:\s?(.*)$")
_QUANTITY_RE = re.compile(r"^\d{1,12}(?:\.\d{1,6})?$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_API_RE = price._SERVICE_RE
_MODE_UPFRONT_RE = re.compile(r"^(?:ru(12|24|36)|(?:reserved)?upfront(12|24|36)|reserved(12|24|36)upfront)$")
_MODE_RESERVED_RE = re.compile(r"^(?:r|reserved)(12|24|36)$")
_TOP_KEYS = ("title", "region", "language")
_LINE_KEYS = ("service", "api", "flavor", "os", "id", "region", "quantity", "hours", "mode", "assumption",
              "assumption_de", "assumption_en")


class PricebookError(Exception):
    """A price workbook command cannot do its work. The message says why."""


class SpecError(PricebookError):
    """A specification that cannot be used. `problems` lists everything found, one text each."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------- the words of the workbook

TEXT = {
    "en": {
        "lines": "Lines", "sources": "Sources", "changes": "Changes",
        "h_no": "No.", "h_item": "Item", "h_service": "Service", "h_flavor": "Flavor or product",
        "h_os": "Operating system", "h_region": "Region", "h_mode": "Pricing mode", "h_unit": "Unit",
        "h_unit_price": "Unit price (EUR)", "h_quantity": "Quantity", "h_hours": "Hours per month",
        "h_monthly": "Monthly (EUR)", "h_yearly": "Yearly (EUR)", "h_assumption": "Assumption",
        "h_record": "Price record",
        "mode_PAYG": "Pay as you go", "mode_R12": "Reserved 12 months", "mode_R24": "Reserved 24 months",
        "mode_R36": "Reserved 36 months", "mode_RU12": "Reserved 12 months, up front",
        "mode_RU24": "Reserved 24 months, up front", "mode_RU36": "Reserved 36 months, up front",
        "subtitle": "All prices in EUR, as the public price API listed them on {snapshot}. Details on the sheet "
                    "Sources.",
        "totals": "Totals", "monthly_total": "Monthly total (EUR)", "yearly_total": "Yearly total (EUR)",
        "months": "Months per year", "default_hours": "Hours per month when a line gives none",
        "s_item": "Item", "s_value": "Value", "s_api": "Price API", "s_snapshot": "Price API snapshot",
        "s_cache": "Served from the cache of the API of", "s_region": "Region",
        "s_revision": "Service description revision", "s_hours": "Hours per month when a line gives none",
        "s_rounding": "Rounding", "s_calc": "Recorded calculations", "s_spec": "Specification (checksum)",
        "s_built": "Built with", "rounding": "Each line total is rounded half up to the cent. The yearly figure is "
                                              "the monthly figure times 12. Reserved rates are monthly values.",
        "s_services": "Services", "s_section": "Section of the service description", "s_status": "Status",
        "status_offered": "offered", "status_optional": "optional service", "status_preview": "preview",
        "s_notes": "Notes", "calc_monthly": "monthly total", "calc_yearly": "yearly total",
        "c_change": "Change", "c_field": "Field", "c_old": "Old", "c_new": "New", "c_diff": "Difference",
        "c_totals": "Totals against the old version",
        "added": "added", "removed": "removed", "changed": "changed",
        "md_prices": "Prices of {snapshot}, EUR, from the public price API. Region: {regions}. Service description "
                     "of {revision}.",
        "md_totals": "Totals", "md_monthly": "Monthly", "md_yearly": "Yearly",
        "md_biggest": "Biggest lines", "md_per_month": "per month", "md_share": "of the monthly total",
        "md_assumptions": "Assumptions", "md_none": "No assumption was written for the lines.",
        "md_default_hours": "Where an hourly rate has no hours the price calculator counts {hours} hours a month.",
        "md_reserved": "Reserved rates are monthly values and hours do not apply to them.",
        "md_changes": "Changes against the old version", "md_no_changes": "Nothing changed.",
        "md_calc": "Recorded calculations: {ids}.",
        "md_from_to": "from {old} to {new} EUR (difference {diff})",
    },
    "de": {
        "lines": "Positionen", "sources": "Quellen", "changes": "Änderungen",
        "h_no": "Pos.", "h_item": "Position", "h_service": "Dienst", "h_flavor": "Flavor oder Produkt",
        "h_os": "Betriebssystem", "h_region": "Region", "h_mode": "Preismodell", "h_unit": "Einheit",
        "h_unit_price": "Einzelpreis (EUR)", "h_quantity": "Menge", "h_hours": "Stunden pro Monat",
        "h_monthly": "Monatlich (EUR)", "h_yearly": "Jährlich (EUR)", "h_assumption": "Annahme",
        "h_record": "Preis-ID",
        "mode_PAYG": "Pay as you go", "mode_R12": "Reserviert 12 Monate", "mode_R24": "Reserviert 24 Monate",
        "mode_R36": "Reserviert 36 Monate", "mode_RU12": "Reserviert 12 Monate, Vorauszahlung",
        "mode_RU24": "Reserviert 24 Monate, Vorauszahlung", "mode_RU36": "Reserviert 36 Monate, Vorauszahlung",
        "subtitle": "Alle Preise in EUR, wie die öffentliche Preis-API sie am {snapshot} nannte. Einzelheiten im "
                    "Blatt Quellen.",
        "totals": "Summen", "monthly_total": "Summe monatlich (EUR)", "yearly_total": "Summe jährlich (EUR)",
        "months": "Monate pro Jahr", "default_hours": "Stunden pro Monat, wenn eine Position keine nennt",
        "s_item": "Angabe", "s_value": "Wert", "s_api": "Preis-API", "s_snapshot": "Stand der Preis-API",
        "s_cache": "Aus dem Cache der API vom", "s_region": "Region",
        "s_revision": "Stand der Leistungsbeschreibung", "s_hours": "Stunden pro Monat, wenn eine Position keine "
                                                                    "nennt",
        "s_rounding": "Rundung", "s_calc": "Erfasste Berechnungen", "s_spec": "Spezifikation (Prüfsumme)",
        "s_built": "Erstellt mit", "rounding": "Jede Positionssumme wird kaufmännisch auf den Cent gerundet. Der "
                                               "Jahreswert ist der Monatswert mal 12. Reservierte Preise sind "
                                               "Monatswerte.",
        "s_services": "Dienste", "s_section": "Abschnitt der Leistungsbeschreibung", "s_status": "Status",
        "status_offered": "angeboten", "status_optional": "optionaler Dienst", "status_preview": "Vorschau",
        "s_notes": "Hinweise", "calc_monthly": "Summe monatlich", "calc_yearly": "Summe jährlich",
        "c_change": "Änderung", "c_field": "Feld", "c_old": "Alt", "c_new": "Neu", "c_diff": "Unterschied",
        "c_totals": "Summen gegenüber der alten Fassung",
        "added": "neu", "removed": "entfallen", "changed": "geändert",
        "md_prices": "Preise vom {snapshot}, EUR, aus der öffentlichen Preis-API. Region: {regions}. "
                     "Leistungsbeschreibung vom {revision}.",
        "md_totals": "Summen", "md_monthly": "Monatlich", "md_yearly": "Jährlich",
        "md_biggest": "Die größten Positionen", "md_per_month": "pro Monat", "md_share": "der Monatssumme",
        "md_assumptions": "Annahmen", "md_none": "Für die Positionen wurde keine Annahme notiert.",
        "md_default_hours": "Wo ein Stundenpreis keine Stunden hat, rechnet der Preisrechner mit {hours} Stunden "
                            "im Monat.",
        "md_reserved": "Reservierte Preise sind Monatswerte und Stunden gelten für sie nicht.",
        "md_changes": "Änderungen gegenüber der alten Fassung", "md_no_changes": "Es hat sich nichts geändert.",
        "md_calc": "Erfasste Berechnungen: {ids}.",
        "md_from_to": "von {old} auf {new} EUR (Unterschied {diff})",
    },
}

# the fields of a line in the order of the sheet; the header text of each is "h_" + the field
COLUMNS = ("no", "item", "service", "flavor", "os", "region", "mode", "unit", "unit_price", "quantity", "hours",
           "monthly", "yearly", "assumption", "record")
COL = {name: i for i, name in enumerate(COLUMNS, start=1)}
WIDTHS = {"no": 6, "item": 28, "service": 14, "flavor": 24, "os": 16, "region": 9, "mode": 28, "unit": 12,
          "unit_price": 15, "quantity": 10, "hours": 10, "monthly": 15, "yearly": 15, "assumption": 52,
          "record": 24}
COMPARED = ("service", "flavor", "os", "region", "mode", "unit", "unit_price", "quantity", "hours", "monthly",
            "yearly", "assumption", "record")
MONEY_FIELDS = ("monthly", "yearly")
FIELD_ENGLISH = {"service": "service", "flavor": "flavor", "os": "OS", "region": "region", "mode": "pricing mode",
                 "unit": "unit", "unit_price": "unit price", "quantity": "quantity", "hours": "hours",
                 "monthly": "monthly", "yearly": "yearly", "assumption": "assumption", "record": "price record"}
HEADER_ROW = 4
FIRST_ROW = 5


# --------------------------------------------------------------------------- the specification


@dataclass(frozen=True)
class Line:
    label: str
    service: str
    api: str
    flavor: str
    os: str
    id: str
    region: str
    quantity: Decimal
    hours: Decimal | None
    term: str
    assumption: str
    assumption_de: str
    assumption_en: str

    def sn(self) -> str:
        """The service name of the price API: `api`, else the service when it reads like one."""
        return self.api or self.service.strip().lower()

    def assumption_for(self, lang: str) -> str:
        return (self.assumption_de if lang == "de" else self.assumption_en) or self.assumption


@dataclass(frozen=True)
class Spec:
    title: str
    region: str
    language: str
    lines: tuple
    digest: str
    name: str


def mode_term(text: str) -> str | None:
    """payg, r12, "pay as you go", "reserved 24", "up front 12", "ru36" ... as PAYG, R12 ... RU36; None if unknown."""
    s = re.sub(r"[\s_-]+", "", text.lower()).replace("months", "").replace("month", "")
    if s in ("payg", "payasyougo"):
        return "PAYG"
    m = _MODE_RESERVED_RE.match(s)
    if m:
        return "R" + m.group(1)
    m = _MODE_UPFRONT_RE.match(s)
    if m:
        return "RU" + next(g for g in m.groups() if g)
    return None


def _clean_text(value, what: str, limit: int, problems: list[str], where: str) -> str:
    text = " ".join(str(value).replace("\t", " ").split())
    if _CONTROL_RE.search(text):
        problems.append("%s%s has a control character" % (where, what))
        return ""
    if len(text) > limit:
        problems.append("%s%s is longer than %d characters" % (where, what, limit))
        return ""
    return text


def _parse_text(text: str, problems: list[str]) -> tuple[dict, list[tuple[str, int, dict]]]:
    """(settings, [(label, line number, {key: value})]) of the text form."""
    top: dict[str, str] = {}
    items: list[tuple[str, int, dict]] = []
    current: dict | None = None
    last_key: str | None = None
    for n, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if raw[0] in " \t":
            if last_key is None:
                problems.append("line %d: text without a key above it" % n)
                continue
            target = current if current is not None else top
            target[last_key] = (target[last_key] + " " + raw.strip()).strip()
            continue
        head = _LABEL_HEAD_RE.match(raw.strip())
        if head:
            current = {}
            items.append((head.group(1).strip(), n, current))
            last_key = None
            continue
        m = _KEY_RE.match(raw.strip())
        if not m:
            problems.append("line %d: not a setting (key: value) and not an item head ([name])" % n)
            last_key = None
            continue
        key = m.group(1).lower().replace(".", "_").replace("-", "_")
        target = current if current is not None else top
        allowed = _LINE_KEYS if current is not None else _TOP_KEYS
        if key not in allowed:
            problems.append("line %d: the key %s is not known%s" % (n, key, "" if current is not None else
                                                                    " at the top (title, region, language)"))
            last_key = None
            continue
        if key in target:
            problems.append("line %d: the key %s is given twice" % (n, key))
            last_key = None
            continue
        target[key] = m.group(2).strip()
        last_key = key
    return top, items


def _parse_json(text: str, problems: list[str]) -> tuple[dict, list[tuple[str, int, dict]]]:
    try:
        data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except ValueError:
        problems.append("the file is not valid JSON")
        return {}, []
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        problems.append("the JSON holds an object with a list `lines`")
        return {}, []
    top: dict[str, str] = {}
    for key, value in data.items():
        if key == "lines":
            continue
        if key not in _TOP_KEYS:
            problems.append("the key %s is not known at the top (title, region, language, lines)" % key)
        elif not isinstance(value, str):
            problems.append("the key %s is a text" % key)
        else:
            top[key] = value
    items = []
    for n, obj in enumerate(data["lines"], start=1):
        if not isinstance(obj, dict) or not isinstance(obj.get("label"), str):
            problems.append("item %d of `lines` has no label" % n)
            continue
        body = {}
        for key, value in obj.items():
            if key == "label":
                continue
            if key not in _LINE_KEYS:
                problems.append("[%s] the key %s is not known" % (obj["label"], key))
            elif isinstance(value, bool) or not isinstance(value, (str, Decimal)):
                problems.append("[%s] the key %s is a text or a number" % (obj["label"], key))
            else:
                body[key] = format(value, "f") if isinstance(value, Decimal) else value
        items.append((obj["label"].strip(), n, body))
    return top, items


def _decimal_of(value: str, key: str, where: str, problems: list[str]) -> Decimal | None:
    if not _QUANTITY_RE.match(value):
        problems.append("%s%s is a positive number with a decimal point, up to 6 places" % (where, key))
        return None
    try:
        number = Decimal(value)
    except DecimalException:
        problems.append("%s%s cannot be read as a number" % (where, key))
        return None
    if number <= 0:
        problems.append("%s%s must be larger than zero" % (where, key))
        return None
    return number


def parse_spec(data: bytes, name: str = "specification") -> Spec:
    """The specification in `data`. Raises SpecError with every problem found."""
    problems: list[str] = []
    if len(data) > MAX_SPEC_BYTES:
        raise SpecError(["the specification is larger than %d bytes" % MAX_SPEC_BYTES])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SpecError(["the specification is not UTF-8 text"]) from None
    as_json = text.lstrip().startswith("{") or name.lower().endswith(".json")
    top, items = (_parse_json if as_json else _parse_text)(text, problems)
    title = _clean_text(top.get("title") or Path(name).stem, "the title", 120, problems, "")
    region = (top.get("region") or price.DEFAULT_REGION).strip().lower()
    if region not in REGIONS:
        problems.append("the region is one of %s" % ", ".join(REGIONS))
    language = (top.get("language") or "en").strip().lower()
    if language not in LANGS:
        problems.append("the language is en or de")
    if not items:
        problems.append("the specification has no line item ([name] blocks)")
    if len(items) > MAX_LINES:
        problems.append("more than %d line items; split the estimate" % MAX_LINES)
    lines: list[Line] = []
    seen: set[str] = set()
    for label, n, body in items[:MAX_LINES]:
        where = "[%s] " % label
        if not label or len(label) > MAX_LABEL or _CONTROL_RE.search(label):
            problems.append("line %s: an item name is 1 to %d characters" % (n, MAX_LABEL))
            continue
        folded = " ".join(label.lower().split())
        if folded in seen:
            problems.append("%sthe item name is used twice" % where)
            continue
        seen.add(folded)
        service = _clean_text(body.get("service", ""), "service", 80, problems, where)
        api = body.get("api", "").strip().lower()
        flavor = _clean_text(body.get("flavor", ""), "flavor", 80, problems, where)
        os_text = _clean_text(body.get("os", ""), "os", 80, problems, where)
        rec_id = body.get("id", "").strip()
        line_region = (body.get("region") or region).strip().lower()
        if not service:
            problems.append("%sservice is needed" % where)
        if api and not _API_RE.match(api):
            problems.append("%sapi is a price API service name such as ecs" % where)
        if not api and service and not _API_RE.match(service.lower()):
            problems.append("%sgive api, the service name of the price API, for %s" % (where, "this service"))
        if not flavor and not rec_id:
            problems.append("%sa line needs a flavor or an id" % where)
        if rec_id and (flavor or os_text):
            problems.append("%sid replaces flavor and os, give one of them" % where)
        if rec_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", rec_id):
            problems.append("%sid is a price record id" % where)
        if line_region not in REGIONS:
            problems.append("%sthe region is one of %s" % (where, ", ".join(REGIONS)))
        quantity = _decimal_of(body["quantity"], "quantity", where, problems) if "quantity" in body else None
        if "quantity" not in body:
            problems.append("%squantity is needed" % where)
        hours = None
        if body.get("hours"):
            hours = _decimal_of(body["hours"], "hours", where, problems)
            if hours is not None and hours > MAX_HOURS:
                problems.append("%shours is more than the %s a long month has" % (where, MAX_HOURS))
                hours = None
        term = "PAYG"
        if "mode" in body:
            parsed = mode_term(body["mode"])
            if parsed is None:
                problems.append("%smode is payg, r12, r24, r36, ru12, ru24 or ru36" % where)
            else:
                term = parsed
        texts = {k: _clean_text(body.get(k, ""), k.replace("_", "."), MAX_TEXT, problems, where)
                 for k in ("assumption", "assumption_de", "assumption_en")}
        if quantity is None:
            continue
        lines.append(Line(label, service, api, flavor, os_text, rec_id, line_region, quantity, hours, term,
                          texts["assumption"], texts["assumption_de"], texts["assumption_en"]))
    if problems:
        raise SpecError(problems)
    return Spec(title, region, language, tuple(lines), hashlib.sha256(data).hexdigest(), Path(name).name)


def load_spec(path: Path) -> Spec:
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise PricebookError("the specification cannot be read (%s)" % type(err).__name__) from None
    return parse_spec(data, str(path))


class LineRefused(PricebookError):
    """Lines the service description or the price API refuses, as {label: reason}. Nothing is written."""

    def __init__(self, problems: dict):
        super().__init__("%d line(s) refused" % len(problems))
        self.problems = problems


# --------------------------------------------------------------------------- the offered check


def offered_problems(spec: Spec, catalog: offered.Catalog, today: str) -> dict[str, str]:
    """label -> why the line is refused, for every line whose service the service description does not offer."""
    out: dict[str, str] = {}
    for line in spec.lines:
        verdict = offered.check(line.service, catalog, today)
        if not verdict.offered:
            hint = ""
            if not verdict.services:
                hint = (" Name the service of the service description in `service` and the family of the price "
                        "API in `api`.")
            out[line.label] = verdict.note + hint
            continue
        if line.api:
            gone = offered.mentions(line.api, catalog, today) or offered.mentions(line.api.upper(), catalog, today)
            if gone:
                out[line.label] = ("the price API family %s is %s, which the service description does not offer"
                                   % (line.api, gone[0]))
    return out


# --------------------------------------------------------------------------- pricing


class LineProblem(Exception):
    """One line that cannot be priced; the message says why."""


@dataclass
class Priced:
    line: Line
    record: price.Record
    unit_price: Decimal
    unit: str
    hours: Decimal | None            # None: no hours apply (a reserved rate or a monthly unit)
    hours_default: bool              # the default hours were used because the line gave none
    monthly: Decimal
    yearly: Decimal
    notes: list


def price_line(line: Line, f: price.Fetch) -> Priced:
    """One line against the records of its region. Raises LineProblem."""
    records = f.records
    sn = line.sn()
    if line.id:
        found = [r for r in records if r.id == line.id]
        if not found:
            raise LineProblem("no record with the id %s in the price API for %s; the API covers part of the catalog "
                              "(KB-AI2E, KB-RMW2)" % (line.id, line.region))
        rec = found[0]
        if rec.service != sn:
            raise LineProblem("the record %s belongs to the price API service %s, not %s; set api to that service"
                              % (rec.id, rec.service, sn))
    else:
        same = [r for r in records if r.service == sn and r.flavor.lower() == line.flavor.lower()]
        if not same:
            others = sorted({r.service for r in records if r.flavor.lower() == line.flavor.lower()})
            if others:
                raise LineProblem("no record under sn=%s; the flavor is listed under %s, so set api to that "
                                  "service (KB-3KHR)" % (sn, ", ".join("sn=" + o for o in others)))
            raise LineProblem("no record for service %s and flavor %s in the price API for %s; the API covers part "
                              "of the catalog, so check the service description (KB-AI2E, KB-RMW2)"
                              % (sn, line.flavor, line.region))
        cands = price.select(same, os_text=line.os) if line.os else same
        if line.os and len(cands) > 1:
            cands = [r for r in cands if r.os.lower() == line.os.lower()] or cands
        if not cands:
            raise LineProblem("no record of the flavor %s has an OS unit like %s (it has: %s)"
                              % (line.flavor, line.os, ", ".join(sorted({r.os or "-" for r in same}))))
        if len(cands) > 1:
            raise LineProblem("%d records match (OS units: %s; ids: %s); give os or the id"
                              % (len(cands), ", ".join(sorted({r.os or "-" for r in cands})),
                                 ", ".join(sorted(r.id for r in cands)[:6])))
        rec = cands[0]
    term = line.term
    rate = rec.price(term)
    if rate is None:
        raise LineProblem("the API value of %s is not readable" % term)
    if rec.currency and rec.currency != "EUR":
        raise LineProblem("the API gives this rate in %s, the workbook counts in EUR" % rec.currency)
    notes: list[str] = []
    hours: Decimal | None = None
    default_used = False
    if term in price.RESERVED:
        if rate == 0:
            raise LineProblem("the API carries no %s rate for this record (KB-T5E6, KB-HSTO)" % term)
        if line.hours is not None:
            raise LineProblem("reserved rates are flat monthly values, so hours stay empty (KB-T5E6, KB-BH5O)")
    else:
        if price.is_hourly(rec.unit):
            hours, default_used = (line.hours, False) if line.hours is not None else (DEFAULT_HOURS, True)
        elif line.hours is not None:
            raise LineProblem("hours apply to an hourly rate; the unit of this record is %s" % (rec.unit or "-"))
        if rate == 0:
            notes.append("the API rate is zero")
    if rec.tier_group:
        notes.append("tiered rate, group %s, from %s up to %s; the rate depends on the quantity"
                     % (rec.tier_group, rec.tier_from, rec.tier_up_to))
    monthly = calc.evaluate("round(p * q * h, 2)", {"p": rate, "q": line.quantity, "h": hours or Decimal(1)})
    yearly = calc.evaluate("m * 12", {"m": monthly})
    return Priced(line, rec, rate, rec.unit, hours, default_used, monthly, yearly, notes)


FetchRegion = Callable[[str], price.Fetch]


def price_spec(spec: Spec, fetch_region: FetchRegion, skip: dict | None = None) -> tuple[list[Priced], dict]:
    """(priced lines, {label: problem}). A label in `skip` is left out with that reason."""
    priced: list[Priced] = []
    problems: dict[str, str] = dict(skip or {})
    for line in spec.lines:
        if line.label in problems:
            continue
        f = fetch_region(line.region)
        if f.listing.state == jobs.UNKNOWN:
            raise PricebookError("the price API could not be read for %s: %s" % (line.region, f.listing.reason))
        if f.listing.state == jobs.EMPTY:
            problems[line.label] = "the price API holds no records for %s" % line.region
            continue
        try:
            priced.append(price_line(line, f))
        except LineProblem as err:
            problems[line.label] = str(err)
    return priced, problems


# --------------------------------------------------------------------------- numbers and words


def money(value: Decimal, lang: str) -> str:
    """1234.5 as 1,234.50 (en) or 1.234,50 (de)."""
    text = calc.text(value, 2)
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    sep, dec = (".", ",") if lang == "de" else (",", ".")
    return sign + sep.join(groups) + dec + frac


def percent(part: Decimal, whole: Decimal, lang: str = "en") -> str | None:
    """part of whole in percent with one place; None for a whole of zero."""
    if whole == 0:
        return None
    text = calc.text(calc.evaluate("pct(a, b)", {"a": part, "b": whole}), 1)
    return text.replace(".", ",") if lang == "de" else text


def when(snapshot: str, lang: str) -> str:
    """2026-10-11 12:00 UTC as it reads in the language: 11.10.2026 12:00 UTC in German."""
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d) (.*)", snapshot)
    return "%s.%s.%s %s" % (m.group(3), m.group(2), m.group(1), m.group(4)) if m and lang == "de" else snapshot


def snapshot_of(fetches: dict) -> str:
    return min(f.fetched_at for f in fetches.values())


def created_of(fetches: dict) -> str:
    try:
        when = datetime.datetime.strptime(snapshot_of(fetches), "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return "2026-01-01T00:00:00Z"
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the comparable book


@dataclass
class Book:
    """What two versions are compared on: the fields of each line by item name and the totals."""
    kind: str                                  # spec, priced or workbook
    title: str
    lines: dict                                # key -> (label, {field: value})
    monthly: Decimal | None
    yearly: Decimal | None
    notes: list = field(default_factory=list)


def _key(label: str) -> str:
    return " ".join(label.lower().split())


def _spec_fields(line: Line, lang: str) -> dict:
    f: dict = {"service": line.service, "region": line.region, "mode": line.term, "quantity": line.quantity,
               "assumption": line.assumption_for(lang)}
    if line.flavor:
        f["flavor"] = line.flavor
    if line.os:
        f["os"] = line.os
    if line.id:
        f["record"] = line.id
    if line.hours is not None:
        f["hours"] = line.hours
    return f


def _priced_fields(p: Priced, lang: str) -> dict:
    f = _spec_fields(p.line, lang)
    f.update(flavor=p.record.flavor or p.record.name, os=p.record.os, record=p.record.id, unit=p.unit,
             unit_price=p.unit_price, monthly=p.monthly, yearly=p.yearly)
    f.pop("hours", None)
    if p.hours is not None:
        f["hours"] = p.hours
    return f


def book_of_spec(spec: Spec, priced: list[Priced] | None, problems: dict, lang: str) -> Book:
    """A specification as a book. `priced` None: not priced (only the settings of the lines)."""
    by_label = {p.line.label: p for p in (priced or [])}
    lines = {}
    for line in spec.lines:
        p = by_label.get(line.label)
        lines[_key(line.label)] = (line.label, _priced_fields(p, lang) if p else _spec_fields(line, lang))
    notes = ["%s: not priced (%s)" % (label, why) for label, why in problems.items()]
    monthly = yearly = None
    if priced is not None and not problems:
        monthly = sum((p.monthly for p in priced), Decimal(0))
        yearly = sum((p.yearly for p in priced), Decimal(0))
    return Book("priced" if priced is not None else "spec", spec.title, lines, monthly, yearly, notes)


def mode_labels() -> dict[str, str]:
    out = {}
    for lang in LANGS:
        for term in price.TERMS:
            out[_key(TEXT[lang]["mode_" + term])] = term
    return out


def book_of_workbook(path) -> Book:
    """The lines sheet of a workbook (the first sheet) as a book. Both languages of the headers are read."""
    try:
        sheets = xlsxlite.read(path)
    except xlsxlite.XlsxError as err:
        raise PricebookError("the workbook cannot be read: %s" % err) from None
    if not sheets:
        raise PricebookError("the workbook has no sheet")
    names = {}
    for lang in LANGS:
        for name in COLUMNS:
            names[_key(TEXT[lang]["h_" + name])] = name
    sheet = sheets[0]
    header_row, cols = None, {}
    for r in sorted(sheet.rows)[:15]:
        found = {c: names[_key(v)] for c, v in sheet.rows[r].items() if isinstance(v, str) and _key(v) in names}
        if len(found) >= 8:
            header_row, cols = r, found
            break
    if header_row is None or not {"item", "service", "quantity"} <= set(cols.values()):
        raise PricebookError("the first sheet of the workbook is not a lines sheet of awb pricebook")
    by_field = {name: c for c, name in cols.items()}
    labels = mode_labels()
    lines: dict = {}
    notes: list[str] = []
    r = header_row + 1
    while r in sheet.rows and (sheet.rows[r].get(by_field["item"]) or sheet.rows[r].get(by_field["service"])):
        row = sheet.rows[r]
        fields: dict = {}
        for name, c in by_field.items():
            if name in ("no", "item") or c not in row:
                continue
            v = row[c]
            if name == "mode" and isinstance(v, str):
                v = labels.get(_key(v), v)
            fields[name] = v
        label = row.get(by_field["item"])
        label = label if isinstance(label, str) and label.strip() else "line %d" % (r - header_row)
        if _key(label) in lines:
            raise PricebookError("two lines of the workbook have the same item name")
        lines[_key(label)] = (label, fields)
        r += 1
    if not lines:
        raise PricebookError("the workbook has no line")
    monthly = _sum_field(lines, "monthly")
    yearly = _sum_field(lines, "yearly")
    title = sheet.rows.get(1, {}).get(1)
    if monthly is None or yearly is None:
        notes.append("not every line holds a stored monthly and yearly figure; open the workbook in a spreadsheet "
                     "program and save it")
    return Book("workbook", title if isinstance(title, str) else "", lines, monthly, yearly, notes)


def _sum_field(lines: dict, name: str) -> Decimal | None:
    values = [f.get(name) for _, f in lines.values()]
    if any(not isinstance(v, Decimal) for v in values):
        return None
    return sum(values, Decimal(0))


def load_book(path: Path, fetch_region: FetchRegion | None, lang: str, catalog: offered.Catalog, today: str,
              prices: bool = True) -> Book:
    """A workbook (.xlsx) or a specification, as a book. A specification is priced when `fetch_region` is given."""
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        return book_of_workbook(p)
    spec = load_spec(p)
    if fetch_region is None or not prices:
        return book_of_spec(spec, None, {}, lang)
    skip = offered_problems(spec, catalog, today)
    priced, problems = price_spec(spec, fetch_region, skip)
    return book_of_spec(spec, priced, problems, lang)


# --------------------------------------------------------------------------- the comparison


@dataclass
class Change:
    kind: str                       # added, removed, changed
    label: str
    name: str                       # the field, empty for an added or a removed line
    old: object = None
    new: object = None


@dataclass
class Comparison:
    changes: list
    old_monthly: Decimal | None
    new_monthly: Decimal | None
    old_yearly: Decimal | None
    new_yearly: Decimal | None
    notes: list

    @property
    def changed(self) -> bool:
        return bool(self.changes) or self.old_monthly != self.new_monthly or self.old_yearly != self.new_yearly

    @property
    def totals_known(self) -> bool:
        return None not in (self.old_monthly, self.new_monthly, self.old_yearly, self.new_yearly)


def _same(a, b) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return _key(a) == _key(b)
    return False


def _optional_fields(old_kind: str, new_kind: str) -> tuple:
    """The fields whose presence on one side only is a change: what a line may leave out."""
    if old_kind == new_kind == "spec":
        return ("flavor", "os", "record", "hours")
    if old_kind != "spec" and new_kind != "spec":
        return ("hours",)
    return ()


def compare(old: Book, new: Book) -> Comparison:
    changes: list[Change] = []
    for key, (label, fields) in old.lines.items():
        if key not in new.lines:
            changes.append(Change("removed", label, "", fields, None))
    mixed = old.kind != new.kind and "spec" in (old.kind, new.kind)     # a flavor or OS as typed against as priced
    optional = _optional_fields(old.kind, new.kind)
    for key, (label, fields) in new.lines.items():
        if key not in old.lines:
            changes.append(Change("added", label, "", None, fields))
            continue
        before = old.lines[key][1]
        for name in COMPARED:
            if mixed and name in ("flavor", "os"):
                continue
            if name in before and name in fields:
                if not _same(before[name], fields[name]):
                    changes.append(Change("changed", label, name, before[name], fields[name]))
            elif (name in before) != (name in fields) and name in optional:
                changes.append(Change("changed", label, name, before.get(name), fields.get(name)))
    notes = ["old: " + n for n in old.notes] + ["new: " + n for n in new.notes]
    return Comparison(changes, old.monthly, new.monthly, old.yearly, new.yearly, notes)


def describe_line(fields: dict) -> str:
    """A short text for an added or a removed line."""
    bits = [fields.get("service", ""), fields.get("flavor", "") or fields.get("record", "")]
    text = " ".join(b for b in bits if b)
    extra = []
    if "quantity" in fields:
        extra.append("quantity %s" % _value_text("quantity", fields["quantity"]))
    if "monthly" in fields:
        extra.append("%s EUR per month" % _value_text("monthly", fields["monthly"]))
    return text + (", " + ", ".join(extra) if extra else "")


def _value_text(name: str, value) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return calc.text(value, 2) if name in MONEY_FIELDS else calc.text(value)
    if name == "mode":
        return str(value)
    return str(value) or "-"


def comparison_lines(c: Comparison) -> list[str]:
    out = []
    for ch in c.changes:
        if ch.kind == "changed":
            line = "changed  %s  %s  %s -> %s" % (ch.label, FIELD_ENGLISH[ch.name], _value_text(ch.name, ch.old),
                                                 _value_text(ch.name, ch.new))
            if ch.name in ("unit_price", "quantity", "monthly", "yearly") and isinstance(ch.old, Decimal) \
                    and isinstance(ch.new, Decimal) and ch.old != 0:
                line += "  (%s %%)" % signed(percent(ch.new - ch.old, ch.old))
            out.append(line)
        else:
            out.append("%-8s %s  (%s)" % (ch.kind, ch.label, describe_line(ch.new if ch.kind == "added" else ch.old)))
    if c.totals_known:
        for name, a, b in (("monthly", c.old_monthly, c.new_monthly), ("yearly", c.old_yearly, c.new_yearly)):
            diff = b - a
            line = "totals   %s  %s -> %s" % (name, calc.text(a, 2), calc.text(b, 2))
            if a != b:
                line += "  (%s%s EUR%s)" % ("+" if diff > 0 else "", calc.text(diff, 2),
                                            "" if a == 0 else ", %s %%" % signed(percent(diff, a)))
            out.append(line)
    else:
        out.append("totals   not compared: a total is not known for both versions")
    return out


def signed(text: str | None) -> str:
    if text is None:
        return "-"
    return text if text.startswith("-") or text == "0" else "+" + text


# --------------------------------------------------------------------------- the workbook


@dataclass
class Built:
    sheets: list
    monthly: Decimal
    yearly: Decimal


def _services_table(spec: Spec, catalog: offered.Catalog, today: str) -> list[tuple[str, str, str]]:
    """(service as written, section and name, status key) for each distinct service of the lines."""
    rows, seen = [], set()
    for line in spec.lines:
        if line.service.lower() in seen:
            continue
        seen.add(line.service.lower())
        verdict = offered.check(line.service, catalog, today)
        live = [s for s in verdict.services if not s.withdrawn(today)]
        if live:
            s = live[0]
            rows.append((line.service, "%s %s" % (s.number, s.name), s.status))
    return rows


def build_sheets(spec: Spec, priced: list[Priced], fetches: dict, catalog: offered.Catalog, today: str, lang: str,
                 calc_ids: list[str], comparison: Comparison | None) -> Built:
    L = TEXT[lang]
    n = len(priced)
    last = FIRST_ROW + n - 1
    totals_row = last + 2
    r_monthly, r_yearly, r_months, r_hours = totals_row + 1, totals_row + 2, totals_row + 3, totals_row + 4
    VALUE = 3                                              # the column that holds the values of the totals block
    months_ref = xlsxlite.ref(r_months, VALUE, True)
    hours_ref = xlsxlite.ref(r_hours, VALUE, True)
    monthly = sum((p.monthly for p in priced), Decimal(0))
    yearly = sum((p.yearly for p in priced), Decimal(0))

    sh = xlsxlite.Sheet(L["lines"])
    sh.freeze_rows = HEADER_ROW
    for name, w in WIDTHS.items():
        sh.widths[COL[name]] = w
    sh.put(1, 1, spec.title, style="title")
    sh.put(2, 1, L["subtitle"].format(snapshot=when(snapshot_of(fetches), lang)), style="note")
    for name in COLUMNS:
        sh.put(HEADER_ROW, COL[name], L["h_" + name], style="header")
    c = COL
    for k, p in enumerate(priced):
        r = FIRST_ROW + k
        sh.put(r, c["no"], Decimal(k + 1))
        sh.put(r, c["item"], p.line.label)
        sh.put(r, c["service"], p.line.service)
        sh.put(r, c["flavor"], p.record.flavor or p.record.name)
        sh.put(r, c["os"], p.record.os)
        sh.put(r, c["region"], p.line.region)
        sh.put(r, c["mode"], L["mode_" + p.line.term])
        sh.put(r, c["unit"], p.unit)
        sh.put(r, c["unit_price"], p.unit_price, style="price")
        sh.put(r, c["quantity"], p.line.quantity, style="number")
        if p.hours is not None and p.hours_default:
            sh.put(r, c["hours"], p.hours, formula=hours_ref, style="number")
        elif p.hours is not None:
            sh.put(r, c["hours"], p.hours, style="number")
        up, qt, hr, mo = (xlsxlite.ref(r, c[x]) for x in ("unit_price", "quantity", "hours", "monthly"))
        sh.put(r, c["monthly"], p.monthly, formula="ROUND(%s*%s*IF(%s=\"\",1,%s),2)" % (up, qt, hr, hr),
               style="money")
        sh.put(r, c["yearly"], p.yearly, formula="%s*%s" % (mo, months_ref), style="money")
        sh.put(r, c["assumption"], p.line.assumption_for(lang), style="wrap")
        sh.put(r, c["record"], p.record.id)
    col_m, col_y = xlsxlite.column_letter(c["monthly"]), xlsxlite.column_letter(c["yearly"])
    sh.put(totals_row, 2, L["totals"], style="label")
    sh.put(r_monthly, 2, L["monthly_total"], style="label")
    sh.put(r_monthly, VALUE, monthly, formula="SUM(%s%d:%s%d)" % (col_m, FIRST_ROW, col_m, last), style="total")
    sh.put(r_yearly, 2, L["yearly_total"], style="label")
    sh.put(r_yearly, VALUE, yearly, formula="SUM(%s%d:%s%d)" % (col_y, FIRST_ROW, col_y, last), style="total")
    sh.put(r_months, 2, L["months"])
    sh.put(r_months, VALUE, MONTHS)
    sh.put(r_hours, 2, L["default_hours"])
    sh.put(r_hours, VALUE, DEFAULT_HOURS)
    sheets = [sh, _sources_sheet(spec, priced, fetches, catalog, today, lang, calc_ids)]
    if comparison is not None:
        sheets.append(_changes_sheet(lang, comparison, sh.name, VALUE, r_monthly, r_yearly))
    return Built(sheets, monthly, yearly)


def _sources_sheet(spec: Spec, priced: list[Priced], fetches: dict, catalog: offered.Catalog, today: str,
                   lang: str, calc_ids: list[str]) -> xlsxlite.Sheet:
    L = TEXT[lang]
    sh = xlsxlite.Sheet(L["sources"])
    sh.widths.update({1: 40, 2: 70, 3: 20})
    sh.put(1, 1, L["sources"], style="title")
    sh.put(3, 1, L["s_item"], style="header")
    sh.put(3, 2, L["s_value"], style="header")
    rows: list[tuple[str, str]] = []
    first = next(iter(fetches.values()))
    rows.append((L["s_api"], first.url))
    for region, f in sorted(fetches.items()):
        rows.append(("%s (%s)" % (L["s_snapshot"], region), when(f.fetched_at, lang)))
        if f.cached_at:
            rows.append(("%s (%s)" % (L["s_cache"], region), when(f.cached_at, lang)))
    rows.append((L["s_region"], ", ".join(sorted(fetches))))
    rows.append((L["s_revision"], "%s (%s)" % (offered.revision_label(catalog), catalog.revision)))
    rows.append((L["s_hours"], calc.text(DEFAULT_HOURS)))
    rows.append((L["s_rounding"], L["rounding"]))
    if calc_ids:
        rows.append((L["s_calc"], ", ".join(calc_ids)))
    rows.append((L["s_spec"], spec.digest[:16]))
    rows.append((L["s_built"], "awb pricebook"))
    r = 4
    for key, value in rows:
        sh.put(r, 1, key, style="label")
        sh.put(r, 2, value, style="wrap")
        r += 1
    table = _services_table(spec, catalog, today)
    if table:
        r += 1
        sh.put(r, 1, L["s_services"], style="header")
        sh.put(r, 2, L["s_section"], style="header")
        sh.put(r, 3, L["s_status"], style="header")
        for service, section, status in table:
            r += 1
            sh.put(r, 1, service)
            sh.put(r, 2, section)
            sh.put(r, 3, L["status_" + status])
    notes = [(p.line.label, note) for p in priced for note in p.notes]
    if notes:
        r += 2
        sh.put(r, 1, L["s_notes"], style="header")
        sh.put(r, 2, "", style="header")
        for label, note in notes:
            r += 1
            sh.put(r, 1, label)
            sh.put(r, 2, note, style="wrap")
    return sh


def _cell_value(value):
    """The value of a field for a cell of the sheet of changes: numbers stay numbers."""
    if value is None:
        return ""
    return value if isinstance(value, Decimal) else str(value)


def _changes_sheet(lang: str, comparison: Comparison, lines_sheet: str, value_col: int, r_monthly: int,
                   r_yearly: int) -> xlsxlite.Sheet:
    L = TEXT[lang]
    sh = xlsxlite.Sheet(L["changes"])
    sh.widths.update({1: 14, 2: 30, 3: 24, 4: 40, 5: 40})
    sh.put(1, 1, L["changes"], style="title")
    for i, key in enumerate(("c_change", "h_item", "c_field", "c_old", "c_new"), start=1):
        sh.put(3, i, L[key], style="header")
    r = 3
    for ch in comparison.changes:
        r += 1
        sh.put(r, 1, L[ch.kind])
        sh.put(r, 2, ch.label)
        if ch.kind == "changed":
            sh.put(r, 3, L["h_" + ch.name])
            style = "money" if ch.name in MONEY_FIELDS else "default"
            sh.put(r, 4, _mode_label(ch.old, lang) if ch.name == "mode" else _cell_value(ch.old), style=style)
            sh.put(r, 5, _mode_label(ch.new, lang) if ch.name == "mode" else _cell_value(ch.new), style=style)
        else:
            sh.put(r, 4, describe_line(ch.old) if ch.old else "")
            sh.put(r, 5, describe_line(ch.new) if ch.new else "")
    if not comparison.changes:
        sh.put(4, 1, "-")
    if comparison.totals_known:
        r += 3
        sh.put(r, 2, L["c_totals"], style="label")
        for i, key in enumerate(("c_old", "c_new", "c_diff"), start=3):
            sh.put(r + 1, i, L[key], style="header")
        sh.put(r + 1, 2, "", style="header")
        quoted = "'%s'" % lines_sheet.replace("'", "''")
        for k, (key, old, new, src) in enumerate((("monthly_total", comparison.old_monthly, comparison.new_monthly,
                                                    r_monthly),
                                                   ("yearly_total", comparison.old_yearly, comparison.new_yearly,
                                                    r_yearly))):
            rr = r + 2 + k
            sh.put(rr, 2, L[key], style="label")
            sh.put(rr, 3, old, style="money")
            sh.put(rr, 4, new, formula="%s!%s" % (quoted, xlsxlite.ref(src, value_col, True)), style="money")
            sh.put(rr, 5, new - old, formula="D%d-C%d" % (rr, rr), style="money")
    return sh


def _mode_label(value, lang: str) -> str:
    return TEXT[lang].get("mode_" + str(value), str(value)) if value is not None else ""


# --------------------------------------------------------------------------- the summary for a mail


def build_summary(spec: Spec, priced: list[Priced], fetches: dict, catalog: offered.Catalog, lang: str,
                  built: Built, calc_ids: list[str], comparison: Comparison | None) -> str:
    L = TEXT[lang]
    out = ["# %s" % spec.title, "",
           L["md_prices"].format(snapshot=when(snapshot_of(fetches), lang), regions=", ".join(sorted(fetches)),
                                 revision=offered.revision_label(catalog)), "",
           "## %s" % L["md_totals"], "",
           "- %s: %s EUR" % (L["md_monthly"], money(built.monthly, lang)),
           "- %s: %s EUR" % (L["md_yearly"], money(built.yearly, lang)), "",
           "## %s" % L["md_biggest"], ""]
    ranked = sorted(enumerate(priced), key=lambda t: (-t[1].monthly, t[0]))[:3]
    for rank, (_, p) in enumerate(ranked, start=1):
        share = percent(p.monthly, built.monthly, lang)
        out.append("%d. %s: %s EUR %s%s" % (rank, p.line.label, money(p.monthly, lang), L["md_per_month"],
                                            " (%s %% %s)" % (share, L["md_share"]) if share else ""))
    out += ["", "## %s" % L["md_assumptions"], ""]
    given = [(p.line.label, p.line.assumption_for(lang)) for p in priced if p.line.assumption_for(lang)]
    out += ["- %s: %s" % pair for pair in given]
    if not given:
        out.append(L["md_none"])
    if any(p.hours_default for p in priced):
        out.append("- " + L["md_default_hours"].format(hours=calc.text(DEFAULT_HOURS)))
    if any(p.line.term in price.RESERVED for p in priced):
        out.append("- " + L["md_reserved"])
    if comparison is not None:
        out += ["", "## %s" % L["md_changes"], ""]
        if not comparison.changed:
            out.append(L["md_no_changes"])
        for ch in comparison.changes:
            if ch.kind == "changed":
                out.append("- %s, %s: %s" % (ch.label, TEXT[lang]["h_" + ch.name], _change_text(ch, lang)))
            else:
                out.append("- %s: %s" % (ch.label, L[ch.kind]))
        if comparison.totals_known:
            for key, a, b in (("md_monthly", comparison.old_monthly, comparison.new_monthly),
                              ("md_yearly", comparison.old_yearly, comparison.new_yearly)):
                if a != b:
                    out.append("- %s: %s" % (L[key], L["md_from_to"].format(
                        old=money(a, lang), new=money(b, lang), diff=("+" if b > a else "") + money(b - a, lang))))
    if calc_ids:
        out += ["", L["md_calc"].format(ids=", ".join(calc_ids))]
    return "\n".join(out) + "\n"


def _change_text(ch: Change, lang: str) -> str:
    def show(v):
        if ch.name == "mode":
            return _mode_label(v, lang)
        if isinstance(v, Decimal):
            return money(v, lang) if ch.name in MONEY_FIELDS else calc.text(v)
        return str(v) if v not in (None, "") else "-"
    return "%s -> %s" % (show(ch.old), show(ch.new))


# --------------------------------------------------------------------------- build


def find_project(start: Path) -> Path | None:
    here = Path(start).resolve()
    for folder in (here, *here.parents):
        if (folder / "SCOPE.md").is_file():
            return folder
    return None


def record_totals(project: Path, priced: list[Priced], monthly: Decimal, yearly: Decimal) -> list[str]:
    """The two totals recorded with awb calc; raises calc.CalcError when the expression does not fit."""
    expr = "sum(%s)" % ", ".join(calc.text(p.monthly, 2) for p in priced)
    if calc.evaluate(expr) != monthly:
        raise calc.CalcError("the recorded sum differs from the total")
    rec_m = calc.record(project, expr, [], calc.text(monthly, 2), "pricebook monthly total")
    rec_y = calc.record(project, "%s * 12" % expr, [], calc.text(yearly, 2), "pricebook yearly total")
    return [rec_m.id, rec_y.id]


@dataclass
class Result:
    xlsx: Path
    summary: Path
    monthly: Decimal
    yearly: Decimal
    lines: int
    calc_ids: list
    notes: list
    comparison: Comparison | None


def build(spec_path: Path, *, out: Path | None = None, lang: str | None = None, against: Path | None = None,
          fetcher: Callable[..., price.Fetch] = price.fetch, record: bool = True, project: Path | None = None,
          today: str | None = None) -> Result:
    """Build the workbook and the summary. Raises SpecError (a bad specification), LineRefused (a line the service
    description or the price API refuses) or PricebookError (anything else, the price API not answering included)."""
    spec = load_spec(spec_path)
    lang = lang or spec.language
    if lang not in LANGS:
        raise PricebookError("the language is en or de")
    today = today or datetime.date.today().isoformat()
    out = Path(out) if out else Path(spec_path).with_suffix(".xlsx")
    if out.suffix.lower() != ".xlsx":
        raise PricebookError("--out is a file with the ending .xlsx")
    if not out.parent.is_dir():
        raise PricebookError("the folder of --out does not exist")
    if out.resolve() == Path(spec_path).resolve():
        raise PricebookError("--out is the specification itself")
    summary_path = out.with_suffix(".md")
    catalog = offered.load()
    refused = offered_problems(spec, catalog, today)
    if refused:
        raise LineRefused(refused)
    fetches: dict[str, price.Fetch] = {}

    def fetch_region(region: str) -> price.Fetch:
        if region not in fetches:
            fetches[region] = fetcher(None, region)
        return fetches[region]

    priced, problems = price_spec(spec, fetch_region)
    if problems:
        raise LineRefused(problems)
    used = {region: fetches[region] for region in sorted({p.line.region for p in priced})}
    comparison = None
    if against is not None:
        old = load_book(Path(against), fetch_region, lang, catalog, today)
        comparison = compare(old, book_of_spec(spec, priced, {}, lang))
    monthly = sum((p.monthly for p in priced), Decimal(0))
    yearly = sum((p.yearly for p in priced), Decimal(0))
    notes: list[str] = []
    calc_ids: list[str] = []
    if record:
        folder = find_project(project or Path(spec_path).parent)
        if folder is None:
            notes.append("the totals are not recorded: the specification is not inside a project (no SCOPE.md)")
        else:
            try:
                calc_ids = record_totals(folder, priced, monthly, yearly)
            except calc.CalcError as err:
                notes.append("the totals are not recorded: %s" % err)
            except OSError as err:
                notes.append("the totals are not recorded (%s)" % type(err).__name__)
    built = build_sheets(spec, priced, used, catalog, today, lang, calc_ids, comparison)
    try:
        data = xlsxlite.build(built.sheets, title=spec.title, created=created_of(used))
    except xlsxlite.XlsxError as err:
        raise PricebookError("the workbook cannot be written: %s" % err) from None
    summary = build_summary(spec, priced, used, catalog, lang, built, calc_ids, comparison)
    check = compare(book_of_spec(spec, priced, {}, lang), book_of_workbook(io.BytesIO(data)))
    if check.changed:
        raise PricebookError("the workbook read back differs from what was computed; nothing written")
    xlsxlite.write_bytes(out, data)
    xlsxlite.write_bytes(summary_path, summary.encode("utf-8"))
    for p in priced:
        notes += ["%s: %s" % (p.line.label, n) for n in p.notes]
    return Result(out, summary_path, monthly, yearly, len(priced), calc_ids, notes, comparison)


# --------------------------------------------------------------------------- command line


def _cmd_build(args, job) -> int:
    def fetcher(service, region):
        return price.fetch(service, region, job=job)

    try:
        res = build(Path(args.spec), out=Path(args.out) if args.out else None, lang=args.lang,
                    against=Path(args.against) if args.against else None, fetcher=fetcher,
                    record=not args.no_record, project=Path(args.project) if args.project else None)
    except SpecError as err:
        print("awb pricebook: nothing written, the specification has %d problem(s):" % len(err.problems),
              file=sys.stderr)
        for p in err.problems:
            print("  %s" % p, file=sys.stderr)
        return 2
    except LineRefused as err:
        print("awb pricebook: nothing written, %d line(s) refused:" % len(err.problems), file=sys.stderr)
        for label, why in err.problems.items():
            print("  [%s] %s" % (label, why), file=sys.stderr)
        return 1
    except PricebookError as err:
        print("awb pricebook: nothing written: %s" % err, file=sys.stderr)
        return 2
    print("awb pricebook build: %d line(s), monthly total %s EUR, yearly total %s EUR"
          % (res.lines, money(res.monthly, "en"), money(res.yearly, "en")))
    print("  workbook  %s" % res.xlsx)
    print("  summary   %s" % res.summary)
    if res.calc_ids:
        print("  recorded  %s (evidence for the review: calc:%s)" % (", ".join(res.calc_ids), res.calc_ids[0]))
    if res.comparison is not None:
        print("  against   %s" % ("no difference" if not res.comparison.changed else
                                  "%d change(s) in the lines; see the sheet of changes" % len(res.comparison.changes)))
    for note in res.notes:
        print("  note      %s" % note)
    return 0


def _cmd_diff(args, job) -> int:
    today = datetime.date.today().isoformat()
    catalog = offered.load()
    fetches: dict[str, price.Fetch] = {}

    def fetch_region(region: str) -> price.Fetch:
        if region not in fetches:
            fetches[region] = price.fetch(None, region, job=job)
        return fetches[region]

    lang = args.lang
    if lang is None:
        new_path = Path(args.new)
        lang = "en"
        if new_path.suffix.lower() != ".xlsx":
            try:
                lang = load_spec(new_path).language
            except PricebookError:
                lang = "en"
    try:
        old = load_book(Path(args.old), fetch_region, lang, catalog, today, prices=not args.no_prices)
        new = load_book(Path(args.new), fetch_region, lang, catalog, today, prices=not args.no_prices)
    except SpecError as err:
        print("awb pricebook: the specification has %d problem(s):" % len(err.problems), file=sys.stderr)
        for p in err.problems:
            print("  %s" % p, file=sys.stderr)
        return 2
    c = compare(old, new)
    print("awb pricebook diff: %s (%s) against %s (%s)" % (Path(args.old).name, old.kind, Path(args.new).name,
                                                           new.kind))
    if fetches:
        print("  price API: %s" % "; ".join("%s fetched %s" % (r, f.fetched_at) for r, f in sorted(fetches.items())))
    lines = comparison_lines(c)
    for line in lines:
        print(line)
    for note in c.notes:
        print("note: %s" % note)
    if not c.changes and not c.changed:
        print("no difference")
    return 1 if c.changed else 0


def main(argv: list[str] | None = None) -> int:
    """`awb pricebook build|diff`. Exit 0 done or no difference, 1 findings, 2 unknown, usage or error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb pricebook", description="A price workbook from a specification, priced live.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    b = sub.add_parser("build", help="price a specification and write the workbook and a summary for a mail")
    b.add_argument("spec")
    b.add_argument("--out", default=None, help="the workbook (default: the specification with the ending .xlsx)")
    b.add_argument("--lang", choices=LANGS, default=None, help="en or de (default: the language of the specification)")
    b.add_argument("--against", default=None, help="an older workbook or specification: adds the sheet of changes")
    b.add_argument("--no-record", action="store_true", help="do not record the totals with awb calc")
    b.add_argument("--project", default=None, help="the project that records the totals (default: the one around "
                                                    "the specification)")
    d = sub.add_parser("diff", help="what changed between two specifications or two workbooks")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--lang", choices=LANGS, default=None)
    d.add_argument("--no-prices", action="store_true", help="compare the settings of the lines only")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    job = jobs.Job("pricebook", None)
    try:
        return _cmd_build(args, job) if args.command == "build" else _cmd_diff(args, job)
    except PricebookError as err:
        print("awb pricebook: %s" % err, file=sys.stderr)
        return 2
    except price.PriceError as err:
        print("awb pricebook: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
