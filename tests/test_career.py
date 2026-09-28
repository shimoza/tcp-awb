"""Tests of awb/career.py: the career log (add, update, due).

The writing checker is another module; the tests put a fake or a missing `awb.writing` in its place so that the
update is tested on its own. Every name here is an invented fixture name.
"""
from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from awb import career, cli, register
from tests import fixtures

TODAY = date(2026, 9, 22)

ENTRY = {
    "title": "Landing zone automation",
    "role": "Designed and built",
    "stack": "Terraform, ECS and VPC",
    "outcome": "Setup time cut from two days to one hour",
    "cv_line": "Built a Terraform landing zone that cut setup time from two days to one hour",
}


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def log_lines(home) -> list[str]:
    f = home.shared / "career.jsonl"
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def add_args(**over) -> list[str]:
    values = dict(ENTRY, **over)
    return ["career", "add", "--title", values["title"], "--role", values["role"], "--stack", values["stack"],
            "--outcome", values["outcome"], "--cv-line", values["cv_line"]]


@dataclass
class FakeTell:
    line: int
    cls: str
    blocking: bool
    hint: str


def fake_writing(monkeypatch, tells=()) -> list[tuple]:
    """A fake awb.writing whose check_text records its calls and returns the given tells."""
    calls: list[tuple] = []

    def check_text(text, mode="doc", scope="tcp"):
        calls.append((text, mode, scope))
        return list(tells), {"sentences": text.count(".")}

    mod = types.ModuleType("awb.writing")
    mod.check_text = check_text
    mod.Tell = FakeTell
    monkeypatch.setitem(sys.modules, "awb.writing", mod)
    return calls


def missing_writing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "awb.writing", None)


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(career, "today", lambda: TODAY)
    return TODAY


# --------------------------------------------------------------------------- add


def test_add_writes_every_field(home, capsys, today):
    code, out, err = run(add_args() + ["--tag", "terraform", "--tag", "ecs"], capsys)
    assert code == 0, err
    rows = log_lines(home)
    assert len(rows) == 1
    obj = json.loads(rows[0])
    assert obj == dict(ENTRY, date="2026-09-22", tags=["terraform", "ecs"])
    assert list(obj) == list(career.FIELDS)
    assert "Automation" in out
    assert career.load(home)[0].as_dict() == obj


def test_add_takes_the_tags_from_the_stack_and_a_given_date(home):
    e = career.add(**ENTRY, day="2026-05-04", p=home)
    assert e.tags == ["terraform", "ecs", "vpc"]
    assert e.date == "2026-05-04"
    assert json.loads(log_lines(home)[0])["tags"] == ["terraform", "ecs", "vpc"]


@pytest.mark.parametrize("field", ["title", "role", "stack", "outcome", "cv_line"])
@pytest.mark.parametrize("value,cls", [
    ("Landing zone for " + fixtures.CUSTOMER_CODE, "code"),
    ("Landing zone with " + fixtures.PARTNER_CODE, "code"),
    ("Landing zone for " + fixtures.PERSON_CODE, "code"),
    ("Landing zone in tcp-q7m4", "project-code"),
    ("Landing zone for " + fixtures.CUSTOMER_FORMS[0], "name"),
    ("Landing zone for " + fixtures.CUSTOMER_FORMS[1].lower(), "name"),
    ("Landing zone with " + fixtures.PERSON_FORMS[0], "name"),
    ("Landing zone for CLI" + "ENT5", "blocklist"),
])
def test_add_refuses_a_code_a_project_code_or_a_name(home, capsys, field, value, cls):
    code, out, err = run(add_args(**{field: value}), capsys)
    assert code == 1
    assert "%s refused" % field.replace("_", "-") in err
    assert cls in err
    assert "names no customer" in err
    fixtures.assert_no_fixture_name(out + err, "career output")
    for secret in (fixtures.CUSTOMER_CODE, fixtures.PARTNER_CODE, "tcp-q7m4", "CUSTOMER"):
        assert secret not in out + err
    assert log_lines(home) == []


