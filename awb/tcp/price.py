"""Live prices of T Cloud Public (TCP) from its public price API (T-85, T-86, T-88).

    awb price find SERVICE [--region R] [--flavor F] [--os TEXT] [--grep TEXT] [--json]
    awb price find --id ID [--region R] [--json]
    awb price snapshot [--region R]... [--replace]
    awb price diff [--region R] [OLD [NEW]]
    awb price check SHEET [--max-age DAYS]

The API is public and needs no key, so a working session runs every command here. Every answer is one of three
states (awb/jobs.py): records, empty (the API answered and holds none) or unknown (no usable answer, with the
reason). Empty is no proof that an item does not exist, because the API covers part of the catalog (KB-AI2E,
KB-RMW2). A record does not show that the item can still be ordered (KB-XELM, KB-7VPP). One platform service can
sit under several service names: the ECS families are spread over six (KB-3KHR), so a flavor that is not found under
the name asked for is looked for under every name of the region before anything is called missing.

`snapshot` keeps the whole listing of a region under `<shared>/prices/tcp/<region>/<date>.json` and refuses a
listing without records, because an empty snapshot becomes a wrong baseline. `diff` compares every price value by
record id, never the number of rows: the row count stayed the same through a reprice of more than half.

`check` reads a price sheet, CSV or TSV with a header row, one position per row:

    id or service + flavor (+ os), region (eu-de), term (PAYG, R12, R24, R36, RU12, RU24, RU36),
    unit_price, quantity, hours (for an hourly rate), total, source, date (YYYY-MM-DD)

A row with the id TOTAL carries the grand total. Every price is fetched again and every total recomputed to the
cent. The rules come from the knowledge base and name their entries: reserved rates are flat monthly values
(KB-T5E6, KB-BH5O), a term the API carries no rate for is an error (KB-T5E6, KB-HSTO), the public calculator
counts 720 hours a month (KB-YNDP), the OS decides the tier. Before a sheet is checked, the check plants wrong
rows against the same records and refuses to run when it misses one of them.

This module belongs to one platform and sits below the neutral core, which never imports it (T-88).
`AWB_PRICE_API` (tests) replaces the address of the API; the work user of the seal always uses the public address.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

from awb import config, jobs

API = "https://calculator.otc-service.com/en/open-telekom-price-api/"
API_ENV = "AWB_PRICE_API"
PAGE = 10000
MAX_PAGES = 100
DEFAULT_REGION = "eu-de"
SNAPSHOT_REGIONS = ("eu-de", "eu-nl", "eu-ch2")
TERMS = ("PAYG", "R12", "R24", "R36", "RU12", "RU24", "RU36")
RESERVED = TERMS[1:]
FIELDS = {"PAYG": "priceAmount", "R12": "R12", "R24": "R24", "R36": "R36", "RU12": "RU12", "RU24": "RU24",
          "RU36": "RU36"}
CALCULATOR_HOURS = Decimal(720)
MAX_AGE_DAYS = 30
CENT = Decimal("0.01")
SHEET_COLUMNS = ("id", "service", "flavor", "os", "region", "term", "unit_price", "quantity", "hours", "total",
                 "source", "date")
BAD_VERDICTS = ("differs", "missing", "ambiguous", "invalid")

# A comma is read as a thousands separator only next to a decimal point: "1,085" alone could be 1085 or 1.085.
_AMOUNT_RE = re.compile(r"^(-?(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)\.\d+) ([A-Z]{3})$")
_NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.(\d+))?|[1-9]\d{0,2}(?:,\d{3})+\.(\d+))$")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z0-9]{2,10}$")
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_HOURLY_RE = re.compile(r"(?:^|/)h(?:/zone)?$")
_TRANSIENT = (429, 502, 503, 504)


class PriceError(Exception):
    """A price command cannot do its work. The message says why."""


class _Transient(Exception):
    pass


# --------------------------------------------------------------------------- records


def parse_amount(text: Any) -> tuple[Decimal, str] | None:
    """"1,234.500000 EUR" -> (Decimal("1234.500000"), "EUR"); None for anything else, a decimal comma included."""
    if not isinstance(text, str):
        return None
    m = _AMOUNT_RE.match(text.strip())
    if not m:
        return None
    return Decimal(m.group(1).replace(",", "")), m.group(2)


def _s(raw: dict, key: str) -> str:
    v = raw.get(key)
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


@dataclass(frozen=True)
class Record:
    id: str
    service: str
    flavor: str
    name: str
    os: str
    unit: str
    region: str
    currency: str
    tier_group: str
    tier_from: Any
    tier_up_to: Any
    prices: dict = field(compare=False)        # term -> Decimal, or None when the field is not readable
    raw: dict = field(compare=False, repr=False)

    @classmethod
    def from_raw(cls, raw: dict) -> "Record":
        prices: dict[str, Decimal | None] = {}
        currency = ""
        for term, key in FIELDS.items():
            parsed = parse_amount(raw.get(key))
            prices[term] = parsed[0] if parsed else None
            if parsed and term == "PAYG":
                currency = parsed[1]
        return cls(id=_s(raw, "id"), service=_s(raw, "productIdParameter").lower(), flavor=_s(raw, "opiFlavour"),
                   name=_s(raw, "productName"), os=_s(raw, "osUnit"), unit=_s(raw, "unit"),
                   region=_s(raw, "region"), currency=currency or _s(raw, "currency"),
                   tier_group=_s(raw, "idGroupTiered"), tier_from=raw.get("fromOn"), tier_up_to=raw.get("upTo"),
                   prices=prices, raw=raw)

    def price(self, term: str) -> Decimal | None:
        return self.prices.get(term)

    def summary(self) -> dict:
        out = {"id": self.id, "service": self.service, "flavor": self.flavor, "name": self.name, "os": self.os,
               "unit": self.unit, "region": self.region, "currency": self.currency}
        out.update({t: (str(v) if v is not None else None) for t, v in self.prices.items()})
        if self.tier_group:
            out.update({"tier_group": self.tier_group, "tier_from": self.tier_from, "tier_up_to": self.tier_up_to})
        return out


def is_hourly(unit: str) -> bool:
    """h, Mbit/h, rules/h, LCU/h/zone: a rate per hour. h/month and GB hours are not taken for hourly."""
    return bool(_HOURLY_RE.search(unit or ""))


# --------------------------------------------------------------------------- fetch


@dataclass
class Fetch:
    listing: jobs.Listing          # items are Records
    url: str
    fetched_at: str
    cached_at: str | None = None
    count: int | None = None
    stray: int = 0                 # records of another service or region left out (KB-6A7H)
    unreadable: int = 0            # price fields not in the form "<number> <currency>"

    @property
    def records(self) -> list[Record]:
        return list(self.listing.items) if self.listing.state == jobs.LIST else []

    def source_line(self) -> str:
        line = "price API, fetched %s" % self.fetched_at
        if self.cached_at:
            line += ", served from the API's cache of %s (server time)" % self.cached_at
            try:
                age = (datetime.date.fromisoformat(self.fetched_at[:10])
                       - datetime.date.fromisoformat(self.cached_at[:10])).days
            except ValueError:
                age = 0
            if age >= 1:
                line += ", %d day(s) old (KB-5LLN)" % age
        return line


def api_url() -> str:
    if not config.is_work_user() and os.environ.get(API_ENV):
        return os.environ[API_ENV]
    return API


def check_region(region: str) -> str:
    region = (region or "").strip().lower()
    if not _REGION_RE.match(region):
        raise PriceError("a region reads like eu-de")
    return region


def _get_json(url: str, timeout: float) -> tuple[int, Any]:
    try:
        from awb.tcp import throttle

        throttle.wait_turn(urllib.parse.urlsplit(url).netloc)   # pages of a listing never arrive as a burst
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in _TRANSIENT:
            raise _Transient("HTTP %d" % exc.code)
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        raise _Transient(type(exc).__name__)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def fetch(service: str | None = None, region: str | None = DEFAULT_REGION, *, base: str | None = None,
          job: jobs.Job | None = None, timeout: float = 60.0, now: datetime.datetime | None = None) -> Fetch:
    """Every record of `service` (every service when None) in `region`, all pages. Never raises for the API."""
    base = base or api_url()
    fetched_at = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    params: dict[str, str] = {}
    if service:
        service = service.strip().lower()
        if not _SERVICE_RE.match(service):
            raise PriceError("a service name reads like ecs or rds")
        params["sn"] = service
    if region:
        params["rn"] = region = check_region(region)
    params["limitMax"] = str(PAGE)

    def unknown(reason: str, **kw) -> Fetch:
        return Fetch(jobs.Listing.unknown(reason), base, fetched_at, **kw)

    raw_records: list = []
    count: int | None = None
    cached: str | None = None
    for page in range(MAX_PAGES):
        q = dict(params)
        if page:
            q["limitFrom"] = str(len(raw_records))
        url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(q)
        try:
            status, body = jobs.retry(lambda: _get_json(url, timeout), attempts=3, delay=2.0, job=job,
                                      retry_on=(_Transient,))
        except _Transient as exc:
            return unknown("the price API did not answer (%s)" % exc)
        resp = jobs.dig(body, "response")
        code = jobs.dig(resp, "code")
        if status != 200 or code != "Success":
            if status == 500 and code == "Error":
                return unknown("the price API refused the query (HTTP 500); it answers this way to a service name "
                               "it does not know (KB-BVUH, KB-SEDA)")
            return unknown("the price API answered HTTP %d with code %s"
                           % (status, code if isinstance(code, str) else "none"))
        if isinstance(jobs.dig(resp, "cachedAt"), str):
            cached = cached or resp["cachedAt"]
        n = jobs.dig(resp, "stats", "count")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return unknown("the answer carries no record count")
        count = n
        result = jobs.dig(resp, "result")
        chunk: list = []
        if isinstance(result, dict):
            for rows in result.values():
                if not isinstance(rows, list):
                    return unknown("the answer holds no list of records")
                chunk.extend(rows)
        elif not (isinstance(result, list) and not result):
            return unknown("the answer holds no result")
        raw_records.extend(chunk)
        if not chunk or len(raw_records) >= count:
            break
    if count is None or len(raw_records) != count:
        return unknown("the price API sent %d of %s records" % (len(raw_records), count), cached_at=cached)

    records: list[Record] = []
    stray = unreadable = 0
    for raw in raw_records:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
            return unknown("a record of the answer has no id", cached_at=cached, count=count)
        rec = Record.from_raw(raw)
        if (service and rec.service != service) or (region and rec.region != region):
            stray += 1
            continue
        unreadable += sum(1 for v in rec.prices.values() if v is None)
        records.append(rec)
    return Fetch(jobs.Listing.of(records), base, fetched_at, cached, count, stray, unreadable)


def select(records: list[Record], *, flavor: str | None = None, os_text: str | None = None,
           grep: str | None = None, ids: set[str] | None = None) -> list[Record]:
    """Records by exact id, exact flavor (any case), a part of the OS unit and a part of id, flavor or name."""
    out = []
    for r in records:
        if ids and r.id not in ids:
            continue
        if flavor and r.flavor.lower() != flavor.strip().lower():
            continue
        if os_text and os_text.strip().lower() not in r.os.lower():
            continue
        if grep and grep.strip().lower() not in ("%s %s %s" % (r.id, r.flavor, r.name)).lower():
            continue
        out.append(r)
    return out


def _cell(v: Decimal | None) -> str:
    return "-" if v is None else str(v)


def table(records: list[Record]) -> list[str]:
    rows = [("id", "flavor", "os", "PAYG", "unit", "R12", "R24", "R36", "name")]
    for r in records:
        rows.append((r.id, r.flavor or "-", r.os or "-", _cell(r.price("PAYG")), r.unit or "-",
                     _cell(r.price("R12")), _cell(r.price("R24")), _cell(r.price("R36")), r.name or "-"))
    widths = [min(max(len(row[i]) for row in rows), 40) for i in range(len(rows[0]) - 1)]
    return ["  ".join(c.ljust(w) for c, w in zip(row[:-1], widths)) + "  " + row[-1] for row in rows]


# --------------------------------------------------------------------------- snapshot and diff


def snapshot_dir(p: config.Paths) -> Path:
    return p.shared / "prices" / "tcp"


def write_snapshot(p: config.Paths, f: Fetch, region: str, *, replace: bool = False,
                   today: datetime.date | None = None) -> Path:
    """The whole listing of `region` as `<shared>/prices/tcp/<region>/<date>.json`; refused without records."""
    if f.listing.state != jobs.LIST:
        raise PriceError("no snapshot of %s: the listing is %s; an empty snapshot would become a wrong baseline"
                         % (region, f.listing.describe()))
    day = (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
    folder = snapshot_dir(p) / check_region(region)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (day + ".json")
    if path.exists() and not replace:
        raise PriceError("a snapshot of %s for %s exists; give --replace to take it again" % (region, day))
    data = {"source": f.url, "region": region, "fetched_at": f.fetched_at, "cached_at": f.cached_at,
            "count": len(f.records), "records": [r.raw for r in f.records]}
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_snapshot(path: Path) -> tuple[dict, list[Record]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise PriceError("the snapshot cannot be read (%s)" % type(err).__name__) from None
    rows = jobs.dig(data, "records")
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise PriceError("the snapshot holds no records")
    meta = {k: v for k, v in data.items() if k != "records"}
    return meta, [Record.from_raw(r) for r in rows]


def latest_snapshot(p: config.Paths, region: str) -> Path | None:
    files = sorted((snapshot_dir(p) / check_region(region)).glob("????-??-??.json"))
    return files[-1] if files else None


@dataclass
class Change:
    id: str
    term: str
    old: Decimal | None
    new: Decimal | None

    def line(self) -> str:
        pct = ""
        if self.old and self.new is not None:
            pct = "  (%+.1f %%)" % ((self.new - self.old) / self.old * 100)
        return "%s  %s  %s -> %s%s" % (self.id, self.term, _cell(self.old), _cell(self.new), pct)


@dataclass
class Diff:
    before: int
    after: int
    added: list[str]
    removed: list[str]
    changes: list[Change]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.changes)

    def lines(self) -> list[str]:
        out = ["before %d records, after %d: %d added, %d removed, %d price value(s) changed"
               % (self.before, self.after, len(self.added), len(self.removed), len(self.changes))]
        if self.before == self.after and self.changes:
            out.append("the number of records is the same, the values are not: rows alone would have said "
                       "nothing changed")
        out += ["added    %s" % i for i in self.added]
        out += ["removed  %s" % i for i in self.removed]
        out += ["changed  %s" % c.line() for c in self.changes]
        return out


def diff(old: list[Record], new: list[Record]) -> Diff:
    """Every price value of every record, by id."""
    o = {r.id: r for r in old}
    n = {r.id: r for r in new}
    changes = [Change(i, t, o[i].price(t), n[i].price(t))
               for i in sorted(set(o) & set(n)) for t in TERMS if o[i].price(t) != n[i].price(t)]
    return Diff(len(o), len(n), sorted(set(n) - set(o)), sorted(set(o) - set(n)), changes)


# --------------------------------------------------------------------------- the sheet check


@dataclass
class Number:
    value: Decimal
    places: int


def parse_number(text: Any) -> Number | None:
    """"0.085", "12", "1,234.56", "12 EUR", "EUR 12"; None for anything else. A comma counts only as a thousands
    separator next to a decimal point, so "0,085" and "1,085" are refused: each could be read two ways."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    for mark in ("EUR", "€"):
        if t.endswith(mark):
            t = t[: -len(mark)].strip()
        if t.startswith(mark):
            t = t[len(mark):].strip()
    m = _NUMBER_RE.match(t)
    if not m:
        return None
    return Number(Decimal(t.replace(",", "")), len(m.group(1) or m.group(2) or ""))


