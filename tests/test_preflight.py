"""UX1: planted failures, GET-only readers, unavailable evidence and project entry points."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import cli, config, grants, hooks, preflight, projects
from awb.tcp import cloud, doc, keys, tenanttags
from tests import fixtures as fx

CODE = "tcp-q7m4"
ALIAS = fx.TENANT_ONE


@pytest.fixture
def rig(home, tmp_path, monkeypatch):
    root = home.projects_root / CODE
    (root / "input").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "SCOPE.md").write_text("- kind: project\n", encoding="utf-8")
    row = projects.Project(CODE, "project", "none", "tcp", str(root), projects.memory_key(root),
                           "active", fx.TODAY)
    projects._save(home, [row])
    home.vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    grants.grant(home, CODE, "project")
    store = tmp_path / "store"
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(store))
    times = {}
    for part in ("domain", "user", "password"):
        entry = "awb/admin/%s/%s" % (ALIAS, part)
        path = store / (entry + ".gpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"encrypted stand-in")
        times[entry] = path.stat().st_mtime_ns
    metadata = {ALIAS: {"roles": ["read"], "admin": True, "store_times": times}}
    resources = [{"type": t, "used": 1, "quota": 2} for t in ("floatingip", "vpc", "routetable")]
    requests = []
    state = SimpleNamespace(root=root, row=row, store=store, metadata=metadata, resources=resources,
                            requests=requests, down=False, status=200)

    def request(sock, req, **kwargs):
        requests.append(req)
        if req["op"] == "tenants":
            return {"ok": True, "tenants": metadata}
        assert req["op"] == "call" and req["method"] == "GET" and req["role"] == "read"
        assert req["project"] == CODE and "token" in req
        if state.down:
            raise OSError(fx.CUSTOMER_FORMS[0])
        return {"ok": True, "status": state.status, "data": {"quotas": {"resources": resources}}}

    monkeypatch.setattr(keys, "request", request)
    return state


def observed(home):
    return {c.handle: c for c in cli.preflight_checks(home, CODE)}


def test_planted_grant_quota_and_host_fail_then_all_fixed(home, rig, monkeypatch, capsys):
    grants.path(home).write_text("{}")
    rig.resources[0]["used"] = 2
    (rig.root / "input" / "links.txt").write_text("https://" + fx.CUSTOMER_DOMAIN + "/guide")
    monkeypatch.setattr(tenanttags, "preflight_hosts", lambda: [])
    assert cli.main(["projects", "preflight", CODE]) == 1
    lines = capsys.readouterr().out.splitlines()
    missing = [s for s in lines if " missing" in s]
    assert len(missing) == 3
    assert CODE + " grant missing kind=project" in missing
    assert CODE + " " + ALIAS + "/eip missing free=0" in missing
    assert CODE + " " + preflight.handle("host", fx.CUSTOMER_DOMAIN) + " missing host-list" in missing
    grants.grant(home, CODE, "project")
    rig.resources[0]["used"] = 1
    monkeypatch.setattr(tenanttags, "preflight_hosts", lambda: [fx.CUSTOMER_DOMAIN])
    assert cli.main(["projects", "preflight", CODE]) == 0
    assert all(" ok" in line for line in capsys.readouterr().out.splitlines())


def test_unanswered_tenant_and_absent_quota_are_unknown(home, rig):
    rig.down = True
    found = observed(home)
    assert found[ALIAS + "/read"].status == "unknown"
    assert all(found[ALIAS + "/" + k].status == "unknown" for k in ("eip", "vpc", "route-table"))
    rig.down = False
    rig.resources.clear()
    found = observed(home)
    assert found[ALIAS + "/read"].status == "ok"
    assert all(found[ALIAS + "/" + k].status == "unknown" for k in ("eip", "vpc", "route-table"))


def test_each_capacity_is_checked_including_zero_and_malformed(home, rig):
    for quota, label in zip(rig.resources, ("eip", "vpc", "route-table")):
        quota.update(used=0, quota=0)
        assert observed(home)[ALIAS + "/" + label].status == "missing"
        quota["quota"] = True
        assert observed(home)[ALIAS + "/" + label].status == "unknown"
        quota.update(used=1, quota=2)


def test_grant_kind_and_unreadable_record_are_not_ok(home, rig):
    grants.grant(home, CODE, "query")
    assert observed(home)["grant"].status == "missing"
    grants.path(home).write_text("{")
    assert observed(home)["grant"].status == "unknown"
    grants.path(home).write_text(json.dumps({CODE: None}))
    assert observed(home)["grant"].status == "unknown"


def test_get_only_leaves_real_key_service_and_uses_target_token(home, rig, monkeypatch, tmp_path):
    """The service dispatch is real; its outbound cloud client rejects every non-GET request."""
    svc = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock", paths_fn=lambda: home)
    svc.tenants = {ALIAS: {"roles": {"read": {"ak": "invented", "sk": "invented"}},
                           "admin": {"domain": fx.CUSTOMER_DOMAIN, "user": fx.LOG_USER,
                                     ("pass" + "word"): fx.CUSTOMER_FORMS[0]},
                           "store_times": rig.metadata[ALIAS]["store_times"]}}
    monkeypatch.setattr(svc, "_registered", lambda: {ALIAS: ["eu-de"]})
    monkeypatch.setattr(svc, "_log", lambda *a: None)
    outgoing = []

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, service, path, **kwargs):
            assert method == "GET"
            outgoing.append((method, service, path))
            return SimpleNamespace(status=200, data={"quotas": {"resources": rig.resources}}, text="", size=0,
                                   cut=False)

    monkeypatch.setattr(cloud, "Client", Reader)
    token = projects.write_key_token(rig.root)
    grants.record_token(home, CODE, token)
    work_uid = os.getuid() + 1
    monkeypatch.setattr(svc, "_console_uid", lambda: None)

    def request(sock, req, **kwargs):
        if req["op"] == "tenants":
            return {"ok": True, "tenants": svc._summary()}
        assert req["token"] == token and req["project"] == CODE
        return svc._call_socket(req, work_uid, caller=svc._caller_project(req, work_uid))

    monkeypatch.setattr(keys, "request", request)
    before = {p.relative_to(rig.root): p.read_bytes() for p in rig.root.rglob("*") if p.is_file()}
    assert all(c.status == "ok" for c in observed(home).values())
    assert outgoing == [("GET", "vpc", "/v1/{project_id}/quotas")]
    assert before == {p.relative_to(rig.root): p.read_bytes() for p in rig.root.rglob("*") if p.is_file()}


def test_store_newer_and_unreadable_age_without_login(home, rig):
    entry = "awb/admin/%s/password" % ALIAS
    loaded = rig.metadata[ALIAS]["store_times"][entry]
    path = rig.store / (entry + ".gpg")
    os.utime(path, ns=(loaded + 100, loaded + 100))
    assert observed(home)[ALIAS + "/admin/password"].status == "missing"
    path.unlink()
    assert observed(home)[ALIAS + "/admin/password"].status == "unknown"
    assert {r["op"] for r in rig.requests} == {"tenants", "call"}


def test_login_tenants_and_write_mode_do_not_spend_login_or_use_write_key(home, rig):
    from awb.tcp import readonly

    readonly.grants_path(home).write_text(json.dumps({ALIAS: [CODE]}))
    rig.metadata[ALIAS].update(kind="readonly", auth="login")
    assert observed(home)[ALIAS + "/read"].status == "unknown"
    assert all(r["op"] == "tenants" for r in rig.requests)
    rig.metadata[ALIAS].update(auth="key", mode="write")
    observed(home)
    assert all(r["op"] == "tenants" for r in rig.requests)


def test_each_granted_tenant_probed_and_ungranted_tenant_skipped(home, rig):
    from awb.tcp import readonly

    rig.metadata[fx.TENANT_TWO] = dict(rig.metadata[ALIAS], kind="readonly", auth="key")
    readonly.grants_path(home).write_text(json.dumps({fx.TENANT_TWO: [CODE]}))
    found = observed(home)
    assert found[fx.TENANT_TWO + "/read"].status == "ok"
    assert [r["tenant"] for r in rig.requests if r["op"] == "call"] == [ALIAS, fx.TENANT_TWO]
    rig.requests.clear()
    readonly.grants_path(home).write_text(json.dumps({fx.TENANT_TWO: []}))
    observed(home)
    assert [r["tenant"] for r in rig.requests if r["op"] == "call"] == [ALIAS]


def test_hosts_in_scope_nested_input_and_owner_vendor_lists(home, rig, tmp_path, monkeypatch):
    from awb import vendorhosts

    allowed, extra, vendor = (tmp_path / name for name in ("allowed", "extra", "vendor"))
    allowed.write_text(fx.CUSTOMER_DOMAIN + "\n")
    extra.write_text(fx.MEETING_DOMAINS[0] + " # allowed\n")
    vendor.write_text(fx.MEETING_DOMAINS[1] + "\nportal " + fx.LOG_INTERNAL_HOST + "\n")
    monkeypatch.setattr(doc, "HOSTS_FILE", allowed)
    monkeypatch.setattr(doc, "extra_file", lambda: extra)
    monkeypatch.setattr(vendorhosts, "VENDOR_FILE", vendor)
    (rig.root / "SCOPE.md").write_text("https://" + fx.CUSTOMER_DOMAIN + "/guide\nSCOPE.md\n")
    nested = rig.root / "input" / "nested"
    nested.mkdir()
    (nested / "links.txt").write_text("\n".join(fx.MEETING_DOMAINS + (fx.LOG_INTERNAL_HOST,)))
    found = observed(home)
    for host in (fx.CUSTOMER_DOMAIN,) + fx.MEETING_DOMAINS:
        assert found[preflight.handle("host", host)].status == "ok"
    assert found[preflight.handle("host", fx.LOG_INTERNAL_HOST)].status == "missing"
    assert len([h for h in found if h.startswith("host-")]) == 4
    extra.unlink()
    assert observed(home)[preflight.handle("host", fx.CUSTOMER_DOMAIN)].status == "unknown"


def test_unreadable_inputs_and_symlinks_are_unknown(home, rig, tmp_path):
    (rig.root / "input" / "binary").write_bytes(b"\xff\x00")
    outside = tmp_path / "outside"
    outside.write_text(fx.CUSTOMER_DOMAIN)
    (rig.root / "input" / "link").symlink_to(outside)
    found = observed(home)
    assert found[preflight.handle("file", "input/binary")].status == "unknown"
    assert found[preflight.handle("file", "input/link")].status == "unknown"
    assert preflight.handle("host", fx.CUSTOMER_DOMAIN) not in found


def test_output_never_contains_names_urls_secrets_or_error_text(home, rig, capsys):
    (rig.root / "SCOPE.md").write_text("https://" + fx.LOG_USER + ":" + fx.CUSTOMER_FORMS[1]
                                      + "@" + fx.CUSTOMER_DOMAIN + "/guide\n" + fx.CUSTOMER_FORMS[0])
    rig.down = True
    assert cli.main(["projects", "preflight", CODE]) == 1
    output = capsys.readouterr().out
    fx.assert_no_fixture_name(output)
    assert fx.CUSTOMER_DOMAIN not in output and fx.LOG_USER not in output
    assert all(line.startswith(CODE + " ") for line in output.splitlines())


def test_hook_shows_only_non_ok_once_per_session(home, rig, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "name_hits", lambda *a: [])
    monkeypatch.setattr(hooks, "_sealed", lambda: False)
    calls = []

    def checks(*args):
        calls.append(args)
        return [preflight.Check("grant", "ok"), preflight.Check(ALIAS + "/eip", "missing"),
                preflight.Check(ALIAS + "/read", "unknown")]

    monkeypatch.setattr(cli, "preflight_checks", checks)
    data = {"cwd": str(rig.root), "session_id": "session-1"}
    hooks.hook_session_start(data)
    first = capsys.readouterr().out
    assert ALIAS + "/eip missing" in first and ALIAS + "/read unknown" in first
    assert "grant ok" not in first
    hooks.hook_session_start(data)
    assert ALIAS + "/eip" not in capsys.readouterr().out
    assert len(calls) == 1
    hooks.hook_session_start(dict(data, session_id="session-2"))
    assert ALIAS + "/eip missing" in capsys.readouterr().out


def test_spawn_start_and_unpark_print_preflight(home, rig, monkeypatch, capsys):
    from awb import start

    monkeypatch.setattr(cli, "preflight_checks", lambda *a: [preflight.Check("grant", "missing")])
    monkeypatch.setattr(projects, "record", lambda *a, **kw: None)
    monkeypatch.setattr(projects, "spawn", lambda *a, **kw: rig.row)
    assert cli.main(["spawn", "project", "--goal", "test access"]) == 0
    assert CODE + " grant missing" in capsys.readouterr().out
    rig.row.state = "parked"
    projects._save(home, [rig.row])
    assert cli.main(["projects", "unpark", CODE]) == 0
    assert CODE + " grant missing" in capsys.readouterr().out
    assert cli.main(["projects", "unpark", CODE]) == 0
    assert CODE + " grant missing" in capsys.readouterr().out
    projects._save(home, [])

    def spawned(*args, **kwargs):
        projects._save(home, [rig.row])
        return CODE

    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(start, "spawn_step", spawned)
    monkeypatch.setattr(start, "grant_step", lambda *a: None)
    monkeypatch.setattr(start, "import_step", lambda *a, **kw: 0)
    assert cli.main(["start", "project", "--goal", "test access"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2:] == [CODE + " grant missing", "open: awb projects open " + CODE]


def test_work_side_infers_grant_only_from_successful_read(home, rig, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    grants.path(home).write_text("{")
    assert observed(home)["grant"].status == "ok"
    rig.down = True
    assert observed(home)["grant"].status == "unknown"
    rig.down = False

    def refused(*args, **kwargs):
        return {"ok": False, "error": CODE + " has no grant of the owner"}

    checks = tenanttags.preflight_access(home, rig.row, preflight.Check("grant", "unknown"),
                                       request=lambda sock, req, **kw: (
                                           {"ok": True, "tenants": rig.metadata} if req["op"] == "tenants"
                                           else refused()))
    assert any(c.handle == "grant" and c.status == "missing" for c in checks)


def test_query_checks_grant_and_hosts_without_tenant_calls(home, rig, monkeypatch):
    rig.row.kind = "query"
    projects._save(home, [rig.row])
    grants.grant(home, CODE, "query")
    (rig.root / "SCOPE.md").write_text(fx.CUSTOMER_DOMAIN)
    monkeypatch.setattr(tenanttags, "preflight_hosts", lambda: [])
    found = observed(home)
    assert found["grant"].status == "ok" and found["grant"].detail == "kind=query"
    assert found[preflight.handle("host", fx.CUSTOMER_DOMAIN)].status == "missing"
    assert rig.requests == []
    assert not any(h.startswith(ALIAS + "/") for h in found)


def test_unavailable_service_and_malformed_metadata_are_unknown(home, rig, monkeypatch):
    for response in (None, {}, {"ok": True, "tenants": None},
                     {"ok": True, "tenants": {fx.CUSTOMER_FORMS[0]: {}}}):
        monkeypatch.setattr(keys, "request", lambda *a, **kw: response)
        assert observed(home)["tenants"].status == "unknown"


def test_malformed_tenant_fields_preserve_checks(home, rig):
    rig.metadata[ALIAS].update(roles=None, store_times=None)
    found = observed(home)
    assert found[ALIAS + "/read"].status == "unknown"
    assert found[ALIAS + "/eip"].status == "unknown"
    assert found[ALIAS + "/admin/password"].status == "unknown"
    assert all(r["op"] == "tenants" for r in rig.requests)
    rig.metadata[ALIAS]["admin"] = False
    assert observed(home)[ALIAS + "/admin/password"].status == "missing"


def test_bad_grant_list_and_unreadable_register_do_not_hide_tenants(home, rig, monkeypatch):
    from awb.tcp import readonly, tenants

    rig.metadata[ALIAS].update(kind="readonly", auth="key")
    readonly.grants_path(home).write_text(json.dumps({ALIAS: None}))

    def unreadable(*args):
        raise PermissionError()

    monkeypatch.setattr(tenants, "load", unreadable)
    found = observed(home)
    assert found["tenant-register"].status == "unknown"
    assert found[ALIAS + "/grant"].status == "unknown"
    assert found[ALIAS + "/read"].status == "ok"


def test_denied_and_malformed_tenant_responses_do_not_pass(home, rig, monkeypatch):
    rig.status = 403
    assert observed(home)[ALIAS + "/read"].status == "missing"
    assert observed(home)[ALIAS + "/eip"].status == "unknown"
    rig.status = 503
    assert observed(home)[ALIAS + "/read"].status == "unknown"
    monkeypatch.setattr(keys, "request", lambda sock, req, **kw: (
        {"ok": True, "tenants": rig.metadata} if req["op"] == "tenants" else None))
    assert observed(home)[ALIAS + "/read"].status == "unknown"


def test_symlinked_input_root_is_unknown_without_reading_target(home, rig, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "links.txt").write_text(fx.CUSTOMER_DOMAIN)
    (rig.root / "input").rmdir()
    (rig.root / "input").symlink_to(outside, target_is_directory=True)
    found = observed(home)
    assert found[preflight.handle("file", "input")].status == "unknown"
    assert preflight.handle("host", fx.CUSTOMER_DOMAIN) not in found


def test_start_and_preflight_help_never_read_project_register(home, monkeypatch, capsys):
    def unreadable(*args):
        raise projects.ProjectError("unreadable")

    monkeypatch.setattr(projects, "load", unreadable)
    assert cli.main(["start", "--help"]) == 0
    assert "usage: awb start" in capsys.readouterr().out
    assert cli.main(["projects", "preflight", "--help"]) == 0
    assert "usage: awb projects preflight" in capsys.readouterr().out
