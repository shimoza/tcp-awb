"""T1: the rules notice names a file the pre-tool guard lets a session read.

Found in a project session: after a rules change the prompt hook told the session to read ~/.claude/CLAUDE.md again,
and the pre-tool guard refused the Read, because ~/.claude is the client's folder. The notice now names the installed
work rules file (the file the project's CLAUDE.md imports and the seal copies into ~/.claude byte for byte); the
guard is unchanged and still refuses the client's folder to every tool.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from awb import hooks, projects, rulesync
from tests.test_guard import call, run, world  # noqa: F401  (the fixture)
from tests.test_hooks import run_hook
from tests.test_lifecycle import spawn

_READ_RE = re.compile(r"Read (\S+) \(the Workbench rules\)")


def named_file(text: str) -> Path:
    m = _READ_RE.search(text)
    assert m, "the notice names no rules file to read"
    return Path(m.group(1))


def test_the_notice_names_the_installed_rules_file_the_project_imports(home, register_path):
    path = named_file(rulesync.NOTICE)
    assert path == projects.RULES_FILE and path.is_absolute() and path.is_file()
    assert "~/.claude" not in rulesync.NOTICE and "/.claude/" not in str(path.relative_to(projects.REPO))
    pr = spawn(home, register_path)
    imports = [line[1:] for line in (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("@")]
    assert [Path(i.replace("~", str(Path.home()), 1)) if i.startswith("~/") else Path(i) for i in imports] == [path]


def test_the_guard_lets_a_session_read_the_file_the_notice_names(world, monkeypatch, capsys):  # noqa: F811
    path = named_file(rulesync.NOTICE)
    assert run("pre-tool", call(world, "Read", file_path=str(path)), monkeypatch, capsys) == (0, "", "")
    assert run("pre-tool", call(world, "Bash", command="cat %s" % path), monkeypatch, capsys) == (0, "", "")


def test_the_notice_of_the_prompt_hook_names_a_file_the_guard_passes(world, home, register_path, monkeypatch,
                                                                      capsys):  # noqa: F811
    pr = spawn(home, register_path)
    payload = {"cwd": pr.path, "session_id": "t1-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    with open(Path(pr.path) / "CLAUDE.md", "a", encoding="utf-8") as fh:
        fh.write("- one more rule of this project\n")
    code, out, _ = run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys)
    said = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0
    read = {"tool_name": "Read", "tool_input": {"file_path": str(named_file(said))}, "cwd": pr.path,
            "session_id": "t1-session"}
    assert run("pre-tool", read, monkeypatch, capsys) == (0, "", "")


def test_the_clients_copy_and_its_neighbours_stay_refused_to_every_tool(world, monkeypatch, capsys):  # noqa: F811
    client = world["user"] / ".claude"
    (client / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    (client / "settings.json").write_text("{}\n", encoding="utf-8")
    calls = [("Read", {"file_path": str(client / "CLAUDE.md")}),
             ("Read", {"file_path": "~/.claude/CLAUDE.md"}),
             ("Write", {"file_path": str(client / "CLAUDE.md"), "content": "x"}),
             ("Edit", {"file_path": str(client / "CLAUDE.md"), "old_string": "rules", "new_string": "x"}),
             ("Read", {"file_path": str(client / "settings.json")}),
             ("Edit", {"file_path": str(client / "settings.json"), "old_string": "{}", "new_string": "[]"}),
             ("Bash", {"command": "cat ~/.claude/CLAUDE.md"}),
             ("Grep", {"pattern": "rules", "path": str(client)})]
    for tool, tool_input in calls:
        code, _, err = run("pre-tool", call(world, tool, **tool_input), monkeypatch, capsys)
        assert code == hooks.BLOCK and err.strip() == hooks.refusal(hooks.CLIENT_FOLDER), (tool, tool_input)
