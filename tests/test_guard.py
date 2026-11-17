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


def test_client_settings_guard_the_workflow_tool_and_the_work_user_denies_the_rest_but_workflow():
    """Replaces test_client_settings_guard_every_file_and_shell_tool_and_the_work_user_denies_the_rest (PD3,
    2026-10-22): the place guard takes Workflow next to every file and shell tool, and the work user's settings
    still deny every tool they denied but Workflow, which the guard limits to the Workbench's own workflows."""
    s = hooks.client_settings("/usr/local/bin/awb hook")
    got = {event: [(e.get("matcher"), h["command"]) for e in entries for h in e["hooks"]]
           for event, entries in s["hooks"].items()}
    assert got == {
        "UserPromptSubmit": [(None, "/usr/local/bin/awb hook prompt")],
        "PreToolUse": [("Write|Edit|MultiEdit|NotebookEdit", "/usr/local/bin/awb hook pre-write"),
                       ("Read|Edit|MultiEdit|NotebookEdit|Write|Glob|Grep|Bash|Monitor|Workflow",
                        "/usr/local/bin/awb hook pre-tool")],
        "PostToolUse": [("Write|Edit|MultiEdit|NotebookEdit", "/usr/local/bin/awb hook post-write")],
        "Stop": [(None, "/usr/local/bin/awb hook stop")],
        "SessionStart": [(None, "/usr/local/bin/awb hook session-start")],
    }
    assert set(hooks.SHELL_TOOLS) <= set(hooks.GUARD_TOOLS) and "Workflow" in hooks.GUARD_TOOLS
    assert not set(hooks.GUARD_TOOLS) & set(hooks.DENIED_TOOLS)
    assert hooks.DENIED_TOOLS == ("mcp__*", "PowerShell", "LSP", "NotebookRead", "Artifact", "ArtifactComments",
                                  "ArtifactData", "DesignSync", "SendUserFile", "EnterWorktree", "ExitWorktree",
                                  "RemoteTrigger", "ListMcpResourcesTool", "ReadMcpResourceTool",
                                  "ReadMcpResourceDirTool")
    root = Path(hooks.__file__).resolve().parent.parent
    work = json.loads((root / "seal" / "work-claude" / "settings.json").read_text(encoding="utf-8"))
    assert work["hooks"] == s["hooks"] and work["permissions"] == {"deny": list(hooks.DENIED_TOOLS)}
    managed = json.loads((root / "seal" / "work-claude" / "managed-settings.json").read_text(encoding="utf-8"))
    assert managed == {"hooks": s["hooks"]}
    plugin = json.loads((root / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert [e.get("matcher") for e in plugin["hooks"]["PreToolUse"]] == [hooks.WRITE_MATCHER, hooks.GUARD_MATCHER]
    build = json.loads((root / "seal" / "build-claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Workflow" in build["permissions"]["deny"]


# Hardening cases use only temporary layouts and never start the awb CLI.
import os
from concurrent.futures import ThreadPoolExecutor

from awb import toolguard as tg


def guarded(world, tool="Read", **tool_input):
    return tg.guard(call(world, tool, **tool_input), world["p"], world["user"], sealed=True)


def test_guard_malformed_envelopes_fail_closed(world):
    for payload in (None, [], "value", 1, {}, {"tool_name": []}, {"tool_name": 1}):
        assert tg.guard(payload, world["p"], world["user"], sealed=True) == tg.UNKNOWN_PROJECT
        assert tg.build_guard(payload) == tg.NO_CLONE
    assert tg.guard({"tool_name": "TodoWrite"}) is None


def test_guard_invalid_text_cannot_pass(world):
    for value in ("echo \x00", "echo \ud800"):
        assert guarded(world, "Bash", command=value) is not None
    for value in ("a\x00b", "a\ud800b"):
        assert guarded(world, file_path=value) is not None


def test_guard_parameter_expansion_cannot_hide_a_command(world):
    for command in ("echo ${x:-$(cat %s/STATE.md)}", "echo ${x:-`cat %s/STATE.md`}"):
        assert guarded(world, "Bash", command=command % world["other"]) is not None
    assert guarded(world, "Bash", command="echo ${HOME}/notes") is None


def test_guard_quoted_cd_tracks_the_directory(world):
    for command in ("c''d; cat .ssh/id_ed25519", "c\\d; cat .ssh/id_ed25519"):
        assert guarded(world, "Bash", command=command) is not None


def test_guard_relative_paths_with_spaces_are_checked_whole(world):
    (world["own"] / "private notes").symlink_to(world["other"])
    assert guarded(world, "Bash", command="cat 'private notes/STATE.md'") == tg.ANOTHER_PROJECT


def test_guard_shell_size_is_bounded_before_parsing(world, monkeypatch):
    def parsed(*args):
        pytest.fail("oversized command reached the parser")
    monkeypatch.setattr(tg._ShellReader, "read", parsed)
    assert guarded(world, "Bash", command="x" * 1_000_000) is not None


def test_guard_branching_directory_changes_have_a_budget(world):
    with pytest.raises(tg._Refused):
        tg._shell_checks("; ".join("cd d%d || true" % i for i in range(12)), world["own"], world["user"])


def test_guard_linked_scope_cannot_establish_a_project(world):
    scope = world["own"] / "SCOPE.md"
    scope.unlink()
    scope.symlink_to(world["other"] / "SCOPE.md")
    assert guarded(world, file_path="STATE.md") == tg.UNKNOWN_PROJECT


def test_guard_transcript_hint_cannot_select_a_linked_project(world, tmp_path):
    fake = world["p"].projects_root / "tcp-abcd"
    fake.symlink_to(world["other"])
    payload = call(world, "Read", file_path=str(fake / "STATE.md"))
    payload["cwd"] = str(tmp_path)
    payload["transcript_path"] = str(world["user"] / ".claude" / "projects" /
                                     tg._ENCODE_RE.sub("-", str(fake)) / "s-1.jsonl")
    assert tg.guard(payload, world["p"], world["user"], sealed=True) == tg.UNKNOWN_PROJECT


def test_guard_resolution_errors_fail_closed_but_missing_targets_pass(world, monkeypatch):
    assert guarded(world, "Write", file_path="new/future.txt", content="x") is None
    real_lstat = os.lstat
    target = world["own"] / "closed"
    def unreadable(path, *args, **kwargs):
        if str(path) == str(target):
            raise PermissionError("pass" + "word")
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(os, "lstat", unreadable)
    assert guarded(world, file_path=str(target / "x")) is not None


def test_guard_symlink_loops_fail_closed(world):
    (world["own"] / "loop").symlink_to("loop")
    assert guarded(world, file_path="loop/x") is not None


def test_guard_seal_errors_do_not_unseal(tmp_path, monkeypatch):
    conf = tmp_path / "host"
    monkeypatch.setattr(tg, "HOST_FILE", conf)
    assert tg._sealed() is False
    conf.write_bytes(b"\xff")
    assert tg._sealed() is True
    conf.unlink()
    conf.mkdir()
    assert tg._sealed() is True


def test_guard_seal_read_is_bounded(tmp_path, monkeypatch):
    conf = tmp_path / "host"
    conf.write_bytes(b"#" * 1_000_000)
    monkeypatch.setattr(tg, "HOST_FILE", conf)
    assert tg._sealed() is True


def test_guard_special_metadata_is_not_read(tmp_path, monkeypatch):
    conf = tmp_path / "host"
    os.mkfifo(conf)
    monkeypatch.setattr(tg, "HOST_FILE", conf)
    original = Path.read_text
    def no_fifo_read(path, *args, **kwargs):
        if path == conf:
            pytest.fail("attempted a blocking FIFO read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_fifo_read)
    assert tg._sealed() is True


def test_guard_workflows_require_plain_installed_files(tmp_path):
    folder = tmp_path / "workflows"
    folder.mkdir()
    target = tmp_path / "outside.js"
    target.write_text("export {}", encoding="utf-8")
    script = folder / tg.WORKFLOW_FILES[0]
    script.symlink_to(target)
    assert tg._workflow_class({"scriptPath": str(script)}, tmp_path, folder) == tg.FOREIGN_WORKFLOW
    script.unlink()
    script.write_text("export {}", encoding="utf-8")
    assert tg._workflow_class({"scriptPath": str(script)}, tmp_path, folder) is None


def test_guard_venv_config_cannot_authorize_a_link(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    target = tmp_path / "external"
    target.write_text("executable = /outside/python\n", encoding="utf-8")
    (venv / "pyvenv.cfg").symlink_to(target)
    assert tg._venv_base(venv) == set()
    (venv / "pyvenv.cfg").unlink()
    assert tg._venv_base(venv) == set()


def test_guard_refusal_diagnostics_withhold_values(tmp_path, monkeypatch):
    secret = "pass" + "word"
    private = tmp_path / ("Private" + "Person") / secret
    monkeypatch.setattr(tg.config, "build_root", lambda: private)
    monkeypatch.setattr(tg, "WORKFLOW_DIR", private)
    for line in (tg.refusal(secret + "\nnext"), tg.build_refusal(secret + "\nnext"),
                 tg.refusal(tg.FOREIGN_WORKFLOW), tg.build_refusal(tg.NO_CLONE)):
        assert secret not in line and str(private) not in line
        assert len(line.splitlines()) == 1


def test_guard_logging_errors_cannot_interrupt_a_refusal(world, monkeypatch, capsys):
    secret = "pass" + "word"
    monkeypatch.setattr(tg, "guard", lambda data: tg.ANOTHER_PROJECT)
    def broken(*args, **kwargs):
        raise ValueError(secret)
    monkeypatch.setattr(tg, "_own_project", broken)
    assert tg.hook_pre_tool(call(world, "Read", file_path="x")) == tg.BLOCK
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip() == tg.refusal(tg.ANOTHER_PROJECT)
    assert secret not in output.err


def test_guard_log_fields_are_allowlisted(world):
    secret = "pass" + "word"
    tg._log_refusal(world["p"], {"session_id": "s-1"}, secret + "\n", secret, world["own"])
    text = tg.guard_log(world["p"]).read_text()
    assert secret not in text and len(text.splitlines()) == 1
    assert len(text.rstrip("\n").split("\t")) == 5


def test_guard_private_session_identifiers_are_not_logged(world):
    for value in ("pass" + "word", "Private" + "Person"):
        tg._log_refusal(world["p"], {"session_id": value}, "Read", tg.KEY_FOLDER, world["own"])
        assert value not in tg.guard_log(world["p"]).read_text()


def test_guard_log_refuses_links_and_hardlinks(world, tmp_path):
    log = tg.guard_log(world["p"])
    log.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim"
    victim.write_text("unchanged\n")
    for link in (lambda: log.symlink_to(victim), lambda: os.link(victim, log)):
        link()
        tg._log_refusal(world["p"], {}, "Read", tg.KEY_FOLDER, world["own"])
        assert victim.read_text() == "unchanged\n"
        log.unlink()
    log.parent.rmdir()
    log.parent.symlink_to(tmp_path)
    tg._log_refusal(world["p"], {}, "Read", tg.KEY_FOLDER, world["own"])
    assert not (tmp_path / "guard.log").exists()


def test_guard_log_parent_swap_cannot_redirect_append(world, tmp_path, monkeypatch):
    log = tg.guard_log(world["p"])
    log.parent.mkdir(parents=True, exist_ok=True)
    saved = log.parent.with_name("saved-sessions")
    target = tmp_path / "elsewhere"
    target.mkdir()
    original = os.open
    swapped = False
    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path).endswith("guard.log") and not swapped:
            swapped = True
            log.parent.rename(saved)
            log.parent.symlink_to(target)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    tg._log_refusal(world["p"], {}, "Read", tg.KEY_FOLDER, world["own"])
    assert swapped and not (target / "guard.log").exists()


def test_guard_concurrent_short_appends_preserve_complete_rows(world, monkeypatch):
    original = os.write
    def short(fd, data):
        return original(fd, data[:7])
    monkeypatch.setattr(os, "write", short)
    def append(n):
        tg._log_refusal(world["p"], {"session_id": "s-%d" % n}, "Read", tg.KEY_FOLDER, world["own"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(32)))
    rows = tg.guard_log(world["p"]).read_text().splitlines()
    assert len(rows) == 32
    assert {row.split("\t")[1] for row in rows} == {"s-%d" % n for n in range(32)}
    assert all(row.split("\t")[2:] == ["Read", tg.KEY_FOLDER, OWN] for row in rows)
    assert tg.refusals(world["p"], OWN) == 32


def test_guard_failed_append_rolls_back_partial_row(world, monkeypatch):
    tg._log_refusal(world["p"], {}, "Read", tg.KEY_FOLDER, world["own"])
    log = tg.guard_log(world["p"])
    before = log.read_bytes()
    original = os.write
    calls = 0
    def fail(fd, data):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("pass" + "word")
        return original(fd, data[:5])
    monkeypatch.setattr(os, "write", fail)
    tg._log_refusal(world["p"], {}, "Read", tg.KEY_FOLDER, world["own"])
    assert log.read_bytes() == before


def test_guard_counts_ignore_forged_and_incomplete_rows(world):
    log = tg.guard_log(world["p"])
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat() + "T12:00:00Z"
    valid = "%s\ts-1\tRead\t%s\t%s\n" % (stamp, tg.KEY_FOLDER, OWN)
    log.write_text(valid + valid.replace("T12:00:00Z", "garbage") + valid.replace("Read", "unknown-tool") +
                   valid.replace(tg.KEY_FOLDER, "unknown-class") + valid.rstrip("\n"))
    assert tg.refusals(world["p"], OWN) == 1


def test_guard_counts_refuse_linked_and_oversized_logs(world, tmp_path):
    log = tg.guard_log(world["p"])
    log.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external"
    stamp = datetime.date.today().isoformat() + "T12:00:00Z"
    row = "%s\ts-1\tRead\t%s\t%s\n" % (stamp, tg.KEY_FOLDER, OWN)
    external.write_text(row)
    log.symlink_to(external)
    assert tg.refusals(world["p"], OWN) == 0
    log.unlink()
    with log.open("wb") as stream:
        stream.write(row.encode())
        stream.truncate(64 * 1024 * 1024)
    assert tg.refusals(world["p"], OWN) == 0
