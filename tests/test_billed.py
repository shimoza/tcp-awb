"""Invented Financial Dashboard rows. Every external boundary is replaced locally."""
import datetime as dt
from decimal import Decimal
import io
import json
import os
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from awb import cli, config
from awb.tcp import billed, tenants


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = config.Paths(tmp_path / "shared", tmp_path / "vault", tmp_path / "projects")
    p.shared.mkdir()
    p.projects_root.mkdir()
    monkeypatch.setattr(config, "paths", lambda: p)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(billed.vault, "in_assistant_session", lambda: False)
    monkeypatch.setattr(billed, "now", lambda: dt.datetime(2026, 11, 18, 10, tzinfo=dt.timezone.utc))
    secret = "pass" + "word" + "-invented"
    store_root = tmp_path / "store"
    entries = store_root / "awb" / "fd"
    entries.mkdir(parents=True)
    (entries / "test-1.gpg").touch()
    monkeypatch.setattr(tenants, "Store", lambda: SimpleNamespace(root=store_root, show=lambda entry: secret))
    state = SimpleNamespace(p=p, rows=[], requests=[], secret=secret, error=None)

    def http(request):
        state.requests.append(request)
        assert request.get_header("Authorization") == "Bearer " + secret
        if state.error:
            raise state.error
        return io.BytesIO(b"\n".join(json.dumps(r).encode() if isinstance(r, dict) else r for r in state.rows))

    monkeypatch.setattr(billed, "http", http)
    return state


def row(**changes):
    result = dict(aggregation_id="aggregation-1", amount="0.10", consumption_date="2026-11-17",
                  consumption_type="usage", contract="contract-1", product="compute-small",
                  product_description="Small compute", project_id="region-project-1", quantity="1",
                  quantity_type="h", region="eu-de", resource_id="vm-1", status="billed")
    result.update(changes)
    return result


def hourly(start, quantity, **changes):
    return row(consumption_date="2026-11-17T" + start + "+01:00", quantity=quantity,
               usage_id="usage-1", amount_excluding_discount="1", bill_cycle_day=1,
               external_priceplan="plan-1", **changes)


def cache(env, name="billed-2026-11.json"):
    return tenants.folder(env.p, "test-1") / name


def owner_args(**changes):
    args = dict(alias="test-1", month="2026-11", hourly=False, date=None)
    args.update(changes)
    return SimpleNamespace(**args)


def test_pull_file_aggregates_privacy_permissions_and_request(env):
    env.rows = [row(project_name="organisation-like-suffix", tags={"private-key": "private-value"}),
                row(amount="0.20", product="obs", product_description="Object storage", resource_id=None,
                    quantity_type="GB", quantity="4", region="eu-nl", consumption_type="storage", status="pending")]
    data = billed.pull(env.p, "test-1", "2026-11")
    stored = json.loads(cache(env).read_text())
    assert stored["total"] == "0.30" and stored["rows"] == 2
    assert stored["days"]["2026-11-17"]["amount"] == "0.30"
    assert stored["products"]["compute-small"]["description"] == "Small compute"
    assert stored["products"]["obs"]["quantities"] == {"GB": "4"}
    assert stored["regions"]["eu-nl"]["amount"] == "0.20"
    assert stored["consumption_types"]["storage"]["amount"] == "0.20"
    assert stored["projects"]["region-project-1"]["amount"] == "0.30"
    assert stored["resources"][""]["obs"]["amount"] == "0.20"
    assert stored["statuses"] == {"billed": 1, "pending": 1}
    assert stored["source"] == billed.SOURCE and stored["pulled_at"] == billed.now().isoformat()
    assert os.stat(cache(env)).st_mode & 0o777 == 0o664
    assert all(s not in cache(env).read_text() for s in ("organisation-like", "private-key", "private-value", env.secret))
    url = urllib.parse.urlsplit(env.requests[0].full_url)
    assert url.path == "/v2/daily/consumption/"
    assert urllib.parse.parse_qs(url.query) == {"year": ["2026"], "month": ["11"]}
    assert data["total"] == stored["total"]


