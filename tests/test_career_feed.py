"""B-115: `awb career feed`, the weekly evidence file for the career project (awb/career.py).

An invented ledger in a throw-away Workbench: two customer entries, one training and one build week. Every name here
is an invented fixture name; the customer code, the project codes, the tenant, the address and the amount are put
into the ledger texts on purpose, so that the test sees them leave the file.
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from awb import career, cli, codes, ledger, projects
from tests import fixtures

FIRST = date(2026, 12, 4)
SECOND = date(2026, 12, 11)
LAB = "tcp-a2bc"
BACKUP = "tcp-d3ef"
TENANT = "tcp-" + "00001"


@pytest.fixture
def planted(home):
    folder = home.projects_root / LAB
    folder.mkdir(parents=True)
    (folder / "STATE.md").write_text("# State\n\nStatus: waiting on the customer\nSector: logistics, 250 to 999 "
                                     "employees\n", encoding="utf-8")
    projects._save(home, [projects.Project(LAB, "project", fixtures.CUSTOMER_CODE, "tcp", str(folder),
                                           projects.memory_key(folder), "active", "2026-11-02")])
    ledger.add("proof", "Built a landing zone proof in %s for %s on %s" % (LAB, fixtures.CUSTOMER_CODE, TENANT),
               project=LAB, customer=fixtures.CUSTOMER_CODE, tags=["ecs", "iam"], outcome="Setup time cut to one hour",
               deliverables=["deliverables/lz.md"], day="2026-11-03", p=home)
    ledger.add("pricing", "Priced a backup target at 1200 EUR per month behind 10.20.30.40", project=BACKUP,
               customer=fixtures.CUSTOMER_CODE, tags=["cbr", "pricing"], day="2026-11-10", p=home)
    ledger.add("training", "Gave a workshop on IAM to six colleagues and handed over the playbook", tags=["iam"],
               day="2026-11-17", p=home)
    ledger.add("code", "Build week of the Workbench, twelve tasks merged", tags=["api"], day="2026-11-24", p=home)
    return home


def run_feed(home, day, **kw):
    return career.feed(home, day=day, repo=None, **kw)


def records(text: str) -> dict[str, list[tuple[str, str]]]:
    """id -> [(field, value)] of every record of a feed file."""
    out = {}
    for block in text.split("\n## ")[1:]:
        head, *lines = block.strip("\n").split("\n")
        if not head.startswith("ev-"):
            continue
        out[head] = [tuple(line.split(": ", 1)) for line in lines]
    return out


def changes(text: str) -> str:
    return text.split("## Changes since the last feed", 1)[1].split("\n## ev-", 1)[0]


def by_claim(recs: dict, word: str) -> tuple[str, dict]:
    for rid, fields in recs.items():
        d = dict(fields)
        if word in d["claim"]:
            return rid, d
    raise AssertionError("no record with the word")


def test_the_ledger_gives_records_with_every_field_and_stable_ids(planted):
    res = run_feed(planted, FIRST)
    assert res.path == planted.shared / "career-feed" / "cv-evidence-2026-12-04.md"
    text = res.path.read_text(encoding="utf-8")
    recs = records(text)
    assert sorted(recs) == ["ev-001", "ev-002", "ev-003", "ev-004"]
    for rid, fields in recs.items():
        assert [k for k, _ in fields] == list(career.RECORD_FIELDS)
        d = dict(fields)
        assert d["id"] == rid and d["answers"] == "feed"
        assert d["status"] in career.STATUSES and d["scope"] in career.SCOPES and d["role"] in career.ROLES
        assert all(v.strip() for v in d.values())
        assert d["changed"] == "new"
        assert d["measured"] == "2026-12-04, changes weekly no"
        assert d["verify"] == "ask"
        assert "ledger entries" in d["counted-by"]
    lab_id, lab = by_claim(recs, "landing zone")
    assert lab["status"] == "PoC" and lab["scope"] == "customer" and lab["role"] == "built"
    assert lab["sector"] == "logistics, 250 to 999 employees"
    assert lab["claim"] == "Built a landing zone proof in a project for a customer on a tenant"
    assert lab["target"] == "4" and lab["period"] == "2026-11-03"
    assert lab["cv-line"] == ("Built a landing zone proof in a project for a customer on a tenant (customer work, "
                              "PoC, 2026-11).")
    _, price = by_claim(recs, "backup")
    assert price["status"] == "proposal" and price["sector"] == "not recorded" and price["target"] == "3"
    assert "10.20.30.40" not in text and "1200" not in text and "an amount" in price["claim"]
    _, training = by_claim(recs, "workshop")
    assert training["scope"] == "internal" and training["role"] == "trained" and training["target"] == "1, 2, 4"
    _, week = by_claim(recs, "Build week")
    assert week["scope"] == "tool" and week["status"] == "ongoing" and week["target"] == "none"
    assert "- new: ev-001, ev-002, ev-003, ev-004" in changes(text)
    assert "Last feed: none" in text

    again = run_feed(planted, SECOND)
    text2 = again.path.read_text(encoding="utf-8")
    recs2 = records(text2)
    assert {rid: dict(f)["claim"] for rid, f in recs2.items()} == {rid: dict(f)["claim"] for rid, f in recs.items()}
    assert all(dict(f)["changed"] == "no" for f in recs2.values())
    assert "- no change" in changes(text2) and "Last feed: 2026-12-04." in text2


def test_a_status_change_is_listed_with_its_id_and_changed_is_filled(planted):
    first = records(run_feed(planted, FIRST).path.read_text(encoding="utf-8"))
    lab_id, lab = by_claim(first, "landing zone")
    assert lab["status"] == "PoC"
    ledger.add("tender", "Answered the tender for the landing zone", project=LAB, customer=fixtures.CUSTOMER_CODE,
               tags=["tender"], day="2026-12-08", p=planted)
    text = run_feed(planted, SECOND).path.read_text(encoding="utf-8")
    recs = records(text)
    d = dict(recs[lab_id])
    assert d["status"] == "proposal"
    assert d["changed"] == "2026-12-08, was PoC"
    assert d["period"] == "2026-11-03 to 2026-12-08" and d["numbers"] == "ledger entries 2; deliverables 1"
    assert "- %s: status from PoC to proposal on 2026-12-08" % lab_id in changes(text)
    assert "- %s: ledger entries from 1 to 2, counted the same way" % lab_id in changes(text)
    assert "new:" not in changes(text)
    assert all(dict(f)["changed"] == "no" for rid, f in recs.items() if rid != lab_id)


def test_a_ledger_line_with_a_registered_name_stops_the_file_and_keeps_the_state_beside_the_folder(planted, capsys,
                                                                                                    monkeypatch):
    """Replaces test_a_ledger_line_with_a_registered_name_stops_the_file_with_the_field_named (G13 of the review of
    2026-11-28): the state of the feed is `<shared>/career-state.json` now, outside the folder that is handed over.
    The asserts are the same."""
    run_feed(planted, FIRST)
    state = (planted.shared / "career-state.json").read_text(encoding="utf-8")
    ledger.add("inquiry", "Answered a question of %s on storage" % fixtures.CUSTOMER_FORMS[1], day="2026-12-07",
               p=planted)
    with pytest.raises(career.FeedStopped) as err:
        run_feed(planted, SECOND)
    msg = str(err.value)
    assert "field claim" in msg and "name" in msg
    fixtures.assert_no_fixture_name(msg, "the message")
    assert not (planted.shared / "career-feed" / "cv-evidence-2026-12-11.md").exists()
    assert (planted.shared / "career-state.json").read_text(encoding="utf-8") == state

    monkeypatch.setattr(career, "today", lambda: SECOND)
    monkeypatch.setattr(career, "REPO_ROOT", None)
    out = planted.shared / "elsewhere.md"
    code = cli.main(["career", "feed", "--out", str(out)])
    _, stderr = capsys.readouterr()
    assert code == 1 and "field claim" in stderr and not out.exists()
    fixtures.assert_no_fixture_name(stderr, "standard error")


def test_no_line_starts_with_linkedin_line_and_no_code_tenant_or_name_appears(planted):
    ledger.add("other", "linkedin-line: a post about %s" % fixtures.PARTNER_CODE, partner=fixtures.PARTNER_CODE,
               day="2026-11-25", p=planted)
    text = run_feed(planted, FIRST).path.read_text(encoding="utf-8")
    assert not [line for line in text.splitlines() if line.lower().startswith("linkedin-line")]
    assert fixtures.CUSTOMER_CODE not in text and fixtures.PARTNER_CODE not in text
    assert codes.count_codes(text) == (0, 0)
    assert LAB not in text and BACKUP not in text and TENANT not in text
    fixtures.assert_no_fixture_name(text, "the feed")
    assert "—" not in text and not re.search(r", (?:and|or)\b", text)


def test_the_command_writes_the_file_and_the_state_beside_the_feed_folder(planted, capsys, monkeypatch):
    """Replaces test_the_command_writes_the_file_from_the_ledger_the_dataset_release_and_the_repository (G13 of the
    review of 2026-11-28): the state is `<shared>/career-state.json`, never a file of `career-feed/`. The other
    asserts are the same."""
    from awb import kbreview
    from awb.tcp import dataset

    folder = kbreview.datasets_dir(planted) / ("%s-2026-11-30" % kbreview.SLUG)
    folder.mkdir(parents=True)
    st = {"released": {"value": "2026-11-30", "counted_by": dataset.JQ_DATE},
          "facts": {"value": 412, "counted_by": dataset.JQ_FACTS},
          "facts_tested_live": {"value": 96, "counted_by": dataset.JQ_LIVE},
          "services_orderable": {"value": 79, "counted_by": dataset.JQ_SERVICES}}
    (folder / dataset.STATS_FILE).write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setattr(career, "today", lambda: FIRST)
    out = planted.shared / "feed.md"
    code = cli.main(["career", "feed", "--out", str(out)])
    stdout, stderr = capsys.readouterr()
    assert code == 0, stderr
    assert "6 records" in stdout and str(out) in stdout
    recs = records(out.read_text(encoding="utf-8"))
    _, ds = by_claim(recs, "public dataset")
    assert ds["numbers"] == "facts 412; facts tested live 96; services orderable 79"
    assert ds["verify"].endswith("repos/%s/contents/STATS.json --jq .facts.value, expected 412" % dataset.GITHUB_REPO)
    assert ds["measured"].startswith("2026-11-30, changes weekly yes, CV form: over 410 facts, 96 facts tested live")
    _, repo = by_claim(recs, "command line tool")
    assert repo["counted-by"].startswith("commits: git rev-list --count")
    assert (planted.shared / "career-state.json").is_file()
    assert not (planted.shared / "career-feed" / "state.json").exists()


def test_the_build_queue_gives_one_record_with_done_and_not_done_apart(home, tmp_path):
    from awb import build

    qdir, runs = tmp_path / "queue", tmp_path / "runs"
    qdir.mkdir()
    blocks = []
    for tid in ("B-901", "B-902", "B-903"):
        blocks.append("## %s: a task\nmodel: opus\neffort: medium\ndepends: none\npaths: awb/career.py\n"
                      "tests_replaced: none\ncheck: none\nbudget: 10\nclock: 90\nprompt: build/queue/%s.md\nend: %s\n"
                      % (tid, tid, tid))
        (qdir / ("%s.md" % tid)).write_text("the task\n", encoding="utf-8")
    (qdir / "QUEUE.md").write_text("\n".join(blocks), encoding="utf-8")
    for tid in ("B-901", "B-902"):
        build.write_state(build.new_run(tid, runs).folder, "done")
    text = career.feed(home, day=FIRST, repo=None, qdir=qdir, runs=runs).path.read_text(encoding="utf-8")
    d = dict(records(text)["ev-001"])
    assert d["numbers"] == "tasks done 2; tasks not done 1"
    assert d["status"] == "ongoing" and d["scope"] == "tool" and "awb build list" in d["counted-by"]
    assert "B-901" not in text


def stats(day: str, counted_by: str, commands: int) -> dict:
    return {"date": {"value": day, "counted_by": "date -I"},
            "commands": {"value": commands, "counted_by": counted_by},
            "modules": {"value": 199, "counted_by": "git ls-files -- 'awb/*.py' | wc -l"}}


def test_weekly_counts_carry_their_method_and_another_method_is_never_growth(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "STATS.json").write_text(json.dumps(stats("2026-12-04", "grep -c one", 274)), encoding="utf-8")
    text = career.feed(home, day=FIRST, repo=repo).path.read_text(encoding="utf-8")
    d = dict(records(text)["ev-001"])
    assert d["numbers"] == "commands 274; modules 199"
    assert d["counted-by"] == "commands: grep -c one; modules: git ls-files -- 'awb/*.py' | wc -l"
    assert d["measured"] == "2026-12-04, changes weekly yes, CV form: over 270 commands, over 190 modules (as of 2026-12-04)"
    assert d["verify"] == ("gh api -H 'Accept: application/vnd.github.raw' repos/%s/contents/STATS.json --jq "
                           ".commands.value, expected 274" % career.PUBLIC_REPO)
    assert d["scope"] == "tool" and d["status"] == "production"
    assert "commands 274; modules 199" in text and "473" not in text           # never summed

    (repo / "STATS.json").write_text(json.dumps(stats("2026-12-11", "grep -c one", 280)), encoding="utf-8")
    text = career.feed(home, day=SECOND, repo=repo).path.read_text(encoding="utf-8")
    assert "- ev-001: commands from 274 to 280, counted the same way" in changes(text)
    assert dict(records(text)["ev-001"])["changed"] == "2026-12-11, numbers changed, status unchanged"

    (repo / "STATS.json").write_text(json.dumps(stats("2026-12-18", "grep -c two", 300)), encoding="utf-8")
    text = career.feed(home, day=date(2026, 12, 18), repo=repo).path.read_text(encoding="utf-8")
    assert "- ev-001: commands is counted by another method now, the two numbers are not compared" in changes(text)
    assert "280 to 300" not in text


def test_no_file_of_the_feed_folder_holds_a_project_code(planted):
    """G13 of the attacker review of 2026-11-28: whoever takes `career-feed/` takes no map from ev-NNN to a project
    code with it. The state lives beside the folder and still gives the same ids to the next feed."""
    first = run_feed(planted, FIRST)
    folder = planted.shared / "career-feed"
    files = [f for f in folder.rglob("*") if f.is_file()]
    assert first.path in files
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not codes.PROJECT_CODE_RE.search(text) and LAB not in text and BACKUP not in text, f.name
    assert LAB in (planted.shared / "career-state.json").read_text(encoding="utf-8")
    ids = set(records(first.path.read_text(encoding="utf-8")))
    second = run_feed(planted, SECOND)
    assert set(records(second.path.read_text(encoding="utf-8"))) == ids
    assert "new:" not in changes(second.path.read_text(encoding="utf-8"))


def test_a_state_of_before_the_move_is_read_once_and_leaves_the_feed_folder(planted):
    """G13: a host that fed before 2026-11-28 has `career-feed/state.json`. The next feed keeps its ids and removes
    the file from the folder."""
    first = run_feed(planted, FIRST)
    ids = set(records(first.path.read_text(encoding="utf-8")))
    new, old = planted.shared / "career-state.json", planted.shared / "career-feed" / "state.json"
    new.rename(old)
    second = run_feed(planted, SECOND)
    assert set(records(second.path.read_text(encoding="utf-8"))) == ids
    assert "new:" not in changes(second.path.read_text(encoding="utf-8"))
    assert new.is_file() and not old.exists()
