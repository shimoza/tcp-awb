"""PD2 and PD3 of 2026-10-22: the tool guard passes text that is no path and runs the Workbench's own workflows.

PD2: a sed on RESOURCES.md, a call that opens a console URL and a here-document script were refused inside the own
project, because a / of text (a sed or awk program, a quoted sentence, a division, a here-document body) was taken
for the root, which holds every project, and a path in a folder the session cannot read failed the project lookup.
Text is now refused only when it names a place inside a refused one; a real path stays as strict as before.

PD3: the work user's settings no longer deny Workflow. The pre-tool hook takes the Workflow tool and passes only the
Workbench's own saved workflows (review.js, request-check.js) by their scriptPath; their agents run in the session's
client under the same settings and hooks. Invented codes only (tests/fixtures.py); no real name anywhere.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

from awb import hooks, toolguard

OWN = "tcp-q7m4"
OTHER = "tcp-z3k5"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(home, monkeypatch):
    """The sealed layout: the projects live in the work user's home next to .ssh, .config and .claude."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = home.projects_root
    monkeypatch.setenv("HOME", str(user))
    for folder in (".ssh", ".config", ".claude"):
        (user / folder).mkdir(parents=True)
    own, other = user / OWN, user / OTHER
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
        (root / "RESOURCES.md").write_text("| ecs | 10.0.0.1 |\n", encoding="utf-8")
    return {"p": home, "own": own, "other": other, "user": user}


def call(w, tool: str, cwd: Path | None = None, sealed: bool = True, **tool_input) -> str | None:
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or w["own"]), "session_id": "s-1"}
    return hooks.guard(payload, w["p"], w["user"], sealed=sealed)


def bash(w, command: str, cwd: Path | None = None) -> str | None:
    return call(w, "Bash", cwd, command=command)


# --------------------------------------------------------------------------- PD2: the reported cases pass


SED_CASES = (
    "sed -i 's/10.0.0.1/10.0.0.2/' RESOURCES.md",
    "sed -i 's/^| ecs | .* |$/| ecs | 10.0.0.2 \\/ 24 |/' RESOURCES.md",
    "sed -i 's/a/b/;s/ / /g' RESOURCES.md",
    "sed -i -e 's/x/ /' -e '/^## Old/,/^## /d' RESOURCES.md",
    "sed -n '/^| ecs/p' {own}/RESOURCES.md",
    "awk -F/ '{{print $1}}' RESOURCES.md",
    "awk '{{ print $2 / 1024 }}' RESOURCES.md | cut -d/ -f1",
    "echo $(( 10 / 2 ))",
)


def test_a_sed_or_awk_on_resources_md_of_the_own_project_passes(world):
    for command in SED_CASES:
        assert bash(world, command.format(own=world["own"])) is None, command


CONSOLE = ("python3 -c \"import webbrowser; webbrowser.open('https://console.example.com/novnc/vnc_auto.html"
           "?token=t0ken&lang=EN&host=192.0.2.10&port=6080&path=websockify')\" && "
           "curl -s -o novnc.html 'https://console.example.com/novnc/vnc_auto.html?token=t0ken&lang=EN' && "
           "echo 'console: https://console.example.com/ / port 6080 / see RESOURCES.md' >> RESOURCES.md")


def test_a_call_that_opens_a_console_url_passes(world):
    assert bash(world, CONSOLE) is None


def test_a_remote_path_in_a_folder_the_session_cannot_read_passes(world, tmp_path):
    """The noVNC case: an ssh line names a path of the remote machine that lies, on this host, in a folder of
    another user. The project lookup of that path failed and refused the call as an unknown project."""
    locked = tmp_path / "locked"
    (locked / "vnc").mkdir(parents=True)
    os.chmod(locked, 0)
    try:
        command = "ssh -i key.pem admin@192.0.2.10 'cat %s/vnc/passwd' > vnc-check.txt" % locked
        assert bash(world, command) is None
    finally:
        os.chmod(locked, 0o700)