def _norm_header(h: str) -> str:
    return re.sub(r"[\s-]+", "_", (h or "").strip().lower())


def read_sheet(path: Path) -> tuple[list[dict], list[str]]:
    """(rows, notes). Each row is a dict of the known columns plus "line"; empty rows and rows starting with # are
    skipped. Raises PriceError when the file cannot be read or misses a needed column."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as err:
        raise PriceError("the sheet cannot be read as text (%s)" % type(err).__name__) from None
    first = text.splitlines()[0] if text.strip() else ""
    delim = "\t" if "\t" in first else (";" if ";" in first and "," not in first else ",")
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = [_norm_header(h) for h in next(reader)]
    except StopIteration:
        raise PriceError("the sheet is empty") from None
    notes = ["column %s is not read" % h for h in header if h and h not in SHEET_COLUMNS]
    for need in ("unit_price", "quantity"):
        if need not in header:
            raise PriceError("the sheet has no column %s" % need)
    if "id" not in header and not {"service", "flavor"} <= set(header):
        raise PriceError("the sheet needs a column id, or the columns service and flavor")
    rows = []
    for n, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells) or cells[0].strip().startswith("#"):
            continue
        row = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header) if h in SHEET_COLUMNS}
        row["line"] = n
        rows.append(row)
    return rows, notes


@dataclass
class RowResult:
    line: int
    label: str
    verdict: str                    # ok, differs, missing, ambiguous, unknown, invalid
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total: Decimal | None = None    # recomputed from the API
    detail: str = ""


def _label(row: dict) -> str:
    if row.get("id"):
        return row["id"]
    return " ".join(x for x in (row.get("service"), row.get("flavor"), row.get("os")) if x) or "(no id)"


def check_row(row: dict, fetch_region: Callable[[str], Fetch], *, today: datetime.date,
              max_age: int = MAX_AGE_DAYS) -> RowResult:
    """One position of a sheet against the price API."""
    res = RowResult(row.get("line", 0), _label(row), "ok")

    def invalid(why: str) -> RowResult:
        res.verdict = "invalid"
        res.problems.append(why)
        return res

    unit = parse_number(row.get("unit_price", ""))
    qty = parse_number(row.get("quantity", ""))
    hours = parse_number(row["hours"]) if row.get("hours") else None
    total = parse_number(row["total"]) if row.get("total") else None
    if unit is None:
        return invalid("unit_price is not a number with a decimal point")
    if qty is None or qty.value <= 0:
        return invalid("quantity is not a positive number with a decimal point")
    if row.get("hours") and (hours is None or hours.value <= 0):
        return invalid("hours is not a positive number with a decimal point")
    if row.get("total") and total is None:
        return invalid("total is not a number with a decimal point")
    term = (row.get("term") or "PAYG").strip().upper()
    if term not in TERMS:
        return invalid("term is one of %s" % ", ".join(TERMS))
    try:
        region = check_region(row.get("region") or DEFAULT_REGION)
    except PriceError as err:
        return invalid(str(err))
    if not row.get("id") and not (row.get("service") and row.get("flavor")):
        return invalid("a row needs an id, or a service and a flavor")

    if not row.get("source"):
        res.notes.append("no source")
    if not row.get("date"):
        res.notes.append("no date")
    else:
        try:
            age = (today - datetime.date.fromisoformat(row["date"])).days
            if age > max_age:
                res.notes.append("the price was taken %d days ago; the API switches to an announced price on its "
                                 "effective date (KB-PL7K)" % age)
        except ValueError:
            res.notes.append("date is not YYYY-MM-DD")

    f = fetch_region(region)
    if f.listing.state == jobs.UNKNOWN:
        res.verdict = "unknown"
        res.problems.append("the price API could not be read: %s" % f.listing.reason)
        return res
    if row.get("id"):
        found = [r for r in f.records if r.id == row["id"]]
    else:
        found = [r for r in f.records if r.service == row["service"].strip().lower()
                 and r.flavor.lower() == row["flavor"].strip().lower()
                 and (not row.get("os") or r.os.strip().lower() == row["os"].strip().lower())]
    if not found:
        res.verdict = "missing"
        others = [] if row.get("id") else sorted({r.service for r in f.records
                                                  if r.flavor.lower() == row["flavor"].strip().lower()})
        if others:
            res.problems.append("no record under sn=%s; the flavor is listed under %s, so give that service or the "
                                "id (KB-3KHR)" % (row["service"].strip().lower(),
                                                  ", ".join("sn=" + o for o in others)))
        else:
            res.problems.append("no record in the price API for %s; the API covers part of the catalog, so check "
                                "the service description (KB-AI2E, KB-RMW2)" % region)
        return res
    if len(found) > 1:
        res.verdict = "ambiguous"
        res.problems.append("%d records match (OS units: %s); give the id or the os"
                            % (len(found), ", ".join(sorted({r.os or "-" for r in found}))))
        return res
    rec = found[0]
    if row.get("os") and row["os"].strip().lower() != rec.os.strip().lower():
        res.problems.append("OS unit: sheet %s, API %s; the OS decides the tier" % (row["os"], rec.os or "-"))
    api = rec.price(term)
    if api is None:
        res.verdict = "unknown"
        res.problems.append("the API value of %s is not readable" % term)
        return res
    if rec.currency and rec.currency != "EUR":
        res.notes.append("the API gives this rate in %s" % rec.currency)
    if term in RESERVED and api == 0:
        res.problems.append("the API carries no %s rate for this record (KB-T5E6, KB-HSTO)" % term)
    elif api == 0:
        res.notes.append("the API rate is zero")

    places = max(unit.places, 2)
    q = Decimal(1).scaleb(-places)
    if api.quantize(q, ROUND_HALF_UP) != unit.value.quantize(q, ROUND_HALF_UP):
        res.problems.append("unit price: sheet %s, API %s" % (row["unit_price"], api))

    hourly = is_hourly(rec.unit)
    if term in RESERVED and hours is not None:
        res.problems.append("reserved rates are flat monthly values, so hours stay empty (KB-T5E6, KB-BH5O)")
    if term == "PAYG" and hourly and hours is None:
        res.problems.append("an hourly rate (unit %s) without hours" % rec.unit)
    if hours is not None and hours.value != CALCULATOR_HOURS:
        res.notes.append("the public calculator counts 720 hours a month (KB-YNDP)")
    if rec.tier_group:
        res.notes.append("tiered rate, group %s, from %s up to %s" % (rec.tier_group, rec.tier_from, rec.tier_up_to))

    factor = hours.value if (hours is not None and term == "PAYG") else Decimal(1)
    res.total = (api * qty.value * factor).quantize(CENT, ROUND_HALF_UP)
    res.detail = "%s %s x %s%s = %s" % (term, api, qty.value, " x %s h" % hours.value if factor != 1 else "",
                                        res.total)
    if total is not None:
        written = total.value.quantize(CENT, ROUND_HALF_UP)
        if written != res.total:
            res.problems.append("total: sheet %s, recomputed %s (difference %s)"
                                % (row["total"], res.total, written - res.total))
    if res.problems:
        res.verdict = "differs"
    return res


def selftest(records: list[Record], *, today: datetime.date) -> list[str]:
    """Planted rows against `records`: a true row must pass; a unit price one cent off, a total one cent off, an id
    the API does not have and a reserved term without a rate must each be caught. Returns what went wrong."""
    rates = [r for r in records if (r.price("PAYG") or 0) > 0]
    pick = next((r for r in rates if is_hourly(r.unit)), rates[0] if rates else None)
    if pick is None:
        return ["no record with a rate to plant a row on"]
    listing = Fetch(jobs.Listing.of(records), "selftest", "selftest")
    api = pick.price("PAYG")
    hours = "720" if is_hourly(pick.unit) else ""
    factor = CALCULATOR_HOURS if hours else Decimal(1)
    total = (api * 2 * factor).quantize(CENT, ROUND_HALF_UP)
    base = {"id": pick.id, "region": pick.region, "term": "PAYG", "unit_price": str(api), "quantity": "2",
            "hours": hours, "total": str(total), "source": "selftest", "date": today.isoformat(), "line": 0}
    cases = [("a true row", base, "ok"),
             ("a unit price one cent off", dict(base, unit_price=str(api + CENT)), "differs"),
             ("a total one cent off", dict(base, total=str(total + CENT)), "differs"),
             ("an id the API does not have", dict(base, id=pick.id + "_SELFTEST_ABSENT"), "missing")]
    zero = next((r for r in records if r.price("R36") == 0), None)
    if zero is not None:
        cases.append(("a reserved term without a rate",
                      {"id": zero.id, "region": zero.region, "term": "R36", "unit_price": "0.00", "quantity": "1",
                       "source": "selftest", "date": today.isoformat(), "line": 0}, "differs"))
    failed = []
    for label, row, want in cases:
        got = check_row(row, lambda region: listing, today=today).verdict
        if got != want:
            failed.append("%s: %s instead of %s" % (label, got, want))
    return failed


@dataclass
class SheetReport:
    rows: list[RowResult]
    fetches: dict[str, Fetch]
    notes: list[str]
    grand_total: RowResult | None = None

    def counts(self) -> dict[str, int]:
        out = {v: 0 for v in ("ok", "differs", "missing", "ambiguous", "unknown", "invalid")}
        for r in self.rows + ([self.grand_total] if self.grand_total else []):
            out[r.verdict] += 1
        return out

    def exit_code(self) -> int:
        c = self.counts()
        if c["unknown"]:
            return 2
        return 1 if any(c[v] for v in BAD_VERDICTS) else 0


def check_sheet(path: Path, *, fetcher: Callable[..., Fetch] = fetch, today: datetime.date | None = None,
                max_age: int = MAX_AGE_DAYS) -> SheetReport:
    """Every row of the sheet against the price API, one fetch per region, after the self-test passed."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    rows, notes = read_sheet(path)
    positions = [r for r in rows if r.get("id", "").upper() != "TOTAL"]
    totals = [r for r in rows if r.get("id", "").upper() == "TOTAL"]
    if not positions:
        raise PriceError("the sheet has no position")
    if len(totals) > 1:
        raise PriceError("the sheet has more than one TOTAL row")
    fetches: dict[str, Fetch] = {}

    def fetch_region(region: str) -> Fetch:
        if region not in fetches:
            fetches[region] = fetcher(None, region)
        return fetches[region]

    tested = False
    for row in positions:
        try:
            region = check_region(row.get("region") or DEFAULT_REGION)
        except PriceError:
            continue
        f = fetch_region(region)
        if f.listing.state == jobs.LIST and not tested:
            failed = selftest(f.records, today=today)
            if failed:
                raise PriceError("the check failed its self-test and checks nothing: %s" % "; ".join(failed))
            tested = True
    report = SheetReport([check_row(r, fetch_region, today=today, max_age=max_age) for r in positions],
                         fetches, notes)
    if not tested and all(r.verdict != "invalid" for r in report.rows):
        report.notes.append("the self-test did not run: no region answered with records")
    if totals:
        t = totals[0]
        g = RowResult(t["line"], "TOTAL", "ok")
        written = parse_number(t.get("total", ""))
        if written is None:
            g.verdict = "invalid"
            g.problems.append("the TOTAL row has no total with a decimal point")
        elif any(r.total is None for r in report.rows):
            g.verdict = "unknown"
            g.problems.append("not every position could be recomputed")
        else:
            g.total = sum((r.total for r in report.rows), Decimal(0))
            g.detail = "sum of %d position(s) = %s" % (len(report.rows), g.total)
            if written.value.quantize(CENT, ROUND_HALF_UP) != g.total:
                g.verdict = "differs"
                g.problems.append("grand total: sheet %s, recomputed %s (difference %s)"
                                  % (t["total"], g.total, written.value.quantize(CENT, ROUND_HALF_UP) - g.total))
        own = [parse_number(r.get("total", "")) for r in positions]
        if written is not None and all(o is not None for o in own):
            own_sum = sum((o.value.quantize(CENT, ROUND_HALF_UP) for o in own), Decimal(0))
            if own_sum != written.value.quantize(CENT, ROUND_HALF_UP):
                g.problems.append("grand total: sheet %s, but the totals of its own rows add up to %s"
                                  % (t["total"], own_sum))
                if g.verdict == "ok":
                    g.verdict = "differs"
        report.grand_total = g
    return report


