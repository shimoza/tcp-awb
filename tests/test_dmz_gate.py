"""DMZ 3 (B-087, ask 2026-11-25): no gate inside, the public push keeps names and real secrets.

The checks stay at the entry of customer material (DMZ 1) and at the exit: the push of the public repositories, the
runner's push of main and `awb pr take`. A commit inside runs no content check: a project folder (an ask project too),
a build clone, the working copy of the public repository itself (its commits are checked at the push), the runner's
prompt and result lines and the deploy. The exit keeps the registered names, secrets, private keys, tokens, home paths
and the owner's blocklist and drops the identifier class. Every name is an invented fixture name; the keys are put
together at run time.
"""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from awb import build, build_checks, gate
from tests import fixtures as fx
from tests.test_build_runner import seeded, world  # noqa: F401 (the fixtures)
from tests.test_gate import HOOK, git, hook_repo, new_repo, push_git, repo_with_one_commit, zero
from tests.test_hooks import project  # noqa: F401 (the fixture)

NAME = fx.CUSTOMER_FORMS[0]
IP = "203.0.113.45"
UUID = "3f2b8c1e-9a47-4d65-" + "b0e1-7c5a2d9f4e68"
HEX32 = "4f1c9a7e2b6d" + "40f8a3c5e9b1d7f2a6c0"
TENANT = "tenant " + "123456"
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRI" + "VATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END " + "OPENSSH PRI" + "VATE KEY-----\n"
TOKEN = "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
HOME_PATH = "/ho" + "me/builder/notes.txt"
PASSWORD_LINE = "db_pass" + "word = " + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
CO_AUTHOR = "Co-Authored-By: Clau" + "de <noreply@anthropic.com>"
DATA = "offer for %s on %s, vm %s, key %s, %s\n" % (NAME, IP, UUID, HEX32, TENANT)


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    assert git(repo, "add", "--", name).returncode == 0


def push(repo: Path, monkeypatch, register_path: Path, old: str, new: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO("refs/heads/main %s refs/heads/main %s\n" % (new, old)))
    return gate.main(["--pushed", "--repo", str(repo), "--register", str(register_path)])


