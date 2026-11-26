"""Tests of the project kind ask (awb/askkind.py): the owner's standing question session about how TCP behaves.

An ask project has no customer and takes no material; its hooks and its commit gate check secrets only; it reads the
test tenants with the read role only. The kind that lifts a check comes from the owner's grant alone: a row or a
SCOPE.md that says ask lifts nothing.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from awb import askkind, bucket, cli, config, gate, grants, hooks, importcmd, material, paste, projects, start
from awb import toolguard
from awb.tcp import cloud, fetch, keys, lab, xchg
from tests import fixtures

GOAL = "short questions on how tcp behaves"
SECRET_VALUE = "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
SECRET_LINE = "db_pass" + "word = " + SECRET_VALUE
NAME_TEXT = "how does ecs behave for %s in eu-de" % fixtures.CUSTOMER_FORMS[0]
KEY_PAIR = ("access key: " + "AKIA" + "Q7M4X2P9" + "B6L3C8D1" + "\nsecret key: " + "wJalrXUt" + "nFEMI/K7M" + "DENG/bPxR"
            + "fiCYQ7m4Z2pK9x" + "\n")


def spawn(home, kind="ask") -> Path:
    pr = projects.spawn(home, kind, GOAL, None, home.register)
    return Path(pr.path)


def granted(home, kind="ask") -> Path:
    folder = spawn(home, kind)
    assert cli.main(["projects", "kind", folder.name, kind]) == 0
    return folder


def run_hook(name: str, payload: dict, monkeypatch, capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture(autouse=True)
def no_project_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


# --------------------------------------------------------------------------- spawn, start and the switch


def test_spawn_of_kind_ask_has_no_customer_and_its_own_rules(home):
    folder = spawn(home)
    row = next(r for r in projects.load(home) if r.code == folder.name)
    assert (row.kind, row.customer) == ("ask", "none")
    assert "- kind: ask\n" in (folder / "SCOPE.md").read_text(encoding="utf-8")
    rules = (folder / "CLAUDE.md").read_text(encoding="utf-8")
    assert "awb kb find" in rules and "awb doc fetch" in rules and "awb kb add" in rules
    assert "awb inbox take <his words>" not in rules and "awb lab run start" not in rules
    assert rules.count("\n") <= projects.MAX_CLAUDE_LINES
    assert projects.read_key_token(folder) is not None


def test_spawn_of_kind_ask_refuses_a_customer_and_a_tenant(home):
    with pytest.raises(projects.ProjectError, match="no customer"):
        projects.spawn(home, "ask", GOAL, fixtures.CUSTOMER_CODE, home.register)
    with pytest.raises(projects.ProjectError, match="no customer"):
        projects.spawn(home, "ask", GOAL, None, home.register, tenant="test-1")
    assert cli.main(["spawn", "ask", "--goal", GOAL, "--customer", "new"]) != 0
    assert projects.load(home) == []


def test_awb_spawn_ask_works(home, capsys):
    assert cli.main(["spawn", "ask", "--goal", GOAL]) == 0
    assert [r.kind for r in projects.load(home)] == ["ask"]


def test_start_ask_spawns_and_grants(home, capsys):
    assert start.start(home, "ask", GOAL) == 0
    (row,) = projects.load(home)
    assert row.kind == "ask"
    assert grants.kind(home, row.code) == "ask"
    assert grants.ask_tokens(home)[row.code] == projects.read_key_token(Path(row.path))
    assert grants.tokens(home) == {}                        # the tenant tokens of the kind project stay apart
    assert "open: awb projects open %s" % row.code in capsys.readouterr().out


def test_start_ask_refuses_a_customer_and_a_folder(home, tmp_path):
    with pytest.raises(start.StartError, match="no --customer"):
        start.start(home, "ask", GOAL, customer="new")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    with pytest.raises(start.StartError, match="no --customer"):
        start.start(home, "ask", GOAL, from_dir=str(tmp_path / "in"), trusted=True)
    assert projects.load(home) == []


def test_the_switch_to_ask_refuses_a_customer_and_material(home, capsys):
    pr = projects.spawn(home, "query", GOAL, None, home.register)
    (Path(pr.path) / "input" / "copy-1.md").write_text("a copy\n", encoding="utf-8")
    assert cli.main(["projects", "kind", pr.code, "ask"]) == 1
    assert "imported material" in capsys.readouterr().err
    with pytest.raises(projects.ProjectError, match="imported material"):
        projects.set_kind(home, pr.code, "ask")
    assert grants.kind(home, pr.code) is None

    rows = projects.load(home)
    with_customer = projects.Project(code="tcp-zzzz", kind="query", customer=fixtures.CUSTOMER_CODE, platform="tcp",
                                     path=str(home.projects_root / "tcp-zzzz"),
                                     memory_key=projects.memory_key(home.projects_root / "tcp-zzzz"), state="active",
                                     created=rows[0].created)
    projects._save(home, rows + [with_customer])
    with pytest.raises(projects.ProjectError, match="has a customer"):
        projects.set_kind(home, "tcp-zzzz", "ask")
    assert cli.main(["projects", "kind", "tcp-zzzz", "ask", "--customer", fixtures.CUSTOMER_CODE]) == 1
    assert grants.kind(home, "tcp-zzzz") is None


def test_the_kind_is_not_switchable_from_inside_the_project(home, monkeypatch, capsys):
    folder = spawn(home, "query")
    # what a session can write: its row and its SCOPE.md say ask
    projects.set_kind(home, folder.name, "ask")
    assert "- kind: ask" in (folder / "SCOPE.md").read_text(encoding="utf-8")
    assert not askkind.is_ask(folder, home)
    code, _, err = run_hook("prompt", {"prompt": NAME_TEXT, "cwd": str(folder)}, monkeypatch, capsys)
    assert code == 0 and err == ""              # DMZ 2: no project runs a name check on a prompt
    # an ask grant with another token than the folder's lifts nothing either
    grants.grant(home, folder.name, "ask")
    grants.record_token(home, folder.name, "0" * 64)
    assert not askkind.is_ask(folder, home)
    # but a row that says ask refuses material all the same
    assert askkind.refuses_material(home, folder.name)


def test_the_owner_grant_counts_when_the_session_switched_its_row_back(home):
    folder = granted(home)
    projects.set_kind(home, folder.name, "query")           # the row, as a session can write it
    assert askkind.is_ask(folder, home)
    assert askkind.refuses_material(home, folder.name)


# --------------------------------------------------------------------------- no material enters


def test_awb_import_and_the_paste_door_refuse_an_ask_project(home, tmp_path):
    folder = granted(home)
    f = tmp_path / "offer.txt"
    f.write_text("plain text\n", encoding="utf-8")
    with pytest.raises(importcmd.ImportRefused, match="ask project"):
        importcmd.run(home, folder.name, [f], trusted=True)
    with pytest.raises(importcmd.ImportRefused, match="ask project"):
        importcmd.run_paste(home, folder.name, stdin=io.BytesIO(b"plain text\n"))
    assert sorted(p.name for p in (folder / "input").iterdir()) == [".gitkeep"]


def test_paste_material_inbox_and_xchg_refuse_in_an_ask_folder(home, monkeypatch, capsys):
    folder = granted(home)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(sys, "stdin", io.StringIO("some output\n"))
    assert paste.main([]) == 2
    assert "ask project" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"a note\n")))
    assert material.main(["add"]) == 2
    assert "ask project" in capsys.readouterr().err
    assert xchg.main_inbox(["take", "the", "offer"]) == 1
    assert "ask project" in capsys.readouterr().err
    assert xchg.main_xchg(["list"]) == 1
    assert "ask project" in capsys.readouterr().err


def test_bucket_pull_and_fetch_refuse_an_ask_project(home, monkeypatch):
    folder = granted(home)
    monkeypatch.setattr(bucket, "owner_terminal", lambda what: None)
    with pytest.raises(bucket.BucketError, match="ask project"):
        bucket.fetch(home, None, folder.name)
    monkeypatch.setattr(fetch, "_owner_side", lambda code: None)
    with pytest.raises(fetch.FetchError, match="ask project"):
        fetch.fetch(home, folder.name)


# --------------------------------------------------------------------------- the hooks: secrets only


def test_the_prompt_hook_lets_a_name_pass_and_refuses_a_secret_in_an_ask_project(home, monkeypatch, capsys):
    """DMZ 2: the one refusal of a prompt is a key pair or a private key block, in an ask project as in every other."""
    folder = granted(home)
    code, _, err = run_hook("prompt", {"prompt": NAME_TEXT, "cwd": str(folder)}, monkeypatch, capsys)
    assert code == 0 and err == ""
    code, _, err = run_hook("prompt", {"prompt": "use %s for the test" % SECRET_LINE, "cwd": str(folder)},
                            monkeypatch, capsys)
    assert code == 0 and err == ""
    code, _, err = run_hook("prompt", {"prompt": "use\n%s\nfor the test" % KEY_PAIR, "cwd": str(folder)},
                            monkeypatch, capsys)
    assert code == 2 and "access-key" in err and "wJalrXUt" not in err


def test_the_prompt_hook_still_refuses_the_name_in_a_project(home, monkeypatch, capsys):
    folder = granted(home, "project")
    code, _, err = run_hook("prompt", {"prompt": NAME_TEXT, "cwd": str(folder)}, monkeypatch, capsys)
    assert code == 0 and err == ""              # DMZ 2: no name check in the prompt hook
    code, _, err = run_hook("prompt", {"prompt": "use\n%s\nfor the test" % KEY_PAIR, "cwd": str(folder)},
                            monkeypatch, capsys)
    assert code == 2 and "access-key" in err


def test_the_write_hook_checks_secrets_only_in_an_ask_project(home, monkeypatch, capsys):
    """DMZ 2: a file written inside a project, ask or not, is written as it is."""
    folder = granted(home)
    notes = folder / "evidence" / "notes.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text("# Notes\n\n%s\n" % NAME_TEXT, encoding="utf-8")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(notes)}, "cwd": str(folder)}
    assert run_hook("post-write", payload, monkeypatch, capsys)[0] == 0
    notes.write_text("# Notes\n\n%s\n" % SECRET_LINE, encoding="utf-8")
    assert run_hook("post-write", payload, monkeypatch, capsys)[0] == 0
    assert SECRET_VALUE in notes.read_text(encoding="utf-8")

    other = granted(home, "project")
    notes = other / "evidence" / "notes.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text("# Notes\n\n%s\n" % NAME_TEXT, encoding="utf-8")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(notes)}, "cwd": str(other)}
    assert run_hook("post-write", payload, monkeypatch, capsys)[0] == 0


def test_the_start_withholds_nothing_but_a_secret_in_an_ask_project(home, monkeypatch, capsys):
    """DMZ 2: the start shows the files of a project as written, a secret included."""
    folder = granted(home)
    (folder / "STATE.md").write_text("# State\n\nStatus: %s\nNext: more\n" % NAME_TEXT, encoding="utf-8")
    (folder / "OPEN.md").write_text("# Open\n\n%s\n" % SECRET_LINE, encoding="utf-8")
    code, out, _ = run_hook("session-start", {"cwd": str(folder), "session_id": "s-1"}, monkeypatch, capsys)
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and "ask project" in context
    assert NAME_TEXT in context
    assert SECRET_VALUE in context and "withheld" not in context

    other = granted(home, "project")
    (other / "STATE.md").write_text("# State\n\nStatus: %s\nNext: more\n" % NAME_TEXT, encoding="utf-8")
    code, out, _ = run_hook("session-start", {"cwd": str(other), "session_id": "s-2"}, monkeypatch, capsys)
    assert NAME_TEXT in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_the_tool_guard_lets_an_unreadable_path_pass_but_no_refused_place(home, tmp_path):
    folder = granted(home)
    other = granted(home, "project")
    user_home = tmp_path / "user-home"
    user_home.mkdir()

    def guard(where: Path, command: str):
        data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(where)}
        return toolguard.guard(data, home, user_home, sealed=True)

    assert guard(folder, "cat $X/notes.txt") is None
    assert guard(folder, "cat $X/notes.txt ../%s/input/copy.md" % other.name) == toolguard.ANOTHER_PROJECT
    assert guard(folder, "cat ../%s/STATE.md" % other.name) == toolguard.ANOTHER_PROJECT
    assert guard(other, "cat $X/notes.txt") == toolguard.UNREADABLE


def test_the_commit_gate_of_an_ask_project_checks_secrets_only(home, monkeypatch, capsys, tmp_path):
    # B-087: a commit inside a project (an ask project too) runs no content check: a name and a secret pass; the
    # attribution of an assistant and a text over the size limit are still refused
    folder = granted(home)
    capsys.readouterr()
    (folder / "notes.md").write_text("%s\n" % NAME_TEXT, encoding="utf-8")
    subprocess.run(["git", "-C", str(folder), "add", "notes.md"], check=True)
    assert gate.main(["--staged", "--repo", str(folder)]) == 0
    (folder / "keys.md").write_text("%s\n" % SECRET_LINE, encoding="utf-8")
    subprocess.run(["git", "-C", str(folder), "add", "keys.md"], check=True)
    assert gate.main(["--staged", "--repo", str(folder)]) == 0
    assert capsys.readouterr().out == ""
    (folder / "big.md").write_text("x" * (gate.TEXT_LIMIT + 1), encoding="utf-8")
    subprocess.run(["git", "-C", str(folder), "add", "big.md"], check=True)
    assert gate.main(["--staged", "--repo", str(folder)]) == 1
    assert capsys.readouterr().out == "too-large  big.md:0\n"
    msg = tmp_path / "MSG"
    msg.write_text("note\n\nCo-Authored-By: Clau" + "de <noreply@anthropic.com>\n", encoding="utf-8")
    assert gate.main(["--message", str(msg), "--repo", str(folder)]) == 1
    assert "attribution  commit message:3" in capsys.readouterr().out

    other = granted(home, "project")
    capsys.readouterr()
    (other / "notes.md").write_text("%s\n" % NAME_TEXT, encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "notes.md"], check=True)
    assert gate.main(["--staged", "--repo", str(other)]) == 0
    assert capsys.readouterr().out == ""


def test_kb_add_from_an_ask_project_runs_every_check(home, monkeypatch, capsys):
    folder = granted(home)
    monkeypatch.chdir(folder)
    argv = ["kb", "add", "--scope", "tcp", "--tag", "ecs", "--grade", "docs", "--class", "api", "--source",
            "docs mirror, user guide"]
    assert cli.main(argv + ["ecs flavor s3.large.2 is offered to %s in eu-de" % fixtures.CUSTOMER_FORMS[1]]) == 1
    assert "name check" in capsys.readouterr().err
    assert cli.main(argv + ["ecs flavor s3.large.2 is offered in eu-de"]) == 0
    assert "added KB-" in capsys.readouterr().out


# --------------------------------------------------------------------------- the tenants: test tenants only


def test_lab_runs_and_lab_calls_are_refused_in_an_ask_folder(home, monkeypatch, capsys):
    """Changed on the owner's word of 2026-11-24 ("ему нужен прямой доступ ... только тестовыми тенантами"): an ask
    project is no longer stopped before a lab run or a lab call; the key service still keeps it to test tenants."""
    folder = granted(home)
    monkeypatch.chdir(folder)
    lab.main(["run", "status"])
    assert "ask project" not in capsys.readouterr().err
    cloud.main(["call", "GET", "ecs", "/v1/x/cloudservers", "--tenant", "test-1", "--role", "lab"])
    assert "ask project" not in capsys.readouterr().err
    cloud.main(["call", "POST", "ecs", "/v1/x/cloudservers", "--tenant", "test-1"])
    assert "ask project" not in capsys.readouterr().err


@pytest.fixture
def service(home, tmp_path, monkeypatch):
    s = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock", paths_fn=lambda: home)
    s.tenants = {"test-1": {"roles": {"read": {"ak": "a", "sk": "b"}, "lab": {"ak": "c", "sk": "d"}}},
                 "cust-1": {"roles": {"read": {"ak": "e", "sk": "f"}}}}
    monkeypatch.setattr(s, "_session_read", lambda uid: True)
    monkeypatch.setattr(s, "_registered", lambda: {"test-1": ("eu-de",), "cust-1": ("eu-de",)})
    monkeypatch.setattr(s, "_readonly", lambda alias: {"regions": ["eu-de"]} if alias == "cust-1" else None)
    return s


def test_the_key_service_answers_the_kind_for_the_folder_token(home, service):
    folder = granted(home)
    query = granted(home, "query")
    assert service._kind({"op": "kind", "token": projects.read_key_token(folder)}) == {
        "ok": True, "kind": "ask", "code": folder.name}
    assert service._kind({"op": "kind", "token": projects.read_key_token(query)}) == {"ok": True, "kind": None}
    assert service._kind({"op": "kind", "token": "0" * 64}) == {"ok": True, "kind": None}


def test_the_key_service_refuses_lab_writes_and_customer_tenants_for_an_ask_project(home, service):
    """Changed on the owner's word of 2026-11-24: the lab role and writes on a test tenant pass the ask check; a
    customer tenant and another project's code are still refused, and no material moves."""
    folder = granted(home)
    token = projects.read_key_token(folder)
    base = {"op": "call", "token": token, "service": "ecs", "path": "/v1/x/cloudservers", "region": "eu-de"}
    for req in (dict(base, tenant="cust-1", role="read", method="GET"),
                dict(base, tenant="test-1", role="read", method="GET", project="tcp-zzzz")):
        with pytest.raises(keys.Refused, match="test tenants only"):
            service._call(req, 4242, caller=None)
    for req in (dict(base, tenant="test-1", role="lab", method="GET", region="eu-nl"),
                dict(base, tenant="test-1", role="lab", method="POST", region="eu-nl")):
        # past the ask check; stopped at the next one (a region the tenant lacks), so no request leaves the host
        with pytest.raises(keys.Refused, match="region is not one of the tenant"):
            service._call(req, 4242, caller=None)
    for op in ("take_lab", "take_owner", "inbox_find", "obs"):
        answer = service._call_socket({"op": op, "project": folder.name, "id": "f-1"}, 4242, caller=None)
        assert answer["ok"] is False and "ask project" in answer["error"]


def test_the_work_user_asks_the_key_service_for_the_kind(home, service, tmp_path, monkeypatch):
    folder = granted(home)
    query = granted(home, "query")
    sock = tmp_path / "kind.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(4)

    def serve():
        for _ in range(3):
            conn, _ = srv.accept()
            with conn:
                line = conn.makefile("rb").readline()
                conn.sendall(json.dumps(service._call_socket(json.loads(line), 4242)).encode("utf-8") + b"\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    try:
        assert askkind.is_ask(folder, home, sock=sock)
        assert not askkind.is_ask(query, home, sock=sock)
        projects.set_kind(home, query.name, "ask")          # the row of the work user lifts nothing
        assert not askkind.is_ask(query, home, sock=sock)
    finally:
        t.join(timeout=10)
        srv.close()
    assert not askkind.is_ask(folder, home, sock=tmp_path / "none.sock")    # no key service: every check runs
