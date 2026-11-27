"""B-097: a tenant is named tcp-NNNNN (five or six digits) and test-NNNNN, its old name, is a synonym for a window of
30 days from a date in rules/tenant-alias-window.txt: one warning line inside it, a refusal after it. A tenant alias is
never read as a project code (tcp- and four characters) and a project code never as a tenant. Every name here is
invented (tests/fixtures.py); the window is a file of the test and the day is the real one."""
from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

import pytest

from awb import codes, labclient, tenantname
from awb.tcp import cloud, keys, labadmin, tenants
from tests import fixtures as fx

OLD, NEW = fx.TENANT_ONE, fx.TENANT_ONE_NEW
PROJECT = "tcp-q7m4"


def window(tmp_path, monkeypatch, days_ago: int | None) -> datetime.date | None:
    """A rules file of the test that opened `days_ago` days ago (None: no file); the last day it is open."""
    f = tmp_path / "tenant-alias-window.txt"
    monkeypatch.setattr(tenantname, "WINDOW_FILE", f)
    if days_ago is None:
        return None
    start = datetime.date.today() - datetime.timedelta(days=days_ago)
    f.write_text("# a window of the test\nwindow-from %s\n" % start.isoformat(), encoding="utf-8")
    return start + datetime.timedelta(days=30)


@pytest.fixture
def key_file(tmp_path):
    f = tmp_path / "keys"
    f.write_text("ak=AKINVENTED\n" + "s" + "k=" + "invented" + "-value\n", encoding="utf-8")
    os.chmod(f, 0o600)
    return f


@pytest.fixture
def old_register(home, key_file):
    """The register as the host has it before the rename: the tenant under its old name."""
    return tenants.add(home, OLD, "file:%s" % key_file, ["eu-de"], today="2026-09-01")


def test_the_new_name_is_the_same_tenant_as_the_old_one(home, old_register, tmp_path, monkeypatch, capsys):
    window(tmp_path, monkeypatch, 0)
    by_new = tenants.get(home, NEW)
    assert by_new == tenants.get(home, OLD) == old_register
    assert tenants.keys_of(by_new).ak == tenants.keys_of(tenants.get(home, OLD)).ak == "AKINVENTED"
    capsys.readouterr()
    assert tenants.main(["now", NEW]) == 0
    assert "%s: no snapshot yet" % OLD in capsys.readouterr().out


def test_the_new_name_works_with_no_window_at_all(home, old_register, tmp_path, monkeypatch):
    window(tmp_path, monkeypatch, None)
    assert tenants.get(home, NEW).alias == OLD


def test_a_renamed_register_is_read_by_either_name(home, key_file, tmp_path, monkeypatch):
    until = window(tmp_path, monkeypatch, 0)
    said = []
    renamed = tenants.add(home, NEW, "file:%s" % key_file, ["eu-de"], today="2026-09-01")
    assert tenants.get(home, NEW) == renamed
    assert tenants.resolve(OLD, [NEW], say=said.append) == NEW
    assert said == ["%s is the old name of %s, until %s" % (OLD, NEW, until.isoformat())]
    with pytest.raises(tenants.TenantError, match="is taken"):
        tenants.add(home, OLD, "file:%s" % key_file, ["eu-de"])


def test_the_old_name_inside_the_window_prints_one_warning_line(home, old_register, tmp_path, monkeypatch, capsys):
    until = window(tmp_path, monkeypatch, 0)
    assert tenants.get(home, OLD) == old_register
    got = capsys.readouterr()
    assert got.out == ""
    assert got.err == "%s is the old name of %s, until %s\n" % (OLD, NEW, until.isoformat())
    assert re.fullmatch(r"test-\d{5} is the old name of tcp-\d{5}, until \d{4}-\d{2}-\d{2}\n", got.err)


def test_the_last_day_of_the_window_still_takes_the_old_name(home, old_register, tmp_path, monkeypatch, capsys):
    window(tmp_path, monkeypatch, 30)
    assert tenants.get(home, OLD) == old_register
    assert "is the old name of" in capsys.readouterr().err


def test_the_old_name_after_the_window_is_refused(home, old_register, tmp_path, monkeypatch, capsys):
    window(tmp_path, monkeypatch, 31)
    with pytest.raises(tenants.TenantError, match="use %s" % NEW):
        tenants.get(home, OLD)
    assert tenants.get(home, NEW) == old_register        # the new name never lapses
    capsys.readouterr()
    assert tenants.main(["now", OLD]) == 1
    got = capsys.readouterr()
    assert "use %s" % NEW in got.err and "old name of" not in got.err


def test_no_rules_file_and_a_bad_date_refuse_the_old_name(home, old_register, tmp_path, monkeypatch):
    window(tmp_path, monkeypatch, None)
    with pytest.raises(tenants.TenantError):
        tenants.get(home, OLD)
    tenantname.WINDOW_FILE.write_text("window-from 2026-13-45\n", encoding="utf-8")
    assert tenantname.window_end() is None
    with pytest.raises(tenants.TenantError):
        tenants.get(home, OLD)


def test_the_shipped_rules_file_holds_one_window_of_30_days():
    shipped = Path(tenantname.__file__).resolve().parents[1] / "rules" / "tenant-alias-window.txt"
    assert tenantname.WINDOW_FILE == shipped
    assert isinstance(tenantname.window_end(shipped), datetime.date)


