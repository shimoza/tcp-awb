"""Side sessions of a project (SS, 2026-11-13): awb side start|status|take|stop|drop.

A fake client stands in for the headless session: it reads the prompt on stdin, writes SIDE-RESULT.md and a
deliverable in its worktree, commits them and exits."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import cli, deploy, side

CODE = "tcp-q7m4"

FAKE = r"""#!%s
import os, subprocess, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
open("argv.txt", "w").write(" ".join(args) + "\n")
assert "--model" in args and "--effort" in args and "--strict-mcp-config" in args
os.makedirs("deliverables", exist_ok=True)
open("deliverables/report.md", "w").write("the report\n")
open("SIDE-RESULT.md", "w").write("result of the side work\n" + ("brief seen\n" if "the brief text" in prompt else ""))
subprocess.run(["git", "add", "SIDE-RESULT.md", "deliverables/report.md", "argv.txt"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "side"],
               check=True)
print('{"result": "DONE", "num_turns": 3, "is_error": false}')
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / CODE
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# rules\n")
    (root / "STATE.md").write_text("Status: x\nNext: y\n")
    (root / "notes").mkdir()
    (root / "notes" / "brief-mail.md").write_text("the brief text: rework the plan mail\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    fake = tmp_path / "fake-client"
    fake.write_text(FAKE % sys.executable)
    fake.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(fake))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")
    return root


def _wait(root: Path, name: str, want: str = "done", limit: float = 30.0) -> str:
    end = time.time() + limit
    while time.time() < end:
        line = side.status_lines(root)[0]
        if line.split()[1] == want:
            return line
        time.sleep(0.1)
    raise AssertionError(side.status_lines(root))


def test_start_runs_the_brief_in_a_worktree_and_take_copies_the_result(proj):
    msg = side.start("mail", "notes/brief-mail.md", "fable", "high", cwd=proj)
    assert "side session mail started (fable, effort high)" in msg
    line = _wait(proj, "mail")
    assert line.startswith("mail  done  fable/high  1 commit(s)  result: yes")
    wt = proj / ".side" / "mail"
    assert "--model fable --effort high" in (wt / "argv.txt").read_text()
    assert "/.side/" in (proj / ".git" / "info" / "exclude").read_text()
    assert _git(proj, "status", "--porcelain") == ""
    out = side.take("mail", cwd=proj)
    assert out[0] == "result: notes/side-mail.md" and "brief seen" in (proj / "notes" / "side-mail.md").read_text()
    assert "commits: 1" in out and any("deliverables/report.md" in x for x in out)
    assert not (proj / "deliverables" / "report.md").exists() and wt.is_dir()


def test_take_merge_brings_the_commits_and_removes_the_side(proj):
    side.start("rep", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "rep")
    out = side.take("rep", merge=True, cwd=proj)
    assert "merged side/rep" in out and "side session rep removed" in out
    assert (proj / "deliverables" / "report.md").read_text() == "the report\n"
    assert not (proj / ".side" / "rep").exists()
    assert "side/rep" not in _git(proj, "branch", "--list")
    assert side.status_lines(proj) == ["no side sessions"]


def test_start_refuses_outside_a_project_a_bad_name_or_a_brief_outside(proj, tmp_path):
    other = tmp_path / "plain"
    other.mkdir()
    with pytest.raises(side.SideError, match="root of a project folder"):
        side.start("a", "x.md", cwd=other)
    with pytest.raises(side.SideError, match="NAME"):
        side.start("Bad_Name", "notes/brief-mail.md", cwd=proj)
    (tmp_path / "outside.md").write_text("x")
    with pytest.raises(side.SideError, match="file of this project"):
        side.start("a", "../outside.md", cwd=proj)
    with pytest.raises(side.SideError, match="LEVEL"):
        side.start("a", "notes/brief-mail.md", "sonnet", "huge", cwd=proj)
    assert not (proj / ".side").exists()


def test_a_second_start_of_the_same_name_is_refused_and_a_side_cannot_start_a_side(proj):
    side.start("mail", "notes/brief-mail.md", cwd=proj)
    with pytest.raises(side.SideError, match="exists"):
        side.start("mail", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "mail")
    with pytest.raises(side.SideError):
        side.start("deeper", "notes/brief-mail.md", cwd=proj / ".side" / "mail")


