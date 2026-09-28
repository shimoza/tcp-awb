"""awb/tcp/refresh.py: the parts of the refresh, the report and applying the results of a re-check."""
from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path

import pytest

from awb import cli, jobs, kb, projects
from awb.tcp import price, refresh
from tests import fixtures as fx
from tests.tcp_fake import FakePriceAPI

TODAY = datetime.date.today()
OLD = (TODAY - datetime.timedelta(days=400)).isoformat()
ROOT = Path(__file__).resolve().parent.parent


def entry(statement, **kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["ecs"], grade="live", cls="availability", source="live API call, eu-de")
    args.update(kw)
    return kb.add(statement, **args)


@pytest.fixture
def mirrors(tmp_path, monkeypatch) -> Path:
    base = tmp_path / "mirrors"
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    return base


def test_the_worklist_holds_the_expired_entries_with_how_to_check_them(home):
    fresh = entry("ECS flavor s3.large.2 is offered in eu-de")
    live = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    api = entry("GET /v3/{project_id}/elb/loadbalancers pages with marker and limit", checked=OLD, cls="api")
    sd = entry("Section 6.5.1 of the service description prices traffic under three items", grade="contract",
               checked=OLD, tags=["network"], source="service description, revision 17.08.2026")
    said = entry("Support answers tickets in German and English", grade="said", checked=OLD, tags=["other"])
    hcs = entry("The on-premises stack offers an object service", scope="hcs", checked=OLD, tags=["obs"])
    work = refresh.worklist(kb.load(home), TODAY)
    methods = {w["id"]: w["method"] for w in work}
    assert fresh.id not in methods
    assert methods[live.id].startswith("read-only live call (awb cloud get)")
    assert methods[api.id].endswith("else the API reference in the mirror (grade docs)")
    assert methods[sd.id].startswith("service description")
    assert methods[said.id].startswith("find a source")
    assert methods[hcs.id] == "hcs: not from this host"
    (home.kb / "bench").mkdir(parents=True, exist_ok=True)
    (home.kb / "bench" / "q.json").write_text(json.dumps([{"question": "is s3.xlarge.2 offered", "accept": [live.id]}]))
    part = refresh.part_knowledge(home, home.shared / "refresh" / "x", today=TODAY)
    assert part.overdue == 5
    assert "retrieval, bench/q.json: 1 answerable, first 1 (100%), in the first 5 1 (100%)" in part.lines
    lines = (home.shared / "refresh" / "x" / "worklist.jsonl").read_text().splitlines()
    assert {json.loads(line)["id"] for line in lines} == set(methods)


