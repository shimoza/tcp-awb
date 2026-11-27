"""`awb inbox take --outbox`: the copies of the project's own `awb import` from the outbox into input/ (ask
2026-11-27-tcp-j5sy-1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, projects, rulesync
from awb.tcp import xchg
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"
REPORT = "intake-report.md"


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _copy(home, folder: str, name: str, text: str) -> Path:
    box = home.outbox / folder
    box.mkdir(parents=True, exist_ok=True)
    (box / name).write_text(text, encoding="utf-8")
    return box / name


@pytest.fixture
def two_of_a_customer(home, register_path, monkeypatch):
    mine = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    other = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    monkeypatch.chdir(mine.path)
    return mine, other


def test_the_take_moves_only_the_projects_own_copies_and_a_second_take_takes_nothing(home, two_of_a_customer,
                                                                                         capsys):
    mine, other = two_of_a_customer
    _copy(home, fx.CUSTOMER_CODE, "F-AAAA.md", "sizing of %s: 12 VMs\n" % fx.CUSTOMER_CODE)
    _copy(home, fx.CUSTOMER_CODE, "F-BBBB.md", "the other project's copy\n")
    _copy(home, fx.CUSTOMER_CODE, REPORT, "# public report\n")
    rulesync.post_input(home, fx.CUSTOMER_CODE, mine.code, ["F-AAAA"])
    rulesync.post_input(home, fx.CUSTOMER_CODE, other.code, ["F-BBBB"])

    code, out, err = run(["inbox", "take", "--outbox"], capsys)
    assert code == 0, err
    assert "F-AAAA.md taken into input/" in out and "F-BBBB" not in out
    assert (Path(mine.path) / "input" / "F-AAAA.md").read_text() == "sizing of %s: 12 VMs\n" % fx.CUSTOMER_CODE
    assert not (home.outbox / fx.CUSTOMER_CODE / "F-AAAA.md").exists()
    assert (home.outbox / fx.CUSTOMER_CODE / "F-BBBB.md").exists()
    assert (home.outbox / fx.CUSTOMER_CODE / REPORT).read_text() == "# public report\n"
    assert not (Path(mine.path) / "input" / "F-BBBB.md").exists()

    code, out, err = run(["inbox", "take", "--outbox"], capsys)
    assert code == 0, err
    assert out.strip() == xchg.OUTBOX_NONE % mine.code


def test_a_project_without_a_customer_takes_every_copy_of_its_own_folder_but_the_report(home, register_path,
                                                                                         monkeypatch, capsys):
    lab = projects.spawn(home, "lab", GOAL, None, register_path)
    monkeypatch.chdir(lab.path)
    _copy(home, lab.code, "F-CCCC.md", "no notice for this one\n")
    _copy(home, lab.code, "paste-2026-11-27-1.md", "a pasted mail\n")
    _copy(home, lab.code, REPORT, "# public report\n")

    code, out, err = run(["inbox", "take", "--outbox"], capsys)
    assert code == 0, err
    root = Path(lab.path) / "input"
    assert (root / "F-CCCC.md").is_file() and (root / "paste-2026-11-27-1.md").is_file()
    assert not (root / REPORT).exists() and (home.outbox / lab.code / REPORT).is_file()
    assert "2 copy(ies) from the outbox in input/" in out


def test_a_copy_with_a_registered_name_stays_in_the_outbox_with_the_hold_line(home, two_of_a_customer, capsys):
    mine, _ = two_of_a_customer
    _copy(home, fx.CUSTOMER_CODE, "F-DDDD.md", "call %s on Monday\n" % fx.PERSON_FORMS[0])
    rulesync.post_input(home, fx.CUSTOMER_CODE, mine.code, ["F-DDDD"])

    code, out, err = run(["inbox", "take", "--outbox"], capsys)
    assert code == 1
    assert "F-DDDD.md held in the outbox" in out and xchg.hold_line(mine.code) in out
    assert fx.PERSON_FORMS[0] not in out + err
    assert (home.outbox / fx.CUSTOMER_CODE / "F-DDDD.md").exists()
    assert not (Path(mine.path) / "input" / "F-DDDD.md").exists()


def test_a_copy_that_differs_in_input_is_replaced_only_after_a_redo(home, two_of_a_customer, capsys):
    mine, _ = two_of_a_customer
    target = Path(mine.path) / "input" / "F-EEEE.md"
    target.write_text("first run\n", encoding="utf-8")
    _copy(home, fx.CUSTOMER_CODE, "F-EEEE.md", "second run\n")
    rulesync.post_input(home, fx.CUSTOMER_CODE, mine.code, ["F-EEEE"])

    code, out, _ = run(["inbox", "take", "--outbox"], capsys)
    assert code == 1 and "F-EEEE.md not taken" in out
    assert target.read_text() == "first run\n"

    rulesync.post_input(home, fx.CUSTOMER_CODE, mine.code, ["F-EEEE"], replaced=True)
    code, out, err = run(["inbox", "take", "--outbox"], capsys)
    assert code == 0, err
    assert "F-EEEE.md replaced in input/" in out
    assert target.read_text() == "second run\n"
    assert not (home.outbox / fx.CUSTOMER_CODE / "F-EEEE.md").exists()


def test_a_copy_whose_bytes_are_in_input_already_is_not_taken_again(home, two_of_a_customer, capsys):
    mine, _ = two_of_a_customer
    (Path(mine.path) / "input" / "F-FFFF.md").write_text("same\n", encoding="utf-8")
    _copy(home, fx.CUSTOMER_CODE, "F-FFFF.md", "same\n")
    rulesync.post_input(home, fx.CUSTOMER_CODE, mine.code, ["F-FFFF"])

    code, out, _ = run(["inbox", "take", "--outbox"], capsys)
    assert code == 0 and out.strip() == xchg.OUTBOX_NONE % mine.code
    assert not (home.outbox / fx.CUSTOMER_CODE / "F-FFFF.md").exists()


def test_outbox_takes_no_words_or_other_options(home, two_of_a_customer, capsys):
    code, _, err = run(["inbox", "take", "--outbox", "the pdf"], capsys)
    assert code == 1 and "--outbox takes no words" in err


def test_the_project_rules_name_the_command_and_sync_replaces_the_hand_move(home, two_of_a_customer):
    mine, _ = two_of_a_customer
    claude = Path(mine.path) / "CLAUDE.md"
    text = claude.read_text()
    assert "`awb inbox take --outbox` takes them into input/" in text
    assert "when they come): move them into input/." not in text
    old = text.replace("`awb inbox take --outbox` takes them into input/ with the name check.",
                       "move them into input/.")
    claude.write_text(old)
    rulesync.sync_project(Path(mine.path))
    assert claude.read_text() == text


def test_the_new_input_notice_names_the_command(home, two_of_a_customer):
    assert "awb inbox take --outbox" in rulesync.INPUT and "awb inbox take --outbox" in rulesync.REPLACED


def test_a_project_of_the_intake_era_gets_the_command_line_from_the_sync(home, two_of_a_customer):
    mine, _ = two_of_a_customer
    claude = Path(mine.path) / "CLAUDE.md"
    text = claude.read_text()
    old = text.replace("`awb import` wait in the outbox (the prompt says \"new input\" when they come): `awb inbox "
                       "take --outbox` takes them into input/ with the name check.",
                       "`awb intake` wait in the outbox: move them into input/.")
    assert old != text
    claude.write_text(old)
    rulesync.sync_project(Path(mine.path))
    assert claude.read_text() == text
