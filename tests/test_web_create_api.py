"""The creation adapters (awb/tcp/web/create_api.py): customers on the owner side, projects on the work side, every
submission idempotent by its request id, names never in the journal, answers that fit the contract.

Ported from the tests the web side wrote for the deployed service. The projects use the kinds of the repository
(query and project) where the deployed backend still had topic and engagement; the contract checks are new.
"""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from awb import projects, register
from awb.tcp.web import contract
from awb.tcp.web.create_api import CustomerStore, Handler, Problem, ProjectStore, Server, UnixConnection
from awb.tcp.web import create_api
from tests import fixtures


def customer_payload(key="a" * 32):
    return {"request_id": key, "name": fixtures.CUSTOMER_FORMS[0], "aliases": list(fixtures.CUSTOMER_FORMS[1:])}


def empty_customers(home, tmp_path):
    # invented temporary register only, no file of the owner
    register.save(home.register, [])
    return CustomerStore(home, tmp_path / "customer-state")


def project_payload(key="b" * 32, **kwargs):
    return {"request_id": key, "kind": "query", "goal": "Compare storage options", "customer": "none", "tags": [],
            **kwargs}


def test_customer_is_real_and_aliases_atomic(home, tmp_path):
    store = empty_customers(home, tmp_path)
    result = store.create(customer_payload())
    rows = register.load(home.register)
    assert {e.form for e in rows} == set(fixtures.CUSTOMER_FORMS)
    assert all(e.code == result["code"] for e in rows)
    assert (home.outbox / result["code"]).is_dir()
    assert store.list()["customers"][0]["name"] == fixtures.CUSTOMER_FORMS[0]
    assert store.active(result["code"])
    raw = (tmp_path / "customer-state" / "operations.sqlite3").read_bytes()
    assert all(x.encode() not in raw for x in fixtures.CUSTOMER_FORMS)


def test_repeat_concurrent_and_restart_customer(home, tmp_path):
    store = empty_customers(home, tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: store.create(customer_payload()), range(3)))
    assert results[0] == results[1] == results[2]
    assert CustomerStore(home, tmp_path / "customer-state").create(customer_payload()) == results[0]
    with pytest.raises(Problem) as e:
        store.create({**customer_payload(), "name": fixtures.CUSTOMER_FORMS[0] + " " + fixtures.CUSTOMER_FORMS[1]})
    assert e.value.status == 409


def test_duplicate_customer_normalized(home, tmp_path):
    store = CustomerStore(home, tmp_path / "state")
    with pytest.raises(Problem) as e:
        store.create({**customer_payload(), "name": " " + fixtures.CUSTOMER_FORMS[0].upper() + " "})
    assert e.value.status == 409 and e.value.data["matches"]


def test_customer_resume_after_marker_failure(home, tmp_path):
    store = empty_customers(home, tmp_path)
    with patch.object(store, "marker", side_effect=OSError()):
        with pytest.raises(OSError):
            store.create(customer_payload())
    rows = register.load(home.register)
    result = CustomerStore(home, tmp_path / "customer-state").create(customer_payload())
    assert len(register.load(home.register)) == len(rows)
    assert result["code"] == rows[0].code
    assert (home.outbox / result["code"]).is_dir()


def test_invalid_alias_cannot_partially_create(home, tmp_path):
    store = empty_customers(home, tmp_path)
    with pytest.raises(Problem):
        store.create({**customer_payload(), "aliases": ["invalid\nline"]})
    assert register.load(home.register) == []
    with pytest.raises(Problem):
        store.create({**customer_payload(), "aliases": ["[invalid]"]})
    assert store.journal.codes() == set()


