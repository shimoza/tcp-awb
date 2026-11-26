"""DMZ 4 (B-088, ask 2026-11-25): the ledger, the notes, the asks, the projects, the side sessions and the console run
no content check inside.

The checks stay at the entry of customer material (DMZ 1) and at the exit of the public repositories (the push, the
dataset, the solutions packages, the public page). A registered name, an IP address, a host name, a UUID, a mail or a
key shaped value in a text of these records is written and shown as written. A control character is still refused
where a line format needs it. Every name is an invented fixture name; the values are put together at run time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import (career, cli, config, devask, english, ledger, opensweep, ownerword, ownerwait, period, projects,
                 side)
from awb.tcp import tenantcost
from awb.tcp.web import projects_api
from tests import fixtures as fx

NAME = fx.CUSTOMER_FORMS[0]
IP = "203.0.113.45"
UUID = "-".join(("3f2a1b4c", "9d8e", "4f7a", "8b6c", "5d4e3f2a1b0c"))
HOST = "gw01.qvelmar-demo.net"
TEXT = "sized the clusters of %s behind %s, volume %s, host %s" % (NAME, IP, UUID, HOST)
SECRET = "db_pass" + "word = " + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
CODE = "tcp-q7m4"


def _all_shown(text: str) -> None:
    for part in (NAME, IP, UUID, HOST):
        assert part in text, part


# --------------------------------------------------------------------------- the ledger


def test_a_ledger_line_with_a_name_an_ip_and_a_uuid_is_written_and_shown_as_written(home, capsys):
    e = ledger.add("proof", TEXT, project=CODE, outcome="%s on %s" % (NAME, IP),
                   deliverables=["%s/%s.md" % (UUID, HOST)], open_items=["ask %s about %s" % (NAME, SECRET)], p=home)
    assert e.done == TEXT
    assert json.loads(next(home.ledger.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0])["done"] == TEXT
    assert ledger.load(home)[0].open == ["ask %s about %s" % (NAME, SECRET)]
    assert cli.main(["ledger", "list"]) == 0
    out = capsys.readouterr()
    _all_shown(out.out)
    assert SECRET in out.out and out.err == ""
    assert cli.main(["report", "--by", "kind"]) == 0
    out = capsys.readouterr()
    _all_shown(out.out)
    assert out.err == ""


def test_the_cli_ledger_add_takes_a_name_and_still_refuses_a_control_character(home, capsys):
    assert cli.main(["ledger", "add", "--kind", "other", "--done", TEXT]) == 0
    assert ledger.load(home)[0].done == TEXT
    capsys.readouterr()
    assert cli.main(["ledger", "add", "--kind", "other", "--done", "one\x07two"]) == 2
    assert "control or invisible" in capsys.readouterr().err
    assert len(ledger.load(home)) == 1


def test_a_register_that_is_missing_stops_no_ledger_line(home):
    home.register.unlink()
    ledger.add("other", TEXT, p=home)
    assert ledger.load(home)[0].done == TEXT


def test_the_period_report_and_the_career_log_show_the_texts_as_written(home, capsys, monkeypatch):
    pr = projects.spawn(home, "query", "size %s on %s" % (NAME, IP), fx.CUSTOMER_CODE, home.register)
    ledger.add("proof", TEXT, project=pr.code, p=home)
    text = period.report(None, None, "md", home)
    assert NAME in text and IP in text and "withheld" not in text
    e = career.add("Sizing for %s" % NAME, "architect", "Terraform, Ansible", "ran %s on %s" % (UUID, IP),
                   "sized the platform of %s" % HOST, p=home)
    assert e.title == "Sizing for %s" % NAME
    assert career.load(home)[0].outcome == "ran %s on %s" % (UUID, IP)
    out = career.update(home).draft
    assert NAME in out and IP in out and "withheld" not in out


def test_the_open_sweep_shows_an_item_with_a_name_and_an_ip(home, monkeypatch):
    root = home.projects_root / CODE
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# scope\n")
    (root / "OPEN.md").write_text("# open\n- 2026-11-20 owner: call %s about %s. Default: use %s.\n" % (NAME, IP, UUID))
    shown = opensweep.sweep(root)
    assert NAME in shown and IP in shown and UUID in shown and "withheld" not in shown


# --------------------------------------------------------------------------- queue, notes, asks


def test_a_queue_row_with_a_name_an_ip_and_a_uuid_is_kept_and_listed(home, tmp_path, monkeypatch, capsys):
    path = tmp_path / "work-profile" / "queue.tsv"
    monkeypatch.setenv("AWB_WORK_QUEUE", str(path))
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert cli.main(["queue", "add", "call %s about %s" % (NAME, IP), "--next", "send %s" % UUID,
                     "--who", "owner"]) == 0
    assert cli.main(["queue", "list"]) == 0
    out = capsys.readouterr().out
    assert NAME in out and IP in out and UUID in out
    assert not (path.parent / "queue-verdicts.tsv").exists()
    assert "withheld" not in out


def test_an_english_note_and_an_owner_word_keep_a_name_and_an_ip(home):
    assert english.keep(home, "He wrote: %s on %s" % (NAME, IP))
    assert NAME in english.notes_file(home).read_text(encoding="utf-8")
    note = ownerword.screened(home, "decided", "go with %s on %s" % (NAME, IP))
    assert NAME in note and IP in note


def test_a_dev_ask_with_a_name_an_ip_and_a_uuid_is_written_as_written(home, monkeypatch):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    text = "%s fails on %s with %s, %s" % (HOST, IP, UUID, NAME)
    assert cli.main(["dev", "ask", text]) == 0
    written = sorted((home.shared / "notices/dev").glob("*.md"))[-1].read_text(encoding="utf-8")
    assert text in written


def test_the_ask_screen_keeps_the_line_format_only(home):
    devask.screen(home, "%s on %s %s\n%s" % (NAME, IP, SECRET, UUID))
    with pytest.raises(devask.AskError, match="control character"):
        devask.screen(home, "one\x07two")


def test_a_tenant_cost_note_takes_an_id_a_name_and_an_ip(home):
    assert tenantcost.check_note("kept for %s on %s as %s" % (NAME, IP, UUID)) == \
        "kept for %s on %s as %s" % (NAME, IP, UUID)


# --------------------------------------------------------------------------- projects


def test_a_project_goal_with_a_name_an_ip_and_a_uuid_is_spawned_and_shown_as_written(home, capsys):
    goal = "size %s on %s (%s)" % (NAME, IP, UUID)
    pr = projects.spawn(home, "query", goal, fx.CUSTOMER_CODE, home.register, tags=["pricing"])
    assert projects.goal_of(pr.path) == goal
    assert goal in (Path(pr.path) / "SCOPE.md").read_text(encoding="utf-8")
    assert cli.main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert NAME in out and IP in out and "withheld" not in out and "unavailable" not in out


def test_a_goal_with_a_candidate_name_a_secret_and_no_register_is_spawned_too(home):
    home.register.unlink()
    goal = "ask %s about %s" % (fx.FIRST_LAST, SECRET)
    pr = projects.spawn(home, "query", goal, None, home.register)
    assert projects.goal_of(pr.path) == goal


def test_the_waiting_list_shows_an_owner_item_as_written(home):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    (Path(pr.path) / "OPEN.md").write_text("# Open\n\n- owner: call %s on %s\n" % (NAME, IP), encoding="utf-8")
    (Path(pr.path) / "STATE.md").write_text("Status: x\nNext: ask %s\n" % UUID, encoding="utf-8")
    rows = projects.waiting(home)
    assert rows[0]["owner"] == ["owner: call %s on %s" % (NAME, IP)] and UUID in rows[0]["next"]
    assert any(NAME in line for line in projects.waiting_lines(rows))


def test_owner_status_shows_an_owner_item_as_written(home, monkeypatch):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    (Path(pr.path) / "OPEN.md").write_text("# Open\n\n- owner: call %s on %s\n" % (NAME, IP), encoding="utf-8")
    for name in ("export_items", "winlab_items", "winlab_online_items", "tenant_mode_items"):
        monkeypatch.setattr(ownerwait, name, lambda *a: [])
    monkeypatch.setattr(ownerwait, "queue_file", lambda: home.shared / "none.tsv")
    items, _ = ownerwait.collect(home)
    assert any(NAME in it.text and IP in it.text for it in items)


def test_the_console_shows_the_files_of_a_project_as_written(home):
    assert projects_api.checked("%s on %s, %s" % (NAME, IP, SECRET), home) == (
        "%s on %s, %s" % (NAME, IP, SECRET), False)
    assert projects_api.checked_items(["call %s" % NAME, UUID], home) == (["call %s" % NAME, UUID], False)


# --------------------------------------------------------------------------- side sessions

FAKE = r"""#!%s
import subprocess
open("SIDE-RESULT.md", "w").write("result for %s on %s with %s\n")
subprocess.run(["git", "add", "SIDE-RESULT.md"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "side"],
               check=True)