HEREDOCS = (
    "python3 - <<'PY'\nimport json\nrows = open('RESOURCES.md').read().split(' / ')\nprint(len(rows) / 2)\n"
    "print('a / b', '~ 5 GB', 'see /')\nPY",
    "cat > notes/console.md <<'EOF'\n# Console\nOpen https://console.example.com/ and use / as the root.\n"
    "Cost ~ 12 EUR / month.\nEOF",
    "cat > run.sh <<'EOF'\nset -e\ncp \"$SRC/a\" \"$DST/b\"\necho \"${HOME}/x\"\nEOF",
    "grep -c ecs <<< 'a / b'",
)


def test_a_here_document_script_of_the_own_project_passes(world):
    (world["own"] / "notes").mkdir()
    for command in HEREDOCS:
        assert bash(world, command) is None, command


def test_the_project_is_the_first_folder_when_a_cd_left_it(world, tmp_path):
    """Without CLAUDE_PROJECT_DIR and with a session folder outside the project, the project is the first folder of
    the session, read from the name of its transcript folder."""
    outside = tmp_path / "scratch"
    outside.mkdir()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(world["own"]))
    transcript = world["user"] / ".claude" / "projects" / encoded / "s-1.jsonl"

    def guard(tool_input: dict, tool: str = "Read") -> str | None:
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(outside), "session_id": "s-1",
                   "transcript_path": str(transcript)}
        return hooks.guard(payload, world["p"], world["user"], sealed=True)

    assert guard({"file_path": str(world["own"] / "RESOURCES.md")}) is None
    assert guard({"file_path": str(world["other"] / "RESOURCES.md")}) == hooks.ANOTHER_PROJECT
    assert guard({"command": "sed -i 's/a/ /' %s/RESOURCES.md" % world["own"]}, "Bash") is None
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(world["own"] / "RESOURCES.md")},
               "cwd": str(outside), "session_id": "s-1"}
    assert hooks.guard(payload, world["p"], world["user"], sealed=True) == hooks.UNKNOWN_PROJECT


# --------------------------------------------------------------------------- PD2: real paths stay refused


def test_the_same_commands_naming_a_file_of_another_project_are_refused(world):
    other = world["other"]
    for command in SED_CASES[:5]:
        refused = command.replace("RESOURCES.md", "%s/RESOURCES.md" % other).format(own=other)
        assert bash(world, refused) == hooks.ANOTHER_PROJECT, refused
    assert bash(world, "sed -i 's|10.0.0.1|%s/RESOURCES.md|w %s/x' RESOURCES.md" % (other, other)) \
        == hooks.ANOTHER_PROJECT
    assert bash(world, "awk -f %s/prog.awk RESOURCES.md" % other) == hooks.ANOTHER_PROJECT
    assert bash(world, CONSOLE.replace("-o novnc.html", "-o %s/novnc.html" % other)) == hooks.ANOTHER_PROJECT
    assert bash(world, CONSOLE.replace(">> RESOURCES.md", ">> %s/RESOURCES.md" % other)) == hooks.ANOTHER_PROJECT
    assert bash(world, "echo $(( $(wc -l < %s/RESOURCES.md) / 2 ))" % other) == hooks.ANOTHER_PROJECT


def test_a_here_document_that_writes_a_file_of_another_project_is_refused(world):
    other = world["other"]
    for command in ("cat > %s/RESOURCES.md <<'EOF'\n| ecs | 10.0.0.9 |\nEOF" % other,
                    "python3 - <<'PY'\nopen('%s/RESOURCES.md', 'w').write('x / y')\nPY" % other,
                    "tee -a notes.md <<EOF\n$(cat %s/RESOURCES.md)\nEOF" % other,
                    "bash <<'EOF'\ncd ..\ncat %s/RESOURCES.md > copy.md\nEOF" % OTHER,
                    "grep ecs <<< %s/RESOURCES.md" % other):
        assert bash(world, command) == hooks.ANOTHER_PROJECT, command


def test_a_search_from_the_root_or_the_home_stays_refused(world):
    """A / or the home as a path argument of its own still holds every project; only a / of text passes."""
    user = world["user"]
    for command in ("ls /", "find / -name RESOURCES.md", "grep -r ecs %s" % user, "du -sh ~",
                    "bash <<'EOF'\nls /\nEOF", "cat > x <<EOF\n$(ls %s)\nEOF" % user,
                    "tar -xf a.tar -C %s" % user, "ls '%s'" % user):
        assert bash(world, command) == hooks.ANOTHER_PROJECT, command
    assert bash(world, "python3 - <<'PY'\nprint(open('%s/.ssh/id_ed25519').read())\nPY" % user) == hooks.KEY_FOLDER