def format_report(report: SheetReport, name: str) -> list[str]:
    fetched = ", ".join("%s: %s" % (reg, f.listing.describe()) for reg, f in sorted(report.fetches.items()))
    first = next(iter(report.fetches.values()), None)
    out = ["awb price check: %s, %d position(s); %s (%s)"
           % (name, len(report.rows), first.source_line() if first else "price API not asked", fetched)]
    notes: list[str] = []
    for r in report.rows + ([report.grand_total] if report.grand_total else []):
        out.append("line %-4d %-30s %-9s %s" % (r.line, r.label, r.verdict, r.detail))
        out += ["    %s" % p for p in r.problems]
        notes += ["line %d: %s" % (r.line, n) for n in r.notes]
    notes += report.notes
    if notes:
        out.append("notes:")
        out += ["  %s" % n for n in notes]
    c = report.counts()
    out.append("result: %s" % ", ".join("%d %s" % (c[v], v) for v in c))
    return out


# --------------------------------------------------------------------------- command line


def _cmd_find(args, job) -> int:
    if not args.service and not args.id:
        print("awb price find: give a service name, or --id", file=sys.stderr)
        return 2
    f = fetch(args.service, args.region, job=job)
    what = args.service or "every service"
    if f.listing.state == jobs.UNKNOWN:
        print("awb price find: %s, %s: unknown: %s" % (f.source_line(), args.region, f.listing.reason))
        return 2
    wanted = dict(flavor=args.flavor, os_text=args.os, grep=args.grep, ids={args.id} if args.id else None)
    rows = select(f.records, **wanted)
    elsewhere = False
    if not rows and args.service and (args.flavor or args.grep or args.id):
        g = fetch(None, args.region, job=job)
        if g.listing.state == jobs.LIST:
            rows = [r for r in select(g.records, **wanted) if r.service != args.service]
            elsewhere = bool(rows)
    head = "%s, %s, %d record(s) of %s, %d match" % (f.source_line(), args.region, len(f.records), what,
                                                     0 if elsewhere else len(rows))
    if args.json:
        print(json.dumps({"source": f.url, "fetched_at": f.fetched_at, "cached_at": f.cached_at,
                          "region": args.region, "state": jobs.LIST if rows else jobs.EMPTY,
                          "under_other_service": elsewhere, "records": [r.summary() for r in rows]},
                         ensure_ascii=False, indent=1))
        return 0 if rows else 1
    print(head)
    if f.stray:
        print("%d record(s) of another service or region left out (KB-6A7H)" % f.stray)
    if elsewhere:
        print("not under sn=%s, but under %s (one platform service can sit under several names, KB-3KHR):"
              % (args.service, ", ".join(sorted({"sn=" + r.service for r in rows}))))
    if not rows:
        print("no record matches. That is no proof that the item does not exist: the API covers part of the "
              "catalog (KB-AI2E, KB-RMW2).")
        return 1
    for line in table(rows):
        print(line)
    print("A record does not show that the item can still be ordered (KB-XELM). Reserved rates are flat monthly "
          "values (KB-BH5O).")
    return 0


