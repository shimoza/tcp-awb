"""The questions count (T13): the stop hook counts the replies that end with a question, per project and day, and
`awb report --by questions` prints the monthly counts. Counts only, never a word of a reply."""
from __future__ import annotations

import datetime
import io
import json
import sys
from pathlib import Path

import pytest

from awb import hooks, ledger, questions
from tests import fixtures as fx

CODE = "tcp-q7m4"


def transcript(tmp_path: Path, text: str, name: str = "session.jsonl") -> Path:
    t = tmp_path / name
    lines = [{"type": "user", "message": {"content": "hello"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return t


def run_stop(payload: dict, monkeypatch, capsys) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main(["stop"])
    capsys.readouterr()
    return code


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n" % (CODE, CODE), encoding="utf-8")
    return root


@pytest.mark.parametrize("reply, asked", [
    ("The sizing is done. I take two zones unless you say otherwise. Shall I go on?", True),
    ("Shall I go on with the network?**", True),
    ("Shall I go on?\n\nEnglish note: \"more cheaper\" should be \"cheaper\".", True),
    ("The sizing is done.", False),
    ("Is the zone ready? Yes, it is.", False),
    ("Run this:\n\n```bash\nread -p 'Go on?' answer\necho done?\n```", False),
    ("Run this:\n\n```\nwhich zone?\n", False),                    # a code block left open to the end
    ("", False),
])
def test_the_counter_sees_a_question_at_the_end_and_not_one_inside_a_code_block(reply, asked):
    assert questions.ends_with_question(reply) is asked


def test_the_stop_hook_counts_replies_and_questions_per_project_and_day(home, project, tmp_path, monkeypatch,
                                                                       capsys):
    asked = transcript(tmp_path, "Two zones are set. Shall I size the third?", "a.jsonl")
    told = transcript(tmp_path, "Two zones are set.\n\n```\nwhy?\n```", "b.jsonl")
    for t in (asked, asked, told):
        assert run_stop({"cwd": str(project), "transcript_path": str(t)}, monkeypatch, capsys) == 0
    assert run_stop({"cwd": str(tmp_path), "transcript_path": str(asked)}, monkeypatch, capsys) == 0
    today = datetime.date.today().isoformat()
    rows = questions.counts_file(home).read_text(encoding="utf-8").splitlines()
    assert rows[0] == "date\tproject\treplies\tquestions"
    assert sorted(rows[1:]) == ["%s\tnone\t1\t1" % today, "%s\t%s\t3\t2" % (today, CODE)]
    assert questions.monthly(home) == [(today[:7], 4, 3)]


def test_awb_report_by_questions_prints_the_monthly_counts(home, capsys):
    questions.add(home, CODE, True, datetime.date(2026, 9, 30))
    questions.add(home, CODE, False, datetime.date(2026, 9, 30))
    questions.add(home, "none", True, datetime.date(2026, 10, 1))
    assert ledger.main(["report", "--by", "questions"]) == 0
    out = capsys.readouterr().out
    assert "| 2026-09 | 2 | 1 | 50% |" in out and "| 2026-10 | 1 | 1 | 100% |" in out


def test_the_counter_writes_counts_only_never_a_name_or_a_path(home, tmp_path, monkeypatch, capsys):
    """A planted name in the reply and a folder that is no project code: neither reaches the file or the report."""
    name = fx.CUSTOMER_FORMS[0]
    odd = tmp_path / ("%s-work" % name.split()[0])
    odd.mkdir()
    (odd / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
    t = transcript(tmp_path, "Shall I write to %s about the zones?" % name)
    assert run_stop({"cwd": str(odd), "transcript_path": str(t)}, monkeypatch, capsys) == 0
    questions.add(home, name, True)                      # a caller that passes a name gets "none" written
    text = questions.counts_file(home).read_text(encoding="utf-8")
    assert "\tnone\t2\t2" in text
    assert ledger.main(["report", "--by", "questions"]) == 0
    out = capsys.readouterr().out
    for form in fx.ALL_REGISTERED:
        assert form not in text and form not in out
    assert str(tmp_path) not in text