def test_add_refuses_structured_data(home, capsys):
    code, out, err = run(add_args(outcome="Mailed the result to ops@" + "nrgtz-beratung.de"), capsys)
    assert code == 1 and "outcome refused" in err and "mail" in err
    assert log_lines(home) == []


def test_add_refuses_an_unknown_tag_and_an_empty_field(home, capsys):
    tag = fixtures.CUSTOMER_FORMS[1].lower()
    code, out, err = run(add_args() + ["--tag", tag], capsys)
    assert code == 2 and "rules/tags.txt" in err and tag not in out + err
    code, out, err = run(add_args(title="  "), capsys)
    assert code == 2 and "title is empty" in err
    assert log_lines(home) == []


# --------------------------------------------------------------------------- update


THEMED = [
    # date, tags, cv line
    ("2026-06-01", ["terraform"], "Built a Terraform module library for landing zones"),
    ("2026-07-10", ["security", "iam"], "Designed the access model for a regulated platform"),
    ("2026-08-20", ["migration", "ecs"], "Moved 120 virtual machines to the cloud in six weeks"),
    ("2026-09-01", ["ansible"], "Automated the patch cycle of 300 servers with Ansible"),
    ("2026-09-10", ["hcs-general"], "Ran a private cloud readiness check"),
]


def add_themed(home) -> None:
    for day, tags, line in THEMED:
        career.add("Work of %s" % day, "Designed and built", "Python", "Outcome of %s" % day, line, tags=tags,
                   day=day, p=home)