def test_contract_is_pinned_across_daily_and_hourly_pulls(env):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    env.rows = [row(contract="other-contract", amount="900"), row(amount="0.20")]
    data = billed.pull(env.p, "test-1", "2026-11")
    assert data["total"] == "0.20" and data["rows"] == 1 and data["dropped_contract_rows"] == 1
    env.rows = [hourly("10:00:00", "1", contract="other-contract"), hourly("11:00:00", "1")]
    data = billed.pull_hourly(env.p, "test-1", "2026-11-17")
    assert data["rows"] == 1 and data["dropped_contract_rows"] == 1
    assert data["resources"]["vm-1"]["compute-small"][0]["start"].endswith("11:00:00+01:00")


def test_refused_token_keeps_cache_and_never_prints_secret(env, capsys):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    before = cache(env).read_bytes()
    for status in (401, 403):
        env.error = urllib.error.HTTPError("https://invalid/" + env.secret, status, env.secret, {}, None)
        assert billed.owner_command(env.p, owner_args()) == 2
        out = capsys.readouterr()
        assert out.err.strip() == "the dashboard token of test-1 is refused (expired?): the owner makes a new one"
        assert env.secret not in out.out + out.err
        assert cache(env).read_bytes() == before


def test_timeout_and_broken_stream_publish_nothing(env, capsys):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    before = cache(env).read_bytes()
    contract_before = cache(env, "billed-contract.json").read_bytes()
    env.rows = [row(amount="700"), b"broken " + env.secret.encode()]
    assert billed.owner_command(env.p, owner_args()) == 2
    assert cache(env).read_bytes() == before
    env.error = TimeoutError(env.secret)
    assert billed.owner_command(env.p, owner_args()) == 2
    assert cache(env).read_bytes() == before
    assert cache(env, "billed-contract.json").read_bytes() == contract_before
    assert env.secret not in str(capsys.readouterr())


def test_decimal_totals_round_only_after_summing_and_month_equals_days(env):
    env.rows = [row(amount="0.005") for _ in range(3)] + [row(amount="0.005", consumption_date="2026-11-18")]
    data = billed.pull(env.p, "test-1", "2026-11")
    assert data["products"]["compute-small"]["amount"] == Decimal("0.020")
    assert data["total"] == "0.03"
    stored = json.loads(cache(env).read_text())
    assert Decimal(stored["total"]) == sum(Decimal(d["amount"]) for d in stored["days"].values())
    assert data["total"] == billed.money(sum(Decimal(billed.money(d["amount"])) for d in data["days"].values()))
    assert billed.daily_report(json.loads(cache(env).read_text()))["days"] == {
        "2026-11-17": "0.02", "2026-11-18": "0.01"}


