"""XG: a neutral file name is proposed when the name check refuses a deliverable's name, and the review tier of a
project is set once, in its SCOPE.md. Every name comes from tests/fixtures.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, projects, review, start
from awb.tcp import xchg
from tests import fixtures as fx
from tests.test_review_board import REQUEST, TEXT, reviewed, spawn

GOAL = "move two app clusters to managed k8s"
FULL = fx.CUSTOMER_FORMS[0]
GERMAN = "der plan ist nicht fertig und die werte sind von dem alten system und werden geprueft\n"
RUSSIAN = "план не готов, значения взяты из старой системы и проверяются\n"


def run(argv, capsys):  # noqa
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def scope_of(folder: Path) -> str:
    return (folder / "SCOPE.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------- XG2: the tier


def test_xg2_a_spawn_without_a_tier_writes_no_line_and_the_project_tier_is_3(home, register_path):
    folder = spawn(home, register_path)
    assert "review-tier" not in scope_of(folder)
    assert review.project_tier(folder) == 3


def test_xg2_a_spawn_with_a_tier_writes_it_into_the_scope(home, register_path):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path, review_tier=2)
    assert "- review-tier: 2\n" in scope_of(Path(pr.path))
    assert review.project_tier(Path(pr.path)) == 2
    with pytest.raises(projects.ProjectError):
        projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path, review_tier=4)


def test_xg2_the_spawn_command_and_awb_start_take_the_tier(home, capsys):
    assert "--review-tier" in start.spawn_argv("project", GOAL, None, [], review_tier=2)
    assert "--review-tier" not in start.spawn_argv("project", GOAL, None, [])
    code, out, _ = run(["spawn", "project", "--goal", GOAL, "--review-tier", "1"], capsys)
    assert code == 0
    folder = Path(out.split(": ", 2)[-1].strip().splitlines()[0])
    assert review.project_tier(folder) == 1
    assert run(["spawn", "project", "--goal", GOAL, "--review-tier", "4"], capsys)[0] == 2


def test_xg2_awb_projects_tier_changes_the_line_and_reports_the_old_value(home, register_path, capsys, monkeypatch):
    folder = spawn(home, register_path)
    monkeypatch.setenv("CLAUDECODE", "1")
    code, _, err = run(["projects", "tier", folder.name, "1"], capsys)
    assert code == 2 and "owner" in err and review.project_tier(folder) == 3
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    code, out, _ = run(["projects", "tier", folder.name, "2"], capsys)
    assert code == 0 and "is 2 (was 3)" in out and review.project_tier(folder) == 2
    code, out, _ = run(["projects", "tier", folder.name, "1"], capsys)
    assert "is 1 (was 2)" in out and scope_of(folder).count("review-tier") == 1
    assert run(["projects", "tier", folder.name, "0"], capsys)[0] == 2
    assert run(["projects", "tier", "tcp-zzzz", "2"], capsys)[0] == 2


def test_xg2_a_contract_takes_the_tier_of_its_project_and_the_owner_may_raise_it(home, register_path, tmp_path):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path, review_tier=2)
    folder = Path(pr.path)
    d = folder / "deliverables" / "reply.md"
    d.write_text(TEXT, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    review.init(d, req, None, 400)
    assert review.load_contract(review.review_dir(folder, "reply.md") / review.CONTRACT, "tcp").tier == 2
    review.init(d, req, 3, 400, force=True)
    assert review.load_contract(review.review_dir(folder, "reply.md") / review.CONTRACT, "tcp").tier == 3


def test_xg2_the_send_gate_keeps_refusing_below_the_project_tier(home, register_path, tmp_path):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path, review_tier=2)
    folder = Path(pr.path)
    at2 = reviewed(folder, tmp_path, 2, name="two.md")
    assert review.send_check(folder, at2, True)[0] == review.SEND_OK
    at1 = reviewed(folder, tmp_path, 1, name="one.md")
    verdict, message = review.send_check(folder, at1, True)
    assert verdict == review.SEND_REFUSE and "needs tier 2" in message
    low = reviewed(folder, tmp_path, 1, name="low.md", lowered="his words: tier one is enough here")
    assert review.send_check(folder, low, True)[0] == review.SEND_OK


def test_xg2_a_project_without_the_line_still_needs_tier_3(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    at2 = reviewed(folder, tmp_path, 2, name="two.md")
    verdict, message = review.send_check(folder, at2, True)
    assert verdict == review.SEND_REFUSE and "needs tier 3" in message


def test_xg2_a_tier_the_project_does_not_name_cannot_lower_the_gate(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    (folder / "SCOPE.md").write_text(scope_of(folder) + "- review-tier: 0\n- review-tier: 7\n", encoding="utf-8")
    assert review.project_tier(folder) == 3


# ----------------------------------------------------------------------------------------------- XG1: the name


def test_xg1_the_name_check_itself_is_unchanged_and_a_neutral_name_passes_it(home, register_path, monkeypatch):
    pr = projects.spawn(home, "project", GOAL, None, register_path)
    monkeypatch.chdir(pr.path)
    assert xchg.name_problems("%s-mapping-de.md" % pr.code) == []
    assert xchg.name_problems("mapping %s.md" % FULL)
    assert review.propose_name(Path(pr.path), "x.md", kind_word="Bad Word") is None
    assert review.propose_name(Path(pr.path), "x.md", lang="deu") is None
    assert review.propose_name(Path(pr.path).parent, "x.md") is None
    assert review.propose_name(Path(pr.path), "x.tar.%s" % ("z" * 9)).endswith("-doc-en")


def test_xg1_a_review_command_on_a_refused_name_proposes_the_name_too(home, register_path, tmp_path, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    folder = Path(pr.path)
    d = folder / "deliverables" / ("offer %s.md" % FULL)
    d.write_text(GERMAN, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    code, _, err = run(["review", "init", str(d), "--request", str(req), "--budget", "400"], capsys)
    assert code == 1 and "rename the file to %s-doc-de.md" % pr.code in err
    fx.assert_no_fixture_name(err, "the review output")


def test_xg1_the_session_template_and_the_rules_tell_the_session_to_name_deliverables_so(home, register_path):
    pr = projects.spawn(home, "project", GOAL, None, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "%s-<kind word>-<language>.<ext>" % pr.code in text
    assert len(text.splitlines()) <= projects.MAX_CLAUDE_LINES
    rules = projects.RULES_FILE.read_text(encoding="utf-8")
    assert "<code>-<kind word>-<language>.<ext>" in rules
