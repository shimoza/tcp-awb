"""The ledger draft of the stop hook (tcp-bytg, 2026-11-19): a commit subject with a link or a bare host is drafted
instead of being refused on every stop (DMZ 2: as written, no masking and no text screen), and commits a session
already wrote down with awb ledger add are not drafted again."""
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
from awb import hooks, ledger, projects
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 19, 15, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def gate_on_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setattr(hooks, "DRAFT_HOURS", 0)


def spawn(home, register_path):
    return projects.spawn(home, "engagement", "move two app clusters to managed k8s", fx.CUSTOMER_CODE,
                          register_path, tags=["cce"])


@pytest.fixture
def refusing_ledger(monkeypatch):
    """DMZ 2: the draft has no text screen of its own, so a refusal is planted in the ledger's writer: it refuses a
    draft that carries the name of a person."""
    real = ledger.append_line

    def writer(path, obj, mode=0o640):
        if fx.PERSON_FORMS[0] in obj["done"]:
            raise ledger.Refused("done refused (name 1)")
        return real(path, obj, mode)

    monkeypatch.setattr(ledger, "append_line", writer)


def commit(folder: Path, name: str, subject: str) -> None:
    (folder / name).write_text(subject + "\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(folder), "commit", "-q", "--no-verify", "-m", subject], check=True,
                   capture_output=True, env=env)


def drafts(home, code):
    return [e for e in ledger.load(home) if e.project == code and e.outcome == hooks.DRAFT_OUTCOME]


def head(folder: Path) -> str:
    return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def marker(home, code) -> Path:
    return home.ledger / ".drafts" / (code + ".json")


def by_hand(home, code, text="Sizing and the network layout written down"):
    ledger.add("other", text, project=code, customer=fx.CUSTOMER_CODE, p=home)


def test_a_link_and_a_bare_host_are_taken_out_of_the_draft(home, register_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "Read the guide at %s" % fx.LEDGER_LINK)
    commit(folder, "b.md", "Mirror of %s checked" % fx.LEDGER_LINK_HOST)
    assert hooks.draft_ledger(home, folder, now=NOW) is True
    [d] = drafts(home, pr.code)
    assert d.done == "%s: 2 commit(s): Read the guide at %s; Mirror of %s checked" % (
        pr.code, fx.LEDGER_LINK, fx.LEDGER_LINK_HOST)
    assert json.loads(marker(home, pr.code).read_text())["head"] == head(folder)


def test_planted_the_raw_subject_is_refused_so_the_cleaning_is_what_lets_it_in(home, register_path):
    """DMZ 4: the ledger keeps no text screen, so the raw subject with its link is written as it is by `add` too."""
    pr = spawn(home, register_path)
    text = "%s: 1 commit(s): Read the guide at %s" % (pr.code, fx.LEDGER_LINK)
    assert ledger.add("other", text, project=pr.code, p=home).done == text


def test_entries_by_hand_after_a_refusal_cover_the_refused_commits(home, register_path, refusing_ledger):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "call with %s" % fx.PERSON_FORMS[0])
    with pytest.raises(ledger.Refused):
        hooks.draft_ledger(home, folder, now=NOW)
    assert hooks.draft_ledger(home, folder, now=NOW) is False              # said once, quiet until something changes
    by_hand(home, pr.code)
    assert hooks.draft_ledger(home, folder, now=NOW) is False              # covered: nothing drafted, no refusal
    assert json.loads(marker(home, pr.code).read_text())["head"] == head(folder)
    assert not marker(home, pr.code).with_suffix(".refused").exists() and not drafts(home, pr.code)
    commit(folder, "b.md", "Network layout per stage")
    assert hooks.draft_ledger(home, folder, now=NOW) is True                # only the new commit, the refused one never
    [d] = drafts(home, pr.code)
    assert d.done == "%s: 1 commit(s): Network layout per stage" % pr.code


def test_without_an_entry_by_hand_the_refused_commit_is_tried_again_with_the_next(home, register_path,
                                                                                    refusing_ledger):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "call with %s" % fx.PERSON_FORMS[0])
    with pytest.raises(ledger.Refused):
        hooks.draft_ledger(home, folder, now=NOW)
    commit(folder, "b.md", "Network layout per stage")
    with pytest.raises(ledger.Refused):
        hooks.draft_ledger(home, folder, now=NOW)
    assert not marker(home, pr.code).exists()


def test_entries_by_hand_since_the_last_draft_cover_the_new_commits(home, register_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "Sizing of the two clusters")
    assert hooks.draft_ledger(home, folder, now=NOW) is True
    commit(folder, "b.md", "Network layout per stage")
    by_hand(home, pr.code)
    assert hooks.draft_ledger(home, folder, now=NOW) is False
    assert len(drafts(home, pr.code)) == 1
    assert json.loads(marker(home, pr.code).read_text())["head"] == head(folder)


def test_an_entry_of_the_tooling_covers_nothing(home, register_path):
    pr = spawn(home, register_path)                                         # the spawn wrote "Started ..." itself
    folder = Path(pr.path)
    commit(folder, "a.md", "Sizing of the two clusters")
    ledger.add("other", "material added: inventory", project=pr.code, p=home)
    assert hooks.draft_ledger(home, folder, now=NOW) is True
    assert len(drafts(home, pr.code)) == 1


def test_an_old_refusal_note_with_the_head_alone_is_still_read(home, register_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "Sizing of the two clusters")
    note = marker(home, pr.code).with_suffix(".refused")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(head(folder) + "\n", encoding="utf-8")
    assert hooks.draft_ledger(home, folder, now=NOW) is False
    assert not drafts(home, pr.code)


def test_the_stop_message_names_the_command_and_is_said_once(home, register_path, monkeypatch, capsys,
                                                            refusing_ledger):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    commit(folder, "a.md", "call with %s" % fx.PERSON_FORMS[0])

    def stop():
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": pr.path})))
        code = hooks.main(["stop"])
        return code, capsys.readouterr().err

    code, err = stop()
    assert code == 2 and "awb ledger add --project %s" % pr.code in err and "counts these commits as covered" in err
    assert fx.PERSON_FORMS[0] not in err
    assert "refused the draft" not in stop()[1]
    by_hand(home, pr.code)
    assert "refused the draft" not in stop()[1]
    assert not marker(home, pr.code).with_suffix(".refused").exists()