def test_take_refuses_a_running_side_and_stop_ends_it(proj, tmp_path, monkeypatch):
    slow = tmp_path / "slow-client"
    slow.write_text("#!%s\nimport time, sys\nsys.stdin.read()\ntime.sleep(60)\n" % sys.executable)
    slow.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(slow))
    side.start("long", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "long", "running")
    with pytest.raises(side.SideError, match="still runs"):
        side.take("long", cwd=proj)
    assert side.stop("long", cwd=proj) == "side session long stopped"
    line = _wait(proj, "long", "failed")
    assert "result: no" in line
    assert side.drop("long", cwd=proj) == "side session long removed"


def test_the_client_runs_headless_without_mcp_and_with_the_chosen_model():
    argv = side.client_argv("/c", "opus", "max")
    assert argv[:2] == ["/c", "-p"] and argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "max" and "--strict-mcp-config" in argv


def test_the_work_rules_tell_a_session_how_to_split_work_off():
    rules = (Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md").read_text(encoding="utf-8")
    line = [ln for ln in rules.splitlines() if "awb side start NAME --brief notes/brief-NAME.md" in ln]
    assert len(line) == 1 and "awb side take NAME" in line[0] and "Never write in .side/" in line[0]


def test_the_command_is_wired_and_documented():
    root = Path(__file__).resolve().parent.parent
    assert "side" in cli.DELEGATED and "awb side" in cli.__doc__
    assert "awb/side.py" in deploy.COMMAND_ONLY
    assert "awb side" in (root / "README.md").read_text(encoding="utf-8")
    assert "### `awb side" in (root / "COMMANDS.md").read_text(encoding="utf-8")



def test_a_refused_start_with_no_turn_is_failed_not_done(proj, tmp_path, monkeypatch):
    refused = tmp_path / "refused-client"
    refused.write_text("#!%s\nimport sys\nsys.stdin.read()\nprint('{\"is_error\": false, \"num_turns\": 0, "
                       "\"result\": \"UserPromptSubmit operation blocked by hook\"}')\n" % sys.executable)
    refused.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(refused))
    side.start("blocked", "notes/brief-mail.md", cwd=proj)
    assert "result: no" in _wait(proj, "blocked", "failed")


def test_the_start_guard_finds_the_commit_gate_of_the_project_from_a_side_worktree(proj, tmp_path):
    from awb import receipt

    side.start("g", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "g")
    wt = proj / ".side" / "g"
    assert receipt._side_git(wt, wt / ".git") == proj / ".git"
    other = tmp_path / "tcp-z9z9"
    (other / ".side" / "x").mkdir(parents=True)
    (other / ".git").mkdir()
    (other / ".side" / "x" / ".git").write_text("gitdir: %s/.git/worktrees/g\n" % proj)
    assert receipt._side_git(other / ".side" / "x", other / ".side" / "x" / ".git") is None
    outside = tmp_path / "loose"
    outside.mkdir()
    (outside / ".git").write_text("gitdir: %s/.git/worktrees/g\n" % proj)
    assert receipt._side_git(outside, outside / ".git") is None


def test_the_worktree_gets_the_current_template_lines_even_when_head_has_old_ones(proj):
    from awb import devnote, rulesync

    (proj / "CLAUDE.md").write_text("# rules\n" + devnote.FILE_RULE + "\n- Proof goes to evidence/, results to "
                                    "deliverables/, review records to reviews/.\n")
    _git(proj, "commit", "-qam", "old rules")
    assert not rulesync.current(proj)
    side.start("rules", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "rules")
    assert rulesync.current(proj / ".side" / "rules")


# Hardening regressions use only temporary projects, including in exported builds.
def _record(root, name="audit"):
    import json

    p = side._paths(root, name)
    p["wt"].mkdir(parents=True, exist_ok=True)
    p["state"].write_text(json.dumps({"pid": 2147483647, "model": "sonnet", "effort": "medium"}))
    p["exit"].write_text("0\n")
    p["out"].write_text('{"num_turns": 1, "is_error": false}')
    return p


