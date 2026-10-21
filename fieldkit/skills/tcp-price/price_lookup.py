#!/usr/bin/env python3
"""Look up TCP prices live from the public price API. Standard library only, no credentials.

    python3 price_lookup.py SERVICE [--region eu-de] [--flavor TEXT] [--grep TEXT] [--limit N]

Prints one block per matching row: region, service, product, flavor, operating system, unit, the prices per term and
the time of the lookup (UTC). Exit 0 with rows, 1 and "not found" when the API has no matching row, 2 when the API
did not answer or refused the query. Nothing is cached and nothing is written.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://calculator.otc-service.com/en/open-telekom-price-api/"
PAGE = 10000
MAX_PAGES = 20
TERMS = (("pay as you go", "priceAmount"), ("reserved 12 months", "R12"), ("reserved 24 months", "R24"),
         ("reserved 36 months", "R36"), ("reserved 12 months up front", "RU12"),
         ("reserved 24 months up front", "RU24"), ("reserved 36 months up front", "RU36"))
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
_AMOUNT = re.compile(r"^\s*([0-9][0-9.,]*)\s*([A-Za-z]{3})?\s*$")


class Unanswered(Exception):
    pass


def get(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        body = err.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
        raise Unanswered("the price API did not answer (%s)" % type(err).__name__) from None
    try:
        data = json.loads(body)
    except ValueError:
        raise Unanswered("the price API sent no JSON") from None
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict) or response.get("code") != "Success":
        raise Unanswered("the price API refused the query (an unknown service name answers this way)")
    return response


def fetch(service: str, region: str, base: str, timeout: float = 60.0) -> list[dict]:
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        query = {"sn": service, "rn": region, "limitMax": str(PAGE)}
        if page:
            query["limitFrom"] = str(len(rows))
        response = get(base + ("&" if "?" in base else "?") + urllib.parse.urlencode(query), timeout)
        result = response.get("result")
        chunk = [r for rs in result.values() if isinstance(rs, list) for r in rs] if isinstance(result, dict) else []
        rows.extend(r for r in chunk if isinstance(r, dict))
        count = (response.get("stats") or {}).get("count")
        if not chunk or not isinstance(count, int) or len(rows) >= count:
            return rows
    raise Unanswered("the price API sent more pages than expected")


def amount(value) -> str | None:
    m = _AMOUNT.match(value) if isinstance(value, str) else None
    return "%s %s" % (m.group(1), m.group(2) or "EUR") if m else None


def matches(row: dict, region: str, flavor: str, grep: str) -> bool:
    if str(row.get("region", "")).lower() != region:
        return False
    if flavor and flavor.lower() not in str(row.get("opiFlavour", "")).lower():
        return False
    text = " ".join(str(row.get(k, "")) for k in ("productName", "opiFlavour", "osUnit", "productId")).lower()
    return not grep or grep.lower() in text


def show(row: dict, looked_up: str) -> str:
    lines = ["region: %s" % row.get("region", ""), "service: %s" % row.get("productIdParameter", ""),
             "product: %s" % row.get("productName", ""), "flavor: %s" % (row.get("opiFlavour") or "-"),
             "os: %s" % (row.get("osUnit") or "-"), "unit: %s" % (row.get("unit") or "-")]
    for label, key in TERMS:
        got = amount(row.get(key))
        if got:
            lines.append("%s: %s" % (label, got))
    lines += ["row id: %s" % row.get("id", ""), "looked up: %s" % looked_up]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live TCP price lookup (public price API, no credentials)")
    ap.add_argument("service", help="service name as the API spells it, for example ecs, evs, rds")
    ap.add_argument("--region", default="eu-de", help="region, for example eu-de, eu-nl, eu-ch2")
    ap.add_argument("--flavor", default="", help="part of the flavor name, for example s3.large.2")
    ap.add_argument("--grep", default="", help="a word of the product name or operating system")
    ap.add_argument("--limit", type=int, default=10, help="most rows to print")
    ap.add_argument("--api", default=API, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    service, region = args.service.strip().lower(), args.region.strip().lower()
    if not _NAME.fullmatch(service) or not re.fullmatch(r"[a-z]{2}-[a-z0-9-]{2,12}", region):
        print("price_lookup: a service reads like ecs and a region like eu-de", file=sys.stderr)
        return 2
    looked_up = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        rows = [r for r in fetch(service, region, args.api) if matches(r, region, args.flavor, args.grep)]
    except Unanswered as err:
        print("price_lookup: %s; no price can be given" % err, file=sys.stderr)
        return 2
    if not rows:
        print("not found: the price API has no row for %s in %s%s (looked up %s)"
              % (service, region, " with flavor " + args.flavor if args.flavor else "", looked_up))
        return 1
    for row in rows[:max(1, args.limit)]:
        print(show(row, looked_up) + "\n")
    if len(rows) > args.limit:
        print("%d more rows match; narrow with --flavor or --grep" % (len(rows) - args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