def test_projects_with_an_old_state_are_overdue(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    p1 = projects.spawn(home, "engagement", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, register_path)
    p2 = projects.spawn(home, "lab", "try the new load balancer", None, register_path)
    old = (datetime.datetime.now() - datetime.timedelta(days=30)).timestamp()
    os.utime(Path(p1.path) / "STATE.md", (old, old))
    with open(Path(p1.path) / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("- the sizing of the second cluster, waits on %s\n- the backup window\n" % fx.CUSTOMER_CODE)
    part = refresh.part_projects(home, today=TODAY, max_age=14)
    text = "\n".join(part.lines)
    assert part.overdue == 1
    assert "%s: STATE.md 30 day(s) old, 2 open item(s)  OVERDUE" % p1.code in text
    assert "%s: STATE.md 0 day(s) old, 0 open item(s)" % p2.code in text


def test_defaults_find_stale_pointers(home, mirrors, tmp_path):
    kept = entry("ECS flavor s3.large.2 is offered in eu-de")
    gone = entry("Cloud Eye keeps raw metric data for two days", tags=["ces"])
    kb.retire(gone.id, "replaced by a wider fact")
    contract = entry("Section 6.2 of the service description lists the ECS flavors", grade="contract",
                     source="service description, revision 01.06.2026", tags=["ecs"])
    history = entry("Announced dates move: CSMS was 01.07.2026 in rev. 01.06.2026 and 01.04.2027 in rev. 17.08.2026",
                    grade="contract", source="service description", tags=["kms"])
    code_root = tmp_path / "code"
    (code_root / "awb").mkdir(parents=True)
    (code_root / "seal" / "work-claude").mkdir(parents=True)
    (code_root / "awb" / "x.py").write_text("# see %s and %s and KB-ZZZZ\n" % (kept.id, gone.id))
    (code_root / "seal" / "work-claude" / "CLAUDE.md").write_text("Use `awb price` and `awb teleport`.\n")
    (code_root / "COMMANDS.md").write_text("| `awb kb find` | x |\n| `awb kb show KB-XXXX` | the placeholder |\n")
    sd = mirrors / "service-description" / "2026-08-17"
    sd.mkdir(parents=True)
    (sd / "service-description.txt").write_text("Last revised: 17.08.2026\n")
    (mirrors / "service-description" / "CURRENT").write_text("2026-08-17\n")
    part = refresh.part_defaults(home, mirrors, code_root=code_root)
    text = "\n".join(part.lines)
    assert "CLAUDE.md names commands that do not exist: teleport" in text
    assert "%s is retired since" % gone.id in text and "KB-ZZZZ is not in the knowledge base" in text
    assert kept.id not in text
    assert "%s cites revision 2026-06-01" % contract.id in text
    assert history.id not in text                     # it cites the current revision as well
    assert part.overdue == 4


def test_the_real_rules_and_guide_name_only_commands_that_exist(home, mirrors):
    part = refresh.part_defaults(home, mirrors)
    assert not any("do not exist" in line for line in part.lines), part.lines


def test_prices_compare_today_with_the_snapshot_before(home, monkeypatch):
    with FakePriceAPI() as api:
        monkeypatch.setenv("AWB_PRICE_API", api.url)
        monkeypatch.setattr(price, "SNAPSHOT_REGIONS", ("eu-de",))
        yesterday = TODAY - datetime.timedelta(days=1)
        price.write_snapshot(home, price.fetch(None, "eu-de"), "eu-de", today=yesterday)
        for r in api.records:
            if r["id"] == "TEST_EVS_SSD":
                r["priceAmount"] = "0.150000 EUR"
        part = refresh.part_prices(home, today=TODAY, job=jobs.Job("t", None, out=io.StringIO()))
    text = "\n".join(part.lines)
    assert "eu-de: against %s: before 6 records, after 6: 0 added, 0 removed, 1 price value(s) changed" \
        % yesterday.isoformat() in text
    assert "TEST_EVS_SSD  PAYG  0.120000 -> 0.150000" in text


def test_a_run_writes_the_report_and_the_command_says_what_is_overdue(home, mirrors, capsys):
    entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    assert cli.main(["refresh", "--part", "knowledge", "--part", "projects", "--no-update"]) == 1
    out = capsys.readouterr().out
    assert "knowledge  1 overdue" in out and "projects   nothing overdue" in out
    day = home.shared / "refresh" / datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    report = day / "REPORT-knowledge-projects.md"
    assert report.exists() and "## knowledge: 1 overdue" in report.read_text()
    assert not (day / "REPORT.md").exists()           # a partial run leaves the full report of the day alone


# --------------------------------------------------------------------------- apply


def results(tmp_path, rows) -> Path:
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_apply_records_every_verdict_through_the_checks(home, tmp_path, capsys):
    a = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    b = entry("CSS in eu-de offers two Elasticsearch versions", checked=OLD, tags=["other"])
    c = entry("DMS in eu-de offers Kafka 2.3 only", checked=OLD, tags=["other"])
    d = entry("The docs list a dedicated flavor", checked=OLD, grade="said", tags=["other"])
    rows = [
        {"id": a.id, "verdict": "confirmed", "grade": "live", "source": "GET /v1/{project_id}/cloudservers/flavors"},
        {"id": b.id, "verdict": "changed", "grade": "live", "source": "GET of the CSS versions",
         "statement": "CSS in eu-de offers three Elasticsearch versions"},
        {"id": c.id, "verdict": "refuted", "grade": "live", "source": "GET of the DMS versions",
         "why": "refuted: the version list holds 2.7 and 3.x", "statement": "DMS in eu-de offers Kafka 2.7 and 3.x"},
        {"id": d.id, "verdict": "unchecked", "why": "no source found"},
        {"id": "KB-ZZZZ", "verdict": "confirmed", "grade": "docs", "source": "x"},
        {"id": a.id, "verdict": "confirmed", "grade": "docs", "source": "again"},
    ]
    assert cli.main(["refresh", "apply", str(results(tmp_path, rows))]) == 1
    out = capsys.readouterr().out
    assert kb.get(a.id, home).checked == TODAY.isoformat()
    changed = kb.get(b.id, home)
    assert changed.statement.endswith("three Elasticsearch versions") and changed.checked == TODAY.isoformat()
    assert kb.get(c.id, home).is_retired
    assert "the corrected fact added as KB-" in out
    assert kb.get(d.id, home).checked == OLD and "left as it is: no source found" in out
    assert "KB-ZZZZ  confirmed  refused" in out and "skipped: a second result for the same id" in out


def test_a_batch_that_confirmed_a_planted_false_fact_is_not_applied(home, tmp_path, capsys):
    a = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    b = entry("ECS flavor s3.large.2 is offered in eu-nl", checked=OLD)
    rows = [
        {"id": "PLANT-1", "verdict": "confirmed", "batch": "batch-01"},
        {"id": a.id, "verdict": "confirmed", "grade": "live", "source": "flavor list", "batch": "batch-01"},
        {"id": "PLANT-2", "verdict": "refuted", "batch": "batch-02", "why": "false"},
        {"id": b.id, "verdict": "confirmed", "grade": "live", "source": "flavor list", "batch": "batch-02"},
    ]
    plants = tmp_path / "plants.json"
    plants.write_text(json.dumps({"PLANT-1": "batch-01", "PLANT-2": "batch-02"}))
    assert cli.main(["refresh", "apply", str(results(tmp_path, rows)), "--plants", str(plants)]) == 1
    out = capsys.readouterr().out
    assert "PLANT-1 (batch-01): CONFIRMED although false: nothing of batch-01 is applied" in out
    assert "PLANT-2 (batch-02): caught (refuted)" in out
    assert kb.get(a.id, home).checked == OLD
    assert kb.get(b.id, home).checked == TODAY.isoformat()


def test_results_of_the_wrong_shape_are_refused(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "KB-AAAA", "verdict": "maybe"}\n')
    assert cli.main(["refresh", "apply", str(bad)]) == 2
    assert "needs an id and a verdict" in capsys.readouterr().err