def _cmd_snapshot(args, job) -> int:
    p = config.paths()
    code = 0
    for region in args.region or SNAPSHOT_REGIONS:
        f = fetch(None, region, job=job)
        try:
            path = write_snapshot(p, f, region, replace=args.replace)
        except PriceError as err:
            print("awb price snapshot: %s" % err, file=sys.stderr)
            code = 2
            continue
        print("awb price snapshot: %s, %d records, %s -> %s" % (region, len(f.records), f.source_line(), path))
    return code


def _cmd_diff(args, job) -> int:
    p = config.paths()
    if args.old:
        meta, old = load_snapshot(Path(args.old))
        region = check_region(str(meta.get("region") or args.region))
        label_old = Path(args.old).name
    else:
        region = check_region(args.region)
        last = latest_snapshot(p, region)
        if last is None:
            print("awb price diff: no snapshot of %s yet; take one with awb price snapshot" % region, file=sys.stderr)
            return 2
        meta, old = load_snapshot(last)
        label_old = "%s/%s" % (region, last.name)
    if args.new:
        _, new = load_snapshot(Path(args.new))
        label_new = Path(args.new).name
    else:
        f = fetch(None, region, job=job)
        if f.listing.state != jobs.LIST:
            print("awb price diff: the live listing of %s is %s" % (region, f.listing.describe()), file=sys.stderr)
            return 2
        new = f.records
        label_new = "live (%s)" % f.source_line()
    d = diff(old, new)
    print("awb price diff: %s against %s" % (label_old, label_new))
    for line in d.lines():
        print(line)
    return 1 if d.changed else 0


