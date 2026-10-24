"""UX review 2026-10-24, changes 4 and 5: `awb owner status` (one list of what waits on the owner, a daily mail) and the
interactive `awb tenant add-readonly --login` (hidden prompts, a test GET, the text for the customer's admin).

Every name, login, password and id here is invented; the password is built from pieces and never printed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import build, config, ownerwait, projects
from awb.tcp import keys, owner, readonly, tenants
from tests import fixtures as fx
from tests.test_readonly_tenants import DOMAIN_ID
from tests.test_seal import fakebin  # noqa: F401

GOAL = "move two app clusters to managed k8s"
QUEUE = """# Work queue

## B. From the work profile

| step | what | saves min/week | state | next |
|---|---|---|---|---|
| B1 | the Workbench builds itself | 216 | ACCEPTED | none |
| B3 | lab recipes on a tenant | 75 | waiting: his step | run `awb keys unlock` after the pass entry |
| B4 | voice pass before he sees a draft | 65 | done: 62c7e75 | none |

## K. Knowledge

| step | what | state | next |
|---|---|---|---|
| K2 | review of the findings | waiting (needs his word) | the steering session names the build task |
| K3 | mcp package | done | none |
"""


@pytest.fixture
def proj(home, register_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return projects.spawn(home, "project", GOAL, fx.CUSTOMER_CODE, register_path)


def _open(proj, *lines):
    with open(Path(proj.path) / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- change 4: awb owner status


def test_ux4_open_items_carry_the_code_the_place_and_a_ready_block(home, proj, tmp_path):
    _open(proj, "## Owner", "- run `awb close %s` when the customer confirms" % proj.code, "- decide the region")
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    by_text = {i.text: i for i in items if i.label == proj.code}
    close = next(i for t, i in by_text.items() if "awb close" in t)
    assert close.where == ownerwait.WHERE_TERMINAL and close.block == "awb close %s" % proj.code
    decide = next(i for t, i in by_text.items() if "decide the region" in t)
    assert decide.where == ownerwait.WHERE_SESSION and decide.block is None
    assert parked == 0


def test_ux4_a_parked_project_is_a_count_only(home, proj, tmp_path):
    _open(proj, "## Owner", "- a thing that waits")
    projects.park(home, proj.code, "waits on the customer")
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert [i for i in items if i.label == proj.code] == [] and parked == 1
    assert "1 parked project not listed" in ownerwait.render(items, parked)[-1]


def test_ux4_the_queue_rows_that_wait_on_him_and_a_missing_file(tmp_path):
    q = tmp_path / "QUEUE.md"
    q.write_text(QUEUE, encoding="utf-8")
    got = ownerwait.queue_items(q)
    assert [i.label for i in got] == ["B3", "K2"]
    assert got[0].block == "awb keys unlock" and got[0].where == ownerwait.WHERE_TERMINAL
    assert got[1].block is None and got[1].where == ownerwait.WHERE_STEERING
    assert ownerwait.queue_items(tmp_path / "missing.md") == []


def test_ux4_the_build_runs_that_wait_on_him(tmp_path):
    runs = tmp_path / "runs"
    build.new_run("B-101", runs, state="asked")
    build.new_run("B-102", runs, state="red: checks")
    build.new_run("B-103", runs, state="done")
    got = {i.label: i for i in ownerwait.build_items(runs)}
    assert sorted(got) == ["B-101", "B-102"]
    assert "awb build answer B-101" in got["B-101"].block
    assert "awb build retry B-102" in got["B-102"].block and got["B-102"].where == ownerwait.WHERE_TERMINAL


def test_ux4_a_text_with_a_name_is_withheld_and_the_list_has_no_name(home, proj, tmp_path):
    _open(proj, "## Owner", "- call %s about the dates" % fx.CUSTOMER_FORMS[0])
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    text = "\n".join(ownerwait.render(items, parked))
    assert fx.CUSTOMER_FORMS[0] not in text and "withheld by the name check" in text


def test_ux4_the_mail_goes_once_per_call_with_the_list_and_not_when_empty(home, proj, tmp_path):
    sent = []
    assert ownerwait.mail(home, lambda s, m: sent.append((s, m)) or "sent", tmp_path / "none.md",
                          tmp_path / "runs") == "empty" and sent == []
    _open(proj, "## Owner", "- run `awb close %s`" % proj.code)
    word = ownerwait.mail(home, lambda s, m: sent.append((s, m)) or "sent", tmp_path / "none.md", tmp_path / "runs")
    assert word == "sent" and len(sent) == 1
    assert "1 item waits on you" not in sent[0][0] and "wait on you" in sent[0][0]
    assert proj.code in sent[0][1] and "awb close %s" % proj.code in sent[0][1]
    assert ownerwait.mail(home, lambda s, m: "not sent: no topic", tmp_path / "none.md",
                          tmp_path / "runs").startswith("not sent")


def test_ux4_no_mail_when_the_name_check_cannot_run(home, proj, tmp_path, monkeypatch):
    from awb import ledger

    _open(proj, "## Owner", "- run `awb close %s`" % proj.code)

    def broken(*a, **k):
        raise ledger.LedgerError("locked")

    monkeypatch.setattr(ledger, "hit_indexes", broken)
    sent = []
    assert ownerwait.mail(home, lambda s, m: sent.append(m) or "sent", tmp_path / "none.md", tmp_path / "runs") == (
        "not sent: the name check cannot run") and sent == []


def test_ux4_the_command_prints_the_list_and_json_keeps_the_old_status(home, proj, capsys, monkeypatch):
    monkeypatch.setattr(ownerwait, "queue_file", lambda: Path("/nonexistent/QUEUE.md"))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(home.shared / "no-runs"))
    _open(proj, "## Owner", "- run `awb close %s`" % proj.code)
    assert owner.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "wait on you" in out or "waits on you" in out
    assert "%s  " % proj.code in out and "[his terminal]" in out and "    awb close %s" % proj.code in out
    monkeypatch.setattr(owner, "collect", lambda p=None: {"version": 1})
    monkeypatch.setattr(owner, "validate", lambda s: None)
    assert owner.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"version": 1}


def test_ux4_the_setup_installs_a_daily_berlin_timer_after_the_reload(fakebin):
    from tests.test_seal import SETUP, USER, commands, dropin_lines, run_script

    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        cmds = commands(res.stdout)
        unit = dropin_lines(res.stdout, "/etc/systemd/system/awb-owner-mail.service")
        for want in ("Type=oneshot", "User=%s" % USER, "ExecStart=/usr/local/bin/awb owner status --mail",
                     "ProtectSystem=strict", "RestrictAddressFamilies=AF_UNIX"):
            assert want in unit, want
        timer = dropin_lines(res.stdout, "/etc/systemd/system/awb-owner-mail.timer")
        assert "OnCalendar=*-*-* 08:00:00 Europe/Berlin" in timer
        reload = cmds.index("+ systemctl daemon-reload")
        assert cmds.index("+ write /etc/systemd/system/awb-owner-mail.timer (mode 644, root:root)") < reload
        assert cmds.index("+ systemctl enable --now awb-owner-mail.timer") > reload
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert "awb-owner-mail" not in res.stdout


# --------------------------------------------------------------------------- change 5: interactive add-readonly


class MemStore:
    """A pass store in memory: `has` by entry name, `insert` records the value for the test to look at."""

    def __init__(self, names=()):
        self.names = set(names)
        self.values: dict[str, str] = {}
        self.root = Path("/nonexistent")

    def has(self, entry):
        return entry in self.names

    def insert(self, entry, value, replace=False):
        self.names.add(entry)
        self.values[entry] = value


ALIAS = "cust-5"
SECRET = "-".join(("Invented", "Secret", "4711"))


def _ask(answers):
    seen = []

    def line(prompt):
        seen.append(("line", prompt))
        return answers[len(seen) - 1]

    def secret(prompt):
        seen.append(("secret", prompt))
        return answers[len(seen) - 1]

    return line, secret, seen


def test_ux5_it_asks_the_three_parts_hidden_for_the_password_and_writes_them(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    line, secret, seen = _ask(["account-fixture", "reader-fixture", SECRET])
    store = MemStore()
    asked = readonly.ask_login(ALIAS, store, tty=lambda: True, line=line, secret=secret)
    assert asked == ["domain", "user", "password"]
    assert [k for k, _ in seen] == ["line", "line", "secret"]
    assert store.values["awb/tenant/%s/login/password" % ALIAS] == SECRET
    assert store.values["awb/tenant/%s/login/domain" % ALIAS] == "account-fixture"


def test_ux5_only_the_missing_parts_are_asked_and_a_full_set_asks_nothing(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    full = MemStore("awb/tenant/%s/login/%s" % (ALIAS, k) for k in ("domain", "user", "password"))
    assert readonly.ask_login(ALIAS, full, tty=lambda: True, line=None, secret=None) == []
    part = MemStore(["awb/tenant/%s/login/domain" % ALIAS])
    line, secret, seen = _ask(["reader-fixture", SECRET])
    assert readonly.ask_login(ALIAS, part, tty=lambda: True, line=line, secret=secret) == ["user", "password"]


def test_ux5_no_terminal_keeps_the_old_refusal_and_an_empty_value_writes_nothing(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    with pytest.raises(readonly.ReadonlyError, match="pass insert it first"):
        readonly.ask_login(ALIAS, MemStore(), tty=lambda: False)
    line, secret, seen = _ask(["account-fixture", "", SECRET])
    store = MemStore()
    with pytest.raises(readonly.ReadonlyError, match="empty"):
        readonly.ask_login(ALIAS, store, tty=lambda: True, line=line, secret=secret)
    assert list(store.values) == ["awb/tenant/%s/login/domain" % ALIAS]


@pytest.mark.parametrize("answer, passed, word", [
    ({"ok": True, "status": 200, "data": {}}, True, "ok"),
    ({"ok": False, "error": readonly.CONSOLE_ONLY_LINE}, False, "403"),
    ({"ok": True, "status": 403, "text": "Forbidden"}, False, "403"),
    ({"ok": False, "error": "the login was refused by IAM (HTTP 401 IAM.0062)"}, False, "401"),
    ({"ok": False, "error": readonly.MFA_LINE}, False, "MFA"),
])
def test_ux5_the_test_answer_names_what_failed(answer, passed, word):
    ok, line = readonly.explain_test(answer)
    assert ok is passed and word in line
    assert SECRET not in line


def test_ux5_the_check_asks_the_key_service_one_iam_get_as_the_owner():
    calls = []

    def request(sock, obj, timeout=0):
        calls.append(obj)
        return {"ok": True, "status": 200, "data": {"projects": []}}

    assert readonly.check_login(ALIAS, "eu-de", request)[0] is True
    assert calls[0]["op"] == "call" and calls[0]["method"] == "GET" and calls[0]["service"] == "iam"
    assert calls[0]["tenant"] == ALIAS and "body" not in calls[0]
    assert readonly.check_login(ALIAS, "eu-de", lambda *a, **k: (_ for _ in ()).throw(keys.KeysError("down")))[
        1].startswith("test GET not run")


def test_ux5_the_customer_text_is_german_and_english_with_the_three_points():
    text = readonly.CUSTOMER_TEXT
    assert "Deutsch:" in text and "English:" in text
    for want in ("programmatischer zugriff", "access key", "mfa", "programmatic access"):
        assert want in text.lower()
    assert "—" not in text


def _wire(monkeypatch, tmp_path, store, test_result, answers):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setenv("AWB_KEYS_CONF", str(tmp_path / "keys.conf"))
    monkeypatch.setattr(tenants, "Store", lambda *a, **k: store)
    monkeypatch.setattr(readonly, "terminal_ready", lambda: True)
    line, secret, _ = _ask(answers)
    monkeypatch.setattr(readonly, "read_line", line)
    monkeypatch.setattr(readonly, "read_secret", secret)
    monkeypatch.setattr(keys, "unlock", lambda: {ALIAS: {"roles": [], "secrets": [], "kind": "readonly",
                                                          "auth": "login"}})
    monkeypatch.setattr(readonly, "check_login", lambda alias, region, request=None: test_result)


def test_ux5_the_command_asks_registers_tests_and_prints_the_text(home, tmp_path, monkeypatch, capsys):
    store = MemStore()
    store.root = tmp_path / "store"
    _wire(monkeypatch, tmp_path, store, (True, readonly.TEST_OK_LINE), ["account-fixture", "reader-fixture", SECRET])
    assert tenants.main(["add-readonly", ALIAS, "--domain-id", DOMAIN_ID, "--login"]) == 0
    out = capsys.readouterr()
    assert "written to pass under awb/tenant/%s/login/: domain, user, password" % ALIAS in out.out
    assert readonly.TEST_OK_LINE in out.out and "Deutsch:" in out.out and "English:" in out.out
    assert "next: awb tenant grant" in out.out
    assert SECRET not in out.out + out.err and store.values["awb/tenant/%s/login/password" % ALIAS] == SECRET


def test_ux5_a_failed_test_says_why_gives_the_text_and_does_not_point_to_a_grant(home, tmp_path, monkeypatch, capsys):
    store = MemStore()
    store.root = tmp_path / "store"
    _wire(monkeypatch, tmp_path, store, (False, readonly.NO_ACCESS_LINE), ["account-fixture", "reader-fixture", SECRET])
    assert tenants.main(["add-readonly", ALIAS, "--domain-id", DOMAIN_ID, "--login"]) == 1
    out = capsys.readouterr().out
    assert "(403)" in out and "no grant yet" in out and "programmatischer zugriff" in out.lower()
    assert "next: awb tenant grant" not in out and SECRET not in out


def test_ux5_the_non_interactive_path_is_unchanged(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setenv("AWB_KEYS_CONF", str(tmp_path / "keys.conf"))
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(readonly, "terminal_ready", lambda: False)
    assert tenants.main(["add-readonly", ALIAS, "--domain-id", DOMAIN_ID, "--login"]) == 1
    assert "is missing: pass insert it first" in capsys.readouterr().err


def test_ux4_write_still_writes_the_status_file_the_old_unit_calls(home, tmp_path, monkeypatch, capsys):
    status = {"version": 1, "ui_runs": [], "intake": []}
    seen = []
    monkeypatch.setattr(owner, "collect", lambda p=None: status)
    monkeypatch.setattr(owner, "validate", lambda s: None)
    monkeypatch.setattr(owner, "write", lambda s, path=owner.STATUS: seen.append((s, path)) or "passed")
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert owner.main(["status", "--write", "--out", str(tmp_path / "s.json")]) == 0
    assert seen == [(status, tmp_path / "s.json")] and "written:" in capsys.readouterr().out
    unit = Path(__file__).resolve().parent.parent / "seal/web/awb-owner-status.service"
    assert "ExecStart=/usr/local/bin/awb owner status --write" in unit.read_text(encoding="utf-8")