def sections(draft: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    for line in draft.splitlines():
        if line.startswith("### "):
            current = line[4:]
            out[current] = []
        elif line.startswith("## "):
            current = None
        elif current and line.startswith("- "):
            out[current].append(line[2:])
    return out


def linkedin(draft: str) -> list[str]:
    part = draft.split("## LinkedIn", 1)[1]
    return [line for line in part.splitlines() if line.strip()]


def test_update_groups_by_theme_and_writes_the_date(home, capsys, monkeypatch, today):
    calls = fake_writing(monkeypatch)
    add_themed(home)
    code, out, err = run(["career", "update"], capsys)
    assert code == 0, err
    got = sections(out)
    assert list(got) == ["Cloud migration", "Automation", "Security", "Other"]
    assert got["Automation"] == ["Automated the patch cycle of 300 servers with Ansible.",
                                 "Built a Terraform module library for landing zones."]
    assert got["Cloud migration"] == ["Moved 120 virtual machines to the cloud in six weeks."]
    assert got["Security"] == ["Designed the access model for a regulated platform."]
    assert got["Other"] == ["Ran a private cloud readiness check."]
    para = linkedin(out)
    assert len(para) == 1
    assert "5 pieces of work" in para[0] and "Work of 2026-09-10: Outcome of 2026-09-10." in para[0]
    assert "in cloud migration, automation and security." in para[0]
    assert out.startswith("# Career update 2026-09-22")
    assert (home.shared / "career-updated").read_text(encoding="utf-8") == "2026-09-22\n"
    assert calls == [(out, "doc", "tcp")]
    assert career.days_since_update(home) == 0


def test_update_since_takes_only_the_later_entries(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch)
    add_themed(home)
    code, out, err = run(["career", "update", "--since", "2026-08-01"], capsys)
    assert code == 0, err
    got = sections(out)
    assert list(got) == ["Cloud migration", "Automation", "Other"]
    assert got["Automation"] == ["Automated the patch cycle of 300 servers with Ansible."]
    assert "3 pieces of work" in linkedin(out)[0]


def test_a_blocking_writing_tell_gives_exit_1_and_the_date_is_still_stored(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch, [FakeTell(5, "banned-word", True, "a banned word"),
                               FakeTell(9, "long-word", False, "long words")])
    add_themed(home)
    code, out, err = run(["career", "update"], capsys)
    assert code == 1
    assert "banned-word  line 5  a banned word" in err
    assert "long-word  line 9" in err and "not blocking" in err
    assert "1 blocking tells" in err
    assert (home.shared / "career-updated").read_text(encoding="utf-8") == "2026-09-22\n"


def test_update_without_the_writing_module_skips_that_step_with_a_note(home, capsys, monkeypatch, today):
    missing_writing(monkeypatch)
    add_themed(home)
    code, out, err = run(["career", "update"], capsys)
    assert code == 0
    assert "writing check skipped" in err
    assert "## CV bullets" in out
    res = career.update(home)
    assert res.note and res.tells == [] and res.entries == len(THEMED)


def test_the_draft_is_plain(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch)
    career.add("Backup redesign \u2014 phase one", "Designed, and built", "Terraform, CBR, or OBS",
               "Restore time cut \u2013 from hours, or days, to minutes", "Redesigned the backup \u2014 fast, and cheap",
               day="2026-09-01", p=home)
    code, out, err = run(["career", "update"], capsys)
    assert code == 0, err
    for line in out.splitlines():
        assert "\u2014" not in line, line
        assert not re.search(r"\s\u2013\s", line), line
        assert not re.search(r",\s*(?:and|or)\b", line, re.IGNORECASE), line
    assert "- Redesigned the backup, fast and cheap." in out


def test_the_draft_withholds_a_text_with_a_name_registered_later(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch)
    career.add("Site network", "Built", "VPC", "Linked the office in %s" % fixtures.CONTROL_UNREGISTERED,
               "Built a site network", day="2026-09-01", p=home)
    register.add(home.register, fixtures.CUSTOMER_CODE + "-SITE-2", "SITE", fixtures.CONTROL_UNREGISTERED)
    code, out, err = run(["career", "update"], capsys)
    assert code == 0, err
    assert fixtures.CONTROL_UNREGISTERED not in out + err
    assert "1 texts withheld" in err


def test_update_with_no_entries(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch)
    code, out, err = run(["career", "update"], capsys)
    assert code == 0
    assert "No career entries." in out
    assert (home.shared / "career-updated").read_text(encoding="utf-8") == "2026-09-22\n"


# --------------------------------------------------------------------------- due


def stamp(home, day: date) -> None:
    (home.shared / "career-updated").write_text(day.isoformat() + "\n", encoding="utf-8")


@pytest.mark.parametrize("days,code", [(91, 1), (90, 0), (5, 0), (200, 1)])
def test_due_exits_1_when_the_last_update_is_over_90_days_old(home, capsys, today, days, code):
    stamp(home, TODAY - timedelta(days=days))
    got, out, err = run(["career", "due"], capsys)
    assert got == code
    assert "%d days since the last update" % days in out
    assert career.days_since_update(home) == days
    assert career.due(home) == (days, code == 1)


def test_due_after_91_days_by_the_date_of_the_run(home, capsys, monkeypatch, today):
    fake_writing(monkeypatch)
    career.update(home)                        # stored as 2026-09-22
    assert run(["career", "due"], capsys)[0] == 0
    monkeypatch.setattr(career, "today", lambda: TODAY + timedelta(days=91))
    code, out, _ = run(["career", "due"], capsys)
    assert code == 1 and "91 days" in out and "awb career update" in out


def test_due_before_any_update(home, capsys, today):
    code, out, _ = run(["career", "due"], capsys)
    assert code == 0 and "nothing is due" in out
    assert career.days_since_update(home) is None
    career.add(**ENTRY, day=(TODAY - timedelta(days=100)).isoformat(), p=home)
    code, out, _ = run(["career", "due"], capsys)
    assert code == 1 and "100 days since the oldest entry" in out


def test_a_malformed_career_line_is_refused_by_number_only(home, capsys, today):
    career.add(**ENTRY, p=home)
    with (home.shared / "career.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": fixtures.CUSTOMER_FORMS[0]}) + "\n")
    for argv in (["career", "due"], ["career", "update"]):
        code, out, err = run(argv, capsys)
        assert code == 2 and "career.jsonl line 2" in err
        fixtures.assert_no_fixture_name(out + err, "career output")
    assert not (home.shared / "career-updated").exists()