def test_brief_read_failures_are_fixed_errors(proj, monkeypatch):
    brief = proj / "notes" / "brief-mail.md"
    for data in (b"\xff", b"x" * (side.MAX_BRIEF + 1)):
        brief.write_bytes(data)
        with pytest.raises(side.SideError):
            side.start("audit", "notes/brief-mail.md", cwd=proj)
    brief.unlink()
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    with pytest.raises(side.SideError):
        side.start("audit", "bad\x00path", cwd=proj)
    assert not (proj / ".side" / "audit").exists()


def test_brief_links_and_special_files_are_refused(proj, tmp_path):
    brief = proj / "notes" / "brief-mail.md"
    brief.unlink()
    brief.symlink_to(proj / "CLAUDE.md")
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    brief.unlink()
    os.mkfifo(brief)
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)


def test_project_and_side_directory_links_are_refused(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / ".side").symlink_to(outside, target_is_directory=True)
    for action in (lambda: side.start("audit", "notes/brief-mail.md", cwd=proj),
                   lambda: side.status_lines(proj), lambda: side.drop("audit", cwd=proj)):
        with pytest.raises(side.SideError):
            action()
    assert list(outside.iterdir()) == []
    (proj / ".side").unlink()
    git = proj / ".git"
    git.rename(tmp_path / "git")
    git.symlink_to(tmp_path / "git", target_is_directory=True)
    with pytest.raises(side.SideError):
        side.project_root(proj)


def test_start_refuses_linked_artifacts_and_exclude(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    base = proj / ".side"
    base.mkdir()
    for suffix in ("state", "exit", "json", "err", "prompt"):
        path = base / ("audit." + suffix)
        path.symlink_to(outside)
        with pytest.raises(side.SideError):
            side.start("audit", "notes/brief-mail.md", cwd=proj)
        path.unlink()
        assert outside.read_text() == "unchanged"
        assert not (base / "audit").exists()
    exclude = proj / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.symlink_to(outside)
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    assert outside.read_text() == "unchanged"


def test_state_shapes_and_pids_never_signal_a_process(proj, monkeypatch):
    import json

    p = _record(proj)
    p["exit"].unlink()
    calls = []
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(args))
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(args))
    for meta in ([], None, "text", {}, {"pid": 0}, {"pid": -1}, {"pid": True},
                 {"pid": "bad"}, {"pid": 10 ** 80}, {"pid": os.getpid()}):
        p["state"].write_text(json.dumps(meta))
        assert " lost " in side.status_lines(proj)[0]
        assert side.stop("audit", cwd=proj).endswith("is lost")
    assert calls == []


def test_invalid_exit_and_client_json_are_failed(proj):
    p = _record(proj)
    for data in (b"\xff", b"x", b"9" * 10000):
        p["exit"].write_bytes(data)
        assert " failed " in side.status_lines(proj)[0]
    p["exit"].unlink()
    p["exit"].mkdir()
    assert " failed " in side.status_lines(proj)[0]
    p["exit"].rmdir()
    p["exit"].write_text("0")
    for raw in ('{"num_turns": -1}', '{"num_turns": true}', '{"num_turns": "yes"}',
                '{"num_turns": 1, "is_error": []}', '[]', '{'):
        p["out"].write_text(raw)
        assert " failed " in side.status_lines(proj)[0]
    p["out"].unlink()
    assert " failed " in side.status_lines(proj)[0]


def test_linked_state_and_exit_are_not_trusted(proj, tmp_path):
    p = _record(proj)
    outside = tmp_path / "outside"
    outside.write_text(p["state"].read_text())
    p["state"].unlink()
    p["state"].symlink_to(outside)
    assert " lost " in side.status_lines(proj)[0]
    p["state"].unlink()
    p = _record(proj)
    outside.write_text("0")
    p["exit"].unlink()
    p["exit"].symlink_to(outside)
    assert " failed " in side.status_lines(proj)[0]


def test_status_withholds_hostile_metadata_and_filenames(proj):
    import json

    p = _record(proj)
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    p["state"].write_text(json.dumps({"model": value, "effort": "high\nforged"}))
    bad = p["base"] / (value + ".state")
    bad.write_text(p["state"].read_text())
    text = "\n".join(side.status_lines(proj))
    assert value not in text and "forged" not in text
    assert len(text.splitlines()) == 1


