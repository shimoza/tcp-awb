"""awb voice learn DRAFT - : the sent text on standard input, wiped on the owner side, is the sent text of the pair.

The register is the temporary one of the invented names of tests/fixtures.py and the draft lies in a temporary
project folder. The key line is put together at run time.
"""
from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pytest

import awb
from awb import cli, config, projects, writing
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
DRAFT = "We will leverage the new cluster in order to move fast. The rollout takes three careful steps.\n"
SENT = "I use the new cluster to move fast for %s. The rollout takes three steps.\n" % fx.CUSTOMER_FORMS[0]


class Stdin(io.StringIO):
    def isatty(self):
        return False


class Terminal(io.StringIO):
    def isatty(self):
        return True

    def read(self, *a):
        raise AssertionError("standard input was read")


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(writing, "today", lambda: date.fromisoformat(fx.TODAY))


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


@pytest.fixture
def draft(project) -> Path:
    d = Path(project.path) / "draft.md"
    d.write_text(DRAFT, encoding="utf-8")
    return d


def files_of(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*") if f.is_file()) if root.is_dir() else []


def run(monkeypatch, capsys, argv, text):
    monkeypatch.setattr(sys, "stdin", text if not isinstance(text, str) else Stdin(text))
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out + err


def test_a_registered_form_on_stdin_is_stored_as_its_code(home, project, draft, monkeypatch, capsys):
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(draft), "-"], SENT)
    assert code == 0, shown
    pair = home.shared / "voice" / "pairs" / ("%s-1" % fx.TODAY)
    stored = (pair / "sent.md").read_text(encoding="utf-8")
    assert fx.CUSTOMER_CODE in stored
    assert fx.CUSTOMER_FORMS[0] not in stored
    fx.assert_no_fixture_name(shown, "the output")
    for f in files_of(home.shared / "voice"):
        fx.assert_no_fixture_name(f.read_text(encoding="utf-8"), "a file of the voice folder")
    for f in files_of(home.outbox):
        fx.assert_no_fixture_name(f.read_text(encoding="utf-8"), "a file of the outbox")


def test_the_project_may_be_named_instead_of_found(home, project, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "elsewhere.md"
    outside.write_text(DRAFT, encoding="utf-8")
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(outside), "-"], SENT)
    assert code == 2 and "no project folder" in shown
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(outside), "-", "--project", project.code], SENT)
    assert code == 0, shown
    assert fx.CUSTOMER_FORMS[0] not in (home.shared / "voice" / "pairs" / ("%s-1" % fx.TODAY) / "sent.md").read_text()


def test_a_planted_key_refuses_the_call_and_writes_nothing(home, project, draft, monkeypatch, capsys):
    key = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----\n"
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(draft), "-"], SENT + key)
    assert code == 1, shown
    assert "secret" in shown
    assert not (home.shared / "voice").exists()
    assert files_of(home.outbox) == []


def test_refused_for_the_work_user_a_session_and_a_terminal(home, project, draft, monkeypatch, capsys):
    argv = ["voice", "learn", str(draft), "-"]
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    code, shown = run(monkeypatch, capsys, argv, Terminal())
    assert code == 2 and shown.strip() == writing.HOLD_WORK_USER
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setenv("CLAUDECODE", "1")
    code, shown = run(monkeypatch, capsys, argv, Terminal())
    assert code == 2 and shown.strip() == writing.HOLD_SESSION
    monkeypatch.delenv("CLAUDECODE")
    code, shown = run(monkeypatch, capsys, argv, Terminal())
    assert code == 2 and shown.strip() == writing.HOLD_TERMINAL
    assert not (home.shared / "voice").exists()


def test_the_two_file_form_still_works(home, project, draft, tmp_path, monkeypatch, capsys):
    sent = tmp_path / "sent.txt"
    sent.write_text("I use the new cluster to move fast. The rollout takes three steps.\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDECODE", "1")
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(draft), str(sent)], Terminal())
    assert code == 0, shown
    assert (home.shared / "voice" / "pairs" / ("%s-1" % fx.TODAY) / "sent.md").is_file()