def pushed_repo(tmp_path: Path, content: str, message: str = "change") -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, base = repo_with_one_commit(tmp_path)
    (repo / "b.txt").write_text(content, encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", message)
    return repo, base, push_git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- a commit inside


def test_a_project_commit_with_a_name_an_ip_and_a_uuid_passes(project, register_path, capsys):
    folder = project
    assert git(folder, "init", "-q").returncode == 0
    stage(folder, "notes.md", DATA)
    assert gate.main(["--staged", "--repo", str(folder), "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""
    # no content check at all: a secret, a key, a token and a home path pass as well
    stage(folder, "more.md", "%s\n%s\n%s\n%s\n" % (PRIVATE_KEY, TOKEN, HOME_PATH, PASSWORD_LINE))
    assert gate.main(["--staged", "--repo", str(folder), "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""
    # the name of a file is no finding either, and a binary file is no content to check
    stage(folder, "offer-%s.md" % NAME.replace(" ", "-"), "x\n")
    (folder / "blob.bin").write_bytes(b"\x00\x01\x02 binary \x00")
    assert git(folder, "add", "blob.bin").returncode == 0
    assert gate.main(["--staged", "--repo", str(folder), "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""


def test_what_stays_inside_is_the_size_limit_the_attribution_and_the_dated_owner_item(project, register_path, capsys,
                                                                                     tmp_path):
    folder = project
    assert git(folder, "init", "-q").returncode == 0
    stage(folder, "big.md", "x" * (gate.TEXT_LIMIT + 1))
    assert gate.main(["--staged", "--repo", str(folder), "--register", str(register_path)]) == 1
    assert capsys.readouterr().out == "too-large  big.md:0\n"
    msg = tmp_path / "MSG"
    msg.write_text("note for %s\n\n%s\n" % (NAME, CO_AUTHOR), encoding="utf-8")
    assert gate.main(["--message", str(msg), "--repo", str(folder), "--register", str(register_path)]) == 1
    assert capsys.readouterr().out == "attribution  commit message:3\n"
    msg.write_text("note for %s on %s, %s\n" % (NAME, IP, UUID), encoding="utf-8")
    assert gate.main(["--message", str(msg), "--repo", str(folder), "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""


def test_a_commit_of_the_working_copy_of_the_public_repository_is_checked_at_its_push(tmp_path, monkeypatch,
                                                                                    register_path):
    monkeypatch.setenv("PYTHONPATH", str(Path(HOOK).parent.parent))
    repo = hook_repo(tmp_path)
    stage(repo, "notes.txt", "offer for %s, %s\n%s\n" % (NAME, HOME_PATH, PRIVATE_KEY))
    committed = git(repo, "-c", "core.hooksPath=%s" % HOOK.parent, "commit", "-q", "-m",
                    "offer for %s %s" % (NAME, IP))
    assert committed.returncode == 0, committed.stdout + committed.stderr
    head = push_git(repo, "rev-parse", "HEAD")
    assert push(repo, monkeypatch, register_path, zero(), head) == 1
    # the same commit with a message that names an assistant never gets in
    stage(repo, "more.txt", "clean\n")
    refused = git(repo, "-c", "core.hooksPath=%s" % HOOK.parent, "commit", "-q", "-m", "more\n\n" + CO_AUTHOR)
    assert refused.returncode != 0 and "attribution  commit message:3" in refused.stdout + refused.stderr


def test_the_inside_flag_runs_no_content_check_outside_a_project(tmp_path, register_path, capsys):
    repo = new_repo(tmp_path / "copy")
    stage(repo, "notes.txt", DATA + PRIVATE_KEY)
    assert gate.main(["--staged", "--inside", "--repo", str(repo), "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""
    # without the flag a folder that is no project keeps the exit check
    assert gate.main(["--staged", "--repo", str(repo), "--register", str(register_path)]) == 1
    assert capsys.readouterr().out == "name  notes.txt:1\nprivate-key  notes.txt:2\n"


# --------------------------------------------------------------------------- the exit


def test_a_push_of_the_public_repository_with_a_name_is_refused(tmp_path, monkeypatch, register_path, capsys):
    repo, base, head = pushed_repo(tmp_path, "offer for %s\n" % NAME)
    assert push(repo, monkeypatch, register_path, base, head) == 1
    assert capsys.readouterr().out == "name  %s:b.txt:1\n" % head[:10]


def test_a_push_with_a_private_key_a_token_or_a_home_path_is_refused(tmp_path, monkeypatch, register_path, capsys):
    for text, cls in ((PRIVATE_KEY, "private-key"), (TOKEN + "\n", "token"), (HOME_PATH + "\n", "homepath")):
        repo, base, head = pushed_repo(tmp_path / cls, text)
        assert push(repo, monkeypatch, register_path, base, head) == 1, cls
        assert capsys.readouterr().out == "%s  %s:b.txt:%d\n" % (cls, head[:10], 1)


def test_a_push_with_a_name_in_the_message_or_the_blocklist_word_is_refused(tmp_path, monkeypatch, register_path,
                                                                            capsys):
    repo, base, head = pushed_repo(tmp_path / "msg", "clean\n", "offer for %s" % NAME)
    assert push(repo, monkeypatch, register_path, base, head) == 1
    assert capsys.readouterr().out == "name  commit %s:0\n" % head[:10]


def test_a_push_with_a_uuid_a_32_hex_hash_and_a_tenant_number_passes(tmp_path, monkeypatch, register_path, capsys):
    repo, base, head = pushed_repo(tmp_path, "vm %s, key %s, %s, host %s\n" % (UUID, HEX32, TENANT, IP),
                                   "ids %s %s" % (UUID, HEX32))
    assert push(repo, monkeypatch, register_path, base, head) == 0
    assert capsys.readouterr().out == ""
    # the same text of a ref name and an annotated tag
    push_git(repo, "tag", "-a", "v1", "-m", "release %s" % UUID)
    assert push(repo, monkeypatch, register_path, base, head) == 0


def test_the_exit_check_of_a_merge_message_keeps_the_names_and_drops_the_ids(project, register_path, capsys, tmp_path,
                                                                             monkeypatch):
    # awb pr take runs it from any folder, a project folder too
    monkeypatch.chdir(project)
    msg = tmp_path / "MSG"
    msg.write_text("take pr 12 for %s\n" % NAME, encoding="utf-8")
    assert gate.main(["--message", str(msg), "--exit", "--register", str(register_path)]) == 1
    assert capsys.readouterr().out == "name  commit message:1\n"
    msg.write_text("take pr 12 for vm %s, %s, %s\n" % (UUID, HEX32, TENANT), encoding="utf-8")
    assert gate.main(["--message", str(msg), "--exit", "--register", str(register_path)]) == 0
    assert capsys.readouterr().out == ""
    from awb import prtake

    assert prtake._gate_message(msg) == 0
    msg.write_text("take pr 12 for %s\n" % NAME, encoding="utf-8")
    assert prtake._gate_message(msg) == 1


# --------------------------------------------------------------------------- the build side


def test_a_runner_result_line_with_a_fixture_name_is_shown(world):
    from awb import gate as g

    run = world.run("subject")
    # the commit passes the gate inside; the name is stopped where it leaves, at the push of main
    assert run.state == "held: push refused"
    text = (run.folder / "result.md").read_text()
    assert g._SELFTEST_FORM in text and "subject withheld" not in text
    lines = build_checks._tail("ran for %s on %s\n%s\n" % (NAME, IP, UUID))
    assert lines == ["ran for %s on %s" % (NAME, IP), UUID, ""]
    assert build_checks._node_shown("tests/test_x.py::test_a[%s]" % HEX32) == "tests/test_x.py::test_a[%s]" % HEX32
    assert build_checks._node_shown("tests/test_x.py::test_a[a b]") == "(node withheld)"


def test_the_runner_prompt_and_the_answer_carry_no_content_check(world, tmp_path, register_path, capsys):
    q = world.queue
    tid = world.task("good", extra="for %s on %s, %s %s\n" % (NAME, IP, TOKEN, HOME_PATH))
    (task,), _ = build.read_queue(q)
    assert NAME in build.read_prompt(task, q, register_path)
    assert world.runner().poll_once()
    assert build.latest(tid, world.runs).state != "held: prompt"


def test_the_commits_of_a_task_pass_check_6_and_only_the_attribution_is_red(world):
    assert world.run("good").state == "done"
    red = world.run("coauthor", paths="awb/ tests/")
    assert red.state == "red: gate attribution"


@pytest.mark.skipif(os.name != "posix", reason="shell hooks")
def test_the_hooks_of_a_build_clone_pass_the_inside_flag():
    root = Path(HOOK).parent.parent / "hooks-build"
    pre = (root / "pre-commit").read_text(encoding="utf-8")
    msg = (root / "commit-msg").read_text(encoding="utf-8")
    assert "gate --staged --inside --no-register" in pre
    assert 'gate --message "$msg" --inside --no-register' in msg
    for hook in (root / "pre-commit", root / "commit-msg"):
        assert subprocess.run(["sh", "-n", str(hook)], check=False).returncode == 0 and os.access(hook, os.X_OK)