def test_take_refuses_result_and_destination_links(proj, tmp_path):
    p = _record(proj)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    result = p["wt"] / side.RESULT
    result.symlink_to(outside)
    with pytest.raises(side.SideError):
        side.take("audit", cwd=proj)
    result.unlink()
    result.write_text("safe result")
    target = proj / "notes" / "side-audit.md"
    target.symlink_to(outside)
    with pytest.raises(side.SideError):
        side.take("audit", cwd=proj)
    assert outside.read_text() == "unchanged"
    target.unlink()
    (proj / "notes").rename(proj / "saved-notes")
    (proj / "notes").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(side.SideError):
        side.take("audit", cwd=proj)
    assert not (tmp_path / "side-audit.md").exists()


def test_take_refuses_worktree_link_and_drop_keeps_external_files(proj, tmp_path):
    p = _record(proj)
    p["wt"].rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / side.RESULT).write_text("outside")
    p["wt"].symlink_to(outside, target_is_directory=True)
    for action in (side.take, side.drop):
        with pytest.raises(side.SideError):
            action("audit", cwd=proj)
    assert (outside / side.RESULT).read_text() == "outside"
    assert p["state"].exists()


def test_take_does_not_copy_secrets_or_print_commit_values(proj):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    wt = proj / ".side" / "audit"
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    (wt / side.RESULT).write_text(value)
    with pytest.raises(side.SideError) as err:
        side.take("audit", cwd=proj)
    assert value not in str(err.value)
    assert not (proj / "notes" / "side-audit.md").exists()
    (wt / side.RESULT).write_text("safe result")
    _git(wt, "commit", "--allow-empty", "-qm", value)
    text = "\n".join(side.take("audit", cwd=proj))
    assert value not in text


def test_names_are_checked_in_session_labels_and_git_output(proj, monkeypatch):
    from awb import check

    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    wt = proj / ".side" / "audit"
    value = "client" + "sentinel"
    (wt / value).write_text("safe")
    _git(wt, "add", value)
    _git(wt, "commit", "-qm", value)
    monkeypatch.setattr(check, "check_text", lambda text, reg: [{"cls": "name", "start": 0}]
                        if value in text else [])
    assert value not in "\n".join(side.take("audit", cwd=proj))
    with pytest.raises(side.SideError):
        side.start(value, "notes/brief-mail.md", cwd=proj)
    (proj / ".side" / (value + ".state")).write_text('{}')
    assert value not in "\n".join(side.status_lines(proj))


def test_output_checks_fail_closed_without_echoing_errors(proj, monkeypatch):
    from awb import check

    _record(proj)
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"

    def unavailable(*args, **kwargs):
        raise check.CheckUnavailable(value)

    monkeypatch.setattr(check, "check_text", unavailable)
    assert value not in "\n".join(side.status_lines(proj))
    with pytest.raises(side.SideError) as err:
        side.start("audit2", "notes/brief-mail.md", cwd=proj)
    assert value not in str(err.value)


def test_cli_io_errors_never_echo_values(proj, monkeypatch, capsys):
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    monkeypatch.chdir(proj)

    def denied(*args, **kwargs):
        raise PermissionError(value)

    monkeypatch.setattr(side, "status_lines", denied)
    assert side.main(["status"]) == 2
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_failed_spawn_rolls_back_worktree_and_private_files(proj, monkeypatch):
    def denied(*args, **kwargs):
        raise OSError("cannot spawn")

    # Leave git subprocesses real; only fail the background wrapper's launch.
    popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **kw: denied()
                        if "-I" in argv else popen(argv, *a, **kw))
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    assert not (proj / ".side" / "audit").exists()
    assert not list((proj / ".side").glob("audit.*"))
    assert not _git(proj, "branch", "--list", "side/audit").strip()


def test_side_artifacts_are_private_and_prompt_is_removed(proj):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    base = proj / ".side"
    assert base.stat().st_mode & 0o077 == 0
    assert not (base / "audit.prompt").exists()
    for suffix in ("state", "exit", "json", "err"):
        assert (base / ("audit." + suffix)).stat().st_mode & 0o077 == 0


