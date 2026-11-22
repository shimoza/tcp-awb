"""awb side take copies only a result the side session wrote (tcp-6edq ask 1 of 2026-11-21): a take of three failed
side sessions with no commit wrote notes/side-NAME.md with an old result. A merged side session had brought its
SIDE-RESULT.md into the project, so every new worktree started with it. A result equal to the one of the start
commit is no result of this session: take writes nothing and status says "result: no"."""
from __future__ import annotations

import json
import sys

from awb import side
from tests.test_side import _git, _wait, proj, side_register  # noqa: F401

OLD = "# Result of an earlier side session\n\nAn old lesson text.\n"

FAILING = r"""#!%s
import sys
sys.stdin.read()
print('{"result": "error", "num_turns": 0, "is_error": true}')
sys.exit(1)
"""


def carry_old_result(proj):
    (proj / side.RESULT).write_text(OLD, encoding="utf-8")
    _git(proj, "add", side.RESULT)
    _git(proj, "commit", "-q", "-m", "merged an earlier side")


def failing_client(tmp_path, monkeypatch):
    fake = tmp_path / "failing-client"
    fake.write_text(FAILING % sys.executable)
    fake.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(fake))


def test_6edq_a_failed_side_with_no_commit_takes_no_old_result(proj, tmp_path, monkeypatch):  # noqa: F811
    carry_old_result(proj)
    failing_client(tmp_path, monkeypatch)
    side.start("storage", "notes/brief-mail.md", cwd=proj)
    line = _wait(proj, "storage", "failed")
    assert "0 commit(s)" in line and line.endswith("result: no")
    out = side.take("storage", cwd=proj)
    assert out[0].startswith("result: none (SIDE-RESULT.md is the one of the start commit")
    assert not (proj / "notes" / "side-storage.md").exists() and "commits: 0" in out


def test_6edq_a_record_without_its_start_commit_uses_the_merge_base(proj, tmp_path, monkeypatch):  # noqa: F811
    carry_old_result(proj)
    failing_client(tmp_path, monkeypatch)
    side.start("entra", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "entra", "failed")
    state = proj / ".side" / "entra.state"
    meta = json.loads(state.read_text())
    meta.pop("base")
    state.write_text(json.dumps(meta) + "\n")
    _git(proj, "commit", "-q", "--allow-empty", "-m", "the main session goes on")
    assert side.take("entra", cwd=proj)[0].startswith("result: none (SIDE-RESULT.md is the one of the start")
    assert not (proj / "notes" / "side-entra.md").exists()


def test_6edq_a_side_that_writes_its_own_result_over_the_old_one_is_taken(proj):  # noqa: F811
    carry_old_result(proj)
    side.start("steps", "notes/brief-mail.md", cwd=proj)
    assert _wait(proj, "steps").endswith("result: yes")
    assert side.take("steps", cwd=proj)[0] == "result: notes/side-steps.md"
    assert (proj / "notes" / "side-steps.md").read_text().startswith("result of the side work")