# --------------------------------------------------------------------------- PD3: the Workflow tool


def test_the_workbench_workflows_run_by_their_script_path(world):
    for name in ("review.js", "request-check.js"):
        path = toolguard.WORKFLOW_DIR / name
        assert path.is_file()
        assert call(world, "Workflow", scriptPath=str(path), args={"project": str(world["own"])}) is None
        assert call(world, "Workflow", scriptPath=str(path), resumeFromRunId="r-1") is None


def test_an_inline_script_a_name_or_another_file_is_refused(world):
    copy = world["own"] / "review.js"
    copy.write_text((toolguard.WORKFLOW_DIR / "review.js").read_text(encoding="utf-8"), encoding="utf-8")
    (world["other"] / "x.js").write_text("export const meta = {name: 'x', description: 'x'}\n", encoding="utf-8")
    script = "export const meta = {name: 'x', description: 'x'}\nawait workflow({scriptPath: '%s/x.js'})" \
        % world["other"]
    for tool_input in ({"script": script}, {"name": "awb-review"}, {"scriptPath": str(copy)},
                       {"scriptPath": str(world["other"] / "x.js")}, {},
                       {"scriptPath": str(toolguard.WORKFLOW_DIR / "review.js"), "script": script}):
        assert call(world, "Workflow", **tool_input) == toolguard.FOREIGN_WORKFLOW, tool_input
    assert call(world, "Workflow", sealed=False, script=script) is None   # the plugin on an architect's machine


def test_the_hook_refuses_a_foreign_workflow_with_one_line(world, monkeypatch, capsys):
    payload = {"tool_name": "Workflow", "tool_input": {"script": "export const meta = {name: 'x'}"},
               "cwd": str(world["own"]), "session_id": "s-1"}
    monkeypatch.setattr(toolguard, "_sealed", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(["pre-tool"]) == hooks.BLOCK
    err = capsys.readouterr().err.strip()
    assert err == hooks.refusal(toolguard.FOREIGN_WORKFLOW) and len(err.splitlines()) == 1
    line = hooks.guard_log(world["p"]).read_text(encoding="utf-8").splitlines()[-1]
    assert line.split("\t")[2:] == ["Workflow", toolguard.FOREIGN_WORKFLOW, OWN]


def test_the_build_guard_refuses_workflow():
    assert toolguard.build_guard({"tool_name": "Workflow", "cwd": "/nowhere", "tool_input": {}}) == \
        toolguard.NO_CLONE


BEFORE_PD3 = ("mcp__*", "PowerShell", "LSP", "NotebookRead", "Artifact", "ArtifactComments", "ArtifactData",
              "DesignSync", "SendUserFile", "EnterWorktree", "ExitWorktree", "Workflow", "RemoteTrigger",
              "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool")


def test_the_generated_settings_allow_workflow_and_still_deny_what_they_denied():
    work = json.loads((ROOT / "seal" / "work-claude" / "settings.json").read_text(encoding="utf-8"))
    deny = work["permissions"]["deny"]
    assert "Workflow" not in deny
    assert deny == [t for t in BEFORE_PD3 if t != "Workflow"]
    for name in ("seal/work-claude/settings.json", "seal/work-claude/managed-settings.json",
                 "plugin/hooks/hooks.json", "seal/build-claude/settings.json"):
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        guarded = [e["matcher"] for e in data["hooks"]["PreToolUse"]
                   if any(h["command"].endswith("hook pre-tool") for h in e["hooks"])]
        assert len(guarded) == 1 and re.fullmatch(guarded[0], "Workflow"), name
        assert re.fullmatch(guarded[0], "Bash") and re.fullmatch(guarded[0], "Read"), name
    build = json.loads((ROOT / "seal" / "build-claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(BEFORE_PD3) <= set(build["permissions"]["deny"])
    managed = json.loads((ROOT / "seal" / "work-claude" / "managed-settings.json").read_text(encoding="utf-8"))
    assert "permissions" not in managed