def test_overlapping_commands_are_serialized(proj, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    original = side._exclude

    def held(root):
        entered.set()
        assert release.wait(10)
        original(root)

    monkeypatch.setattr(side, "_exclude", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(side.start, "audit", "notes/brief-mail.md", cwd=proj)
        try:
            assert entered.wait(10)
            for action in (lambda: side.start("other", "notes/brief-mail.md", cwd=proj),
                           lambda: side.drop("audit", cwd=proj), lambda: side.take("audit", cwd=proj),
                           lambda: side.stop("audit", cwd=proj)):
                with pytest.raises(side.SideError, match="busy"):
                    action()
        finally:
            release.set()
        first.result(timeout=15)
    _wait(proj, "audit")
    assert not (proj / ".side" / "other.state").exists()


def test_failed_worktree_removal_keeps_branch_and_state(proj, monkeypatch):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    original = side._git

    def fail_remove(root, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            if kwargs.get("check", True):
                raise side.SideError("git worktree failed")
            return subprocess.CompletedProcess(args, 1, "", "")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(side, "_git", fail_remove)
    with pytest.raises(side.SideError):
        side.drop("audit", cwd=proj)
    assert (proj / ".side" / "audit.state").exists()
    assert _git(proj, "branch", "--list", "side/audit").strip()


def test_merge_keeps_empty_commits(proj):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    _git(proj, "merge", "--ff-only", "side/audit")
    wt = proj / ".side" / "audit"
    _git(wt, "commit", "--allow-empty", "-qm", "empty")
    commit = _git(wt, "rev-parse", "HEAD").strip()
    side.take("audit", merge=True, cwd=proj)
    assert _git(proj, "merge-base", "--is-ancestor", commit, "HEAD") == ""


def test_receipt_rejects_unregistered_or_redirected_worktrees(proj, tmp_path):
    from awb import receipt

    wt = proj / ".side" / "audit"
    wt.mkdir(parents=True)
    gitfile = wt / ".git"
    admin = proj / ".git" / "worktrees" / "audit"
    gitfile.write_text("gitdir: %s\n" % admin)
    assert receipt._side_git(wt, gitfile) is None
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    (admin / "gitdir").write_text(str(tmp_path / ".git"))
    assert receipt._side_git(wt, gitfile) is None
    (admin / "gitdir").write_text(str(gitfile))
    (admin / "commondir").write_text(str(tmp_path))
    assert receipt._side_git(wt, gitfile) is None


def test_receipt_rejects_linked_gate_files(proj, tmp_path):
    from awb import gate, receipt

    for name in gate.HOOK_NAMES:
        hook = proj / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\n" + gate.HOOK_MARK + "\n")
        hook.chmod(0o755)
    assert receipt._guard_gate(proj)
    hook = proj / ".git" / "hooks" / gate.HOOK_NAMES[0]
    outside = tmp_path / "hook"
    hook.rename(outside)
    hook.symlink_to(outside)
    assert not receipt._guard_gate(proj)


def test_receipt_malformed_hook_entries_fail_closed(monkeypatch, tmp_path):
    import json
    from awb import receipt

    source = tmp_path / "source.json"
    source.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": 1}]}}))
    monkeypatch.setattr(receipt, "MANAGED_SOURCE", source)
    monkeypatch.setattr(receipt, "MANAGED_FILE", source)
    assert receipt._guard_hooks() is False


def test_receipt_release_does_not_print_arbitrary_folder_names(monkeypatch):
    from awb import receipt, vault

    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    monkeypatch.setattr(vault, "release_path", lambda: "/tmp/" + value)
    assert receipt._release() == "unknown release"


@pytest.fixture(autouse=True)
def side_register(home):
    """The output checks use the temporary fixture register, never a host service."""
    return home.register


def test_reads_are_bounded_even_if_brief_grows_after_stat(proj, monkeypatch):
    brief = proj / "notes" / "brief-mail.md"
    brief.write_text("x" * (side.MAX_BRIEF + 1))
    original = Path.stat

    def small(path, *args, **kwargs):
        st = original(path, *args, **kwargs)
        if path == brief:
            fields = list(st)
            fields[6] = 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(Path, "stat", small)
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    assert not (proj / ".side" / "audit").exists()


def test_deep_and_oversized_state_are_lost(proj):
    p = _record(proj)
    for raw in ("[" * 2000 + "0" + "]" * 2000,
                '{"padding":"' + "x" * (1024 * 1024) + '"}'):
        p["state"].write_text(raw)
        assert " lost " in side.status_lines(proj)[0]


def test_cli_parser_never_echoes_quoted_or_control_values(capsys):
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    for args in (["bad'" + value], ["start", "audit", "--brief"], ["status", "--" + value]):
        with pytest.raises(SystemExit) as err:
            side.main(args)
        assert err.value.code == 2
        captured = capsys.readouterr()
        assert value not in captured.out + captured.err


def test_live_process_cannot_be_hidden_by_an_exit_file(proj, tmp_path, monkeypatch):
    slow = tmp_path / "slow-client"
    slow.write_text("#!%s\nimport sys, time\nsys.stdin.read()\ntime.sleep(60)\n" % sys.executable)
    slow.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(slow))
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit", "running")
    (proj / ".side" / "audit.exit").write_text("0")
    (proj / ".side" / "audit.json").write_text('{"num_turns": 1}')
    try:
        with pytest.raises(side.SideError, match="still runs"):
            side.take("audit", cwd=proj)
        with pytest.raises(side.SideError, match="still runs"):
            side.drop("audit", cwd=proj)
    finally:
        import json
        import signal

        pid = json.loads((proj / ".side" / "audit.state").read_text())["pid"]
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_stop_does_not_report_success_when_signal_is_denied(proj, tmp_path, monkeypatch):
    import json
    import signal

    slow = tmp_path / "slow-client"
    slow.write_text("#!%s\nimport sys, time\nsys.stdin.read()\ntime.sleep(60)\n" % sys.executable)
    slow.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(slow))
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit", "running")
    pid = json.loads((proj / ".side" / "audit.state").read_text())["pid"]
    original = os.killpg

    def denied(*args):
        raise PermissionError("signal denied")

    monkeypatch.setattr(os, "killpg", denied)
    try:
        with pytest.raises(side.SideError):
            side.stop("audit", cwd=proj)
        assert not (proj / ".side" / "audit.exit").exists()
    finally:
        original(pid, signal.SIGKILL)


def test_git_filenames_are_checked_whole_and_controls_withheld(proj, monkeypatch):
    from awb import check

    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    wt = proj / ".side" / "audit"
    value = "client" + " sentinel"
    for name in (value, "bad\nforged"):
        (wt / name).write_text("safe")
        _git(wt, "add", name)
    _git(wt, "commit", "-qm", "files")
    monkeypatch.setattr(check, "check_text", lambda text, reg: [{"cls": "name", "start": 0}]
                        if value in text else [])
    output = "\n".join(side.take("audit", cwd=proj))
    assert "client" not in output and "sentinel" not in output and "forged" not in output


def test_unreadable_brief_is_a_fixed_refusal(proj, monkeypatch):
    value = "pass" + "word=" + "N7v2" + "q9Z!k6R8" + "b5C3s1T4"
    original_open, original_path_open = os.open, Path.open

    def denied_os(path, *args, **kwargs):
        if str(path).endswith("brief-mail.md"):
            raise PermissionError(value)
        return original_open(path, *args, **kwargs)

    def denied_path(path, *args, **kwargs):
        if path.name == "brief-mail.md":
            raise PermissionError(value)
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied_os)
    monkeypatch.setattr(Path, "open", denied_path)
    with pytest.raises(side.SideError) as err:
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    assert value not in str(err.value)
    assert not (proj / ".side" / "audit").exists()


