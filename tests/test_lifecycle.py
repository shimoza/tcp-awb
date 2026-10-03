"""Package 5, the life of a project: tags from the list, spawn from the outbox, the first and the last ledger
entry, close, one owner per session, the idle and size notices, the drafted ledger entry, the English notes, the
ledger in the career update and the detector of folders nobody registered."""
from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import awb
from awb import career, cli, english, hooks, ledger, projects, sessions
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"


@pytest.fixture(autouse=True)
def gate_on_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))


def spawn(home, register_path, **kw):
    args = dict(kind="engagement", goal=GOAL, customer=fx.CUSTOMER_CODE, register_path=register_path)
    args.update(kw)
    return projects.spawn(home, args.pop("kind"), args.pop("goal"), args.pop("customer"), args.pop("register_path"),
                          **args)


def run_hook(name, payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def commit(folder: Path, name: str, text: str, subject: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(folder), "commit", "-q", "--no-verify", "-m", subject], check=True,
                   capture_output=True, env=env)


# --------------------------------------------------------------------------- spawn


def test_a_tag_must_come_from_the_list(home, register_path):
    with pytest.raises(projects.ProjectError, match="not in rules/tags.txt"):
        spawn(home, register_path, tags=["kubernetes-ish"])
    pr = spawn(home, register_path, tags=["cce"])
    assert "tags: cce" in (Path(pr.path) / "SCOPE.md").read_text()


