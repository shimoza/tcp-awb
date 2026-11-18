"""Owner pulls of Financial Dashboard v2 and cache-only billed cost reports.

The first nonempty pull pins the first row's contract. Bootstrap with a contract-scoped
service account token. Subsequent pulls filter locally even when the API sees more contracts.
Money and quantities stay decimal. Published day totals and their month sum use cents;
product and resource amounts retain precision for subsequent window and project reports.
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from awb import config, projects, vault
from awb.tcp import tenants

BASE = "https://api-enterprise-dashboard.otc-service.com"
BERLIN = ZoneInfo("Europe/Berlin")
SOURCE = "financial dashboard api v2"
ZERO = Decimal(0)


class BilledError(Exception):
    """A fixed diagnostic that never contains a response or credential."""


def now():
    return dt.datetime.now(dt.timezone.utc)


def money(value):
    return format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError
    return result


def month_value(value):
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise BilledError("the month reads YYYY-MM")
    day_value(value + "-01")
    return value


def day_value(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BilledError("the date reads YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise BilledError("the date is not valid") from None


def folder(p, alias):
    if not tenants._ALIAS_RE.fullmatch(alias):
        raise BilledError("an alias reads like test-1")
    path = tenants.folder(p, alias)
    # Refuse links in ancestors as well as in the final file.
    if path.resolve() != path.absolute():
        raise BilledError("the billed folder is a link")
    return path


def regular(path):
    if path.parent.resolve() != path.parent.absolute():
        raise BilledError("the billed file is a link or not a regular file")
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise BilledError("the billed file is a link or not a regular file")


def read(path):
    regular(path)
    try:
        return json.loads(tenants._read_regular(path))
    except FileNotFoundError:
        raise
    except (OSError, ValueError, tenants.TenantError):
        raise BilledError("the billed file cannot be read") from None


def write(path, data):
    regular(path)
    fd, name = tempfile.mkstemp(prefix=".billed-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, default=str, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fchmod(out.fileno(), 0o664)
            os.fsync(out.fileno())
        regular(path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A bearer token must not follow a redirect to another service.
        return None


def http(request):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=60)


def rows(alias, params, hourly=False):
    try:
        token = tenants.Store().show("awb/fd/" + alias)
        url = BASE + ("/v2/hourly/consumption/" if hourly else "/v2/daily/consumption/")
        request = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                         headers={"Authorization": "Bearer " + token})
        with http(request) as response:
            for line in response:
                if not line.strip():
                    continue
                row = json.loads(line, parse_float=Decimal, parse_int=Decimal)
                if not isinstance(row, dict):
                    raise ValueError
                yield row
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise BilledError("the dashboard token of %s is refused (expired?): the owner makes a new one"
                              % alias) from None
        raise BilledError("the dashboard request failed for " + alias) from None
    except Exception:
        # Never echo subprocess diagnostics, URLs, response text or parser values.
        raise BilledError("the dashboard pull failed for %s (timeout or broken data)" % alias) from None


def field(row, key, nullable=False):
    value = row[key]
    if value is None and nullable:
        return ""
    if key == "contract" and isinstance(value, (int, Decimal)):
        value = str(value)
    if not isinstance(value, str) or len(value) > 512 or (value and not value.isprintable()):
        raise ValueError
    return value


def bucket():
    return {"amount": ZERO, "quantities": {}}


def add(target, row):
    target["amount"] += number(row["amount"])
    unit = field(row, "quantity_type")
    target["quantities"][unit] = target["quantities"].get(unit, ZERO) + number(row["quantity"])


def aggregate(data, row):
    product = field(row, "product")
    description = field(row, "product_description")
    day = field(row, "consumption_date")[:10]
    for dimension, key in (("days", day), ("products", product), ("regions", field(row, "region")),
                           ("consumption_types", field(row, "consumption_type")),
                           ("projects", field(row, "project_id", True))):
        target = data[dimension].setdefault(key, bucket())
        add(target, row)
        if dimension == "products":
            target["description"] = description
    resource = data["resources"].setdefault(field(row, "resource_id", True), {})
    target = resource.setdefault(product, dict(bucket(), description=description, days={}))
    add(target, row)
    add(target["days"].setdefault(day, bucket()), row)


def owner_metadata(p, alias, data):
    """Publish only handles and ids needed to join the public snapshots to these bills."""
    path = folder(p, alias) / tenants.HANDLES
    try:
        handles = read(path).get("handles", {})
    except FileNotFoundError:
        handles = {}
    data["resource_handles"] = {h: rid for h, rid in handles.items() if rid in data["resources"]}
    data["resource_disks"] = {}
    snaps = tenants.snapshots(p, alias)
    if snaps:
        for item in read(snaps[-1]).get("items", []):
            if item.get("kind") == "evs" and item.get("handle") in handles:
                for server in item.get("attached", "").split(","):
                    if server in handles:
                        data["resource_disks"].setdefault(handles[server], []).append(handles[item["handle"]])


def _pull(p, alias, period, hourly):
    where = folder(p, alias)
    if hourly:
        day_value(period)
        params = {"date": period}
    else:
        month_value(period)
        year, month = period.split("-")
        params = {"year": year, "month": month}
    config.make_dir(where, 0o2770, shared=True)
    dest = where / ("billed-hourly-" if hourly else "billed-")
    dest = dest.with_name(dest.name + period + ".json")
    contract_file = where / "billed-contract.json"
    with tenants._Lock(where):
        regular(dest)
        try:
            contract = read(contract_file)["contract"]
        except FileNotFoundError:
            contract = None
        data = {"alias": alias, "source": SOURCE, "pulled_at": now().isoformat(),
                "date" if hourly else "month": period, "rows": 0, "dropped_contract_rows": 0,
                "statuses": {}, "resources": {}}
        if not hourly:
            data.update({k: {} for k in ("days", "products", "regions", "consumption_types", "projects")})
        try:
            for row in rows(alias, params, hourly):
                row_contract = field(row, "contract")
                if not row_contract:
                    raise ValueError
                if contract is None:
                    contract = row_contract
                if row_contract != contract:
                    data["dropped_contract_rows"] += 1
                    continue
                start = field(row, "consumption_date")
                day_value(start[:10])
                if not start.startswith(period):
                    raise ValueError
                amount, quantity = number(row["amount"]), number(row["quantity"])
                if quantity < 0:
                    raise ValueError
                status = field(row, "status")
                if hourly:
                    stamp = dt.datetime.fromisoformat(start)
                    if stamp.utcoffset() is None or stamp.astimezone(BERLIN).isoformat() != stamp.isoformat():
                        raise ValueError
                    product, rid = field(row, "product"), field(row, "resource_id", True)
                    unit = field(row, "quantity_type")
                    # an "h" row above 1 is several units in one hour (a licence of 4 units bills 4.0 per hour)
                    if unit not in ("h", "GB", "pc"):
                        raise ValueError
                    target = data["resources"].setdefault(rid, {}).setdefault(product, [])
                    target.append({"start": start, "quantity": str(quantity), "quantity_type": unit,
                                   "amount": str(amount)})
                else:
                    aggregate(data, row)
                data["rows"] += 1
                data["statuses"][status] = data["statuses"].get(status, 0) + 1
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise BilledError("the dashboard pull failed for %s (broken data)" % alias) from None
        data["contract"] = contract
        if not hourly:
            # The displayed month is exactly the sum of the displayed days.
            for day in data["days"].values():
                day["amount"] = money(day["amount"])
            data["total"] = money(sum((number(d["amount"]) for d in data["days"].values()), ZERO))
        owner_metadata(p, alias, data)
        # Nothing is published until the complete stream and metadata have been checked.
        if contract is not None:
            write(contract_file, {"contract": contract})
        write(dest, data)
        if hourly:
            cutoff = now().astimezone(BERLIN).date() - dt.timedelta(days=61)
            files = sorted(where.glob("billed-hourly-????-??-??.json"))
            for old in files:
                if old.name[14:24] < cutoff.isoformat() or old in files[:-62]:
                    regular(old)
                    old.unlink()
        return data


def pull(p, alias, month):
    return _pull(p, alias, month, False)


def pull_hourly(p, alias, date):
    return _pull(p, alias, date, True)


def owner_command(p, args):
    try:
        # the work user has no route and no token; the steering session may pull (his word 2026-11-18: the
        # session uses the dashboard for its own cost counts)
        if config.is_work_user():
            raise BilledError("billed costs are pulled on the owner side (user ubuntu)")
        if args.date and not args.hourly or args.month and args.hourly:
            raise BilledError("--date needs --hourly; --month needs daily data")
        store = tenants.Store()
        aliases = [args.alias] if args.alias else sorted(
            f.stem for f in (store.root / "awb" / "fd").glob("*.gpg")
            if tenants._ALIAS_RE.fullmatch(f.stem))
        today = now().astimezone(BERLIN).date()
        if args.hourly:
            periods = [args.date] if args.date else [today.isoformat(), (today - dt.timedelta(days=1)).isoformat()]
        else:
            periods = [month_value(args.month)] if args.month else [today.strftime("%Y-%m")]
            if not args.month and today.day <= 3:
                periods.append((today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m"))
        failed = False
        for alias in aliases:
            summaries = []
            try:
                for period in periods:
                    data = (pull_hourly if args.hourly else pull)(p, alias, period)
                    summaries.append("%s %s EUR, %d rows, %d days" % (
                        period, money(sum((number(r["amount"]) for products_ in data["resources"].values()
                                           for rs in products_.values() for r in rs), ZERO))
                        if args.hourly else data["total"], data["rows"], 1 if args.hourly else len(data["days"])))
                print(alias + ": " + "; ".join(summaries))
            except BilledError as err:
                print(str(err), file=sys.stderr)
                failed = True
        return 2 if failed else 0
    except (BilledError, OSError, ValueError, KeyError, TypeError, tenants.TenantError) as err:
        # BilledError messages are fixed safe text.
        print(str(err) if isinstance(err, BilledError) else "the billed cache cannot be updated", file=sys.stderr)
        return 2


def project_resources(p, alias, code, data):
    if not tenants._CODE_RE.fullmatch(code):
        raise BilledError("a project code reads like tcp-q7m4")
    ids = set()
    project = next((pr for pr in projects.load(p) if pr.code == code), None)
    if project:
        path = Path(project.path) / "RESOURCES.md"
        regular(path)
        try:
            text = tenants._read_regular(path)
        except FileNotFoundError:
            text = ""
        head = None
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                head = None
                continue
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if "id" in [c.lower() for c in cells]:
                head = [c.lower() for c in cells]
            elif head and len(cells) == len(head):
                rid = dict(zip(head, cells))["id"]
                if rid and not set(rid) <= set("-: "):
                    ids.add(rid)
    snaps = tenants.snapshots(p, alias)
    if snaps:
        handles = data.get("resource_handles", {})
        for item in read(snaps[-1]).get("items", []):
            if item.get("project") == code:
                rid = handles.get(item.get("handle"))
                if rid:
                    ids.add(rid)
    return ids


def age(data):
    return max(0, int((now() - dt.datetime.fromisoformat(data["pulled_at"])).total_seconds()))


def daily_report(data, ids=None):
    result = {"alias": data["alias"], "month": data["month"], "age_seconds": age(data)}
    if ids is None:
        result.update(total=data["total"], days=data["days"], products=data["products"])
    else:
        days, products_ = {}, {}
        for rid in ids:
            for product, value in data["resources"].get(rid, {}).items():
                target = products_.setdefault(product, {"description": value["description"], "amount": ZERO})
                target["amount"] += number(value["amount"])
                for day, value_ in value["days"].items():
                    days.setdefault(day, {"amount": ZERO})["amount"] += number(value_["amount"])
        total = sum((Decimal(money(d["amount"])) for d in days.values()), ZERO)
        result.update(total=money(total), days=days, products=products_,
                      not_of_this_project=money(number(data["total"]) - total))
    result["days"] = {k: money(v["amount"]) for k, v in result["days"].items()}
    result["products"] = {k: {"description": v["description"], "amount": money(v["amount"])}
                          for k, v in result["products"].items()}
    return result


def window(date, start=None, end=None):
    day = day_value(date)
    def at(value, fallback):
        if value is None:
            return fallback
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise BilledError("window times read HH:MM in Europe/Berlin")
        local = dt.datetime.combine(day, dt.time.fromisoformat(value), BERLIN)
        utc = local.astimezone(dt.timezone.utc)
        if utc.astimezone(BERLIN).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise BilledError("the window time does not exist in Europe/Berlin")
        if local.replace(fold=1).utcoffset() != local.utcoffset():
            raise BilledError("the window time is ambiguous in Europe/Berlin")
        return utc
    lo = at(start, dt.datetime.combine(day, dt.time(), BERLIN).astimezone(dt.timezone.utc))
    hi = at(end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), BERLIN).astimezone(dt.timezone.utc))
    if hi <= lo:
        raise BilledError("--to must be after --from on the same day")
    return lo, hi


def iso(value):
    return value.astimezone(BERLIN).isoformat(timespec="seconds")


def hourly_report(data, ids, lo, hi):
    result = {"alias": data["alias"], "date": data["date"], "age_seconds": age(data),
              "note": "the dashboard is about 1.5 h behind", "window": [iso(lo), iso(hi)],
              "resources": {}, "no_row_resources": {}}
    for rid in sorted(ids):
        products_ = {}
        for product, records in data["resources"].get(rid, {}).items():
            intervals, quantities, amount = [], {}, ZERO
            for row in records:
                start = dt.datetime.fromisoformat(row["start"]).astimezone(dt.timezone.utc)
                quantity, unit = number(row["quantity"]), row["quantity_type"]
                # A row covers its hour slot from its start to the next full hour. An "h" row of at most 1 is
                # the run time from its start (17:21:17 with 0.64528 runs to 18:00:00, a full-hour row with 0.5
                # stops at half past); above 1 it is several units over the whole slot. GB and pc fill the slot.
                slot_end = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
                slot = int((slot_end - start).total_seconds())
                if unit == "h" and quantity <= 1:
                    seconds = min(slot, int((quantity * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
                else:
                    seconds = slot
                end = start + dt.timedelta(seconds=seconds)
                left, right = max(lo, start), min(hi, end)
                if right <= left or seconds <= 0:
                    continue
                fraction = Decimal(str((right - left).total_seconds())) / Decimal(seconds)
                amount += number(row["amount"]) * fraction
                quantities[unit] = quantities.get(unit, ZERO) + quantity * fraction
                intervals.append((left, right))
            merged = []
            for left, right in sorted(intervals):
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            gaps, cursor = [], lo
            for left, right in merged:
                if left > cursor:
                    gaps.append([iso(cursor), iso(left)])
                cursor = max(cursor, right)
            if cursor < hi:
                gaps.append([iso(cursor), iso(hi)])
            products_[product] = {"intervals": [[iso(a), iso(b)] for a, b in merged], "no_row": gaps,
                                  "quantities": {k: str(v.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))
                                                 for k, v in quantities.items()}, "amount": money(amount)}
        result["resources"][rid] = products_
        if not products_:
            result["no_row_resources"][rid] = [[iso(lo), iso(hi)]]
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(prog="awb cost", description="Billed costs from the owner's cache only")
    sub = ap.add_subparsers(dest="command", required=True)
    parser = sub.add_parser("billed", help="daily totals or hourly resource windows from Financial Dashboard caches")
    for option in ("tenant", "month", "project", "resource", "date", "from", "to"):
        parser.add_argument("--" + option, dest="start" if option == "from" else "end" if option == "to" else option)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hourly", action="store_true", help="hourly rows for resources of --project")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        hourly = bool(args.hourly or args.resource)
        if args.hourly and not (args.project or args.resource):
            raise BilledError("--hourly needs --project or --resource")
        if (args.date or args.start or args.end) and not hourly or args.month and hourly:
            raise BilledError("--date, --from and --to need hourly data; --month needs daily data")
        if bool(args.start) != bool(args.end):
            raise BilledError("use --from and --to together")
        p = config.paths()
        today = now().astimezone(BERLIN).date()
        period = args.date or today.isoformat() if hourly else month_value(args.month or today.strftime("%Y-%m"))
        bounds = window(period, args.start, args.end) if hourly else None
        aliases = [args.tenant] if args.tenant else sorted(
            f.name for f in tenants.root(p).iterdir() if tenants._ALIAS_RE.fullmatch(f.name) and f.is_dir())
        reports = []
        for alias in aliases:
            path = folder(p, alias) / (("billed-hourly-" if hourly else "billed-") + period + ".json")
            try:
                data = read(path)
            except FileNotFoundError:
                reports.append({"alias": alias, "missing": period})
                continue
            ids = project_resources(p, alias, args.project, data) if args.project else None
            if hourly:
                if args.resource:
                    selected = {args.resource, *data.get("resource_disks", {}).get(args.resource, [])}
                    ids = selected if ids is None else selected & ids
                reports.append(hourly_report(data, ids or set(), *bounds))
            else:
                reports.append(daily_report(data, ids))
        if args.json:
            print(json.dumps(reports, sort_keys=True))
        else:
            for report in reports:
                print(report["alias"] + ": " + ("no cache for " + report["missing"] if "missing" in report else
                      "%s; cache age %ds" % (report.get("month", report.get("date")), report["age_seconds"])))
                if "total" in report:
                    print("  %s EUR" % report["total"])
                    for product, value in sorted(report["products"].items(),
                                                 key=lambda kv: number(kv[1]["amount"]), reverse=True)[:10]:
                        print("  %s %s: %s EUR" % (product, value["description"], value["amount"]))
                    for day, amount in sorted(report["days"].items()):
                        print("  %s: %s EUR" % (day, amount))
                    if "not_of_this_project" in report:
                        print("  not of this project: %s EUR" % report["not_of_this_project"])
                if "resources" in report:
                    print("  " + report["note"])
                    for rid, products_ in report["resources"].items():
                        if not products_:
                            print("  %s: no rows from %s to %s" % (rid, *report["window"]))
                        for product, value in products_.items():
                            print("  %s %s: %s EUR; %s" % (rid, product, value["amount"], value["quantities"]))
                            for label, key in (("interval", "intervals"), ("no row", "no_row")):
                                for a, b in value[key]:
                                    print("    %s %s to %s" % (label, a, b))
        return 0
    except (BilledError, OSError, ValueError, KeyError, TypeError, InvalidOperation,
            tenants.TenantError, projects.ProjectError) as err:
        print(str(err) if isinstance(err, BilledError) else "the billed cache cannot be read", file=sys.stderr)
        return 2