def test_partial_worktree_creation_is_rolled_back(proj, monkeypatch):
    original = side._git

    def partial(root, *args, **kwargs):
        result = original(root, *args, **kwargs)
        if args[:2] == ("worktree", "add"):
            raise side.SideError("git worktree failed")
        return result

    monkeypatch.setattr(side, "_git", partial)
    with pytest.raises(side.SideError):
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    assert not (proj / ".side" / "audit").exists()
    assert not _git(proj, "branch", "--list", "side/audit").strip()


def test_failed_history_read_never_drops_unmerged_work(proj, monkeypatch):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    original = side._git

    def denied(root, *args, **kwargs):
        if args[0] == "log":
            if kwargs.get("check", True):
                raise side.SideError("git log failed")
            return subprocess.CompletedProcess(args, 1, "", "")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(side, "_git", denied)
    with pytest.raises(side.SideError):
        side.take("audit", merge=True, cwd=proj)
    assert (proj / ".side" / "audit.state").exists()
    assert (proj / ".side" / "audit").exists()
    assert _git(proj, "branch", "--list", "side/audit").strip()


def test_stop_kills_a_client_that_ignores_termination(proj, tmp_path, monkeypatch):
    import signal

    slow = tmp_path / "slow-client"
    slow.write_text("#!%s\nimport os, signal, sys, time\nsys.stdin.read()\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "open('ready', 'w').write(str(os.getpid()))\ntime.sleep(60)\n" % sys.executable)
    slow.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(slow))
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    ready = proj / ".side" / "audit" / "ready"
    end = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < end:
        time.sleep(0.01)
    pid = int(ready.read_text())
    try:
        side.stop("audit", cwd=proj)
        end = time.monotonic() + 2
        while side._alive(pid) and time.monotonic() < end:
            time.sleep(0.01)
        assert not side._alive(pid)
        assert " failed " in side.status_lines(proj)[0]
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_missing_name_register_withholds_output_and_refuses_start(proj, home):
    _record(proj)
    home.register.unlink()
    assert side.status_lines(proj) == ["no side sessions"]
    with pytest.raises(side.SideError, match="withheld"):
        side.start("other", "notes/brief-mail.md", cwd=proj)
    assert not (proj / ".side" / "other").exists()