def test_spawn_takes_the_outbox_of_the_customer_into_input(home, register_path):
    box = home.outbox / fx.CUSTOMER_CODE
    box.mkdir(parents=True, exist_ok=True)
    (box / "brief.md").write_text("sizing for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    pr = spawn(home, register_path, from_outbox=True)
    assert (Path(pr.path) / "input" / "brief.md").read_text().startswith("sizing for")
    assert not any(box.iterdir())
    tracked = subprocess.run(["git", "-C", pr.path, "ls-files"], capture_output=True, text=True).stdout
    assert "input/brief.md" in tracked
    with pytest.raises(projects.ProjectError, match="holds no file"):
        spawn(home, register_path, from_outbox=True)
    with pytest.raises(projects.ProjectError, match="needs a customer"):
        spawn(home, register_path, kind="lab", customer=None, from_outbox=True)


def test_the_command_line_writes_the_first_and_the_last_ledger_entry(home, register_path, capsys):
    assert cli.main(["spawn", "engagement", "--goal", GOAL, "--customer", fx.CUSTOMER_CODE, "--tag", "cce"]) == 0
    code = capsys.readouterr().out.split()[1]
    assert cli.main(["close", code]) == 0
    assert "closed %s" % code in capsys.readouterr().out
    done = [e.done for e in ledger.load(home) if e.project == code]
    assert done[0] == "Started %s (a query): %s" % (code, GOAL)      # two kinds since 2026-10-03
    assert done[-1] == "Closed %s" % code
    assert cli.main(["close", code]) == 0
    assert "was closed already" in capsys.readouterr().out


# --------------------------------------------------------------------------- close


def test_close_waits_for_open_items_and_live_resources(home, register_path, capsys):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    assert "| id | type | region | cost class | expiry | state | note |" in (folder / "RESOURCES.md").read_text()
    with open(folder / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("- the sizing of the second cluster\n")
    with open(folder / "RESOURCES.md", "a", encoding="utf-8") as f:
        f.write("| vpc-01 | vpc | eu-de | free | none | deleted | |\n| ecs-01 | ecs | eu-de | small | 2026-10-01 | live | |\n"
                "| obs-01 | obs | eu-de | small | none | kept | the handover bucket |\n")
    assert (projects.open_items(folder), projects.live_resources(folder)) == (1, 1)
    with pytest.raises(projects.ProjectError, match="1 open item.*1 live resource"):
        projects.close(home, pr.code)
    assert cli.main(["close", pr.code, "--force"]) == 0
    assert "with 1 open item(s) and 1 live resource(s) left" in capsys.readouterr().out
    assert {r.code: r.state for r in projects.load(home)}[pr.code] == "closed"


# --------------------------------------------------------------------------- one owner per project


def test_one_live_owner_per_project(home):
    t0 = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)
    assert sessions.claim(home, "tcp-abcd", "session-a", now=t0) is None
    other = sessions.claim(home, "tcp-abcd", "session-b", now=t0 + datetime.timedelta(minutes=5))
    assert other["session"] == "session-a"
    assert sessions.beat(home, "tcp-abcd", "session-a", now=t0 + datetime.timedelta(minutes=20))
    assert not sessions.beat(home, "tcp-abcd", "session-b", now=t0 + datetime.timedelta(minutes=20))
    late = t0 + datetime.timedelta(minutes=20 + sessions.LIVE_MINUTES + 1)
    assert sessions.claim(home, "tcp-abcd", "session-b", now=late) is None          # the old claim went stale
    assert sessions.owner(home, "tcp-abcd", now=late)["session"] == "session-b"
    with pytest.raises(ValueError):
        sessions.claim(home, "not-a-code", "session-a")


def test_session_start_claims_and_tells_about_another_session_an_idle_project_and_a_long_state(
        home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path, kind="lab", customer=None)
    folder = Path(pr.path)
    code, out, _ = run_hook("session-start", {"cwd": pr.path, "session_id": "first-session-id"}, monkeypatch, capsys)
    assert code == 0 and "another session" not in out
    assert sessions.owner(home, pr.code)["session"] == "first-session-id"
    (folder / "STATE.md").write_text("# State\n" + "line\n" * 200, encoding="utf-8")
    old = (datetime.datetime.now() - datetime.timedelta(days=40)).timestamp()
    os.utime(folder / "STATE.md", (old, old))
    code, out, _ = run_hook("session-start", {"cwd": pr.path, "session_id": "second-session-id"}, monkeypatch, capsys)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "another session (first-se" in text
    assert "awb close %s" % pr.code in text
    assert "STATE.md has 201 lines, over 150" in text
    run_hook("prompt", {"cwd": pr.path, "session_id": "first-session-id", "prompt": "status please"}, monkeypatch,
             capsys)
    assert sessions.owner(home, pr.code)["session"] == "first-session-id"


# --------------------------------------------------------------------------- the stop hook


def test_the_stop_hook_drafts_a_ledger_entry_from_new_commits_quietly(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path, tags=["cce"])
    folder = Path(pr.path)
    assert run_hook("stop", {"cwd": pr.path}, monkeypatch, capsys) == (0, "", "")
    assert not [e for e in ledger.load(home) if e.project == pr.code]            # the spawn commit alone is no work
    commit(folder, "evidence/sizing.md", "two clusters, six nodes\n", "Sizing of the two clusters")
    commit(folder, "evidence/network.md", "one vpc per stage\n", "Network layout per stage")
    monkeypatch.setattr(hooks, "DRAFT_HOURS", 0)
    assert run_hook("stop", {"cwd": pr.path}, monkeypatch, capsys) == (0, "", "")
    drafts = [e for e in ledger.load(home) if e.project == pr.code]
    assert len(drafts) == 1 and drafts[0].customer == fx.CUSTOMER_CODE
    assert drafts[0].done == "%s: 2 commit(s): Sizing of the two clusters; Network layout per stage" % pr.code
    assert "drafted at session end" in drafts[0].outcome
    assert run_hook("stop", {"cwd": pr.path}, monkeypatch, capsys) == (0, "", "")
    assert len([e for e in ledger.load(home) if e.project == pr.code]) == 1       # nothing new, nothing written


def transcript(tmp_path: Path, text: str) -> Path:
    t = tmp_path / "session.jsonl"
    lines = [{"type": "user", "message": {"content": "hello"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return t


def test_the_english_note_of_the_last_reply_is_kept_once_and_never_with_a_name(home, tmp_path, monkeypatch, capsys):
    reply = "The sizing is done.\n\nEnglish note: \"more cheaper\" should be \"cheaper\"."
    assert english.note_of(reply) == "\"more cheaper\" should be \"cheaper\"."
    t = transcript(tmp_path, reply)
    assert run_hook("stop", {"cwd": str(tmp_path), "transcript_path": str(t)}, monkeypatch, capsys) == (0, "", "")
    assert run_hook("stop", {"cwd": str(tmp_path), "transcript_path": str(t)}, monkeypatch, capsys) == (0, "", "")
    assert [n["note"] for n in english.load(home)] == ["\"more cheaper\" should be \"cheaper\"."]
    named = transcript(tmp_path, "Done.\n\nEnglish note: write \"%s\" with a capital." % fx.CUSTOMER_FORMS[0])
    assert not english.capture(home, named)
    assert len(english.load(home)) == 1
    assert cli.main(["english", "list", "--month", datetime.date.today().isoformat()[:7]]) == 0
    assert "1 note(s)" in capsys.readouterr().out


# --------------------------------------------------------------------------- career and the detector


def test_the_career_update_offers_ledger_work_with_the_codes_masked(home, register_path):
    pr = spawn(home, register_path, tags=["cce"])
    ledger.add("code", "Terraform module for the clusters of %s in %s" % (fx.CUSTOMER_CODE, pr.code),
               project=pr.code, customer=fx.CUSTOMER_CODE, tags=["terraform"], outcome="two stages deployed", p=home)
    ledger.add("inquiry", "a question about quotas", p=home)
    res = career.update(home)
    assert "## From the ledger" not in res.draft                                  # the draft itself stays as it was
    section = res.candidates
    assert "Terraform module for the clusters of a customer in a project" in section
    assert "Outcome: two stages deployed" in section and "a question about quotas" not in section
    assert fx.CUSTOMER_CODE not in section and pr.code not in section


def test_the_detector_finds_folders_nobody_registered(home, register_path, capsys):
    pr = spawn(home, register_path)
    for name in ("tcp-zz22", "tcp-%s" % fx.CUSTOMER_FORMS[1].lower().replace(" ", "-"), "tcp-kb", "tcp-shared"):
        (home.projects_root / name).mkdir(parents=True, exist_ok=True)
    found = projects.unregistered(home)
    assert sorted(found) == ["a folder", "tcp-zz22"]
    assert pr.code not in found
    assert cli.main(["projects", "check"]) == 1
    out = capsys.readouterr().out
    assert "2 tcp- folder(s)" in out and fx.CUSTOMER_FORMS[1].lower() not in out


# --------------------------------------------------------------------------- delete


def test_delete_removes_the_folder_of_a_closed_project_and_keeps_the_code(home, register_path, capsys):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    with pytest.raises(projects.ProjectError, match="is active; close it first"):
        projects.delete(home, pr.code)
    assert folder.is_dir()
    projects.close(home, pr.code, force=True)
    assert cli.main(["projects", "delete", pr.code]) == 0
    assert "deleted %s" % pr.code in capsys.readouterr().out
    assert not folder.exists()
    assert [r.state for r in projects.load(home) if r.code == pr.code] == ["deleted"]
    assert cli.main(["projects", "list"]) == 0
    assert pr.code not in capsys.readouterr().out
    assert cli.main(["projects", "list", "--all"]) == 0
    assert pr.code in capsys.readouterr().out
    # again: nothing changes; close does not bring it back
    assert projects.delete(home, pr.code).state == "deleted"
    assert projects.close(home, pr.code).state == "deleted"


def test_delete_waits_for_live_resources_and_a_live_session(home, register_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    projects.close(home, pr.code, force=True)
    with open(folder / "RESOURCES.md", "a", encoding="utf-8") as f:
        f.write("| srv-1 | ecs | eu-de | small | 2026-12-31 | live | lab server |\n")
    with pytest.raises(projects.ProjectError, match="1 live resource"):
        projects.delete(home, pr.code)
    (folder / "RESOURCES.md").write_text((folder / "RESOURCES.md").read_text().replace("| live |", "| deleted |"))
    with pytest.raises(projects.ProjectError, match="a live session holds"):
        projects.delete(home, pr.code, holder=lambda c: True)
    assert folder.is_dir()
    projects.delete(home, pr.code)
    assert not folder.exists()


def test_delete_never_follows_a_link_in_place_of_the_folder(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    projects.close(home, pr.code, force=True)
    folder = Path(pr.path)
    elsewhere = tmp_path / "elsewhere"
    folder.rename(elsewhere)
    folder.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(projects.ProjectError, match="not a plain folder"):
        projects.delete(home, pr.code)
    assert elsewhere.is_dir() and (elsewhere / "SCOPE.md").is_file()


def test_delete_refuses_codes_it_does_not_know(home, capsys):
    assert cli.main(["projects", "delete", "tcp-zzzz", "nonsense"]) == 1
    err = capsys.readouterr().err
    assert "tcp-zzzz is not registered" in err and "not a project code" in err


# --------------------------------------------------------------------------- query and project


def test_two_kinds_and_the_old_ones_as_aliases(home, register_path, capsys):
    q = projects.spawn(home, "query", GOAL, None, register_path)
    pj = projects.spawn(home, "project", GOAL, fx.CUSTOMER_CODE, register_path)
    old = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    assert (q.kind, pj.kind, old.kind) == ("query", "project", "query")
    assert "- kind: query" in (Path(q.path) / "SCOPE.md").read_text()
    assert "no test tenant" in (Path(q.path) / "CLAUDE.md").read_text()
    assert "reaches the test tenants" in (Path(pj.path) / "CLAUDE.md").read_text()
    assert projects.tenant_access("project") and projects.tenant_access("lab")
    assert not projects.tenant_access("query") and not projects.tenant_access("engagement")
    with pytest.raises(projects.ProjectError, match="query, project"):
        projects.spawn(home, "survey", GOAL, None, register_path)


def test_switch_a_query_to_a_project_and_back(home, register_path, capsys):
    q = projects.spawn(home, "query", GOAL, None, register_path)
    assert cli.main(["projects", "kind", q.code, "project"]) == 0
    assert "is now a project (was query)" in capsys.readouterr().out
    assert {r.code: r.kind for r in projects.load(home)}[q.code] == "project"
    assert "- kind: project" in (Path(q.path) / "SCOPE.md").read_text()
    assert cli.main(["projects", "kind", q.code, "project"]) == 0
    assert "a project already" in capsys.readouterr().out
    projects.close(home, q.code, force=True)
    with pytest.raises(projects.ProjectError, match="not active"):
        projects.set_kind(home, q.code, "query")