def test_reader_without_project_is_cache_only(env, capsys, monkeypatch):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    monkeypatch.setattr(tenants, "Store", lambda: pytest.fail("reader touched pass"))
    monkeypatch.setattr(billed, "http", lambda req: pytest.fail("reader touched network"))
    assert billed.main(["billed", "--tenant", "test-1", "--month", "2026-11", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)[0]
    assert data["total"] == "0.10" and data["age_seconds"] == 0
    assert data["products"]["compute-small"]["amount"] == "0.10"
    assert billed.main(["billed", "--month", "2026-11"]) == 0
    text = capsys.readouterr().out
    assert "2026-11-17: 0.10 EUR" in text and "Small compute: 0.10 EUR" in text


def setup_project(env, monkeypatch):
    path = env.p.projects_root / "tcp-q7m4"
    path.mkdir()
    (path / "RESOURCES.md").write_text("| handle | id | state |\n|---|---|---|\n"
                                       "| ecs-1 | vm-deleted | deleted |\n| ecs-2 | vm-live | live |\n")
    monkeypatch.setattr(billed.projects, "load", lambda p: [SimpleNamespace(code="tcp-q7m4", path=str(path))])
    where = tenants.folder(env.p, "test-1")
    (where / "snapshots").mkdir(parents=True)
    (where / "handles.json").write_text(json.dumps({"handles": {"ecs-3": "vm-tagged"}}))
    (where / "snapshots" / "2026-11-18.json").write_text(json.dumps({"items": [
        {"handle": "ecs-3", "project": "tcp-q7m4", "kind": "ecs"}]}))


def test_project_reader_joins_deleted_live_and_latest_tagged_resources_only(env, monkeypatch, capsys):
    setup_project(env, monkeypatch)
    env.rows = [row(resource_id=rid, amount=amount) for rid, amount in
                (("vm-deleted", "1"), ("vm-live", "2"), ("vm-tagged", "4"), ("outsider", "90"))]
    billed.pull(env.p, "test-1", "2026-11")
    cache(env, "handles.json").unlink()  # A reader has no access to the owner's handle file.
    assert billed.main(["billed", "--tenant", "test-1", "--month", "2026-11", "--project", "tcp-q7m4", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert result["total"] == "7.00" and result["not_of_this_project"] == "90.00"
    assert result["days"] == {"2026-11-17": "7.00"}
    assert "outsider" not in json.dumps(result)
    assert billed.main(["billed", "--month", "2026-11", "--project", "tcp-q7m4"]) == 0
    assert "not of this project: 90.00 EUR" in capsys.readouterr().out


def test_hourly_ten_minute_window_uses_start_and_stop_seconds(env):
    env.rows = [hourly("17:51:17", "0.14528", amount="5.23"), hourly("18:00:00", "0.08333", amount="3")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    data = json.loads(cache(env, "billed-hourly-2026-11-17.json").read_text())
    result = billed.hourly_report(data, {"vm-1"}, *billed.window("2026-11-17", "17:55", "18:05"))
    product = result["resources"]["vm-1"]["compute-small"]
    assert product["intervals"] == [["2026-11-17T17:55:00+01:00", "2026-11-17T18:05:00+01:00"]]
    assert product["no_row"] == [] and product["amount"] == "6.00"
    assert abs(Decimal(product["quantities"]["h"]) - Decimal(1) / 6) < Decimal("0.00001")
    result = billed.hourly_report(data, {"vm-1"}, *billed.window("2026-11-17", "17:50", "18:10"))
    product = result["resources"]["vm-1"]["compute-small"]
    assert product["intervals"][0][0] == "2026-11-17T17:51:17+01:00"
    assert product["no_row"] == [["2026-11-17T17:50:00+01:00", "2026-11-17T17:51:17+01:00"],
                                  ["2026-11-17T18:05:00+01:00", "2026-11-17T18:10:00+01:00"]]
    assert data["resources"]["vm-1"]["compute-small"][0]["start"] == "2026-11-17T17:51:17+01:00"
    assert data["pulled_at"] and data["source"] == billed.SOURCE
    assert os.stat(cache(env, "billed-hourly-2026-11-17.json")).st_mode & 0o777 == 0o664
    assert urllib.parse.urlsplit(env.requests[0].full_url).path == "/v2/hourly/consumption/"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(env.requests[0].full_url).query) == {"date": ["2026-11-17"]}


def test_stopped_vm_flavor_ends_while_attached_disk_billing_continues(env, capsys):
    where = tenants.folder(env.p, "test-1")
    (where / "snapshots").mkdir(parents=True)
    (where / "handles.json").write_text(json.dumps({"handles": {"ecs-1": "vm-1", "evs-1": "disk-1"}}))
    (where / "snapshots" / "2026-11-18.json").write_text(json.dumps({"items": [
        {"kind": "evs", "handle": "evs-1", "attached": "ecs-1"}]}))
    env.rows = [hourly("17:00:00", "0.5", amount="1")]
    env.rows += [hourly(h + ":00:00", "40", quantity_type="GB", resource_id="disk-1", product="ssd", amount="0.40")
                 for h in ("17", "18")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    assert billed.main(["billed", "--tenant", "test-1", "--resource", "vm-1", "--date", "2026-11-17",
                        "--from", "17:00", "--to", "19:00", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    vm = result["resources"]["vm-1"]["compute-small"]
    disk = result["resources"]["disk-1"]["ssd"]
    assert vm["no_row"] == [["2026-11-17T17:30:00+01:00", "2026-11-17T19:00:00+01:00"]]
    assert vm["amount"] == "1.00" and Decimal(vm["quantities"]["h"]) == Decimal("0.5")
    assert disk["no_row"] == [] and disk["amount"] == "0.80" and Decimal(disk["quantities"]["GB"]) == Decimal("80")
    assert disk["intervals"] == [["2026-11-17T17:00:00+01:00", "2026-11-17T19:00:00+01:00"]]
    assert "1.5 h behind" in result["note"]


def test_project_hourly_excludes_other_resources(env, monkeypatch, capsys):
    setup_project(env, monkeypatch)
    env.rows = [hourly("12:00:00", "1", resource_id=rid) for rid in ("vm-deleted", "vm-tagged", "outsider")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    assert billed.main(["billed", "--tenant", "test-1", "--project", "tcp-q7m4", "--hourly",
                        "--date", "2026-11-17", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert set(result["resources"]) == {"vm-deleted", "vm-live", "vm-tagged"}
    assert result["resources"]["vm-tagged"]["compute-small"]["amount"] == "0.10"
    assert result["no_row_resources"]["vm-live"] == [["2026-11-17T00:00:00+01:00",
                                                        "2026-11-18T00:00:00+01:00"]]


def test_cache_link_fifo_and_directory_are_refused_for_read_and_pull(env, tmp_path):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    path = cache(env)
    target = tmp_path / "target"
    target.write_text(path.read_text())
    for kind in ("link", "fifo", "directory"):
        path.unlink()
        if kind == "link":
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
        with pytest.raises(billed.BilledError, match="link or not a regular file"):
            billed.read(path)
        with pytest.raises(billed.BilledError, match="link or not a regular file"):
            billed.pull(env.p, "test-1", "2026-11")
        if kind == "directory":
            path.rmdir()
            path.touch()
    assert json.loads(target.read_text())["total"] == "0.10"


def test_hourly_retention_keeps_at_most_62_days(env):
    where = tenants.folder(env.p, "test-1")
    where.mkdir(parents=True)
    today = billed.now().date()
    for n in range(70):
        day = today - dt.timedelta(days=n)
        (where / ("billed-hourly-" + day.isoformat() + ".json")).write_text("{}")
    env.rows = [hourly("10:00:00", "1")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    files = sorted(where.glob("billed-hourly-*.json"))
    assert len(files) == 62
    assert files[0].name == "billed-hourly-2026-09-18.json"


def test_owner_defaults_month_rollover_hourly_days_and_alias_discovery(env, monkeypatch, capsys):
    monkeypatch.setattr(billed, "now", lambda: dt.datetime(2026, 1, 2, 12, tzinfo=dt.timezone.utc))
    calls = []
    def pull(p, alias, month):
        calls.append((alias, month))
        return {"total": "3.00", "rows": 2, "days": {"day": {}}}
    monkeypatch.setattr(billed, "pull", pull)
    assert billed.owner_command(env.p, owner_args(alias=None, month=None)) == 0
    assert calls == [("test-1", "2026-01"), ("test-1", "2025-12")]
    assert len(capsys.readouterr().out.splitlines()) == 1
    calls.clear()
    def pull_hourly(p, alias, date):
        calls.append((alias, date))
        return {"rows": 0, "resources": {}}
    monkeypatch.setattr(billed, "pull_hourly", pull_hourly)
    assert billed.owner_command(env.p, owner_args(month=None, hourly=True)) == 0
    assert calls == [("test-1", "2026-01-02"), ("test-1", "2026-01-01")]


def test_cli_routes_billed_and_refuses_work_user_pull(env, monkeypatch, capsys):
    env.rows = [row()]
    assert tenants.main(["billed", "test-1", "--month", "2026-11"]) == 0
    assert "2026-11 0.10 EUR, 1 rows, 1 days" in capsys.readouterr().out
    assert cli.DELEGATED["cost"][0] == "tcp.billed"
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    before = len(env.requests)
    assert tenants.main(["billed", "test-1"]) == 2
    assert "on the owner side (user ubuntu)" in capsys.readouterr().err
    assert len(env.requests) == before
    assert billed.main(["billed", "--tenant", "test-1", "--month", "2026-11"]) == 0


def test_invalid_periods_and_ambiguous_berlin_window_are_refused(env):
    for value in ("2026-13", "2026-1", "../2026-11"):
        with pytest.raises(billed.BilledError):
            billed.pull(env.p, "test-1", value)
    for date, start, end in (("2026-03-29", "02:30", "03:30"), ("2026-10-25", "02:30", "03:30"),
                             ("2026-11-17", "18:00", "17:00")):
        with pytest.raises(billed.BilledError):
            billed.window(date, start, end)
    lo, hi = billed.window("2026-10-25")
    assert (hi - lo).total_seconds() == 25 * 3600
    assert not env.requests


def test_nonfinite_money_and_wrong_period_leave_previous_file(env):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    before = cache(env).read_bytes()
    for bad in (row(amount="NaN"), row(amount="Infinity"), row(consumption_date="2026-10-17")):
        env.rows = [bad]
        with pytest.raises(billed.BilledError):
            billed.pull(env.p, "test-1", "2026-11")
        assert cache(env).read_bytes() == before


def test_atomic_replace_failure_preserves_previous_cache(env, monkeypatch):
    env.rows = [row()]
    billed.pull(env.p, "test-1", "2026-11")
    before = cache(env).read_bytes()
    replace = os.replace
    def fail(source, dest):
        if dest == cache(env):
            raise OSError("invented write failure")
        replace(source, dest)
    monkeypatch.setattr(os, "replace", fail)
    env.rows = [row(amount="500")]
    with pytest.raises(OSError):
        billed.pull(env.p, "test-1", "2026-11")
    assert cache(env).read_bytes() == before
    assert not list(cache(env).parent.glob(".billed-*"))


def test_an_hour_row_of_several_units_is_kept_and_stays_inside_its_hour(env):
    """Live 2026-11-18: a licence bills 4.0 h per hour; the pull refused it and a window stretched it 4 hours."""
    env.rows = [hourly("10:00:00", "4.0", amount="2.00", product="licence-4", resource_id="db-1")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    data = json.loads(cache(env, "billed-hourly-2026-11-17.json").read_text())
    result = billed.hourly_report(data, {"db-1"}, *billed.window("2026-11-17", "10:30", "12:00"))
    product = result["resources"]["db-1"]["licence-4"]
    assert product["intervals"] == [["2026-11-17T10:30:00+01:00", "2026-11-17T11:00:00+01:00"]]
    assert product["amount"] == "1.00"
    assert Decimal(product["quantities"]["h"]) == Decimal("2.0")
    assert product["no_row"] == [["2026-11-17T11:00:00+01:00", "2026-11-17T12:00:00+01:00"]]


def test_a_start_row_never_runs_past_the_next_full_hour(env):
    env.rows = [hourly("17:21:17", "0.99", amount="1.00")]
    billed.pull_hourly(env.p, "test-1", "2026-11-17")
    data = json.loads(cache(env, "billed-hourly-2026-11-17.json").read_text())
    result = billed.hourly_report(data, {"vm-1"}, *billed.window("2026-11-17", "17:00", "19:00"))
    assert result["resources"]["vm-1"]["compute-small"]["intervals"] == [
        ["2026-11-17T17:21:17+01:00", "2026-11-17T18:00:00+01:00"]]