def test_only_a_five_or_six_digit_number_is_a_tenant_name():
    for alias in ("test-1", "test-2", "test-1234", "test-1234567", "test-0000a", "tcp-1234", "tcp-q7m4"):
        assert tenantname.canonical(alias, say=pytest.fail) == alias and tenantname.twin(alias) is None, alias
    assert tenantname.twin("tcp-123456") == "test-123456" and tenantname.twin("test-123456") == "tcp-123456"


def test_a_tenant_alias_is_never_a_project_code():
    for alias in (NEW, "tcp-123456", OLD, "tcp-10491"):
        assert not codes.is_project_code(alias), alias
        assert not codes.PROJECT_RE.search(alias), alias
        assert not re.fullmatch(codes.TCP_PROJECT_WIDE, alias), alias
        assert not re.fullmatch(codes.TCP_PROJECT, alias), alias
        assert not re.match(codes.TCP_PROJECT_WIDE + r"\b", alias), alias
        assert codes.is_tenant_alias(alias)
    assert codes.count_codes("in %s and %s" % (NEW, PROJECT)) == (0, 1)
    assert codes.PROJECT_RE.findall("%s %s %s" % (NEW, PROJECT, "tcp-1234567")) == [PROJECT]
    assert [m.group(0) for m in codes.TENANT_RE.finditer("%s %s %s" % (NEW, PROJECT, OLD))] == [NEW, OLD]


def test_a_project_code_is_never_a_tenant(home, old_register, key_file, tmp_path, monkeypatch):
    window(tmp_path, monkeypatch, 0)
    for code in (PROJECT, "tcp-q7m0", "tcp-1049", "hcs-q7m4"):
        assert not codes.is_tenant_alias(code) and not codes.TENANT_RE.search(code), code
        assert not tenants._ALIAS_RE.match(code), code
        assert not keys._ALIAS_RE.match(code), code
        with pytest.raises(tenants.TenantError):
            tenants.add(home, code, "file:%s" % key_file, ["eu-de"])
        with pytest.raises(tenants.TenantError, match="no tenant with the alias"):
            tenants.get(home, code)
    assert tenants.main(["now", PROJECT]) == 1
    assert [t.alias for t in tenants.load(home)] == [OLD]


def test_a_register_row_named_like_a_project_is_not_a_tenant(home, old_register):
    reg = tenants.root(home) / tenants.REGISTER
    reg.write_text(reg.read_text(encoding="utf-8") + "%s\teu-de\tfile:/nowhere\t2026-09-01\n" % PROJECT, encoding="utf-8")
    assert [t.alias for t in tenants.load(home)] == [OLD]


def test_the_key_service_takes_either_name_for_the_tenant_keys_conf_holds(tmp_path, monkeypatch, capsys):
    until = window(tmp_path, monkeypatch, 0)
    svc = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock")
    svc.tenants = {OLD: {"roles": {}, "secrets": {}}}
    for alias in (NEW, OLD):
        req = {"op": "call", "tenant": alias}
        svc._name_tenant(req)
        assert req["tenant"] == OLD
    assert capsys.readouterr().err == "%s is the old name of %s, until %s\n" % (OLD, NEW, until.isoformat())
    svc.tenants = {NEW: {"roles": {}, "secrets": {}}}
    for alias in (NEW, OLD):
        req = {"op": "call", "tenant": alias}
        svc._name_tenant(req)
        assert req["tenant"] == NEW
    for other in ("test-1", PROJECT, None, 7):
        req = {"op": "call", "tenant": other}
        svc._name_tenant(req)
        assert req["tenant"] == other
    window(tmp_path, monkeypatch, 31)
    with pytest.raises(keys.KeysError, match="use %s" % NEW):
        svc._name_tenant({"op": "call", "tenant": OLD})


def test_a_lab_tenant_of_the_closed_list_is_known_by_either_name(monkeypatch):
    monkeypatch.setattr(labadmin, "ALLOWED", (OLD, fx.TENANT_TWO))
    assert labadmin.allowed(OLD) and labadmin.allowed(NEW) and labadmin.allowed(fx.TENANT_TWO)
    assert labadmin.allowed("tcp-00002")
    assert not labadmin.allowed("tcp-00003") and not labadmin.allowed(PROJECT) and not labadmin.allowed(None)
    assert labadmin.parse("%s %s" % (NEW, "tcp-00002")) == [NEW, "tcp-00002"]
    assert labadmin.parse(PROJECT) is None


def test_the_clients_refuse_the_old_name_after_the_window(tmp_path, monkeypatch, capsys):
    window(tmp_path, monkeypatch, 31)
    assert cloud.main(["call", "GET", "ecs", "/v1/x", "--tenant", OLD]) == 2
    assert "use %s" % NEW in capsys.readouterr().err
    assert labclient.obs_main(["get", "https://example.invalid/x", str(tmp_path / "x"), "--tenant", OLD]) \
        == labclient.EXIT_REFUSED
    assert "use %s" % NEW in capsys.readouterr().err
    assert labclient.run_main(["start", "--tenant", OLD]) == labclient.EXIT_REFUSED
    assert "use %s" % NEW in capsys.readouterr().err
