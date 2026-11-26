"""Lesson cards (B-095, DESIGN 8, steps L1 and L5): a pattern card of kind `lesson` takes a sort, is graded said and is
best before 90 days after its date; the knowledge base first block shows the lessons of other projects as leads."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from awb import cli, kb, kbfirst, kbpattern
from tests.test_kbpattern import CODE, OTHER, add, project

TODAY = datetime.date.today()
LONG_AGO = (TODAY - datetime.timedelta(days=100)).isoformat()
GOTCHA = "A bucket policy for temporary credentials takes effect at once while an IAM grant needs many minutes."
PROMPT = "the bucket list answers 403 with temporary credentials, which policy takes effect at once?"
FACT = "The bucket answers 403 with temporary credentials until the policy takes effect."


def lesson(home, statement=GOTCHA, *, proj=None, sub="gotcha", **kw):
    return add(home, statement, kind="lesson", tags=["obs"], sub=sub, proj=proj, **kw)


def run(argv, capsys):
    code = cli.main(["kb", "pattern", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_a_lesson_without_sub_is_a_gotcha_and_says_so(home, capsys):
    card = add(home, GOTCHA, kind="lesson", tags=["obs"])
    assert card.sub == "gotcha" and card.grade == "said"
    code, out, _ = run(["add", "--package", "cad-on-tcp", "--kind", "lesson", "--tag", "obs",
                        "--project", str(project(home)), "--force-new", "A second lesson with no sort."], capsys)
    assert code == 0 and "lesson/gotcha" in out
    said = [l for l in out.splitlines() if l.startswith("no --sub given")]
    assert said == ["no --sub given: the lesson is a gotcha; say --sub dead-end|working-path|gotcha|decision when "
                    "it is another sort"]
    code, out, _ = run(["add", "--package", "cad-on-tcp", "--kind", "lesson", "--sub", "dead-end", "--tag", "obs",
                        "--project", str(project(home)), "--force-new", "A third lesson of a named sort."], capsys)
    assert code == 0 and "no --sub given" not in out


def test_an_unknown_sub_and_a_step_with_sub_are_refused(home):
    with pytest.raises(kbpattern.PatternError, match="--sub"):
        add(home, GOTCHA, kind="lesson", tags=["obs"], sub="trap")
    with pytest.raises(kbpattern.PatternError, match="--sub"):
        add(home, GOTCHA, kind="step", tags=["obs"], sub="gotcha")
    assert kbpattern.load(home) == []


def test_a_decision_needs_why_and_no_other_sort_takes_it(home):
    with pytest.raises(kbpattern.PatternError, match="--why"):
        lesson(home, sub="decision")
    with pytest.raises(kbpattern.PatternError, match="--why"):
        lesson(home, sub="gotcha", reason="because")
    card = lesson(home, "We keep the bucket policy as the first way to give temporary credentials access.",
                  sub="decision", reason="An IAM grant took many minutes to work.")
    assert card.sub == "decision" and card.reason == "An IAM grant took many minutes to work."
    assert kbpattern.get(card.id, home) == card


def test_a_lesson_card_has_grade_said_and_an_end_90_days_after_its_date(home, capsys):
    card = lesson(home, checked=LONG_AGO)
    end = (datetime.date.fromisoformat(LONG_AGO) + datetime.timedelta(days=90)).isoformat()
    assert (card.kind, card.sub, card.grade, card.until) == ("lesson", "gotcha", "said", end)
    text = (home.kb / "patterns" / (card.id + ".md")).read_text(encoding="utf-8")
    assert "sub: gotcha\ngrade: said\nuntil: %s\n" % end in text
    assert kbpattern.parse(text, card.id) == card
    # a card of another kind carries none of it
    other = add(home, "An inventory line with 8 GB maps to a flavor of the g7v family.", kind="sizing-rule")
    assert (other.sub, other.grade, other.until) == ("", "", "")
    assert not other.is_lapsed()
    # the command
    code, out, err = run(["add", "--package", "cad-on-tcp", "--kind", "lesson", "--sub", "dead-end", "--tag", "obs",
                          "--project", str(project(home)), "--force-new",
                          "An IAM grant for temporary credentials is a dead end for a quick test."], capsys)
    assert code == 0 and "lesson/dead-end" in out and "grade said, best before %s" % (
        TODAY + datetime.timedelta(days=90)).isoformat() in out
    code, _, err = run(["add", "--package", "cad-on-tcp", "--kind", "lesson", "--sub", "decision", "--tag", "obs",
                        "--project", str(project(home)), "A decision with no reason."], capsys)
    assert code == 2 and "--why" in err


def test_a_card_past_its_end_is_not_found_without_all(home, capsys):
    old = lesson(home, checked=LONG_AGO)
    assert old.is_lapsed()
    assert kbpattern.find("bucket policy temporary credentials", home) == []
    assert [c.id for _, c in kbpattern.find("bucket policy temporary credentials", home, include_lapsed=True)] \
        == [old.id]
    code, out, _ = run(["find", "bucket", "policy"], capsys)
    assert code == 0 and old.id not in out
    code, out, _ = run(["list"], capsys)
    assert code == 0 and old.id not in out
    code, out, _ = run(["find", "--all", "bucket", "policy"], capsys)
    assert old.id in out and "LAPSED" in out
    code, out, _ = run(["list", "--all"], capsys)
    assert old.id in out
    fresh = lesson(home, "A bucket policy answers at once where an IAM grant for credentials needs minutes.")
    code, out, _ = run(["find", "bucket", "policy"], capsys)
    assert fresh.id in out and old.id not in out


def test_the_kbfirst_block_shows_a_lesson_of_another_project_under_its_line(home, register_path):
    own_root = project(home)
    kb.add(FACT, scope="tcp", tags=["obs"], grade="live", cls="stable", source="an invented lab run",
           where=home, register_path=register_path, today=TODAY)
    card = lesson(home, proj=project(home, OTHER))
    said = kbfirst.block(home, "lesson-session", own_root, PROMPT, now=1000.0)
    lines = said.splitlines()
    assert lines[0] == kbfirst.HEAD % 1
    at = lines.index(kbfirst.LESSONS_HEAD)
    assert lines[at] == "Learned in other projects (leads, not facts)"
    assert at > 1 and lines[at + 1].startswith("%s gotcha %s: " % (card.id, TODAY.isoformat()))
    assert len(lines) == at + 2 and len(said) <= kbfirst.MAX_CHARS


def test_a_lesson_of_the_own_project_a_lapsed_one_and_a_fourth_are_left_out(home, register_path):
    own_root = project(home)
    own = lesson(home, proj=own_root, force_new=True)
    assert kbfirst.block(home, "own-only", own_root, PROMPT, now=1000.0) is None
    lapsed = lesson(home, "A bucket policy for temporary credentials is the fast way and not an IAM grant.",
                    proj=project(home, OTHER), checked=LONG_AGO, force_new=True)
    assert kbfirst.block(home, "lapsed-only", own_root, PROMPT, now=1000.0) is None
    # a project that supported the card of another project is its own too
    shared = lesson(home, "Temporary credentials need a bucket policy at once; an IAM grant waits many minutes.",
                    proj=project(home, OTHER), force_new=True)
    kbpattern.support(shared.id, project=own_root, where=home)
    assert kbfirst.block(home, "supported", own_root, PROMPT, now=1000.0) is None
    for i in range(4):
        lesson(home, "Case %d: a bucket policy for temporary credentials works at once while the IAM grant is "
                     "slow%s." % (i, " indeed" * i), proj=project(home, OTHER), force_new=True)
    said = kbfirst.block(home, "four", own_root, PROMPT, now=1000.0)
    lines = said.splitlines()
    ids = [l.split()[0] for l in lines[lines.index(kbfirst.LESSONS_HEAD) + 1:]]
    assert len(ids) == kbfirst.MAX_LESSONS
    assert not {own.id, lapsed.id, shared.id} & set(ids)


def test_a_prompt_that_matches_no_lesson_gets_no_lessons_line(home, register_path):
    own_root = project(home)
    lesson(home, proj=project(home, OTHER))
    assert kbfirst.block(home, "no-match", own_root, "please rename the folder of the invoices", now=1000.0) is None
