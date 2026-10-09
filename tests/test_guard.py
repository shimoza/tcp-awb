"""The tool guard of the hooks: `awb hook pre-tool` keeps a project session inside its project folder.

A Read, Edit, MultiEdit, NotebookEdit, Write, Glob, Grep or Bash call that reaches another project, the client's
folder or a key folder is refused with one line naming the class of the place, never the path; every refusal is a
line in <shared>/sessions/guard.log and the stop hook counts them per project and day.
"""
from __future__ import annotations

import datetime
import io
import json
import sys
from pathlib import Path

import pytest

from awb import hooks, ledger, questions, seal

OWN = "tcp-q7m4"
OTHER = "tcp-z3k5"


def run(name: str, payload: dict, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    """Two projects under the projects root, the knowledge base, and a user home with .ssh, .config and .claude."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = tmp_path / "userhome"
    for folder in (".ssh", ".config", ".claude"):
        (user / folder).mkdir(parents=True)
    (user / ".ssh" / "id_ed25519").write_text("key\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    own, other = home.projects_root / OWN, home.projects_root / OTHER
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
        (root / "STATE.md").write_text("Status: working\nNext: size\n", encoding="utf-8")
    (home.kb / "entries").mkdir(parents=True, exist_ok=True)
    return {"p": home, "own": own, "other": other, "user": user}


def call(world, tool: str, sid: str = "s-1", **tool_input) -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(world["own"]), "session_id": sid}


def assert_refused(result, cls: str, *secret_paths: Path) -> None:
    code, out, err = result
    assert code == hooks.BLOCK
    assert err.strip() == hooks.refusal(cls) and len(err.strip().splitlines()) == 1
    for path in secret_paths:
        assert str(path) not in err and path.name not in err


def test_a_read_of_another_projects_state_is_refused_and_logged(world, monkeypatch, capsys):
    target = world["other"] / "STATE.md"
    assert_refused(run("pre-tool", call(world, "Read", file_path=str(target)), monkeypatch, capsys),
                   hooks.ANOTHER_PROJECT, target, world["other"])
    (line,) = hooks.guard_log(world["p"]).read_text(encoding="utf-8").splitlines()
    when, sid, tool, cls, code = line.split("\t")
    assert (sid, tool, cls, code) == ("s-1", "Read", hooks.ANOTHER_PROJECT, OWN)
    assert when.startswith(datetime.date.today().isoformat())
    assert OTHER not in line


def test_a_bash_cat_of_another_projects_path_is_refused(world, monkeypatch, capsys):
    target = world["other"] / "STATE.md"
    for command in ("cat %s" % target, "cd /tmp && cat '%s' | head" % target, "cat ../%s/STATE.md" % OTHER,
                    "grep -r Status %s/tcp-*/STATE.md" % world["p"].projects_root):
        assert_refused(run("pre-tool", call(world, "Bash", command=command), monkeypatch, capsys),
                       hooks.ANOTHER_PROJECT, target)


def test_a_bash_with_a_path_of_the_own_project_passes(world, monkeypatch, capsys):
    own = world["own"]
    for command in ("cat %s/STATE.md" % own, "ls %s && git -C %s status 2>/dev/null" % (own, own),
                    "cat STATE.md", "wc -l /tmp/notes.txt", "/usr/local/bin/awb kb search obs",
                    "ls %s" % world["p"].outbox):
        assert run("pre-tool", call(world, "Bash", command=command), monkeypatch, capsys) == (0, "", "")


def test_a_grep_over_the_knowledge_base_passes(world, monkeypatch, capsys):
    kb = world["p"].kb
    assert run("pre-tool", call(world, "Grep", pattern="obs", path=str(kb)), monkeypatch, capsys) == (0, "", "")
    assert run("pre-tool", call(world, "Glob", pattern="**/*.md", path=str(kb)), monkeypatch, capsys) == (0, "", "")
    assert run("pre-tool", call(world, "Grep", pattern="obs"), monkeypatch, capsys) == (0, "", "")


def test_a_read_of_the_ssh_key_is_refused(world, monkeypatch, capsys):
    key = world["user"] / ".ssh" / "id_ed25519"
    assert_refused(run("pre-tool", call(world, "Read", file_path=str(key)), monkeypatch, capsys),
                   hooks.KEY_FOLDER, key)
    assert_refused(run("pre-tool", call(world, "Read", file_path="~/.ssh/id_ed25519"), monkeypatch, capsys),
                   hooks.KEY_FOLDER)
    assert_refused(run("pre-tool", call(world, "Bash", command="cat $HOME/.config/x.ini"), monkeypatch, capsys),
                   hooks.KEY_FOLDER)


def test_the_clients_folder_and_a_search_from_above_are_refused(world, monkeypatch, capsys):
    claude = world["user"] / ".claude" / "settings.json"
    assert_refused(run("pre-tool", call(world, "Write", file_path=str(claude), content="{}"), monkeypatch, capsys),
                   hooks.CLIENT_FOLDER)
    assert_refused(run("pre-tool", call(world, "Grep", pattern="Status", path=str(world["p"].projects_root)),
                       monkeypatch, capsys), hooks.ANOTHER_PROJECT)
    assert_refused(run("pre-tool", call(world, "Glob", pattern="%s/*/STATE.md" % world["p"].projects_root),
                       monkeypatch, capsys), hooks.ANOTHER_PROJECT)
    assert_refused(run("pre-tool", call(world, "Bash", command="find / -name STATE.md"), monkeypatch, capsys),
                   hooks.ANOTHER_PROJECT)


def test_a_link_inside_the_own_project_does_not_lead_out(world, monkeypatch, capsys):
    link = world["own"] / "peek"
    link.symlink_to(world["other"])
    assert_refused(run("pre-tool", call(world, "Read", file_path=str(link / "STATE.md")), monkeypatch, capsys),
                   hooks.ANOTHER_PROJECT)
    assert_refused(run("pre-tool", call(world, "Edit", file_path="peek/STATE.md", old_string="a", new_string="b"),
                       monkeypatch, capsys), hooks.ANOTHER_PROJECT)


def test_the_hook_fails_closed(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hooks, "_sealed", lambda: True)
    outside = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "cwd": str(tmp_path / "nowhere")}
    assert_refused(run("pre-tool", outside, monkeypatch, capsys), hooks.UNKNOWN_PROJECT)
    assert_refused(run("pre-tool", call(world, "Read"), monkeypatch, capsys), hooks.UNKNOWN_PROJECT)
    bad = call(world, "Bash")
    bad["tool_input"] = {"command": ["cat", "x"]}
    assert_refused(run("pre-tool", bad, monkeypatch, capsys), hooks.UNKNOWN_PROJECT)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hooks.main(["pre-tool"]) == hooks.BLOCK
    capsys.readouterr()
    monkeypatch.setattr(hooks, "place_class", lambda *a: 1 / 0)
    assert_refused(run("pre-tool", call(world, "Read", file_path=str(world["own"] / "STATE.md")),
                       monkeypatch, capsys), hooks.UNKNOWN_PROJECT)


def test_an_owner_session_is_not_guarded(world, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    target = world["other"] / "STATE.md"
    assert run("pre-tool", call(world, "Read", file_path=str(target)), monkeypatch, capsys) == (0, "", "")
    assert not hooks.guard_log(world["p"]).exists()


def test_the_stop_hook_counts_refusals_per_project_and_day(world, monkeypatch, capsys):
    target = world["other"] / "STATE.md"
    for _ in range(3):
        run("pre-tool", call(world, "Read", file_path=str(target)), monkeypatch, capsys)
    assert run("stop", {"cwd": str(world["own"])}, monkeypatch, capsys)[0] in (0, 2)
    assert run("stop", {"cwd": str(world["own"])}, monkeypatch, capsys)[0] in (0, 2)   # a second stop sets, never adds
    today = datetime.date.today().isoformat()
    rows = questions.refused_file(world["p"]).read_text(encoding="utf-8").splitlines()
    assert rows == ["date\tproject\trefused", "%s\t%s\t3" % (today, OWN)]
    questions.add(world["p"], OWN, True)
    assert ledger.main(["report", "--by", "questions"]) == 0
    assert "| %s | 3 |" % today[:7] in capsys.readouterr().out


def test_the_self_test_fails_when_a_planted_refusal_passes(monkeypatch, capsys):
    assert hooks.selftest() == []
    assert hooks.main(["selftest"]) == hooks.OK
    capsys.readouterr()
    monkeypatch.setattr(hooks, "place_class", lambda *a: None)
    problems = hooks.selftest()
    assert len(problems) == 3
    assert hooks.main(["selftest"]) == hooks.ERROR
    assert "FAIL" in capsys.readouterr().out


def test_seal_check_runs_the_self_test_of_the_guard(home, tmp_path, monkeypatch):
    monkeypatch.setattr(seal, "_sudo_allowed", lambda: False)
    what = "the tool guard of the hooks refuses its planted cases"
    assert [r.ok for r in seal.run_checks(home, tmp_path) if r.what == what] == [True]
    monkeypatch.setattr(hooks, "place_class", lambda *a: None)
    assert [r.ok for r in seal.run_checks(home, tmp_path) if r.what == what] == [False]


def test_client_settings_carry_the_six_hooks_with_the_tool_guard():
    """Replaces test_client_settings_carry_the_five_hooks of tests/test_hooks.py: PreToolUse carries the knowledge
    base guard of the write tools and the place guard of every file tool."""
    s = hooks.client_settings("/usr/local/bin/awb hook")
    got = {event: [(e.get("matcher"), h["command"]) for e in entries for h in e["hooks"]]
           for event, entries in s["hooks"].items()}
    assert got == {
        "UserPromptSubmit": [(None, "/usr/local/bin/awb hook prompt")],
        "PreToolUse": [("Write|Edit|MultiEdit|NotebookEdit", "/usr/local/bin/awb hook pre-write"),
                       ("Read|Edit|MultiEdit|NotebookEdit|Write|Glob|Grep|Bash", "/usr/local/bin/awb hook pre-tool")],
        "PostToolUse": [("Write|Edit|MultiEdit|NotebookEdit", "/usr/local/bin/awb hook post-write")],
        "Stop": [(None, "/usr/local/bin/awb hook stop")],
        "SessionStart": [(None, "/usr/local/bin/awb hook session-start")],
    }
    root = Path(hooks.__file__).resolve().parent.parent
    work = json.loads((root / "seal" / "work-claude" / "settings.json").read_text(encoding="utf-8"))
    assert work["hooks"] == s["hooks"] and work["permissions"] == {"deny": ["mcp__*"]}