print('{"result": "DONE", "num_turns": 3, "is_error": false}')
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def proj(tmp_path, monkeypatch, home):
    root = tmp_path / CODE
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# rules\n")
    (root / "notes").mkdir()
    (root / "notes" / "brief.md").write_text("size the clusters of %s on %s with %s %s\n" % (NAME, IP, UUID, SECRET))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    fake = tmp_path / "fake-client"
    fake.write_text(FAKE % (sys.executable, NAME, IP, UUID))
    fake.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(fake))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")
    return root


def test_a_side_brief_and_result_with_a_name_an_ip_and_a_uuid_are_taken_as_written(proj):
    side.start("mail", "notes/brief.md", cwd=proj)
    end = time.time() + 30
    while time.time() < end and side.status_lines(proj)[0].split()[1] != "done":
        time.sleep(0.1)
    out = side.take("mail", cwd=proj)
    note = (proj / "notes" / "side-mail.md").read_text(encoding="utf-8")
    assert NAME in note and IP in note and UUID in note
    assert out[0] == "result: notes/side-mail.md"


def test_a_side_text_with_a_control_character_is_still_refused(proj):
    (proj / "notes" / "bad.md").write_text("size\x07it\n")
    with pytest.raises(side.SideError, match="control character"):
        side.start("bad", "notes/bad.md", cwd=proj)
    assert not os.path.lexists(proj / ".side" / "bad.state")