def test_project_uses_real_core_and_replays(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    result = store.create(project_payload())
    p = projects.load(home)[0]
    assert p.code == result["code"] and p.customer == "none" and p.kind == "query"
    assert (Path(p.path) / ".git/hooks/pre-commit").exists()
    assert (Path(p.path) / ".claude/settings.json").exists()
    assert ProjectStore(home, tmp_path / "state").create(project_payload()) == result
    assert len(projects.load(home)) == 1


def test_project_resume_after_commit_before_response(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    with patch.object(store.journal, "finish", side_effect=OSError()):
        with pytest.raises(OSError):
            store.create(project_payload())
    result = ProjectStore(home, tmp_path / "state").create(project_payload())
    assert result["code"] == projects.load(home)[0].code
    assert len(projects.load(home)) == 1


def test_interrupted_folder_never_gets_deleted_or_duplicated(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    data = project_payload()
    payload = {k: v for k, v in data.items() if k != "request_id"}
    code = "tcp-q7m4"
    store.journal.start(data["request_id"], payload, code)
    folder = home.projects_root / code
    folder.mkdir(parents=True)
    (folder / "sentinel").write_text("preserve")
    with pytest.raises(Problem) as e:
        store.create(data)
    assert e.value.status == 409 and e.value.data["terminal"]
    assert (folder / "sentinel").read_text() == "preserve"
    assert projects.load(home) == []


def test_the_reserved_code_reaches_the_core(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    data = project_payload()
    store.journal.start(data["request_id"], {k: v for k, v in data.items() if k != "request_id"}, "tcp-q7m4")
    assert store.create(data)["code"] == "tcp-q7m4"
    assert [p.code for p in projects.load(home)] == ["tcp-q7m4"]


def test_real_customer_project_no_import(home, tmp_path):
    customers = empty_customers(home, tmp_path)
    c = customers.create(customer_payload())["code"]
    (home.outbox / c / "waiting.txt").write_text("sanitized input")
    store = ProjectStore(home, tmp_path / "project-state", customers.active)
    result = store.create(project_payload(customer=c))
    assert projects.load(home)[0].customer == c
    assert not (home.projects_root / result["code"] / "input/waiting.txt").exists()
    assert (home.outbox / c / "waiting.txt").exists()


def test_retired_customer_refused(home, tmp_path):
    customers = empty_customers(home, tmp_path)
    c = customers.create(customer_payload())["code"]
    register.retire(home.register, c)
    store = ProjectStore(home, tmp_path / "project-state", customers.active)
    with pytest.raises(Problem) as e:
        store.create(project_payload(customer=c))
    assert e.value.data == {"error": "This customer is unavailable or retired. Select an active customer.",
                            "field": "customer"}
    assert not projects.load(home)


def test_private_goal_refused_and_retry_can_be_edited(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    with pytest.raises(Problem):
        store.create(project_payload(goal="Work for " + fixtures.CUSTOMER_FORMS[0]))
    assert not projects.load(home)
    assert not store.journal.codes()
    assert store.create(project_payload())["created"]


def test_concurrent_project_submissions_are_one_project(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: store.create(project_payload()), range(3)))
    assert results[0] == results[1] == results[2]
    assert len(projects.load(home)) == 1


def test_invalid_fields_and_kind(home, tmp_path):
    store = ProjectStore(home, tmp_path / "state")
    # an old kind word is refused by the repository backend, which serves query and project only
    for data in [project_payload(kind="engagement"), project_payload(kind="topic"), project_payload(path="/tmp/no"),
                 project_payload(kind="unknown"), project_payload(request_id="bad")]:
        with pytest.raises(Problem):
            store.create(data)
    assert not projects.load(home)


def _customer_server(home, tmp_path, web_uid):
    path = str(tmp_path / "api.sock")
    s = Server(path, Handler)
    s.mode = "customers"
    s.store = CustomerStore(home, tmp_path / "state")
    s.web_uid = web_uid
    s.work_uid = os.getuid()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, path


def _ask(path, method, route, body=None):
    c = UnixConnection(path)
    try:
        c.request(method, route, body=body, headers={"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_the_customer_socket_answers_the_web_user_alone(home, tmp_path):
    # F11 (2026-10-09): replaces test_unix_peer_cannot_read_names_and_the_web_cannot_register, whose middle step
    # pinned the work user's POST /internal/customers/CODE answering 200; that route is gone and every route of the
    # customer socket refuses any peer but the web user. The other steps are kept as they were.
    s, path = _customer_server(home, tmp_path, os.getuid() + 10000)
    try:
        assert _ask(path, "GET", "/api/customers")[0] == 403
        code = next(e.code for e in register.load(home.register) if e.kind == "CUST")
        status, raw = _ask(path, "POST", "/internal/customers/" + code)
        assert status == 403 and b"active" not in raw, "planted: the work user asks whether a customer is active"
        assert not (home.outbox / code).exists(), "no outbox folder made for the work user"
        s.web_uid = os.getuid()
        for body in ("[]", json.dumps(customer_payload())):
            status, raw = _ask(path, "POST", "/api/customers", body)
            assert status == 403
            assert json.loads(raw) == {"error": create_api.WEB_REGISTER_OFF}
        assert _ask(path, "POST", "/internal/customers/" + code)[0] == 404, "the route is gone for the web too"
    finally:
        s.shutdown()
        s.server_close()


def test_the_project_side_counts_a_customer_by_its_outbox_folder(home, tmp_path):
    # F11: the project service no longer calls the customer socket; an issued code has its outbox folder
    store = ProjectStore(home, tmp_path / "state")
    with pytest.raises(Problem) as e:
        store.create(project_payload(customer="CUST-ZZ22"))
    assert e.value.data["field"] == "customer" and not projects.load(home)
    (tmp_path / "elsewhere").mkdir()
    (home.outbox / "CUST-ZZ22").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Problem):
        store.create(project_payload(key="c" * 32, customer="CUST-ZZ22"))
    assert not projects.load(home)
    code = fixtures.CUSTOMER_CODE
    (home.outbox / code).mkdir(exist_ok=True)
    assert store.create(project_payload(key="d" * 32, customer=code))["created"]
    assert projects.load(home)[0].customer == code


def test_the_web_list_carries_codes_and_never_a_name(home, tmp_path):
    # D-NOW: the console's customer list answers codes; the forms of the register stay on the owner side
    s, path = _customer_server(home, tmp_path, os.getuid())
    try:
        status, raw = _ask(path, "GET", "/api/customers")
    finally:
        s.shutdown()
        s.server_close()
    assert status == 200
    text = raw.decode()
    assert all(form not in text for form in fixtures.CUSTOMER_FORMS)
    assert all(form.casefold() not in text.casefold() for form in fixtures.CUSTOMER_FORMS)
    rows = json.loads(raw)["customers"]
    codes = sorted({e.code for e in register.load(home.register) if e.kind == "CUST"})
    assert [r["code"] for r in rows] == codes
    assert all(r["name"] == r["code"] and r["aliases"] == [] for r in rows)
    assert contract.validate(contract.build(), json.loads(raw), contract.ref("CustomerList")) == []


def test_the_web_register_refusal_leaves_the_register_and_journal_alone(home, tmp_path):
    store = empty_customers(home, tmp_path)
    s = Server(str(tmp_path / "api2.sock"), Handler)
    s.mode, s.store, s.web_uid, s.work_uid = "customers", store, os.getuid(), os.getuid() + 1
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        status, _ = _ask(str(tmp_path / "api2.sock"), "POST", "/api/customers", json.dumps(customer_payload()))
    finally:
        s.shutdown()
        s.server_close()
    assert status == 403
    assert register.load(home.register) == []
    assert store.journal.codes() == set()


def test_the_adapter_does_not_start_without_the_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["create_api", "projects", "--state", str(tmp_path / "state")])
    with pytest.raises(SystemExit) as exc:
        create_api.main()
    assert exc.value.code == 2


def test_the_answers_fit_the_contract(home, tmp_path):
    doc = contract.build()

    def fits(value, schema):
        assert contract.validate(doc, value, contract.ref(schema)) == []

    customers = empty_customers(home, tmp_path)
    created = customers.create(customer_payload())
    fits(created, "CreationResult")
    fits(customers.list(), "CustomerList")
    fits(customers.journal.status("a" * 32), "OperationStatus")
    fits(customers.journal.status("c" * 32), "OperationStatus")
    store = ProjectStore(home, tmp_path / "project-state", customers.active)
    options = store.options()
    fits(options, "ProjectOptions")
    assert options["kinds"] == list(projects.PROJECT_KINDS)
    fits(store.create(project_payload(customer=created["code"])), "CreationResult")
    fits(store.journal.status("b" * 32), "OperationStatus")
    for bad in (project_payload(key="d" * 32, kind="topic"), project_payload(key="e" * 32, customer="CUST-ZZZZ")):
        with pytest.raises(Problem) as e:
            store.create(bad)
        fits(e.value.data, "Error")
    with pytest.raises(Problem) as e:
        customers.create({**customer_payload("f" * 32), "name": fixtures.CUSTOMER_FORMS[0].upper()})
    fits(e.value.data, "Error")
