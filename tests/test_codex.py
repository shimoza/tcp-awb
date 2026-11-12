"""CX: `awb codex usage`, `run` and `status` against a fake codex executable on PATH (never the real CLI).

The fake answers the JSON-RPC of `codex app-server` and fakes `codex exec` by writing the files named in
FAKE_CODEX_WRITE into its working folder. The state folder is temporary (AWB_CODEX_STATE).
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import cli
from tests import fixtures

FAKE = '''#!%s
import json, os, sys, time
if sys.argv[1] == "app-server":
    if os.environ.get("FAKE_CODEX_MODE") == "silent":
        time.sleep(60)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "method": "note/ignored", "params": {}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            assert msg["params"] == {}
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "accountId": "acct-0000-secret", "email": "someone@example.invalid",
                "rateLimits": {
                    "planType": "pro",
                    "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1794430920},
                    "secondary": {"usedPercent": 53, "windowDurationMins": 10080, "resetsAt": 1794800000},
                    "rateLimitResetCredits": {"credits": [
                        {"status": "available", "expiresAt": 1795000000},
                        {"status": "available", "expiresAt": 1794900000},
                        {"status": "used", "expiresAt": 1794000000}]}}}}), flush=True)
else:
    assert sys.argv[1] == "exec" and "workspace-write" in sys.argv
    task = sys.stdin.read()
    assert "Paths:" in task
    for name in filter(None, os.environ.get("FAKE_CODEX_WRITE", "").split(",")):
        os.makedirs(os.path.dirname(name) or ".", exist_ok=True)
        open(name, "w").write("changed\\n")
    print("exec done")
''' % sys.executable

TASK = """Codex task %s: a small helper

Paths: pkg/helper.py, tests/test_helper.py
Acceptance: the test passes

%s
"""


@pytest.fixture
def bench(tmp_path, monkeypatch, home):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "codex").write_text(FAKE, encoding="utf-8")
    (bindir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("AWB_CODEX_STATE", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "start"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    return tmp_path


def _task(bench: Path, job_id: str = "cx-test-1", text: str = "Add a helper.", body: str | None = None) -> Path:
    path = bench / ("%s.txt" % job_id)
    path.write_text(body if body is not None else TASK % (job_id, text), encoding="utf-8")
    return path


def _branch_commits(job_id: str) -> str:
    return subprocess.run(["git", "rev-list", "--count", "main..codex/%s" % job_id], capture_output=True,
                          text=True, check=True).stdout.strip()


# --------------------------------------------------------------------------- usage


def test_usage_prints_both_windows_plan_and_credits_and_no_account_data(bench, capsys):
    assert cli.main(["codex", "usage"]) == 0
    out = capsys.readouterr().out
    assert "5h: 7% used, resets 2026-11-" in out and "UTC" in out
    assert "week: 53% used, resets" in out
    assert "plan: pro" in out
    assert "reset credits: 2 available, earliest expiry" in out
    for secret in ("acct-0000-secret", "example.invalid", "someone"):
        assert secret not in out


def test_usage_json_has_no_account_data(bench, capsys):
    assert cli.main(["codex", "usage", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"used_percent": 53' in out
    assert "acct-0000" not in out and "example.invalid" not in out


def test_usage_without_codex_exits_2(bench, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(bench / "empty"))
    assert cli.main(["codex", "usage"]) == 2
    assert capsys.readouterr().err.count("\n") == 1


def test_usage_with_a_silent_codex_exits_2_within_the_deadline(bench, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_MODE", "silent")
    monkeypatch.setenv("AWB_CODEX_DEADLINE", "1")
    start = time.monotonic()
    assert cli.main(["codex", "usage"]) == 2
    assert time.monotonic() - start < 10
    assert capsys.readouterr().err.count("\n") == 1


# --------------------------------------------------------------------------- run


def test_run_refuses_a_task_without_paths_or_acceptance(bench, capsys):
    no_paths = _task(bench, "cx-nopaths", body="Codex task cx-nopaths: x\nAcceptance: ok\n")
    assert cli.main(["codex", "run", str(no_paths)]) == 1
    assert "Paths:" in capsys.readouterr().err
    no_acc = _task(bench, "cx-noacc", body="Codex task cx-noacc: x\nPaths: pkg\n")
    assert cli.main(["codex", "run", str(no_acc)]) == 1
    assert "Acceptance:" in capsys.readouterr().err
    assert not (bench / "state" / "cx-nopaths").exists() and not (bench / "state" / "cx-noacc").exists()


def test_run_refuses_a_blocklisted_name(bench, capsys):
    path = _task(bench, "cx-block", text="Ask CLIENT123 about it.")
    assert cli.main(["codex", "run", str(path)]) == 1
    err = capsys.readouterr().err
    assert "blocklist" in err and "CLIENT123" not in err
    assert not (bench / "state" / "cx-block").exists()


def test_run_refuses_a_registered_name(bench, capsys):
    path = _task(bench, "cx-name", text="Ask %s about it." % fixtures.CUSTOMER_FORMS[0])
    assert cli.main(["codex", "run", str(path)]) == 1
    assert fixtures.CUSTOMER_FORMS[0] not in capsys.readouterr().err


def test_run_makes_worktree_and_branch_writes_result_and_leaves_no_commit(bench, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_WRITE", "pkg/helper.py,tests/test_helper.py")
    assert cli.main(["codex", "run", str(_task(bench))]) == 0
    run = bench / "state" / "cx-test-1"
    assert (run / "wt" / "pkg" / "helper.py").read_text() == "changed\n"
    assert (run / "run.log").read_text().strip() == "exec done"
    result = (run / "result.md").read_text()
    assert "exit: 0" in result and "pkg/helper.py" in result and "none" in result
    assert "REFUSED" not in result
    branches = subprocess.run(["git", "branch", "--list", "codex/cx-test-1"], capture_output=True, text=True).stdout
    assert "codex/cx-test-1" in branches
    assert _branch_commits("cx-test-1") == "0"


def test_a_change_outside_paths_is_listed_as_a_refusal(bench, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_WRITE", "pkg/helper.py,other/stray.py")
    assert cli.main(["codex", "run", str(_task(bench))]) == 1
    result = (bench / "state" / "cx-test-1" / "result.md").read_text()
    assert "REFUSED" in result and "- other/stray.py" in result and "- pkg/helper.py" not in result
    assert "REFUSED" in capsys.readouterr().out


def test_a_second_run_while_the_lock_is_held_is_refused_with_the_open_id(bench, capsys):
    root = bench / "state"
    root.mkdir()
    with open(root / "lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write("cx-open-1\n")
        fh.flush()
        assert cli.main(["codex", "run", str(_task(bench))]) == 1
        assert "cx-open-1" in capsys.readouterr().err
    assert not (root / "cx-test-1").exists()


def test_status_lists_the_runs(bench, capsys):
    assert cli.main(["codex", "run", str(_task(bench))]) == 0
    capsys.readouterr()
    assert cli.main(["codex", "status"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("cx-test-1") and "done" in line and "UTC" in line