def test_git_environment_cannot_redirect_side_work_outside_project(proj, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init", "-q", "-b", "main")
    _git(outside, "commit", "--allow-empty", "-qm", "outside")
    with monkeypatch.context() as env:
        env.setenv("GIT_DIR", str(outside / ".git"))
        env.setenv("GIT_WORK_TREE", str(outside))
        env.setenv("GIT_INDEX_FILE", str(outside / "redirected-index"))
        side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    assert _git(outside, "branch", "--list", "side/audit").strip() == ""
    assert not (outside / "redirected-index").exists()
    assert _git(proj, "rev-list", "--count", "HEAD..side/audit").strip() == "1"


def test_receipt_resolves_parent_components_before_trusting_common_directory(proj, tmp_path):
    from awb import receipt

    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    wt = proj / ".side" / "audit"
    admin = Path((wt / ".git").read_text().strip().removeprefix("gitdir: "))
    outside = tmp_path / "outside" / "child"
    outside.mkdir(parents=True)
    (proj / "redirect").symlink_to(outside, target_is_directory=True)
    (admin / "commondir").write_text(str(proj / "redirect" / ".." / ".git"))
    assert receipt._side_git(wt, wt / ".git") is None


def test_running_sessions_from_before_hardening_remain_protected(proj, tmp_path):
    import json
    import signal

    p = _record(proj)
    p["exit"].unlink()
    prompt = p["base"] / "audit.prompt"
    prompt.write_text("safe task")
    client = tmp_path / "legacy-client"
    client.write_text("#!%s\nimport sys, time\nsys.stdin.read()\ntime.sleep(60)\n" % sys.executable)
    client.chmod(0o755)
    proc = subprocess.Popen([sys.executable, "-I", "-c", side._LEGACY_WRAPPER, str(client),
                             str(p["wt"]), str(p["out"]), str(p["err"]), str(p["exit"]), str(prompt)],
                            cwd=p["wt"], start_new_session=True)
    p["state"].write_text(json.dumps({"pid": proc.pid, "model": "sonnet", "effort": "medium"}))
    try:
        assert " running " in _wait(proj, "audit", "running")
        with pytest.raises(side.SideError, match="still runs"):
            side.drop("audit", cwd=proj)
        assert side.stop("audit", cwd=proj).endswith("stopped")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