def _cmd_check(args, job) -> int:
    report = check_sheet(Path(args.sheet), fetcher=lambda service, region: fetch(service, region, job=job),
                         max_age=args.max_age)
    for line in format_report(report, Path(args.sheet).name):
        print(line)
    return report.exit_code()


def main(argv: list[str] | None = None) -> int:
    """`awb price find|snapshot|diff|check`. Exit 0 ok, 1 findings (no match, a difference, a change), 2 unknown,
    usage or error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb price", description="Live prices of T Cloud Public (TCP) from the public price API.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    f = sub.add_parser("find", help="records of one service, or one record by id")
    f.add_argument("service", nargs="?", default=None)
    f.add_argument("--region", default=DEFAULT_REGION)
    f.add_argument("--flavor", default=None)
    f.add_argument("--os", default=None)
    f.add_argument("--grep", default=None)
    f.add_argument("--id", default=None)
    f.add_argument("--json", action="store_true")
    s = sub.add_parser("snapshot", help="keep the whole listing of a region")
    s.add_argument("--region", action="append", default=[])
    s.add_argument("--replace", action="store_true")
    d = sub.add_parser("diff", help="every price value against the last snapshot or between two")
    d.add_argument("old", nargs="?", default=None)
    d.add_argument("new", nargs="?", default=None)
    d.add_argument("--region", default=DEFAULT_REGION)
    c = sub.add_parser("check", help="check a price sheet to the cent")
    c.add_argument("sheet")
    c.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    job = jobs.Job("price", None)
    try:
        if args.command == "find":
            args.region = check_region(args.region)
            return _cmd_find(args, job)
        if args.command == "snapshot":
            args.region = [check_region(r) for r in args.region]
            return _cmd_snapshot(args, job)
        if args.command == "diff":
            return _cmd_diff(args, job)
        return _cmd_check(args, job)
    except PriceError as err:
        print("awb price: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
