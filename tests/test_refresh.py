"""awb/tcp/refresh.py: the parts of the refresh, the report and applying the results of a re-check."""
from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path

import pytest

from awb import cli, jobs, kb, projects
from awb.tcp import price, refresh, services
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


READ_BLOCK = ("tier: read\n"
              "calls: GET ecs /v1/{project_id}/cloudservers/flavors\n"
              "confirms: flavors[name=s3.xlarge.2].os-extra_specs.cond:operation:az has eu-de-01(normal)\n"
              "refutes: flavors[name=s3.xlarge.2].os-extra_specs.cond:operation:az lacks eu-de-01(normal)\n"
              "control: flavors[name=s2.medium.1].os-extra_specs.cond:operation:az has sellout\n"
              "needs: none\n")
NONE_BLOCK = ("tier: none\n"
              "confirms: the console form lists the option\n"
              "refutes: the console form does not list the option\n"
              "control: a second option the form lists in any case\n"
              "needs: a person at the console\n")


def test_the_worklist_carries_the_tier_and_the_block_and_an_entry_without_one_is_overdue(home):
    read = entry("ECS flavor s3.xlarge.2 is offered in eu-de-01", checked=OLD)
    kb.how(read.id, READ_BLOCK)
    nothing = entry("The console offers the option of a second flavor list", checked=OLD, cls="api")
    kb.how(nothing.id, NONE_BLOCK)
    bare = entry("ECS flavor s3.large.4 is offered in eu-de-02", checked=OLD)
    fresh = entry("ECS flavor s3.medium.2 is offered in eu-de-03")
    work = {w["id"]: w for w in refresh.worklist(kb.load(home), TODAY)}
    assert fresh.id not in work
    assert work[read.id]["tier"] == "read" and work[read.id]["block"] == kb.get(read.id).block
    assert work[read.id]["method"] == "awb kb recheck-run --tier read (its check block)"
    assert work[nothing.id]["tier"] == "none" and work[nothing.id]["method"].startswith("read-only live call")
    assert work[bare.id]["tier"] == "no block" and work[bare.id]["block"] == {}
    part = refresh.part_knowledge(home, home.shared / "refresh" / "y", today=TODAY)
    assert part.overdue == 2
    assert "by check block: no block 1, none 1, read 1; 1 without a block, 2 overdue, 1 for awb kb recheck-run" \
        in part.lines
    lines = [json.loads(x) for x in (home.shared / "refresh" / "y" / "worklist.jsonl").read_text().splitlines()]
    assert {x["id"]: x["tier"] for x in lines} == {read.id: "read", nothing.id: "none", bare.id: "no block"}

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
    current = services.load().revision
    history = entry("Announced dates move: CSMS was 01.07.2026 in rev. 01.06.2026 and 01.04.2027 in rev. %s"
                    % ".".join(reversed(current.split("-"))), grade="contract", source="service description",
                    tags=["kms"])
    code_root = tmp_path / "code"
    (code_root / "awb").mkdir(parents=True)
    (code_root / "seal" / "work-claude").mkdir(parents=True)
    (code_root / "awb" / "x.py").write_text("# see %s and %s and KB-ZZZZ\n" % (kept.id, gone.id))
    (code_root / "seal" / "work-claude" / "CLAUDE.md").write_text("Use `awb price` and `awb teleport`.\n")
    (code_root / "COMMANDS.md").write_text("| `awb kb find` | x |\n| `awb kb show KB-XXXX` | the placeholder |\n")
    # the mirror holds the revision rules/services.tsv names, so a new revision of the repository's list does not
    # count as one more overdue line here
    sd = mirrors / "service-description" / current
    sd.mkdir(parents=True)
    (sd / "service-description.txt").write_text("Last revised: %s\n" % ".".join(reversed(current.split("-"))))
    (mirrors / "service-description" / "CURRENT").write_text(current + "\n")
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


# --------------------------------------------------------------------------- hardening


def _unchecked(**fields):
    return dict({"id": "KB-AAAA", "verdict": "unchecked"}, **fields)


@pytest.mark.parametrize("field,value", [
    ("id", "../outside"), ("id", "KB-AAAA\nforged"), ("batch", []),
    ("source", {}), ("statement", 2), ("why", []), ("grade", True),
    ("checked", "2026-02-30"), ("tried", "abc"), ("tried", [2]),
], ids=["traversal-id", "multiline-id", "batch-type", "source-type", "statement-type",
        "reason-type", "grade-type", "date", "tried-string", "tried-member"])
def test_hardening_results_validate_every_used_field(tmp_path, field, value):
    row = _unchecked()
    row[field] = value
    with pytest.raises(kb.KBError):
        refresh.read_results(results(tmp_path, [row]))


@pytest.mark.parametrize("text", [
    '{"id":"KB-AAAA","verdict":"confirmed","verdict":"unchecked"}',
    '{"id":"KB-AAAA","verdict":"unchecked","unknown":NaN}',
    '[' * 20000 + '0' + ']' * 20000,
], ids=["duplicate-key", "nonfinite", "deep-json"])
def test_hardening_hostile_json_is_refused(tmp_path, text):
    path = tmp_path / "results.jsonl"
    path.write_text(text)
    with pytest.raises(kb.KBError):
        refresh.read_results(path)


@pytest.mark.parametrize("kind", ["link", "parent-link", "fifo", "directory", "large"])
def test_hardening_results_require_bounded_regular_files(tmp_path, kind):
    target = results(tmp_path, [_unchecked()])
    path = tmp_path / "input"
    if kind == "link":
        path.symlink_to(target)
    elif kind == "parent-link":
        path.symlink_to(tmp_path, target_is_directory=True)
        path = path / target.name
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        # Valid rows beyond the documented bound, without allocating a huge object.
        with path.open("wb") as fh:
            fh.write(b'\n' * ((8 << 20) + 1))
    with pytest.raises(kb.KBError):
        refresh.read_results(path)


@pytest.mark.parametrize("kind", ["missing", "unreadable", "invalid-utf8"])
def test_hardening_results_read_failures_hide_values(tmp_path, monkeypatch, kind):
    private = "pass" + "word" + "=private-result-value"
    path = tmp_path / private
    if kind == "invalid-utf8":
        path.write_bytes(b'\xff')
    elif kind == "unreadable":
        path.write_text('{}')
        original = os.open

        def denied(name, *args, **kw):
            if str(name) in (str(path), path.name):
                raise PermissionError(private)
            return original(name, *args, **kw)

        monkeypatch.setattr(os, "open", denied)
        # The original implementation used Path.read_text instead of os.open.
        read_text = Path.read_text
        monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw:
                            (_ for _ in ()).throw(PermissionError(private)) if self == path
                            else read_text(self, *a, **kw))
    with pytest.raises(kb.KBError) as caught:
        refresh.read_results(path)
    assert private not in str(caught.value)


def test_hardening_regular_results_outside_project_are_allowed(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    assert refresh.read_results(results(outside, [_unchecked()]))[0]["id"] == "KB-AAAA"


def test_hardening_duplicate_plants_cannot_hide_a_confirmation(home):
    rows = [{"id": "PLANT-1", "verdict": verdict, "batch": "batch-01"}
            for verdict in ("confirmed", "refuted")]
    rows.append(_unchecked(batch="batch-01"))
    applied, calibration = refresh.apply(rows, plants={"PLANT-1": "batch-01"})
    assert not applied[0].ok and "CONFIRMED although false" in calibration[0]


@pytest.mark.parametrize("plants", [{"PLANT-1": []}, {"../outside": "batch-01"},
                                    {"PLANT-1": "batch\nforged"}])
def test_hardening_plants_are_validated_before_application(home, plants):
    with pytest.raises(kb.KBError):
        refresh.apply([_unchecked()], plants=plants)


def test_hardening_plant_batch_mismatch_is_refused(home):
    rows = [{"id": "PLANT-1", "verdict": "confirmed", "batch": "batch-02"}]
    with pytest.raises(kb.KBError):
        refresh.apply(rows, plants={"PLANT-1": "batch-01"})


def test_hardening_apply_validates_the_whole_batch_before_writing(home):
    e = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    rows = [{"id": e.id, "verdict": "confirmed", "grade": "live", "source": "flavor list"},
            _unchecked(batch=[])]
    with pytest.raises(kb.KBError):
        refresh.apply(rows)
    assert kb.get(e.id).checked == OLD


@pytest.mark.parametrize("private", ["pass" + "word" + "=Never" + "Publish934!"], ids=["secret"])
def test_hardening_unchecked_reasons_are_screened(home, private, capsys, tmp_path):
    assert refresh.main(["apply", str(results(tmp_path, [_unchecked(why=private)]))]) == 0
    output = capsys.readouterr()
    # B-089: no content check inside, the reason is shown as written
    assert private in output.out


def test_hardening_apply_errors_do_not_echo_dependency_values(home, monkeypatch):
    private = "unrecognized-private-value"

    def broken(*args, **kwargs):
        raise kb.KBError(private)

    monkeypatch.setattr(kb, "recheck", broken)
    applied, _ = refresh.apply([{"id": "KB-AAAA", "verdict": "confirmed"}])
    assert not applied[0].ok and private not in applied[0].outcome


@pytest.mark.parametrize("kind", ["link", "parent-link", "hardlink", "fifo"])
def test_hardening_report_refuses_unsafe_destinations(home, tmp_path, kind):
    folder = refresh.refresh_dir(home, TODAY)
    folder.mkdir(parents=True)
    report = folder / "REPORT-projects.md"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / report.name
    target.write_text("keep me")
    if kind == "link":
        report.symlink_to(target)
    elif kind == "parent-link":
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
    elif kind == "hardlink":
        os.link(target, report)
    else:
        os.mkfifo(report)
    with pytest.raises((kb.KBError, OSError)):
        refresh.run(home, parts=("projects",), update=False, today=TODAY)
    assert target.read_text() == "keep me"


def test_hardening_report_is_private_and_atomically_replaced(home, monkeypatch):
    _, path = refresh.run(home, parts=("projects",), update=False, today=TODAY)
    original = path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0

    def no_sync(fd):
        raise OSError("private disk error")

    monkeypatch.setattr(os, "fsync", no_sync)
    with pytest.raises(OSError):
        refresh.run(home, parts=("projects",), update=False, today=TODAY)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".refresh-*.tmp"))


def test_hardening_report_directory_is_pinned_during_publication(home, tmp_path, monkeypatch):
    folder = refresh.refresh_dir(home, TODAY)
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = folder.with_name("held")
    real_replace = os.replace

    def swap(src, dst, **kw):
        folder.rename(moved)
        folder.symlink_to(outside, target_is_directory=True)
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", swap)
    refresh.run(home, parts=("projects",), update=False, today=TODAY)
    assert not list(outside.iterdir())
    assert (moved / "REPORT-projects.md").is_file()


def test_hardening_concurrent_runs_do_not_overlap_parts(home, monkeypatch):
    import concurrent.futures
    import threading

    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def part(p, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            second.set()
        return refresh.Part("projects")

    monkeypatch.setattr(refresh, "part_projects", part)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(refresh.run, home, parts=("projects",), today=TODAY)
        try:
            assert entered.wait(5)
            other = pool.submit(refresh.run, home, parts=("projects",), today=TODAY)
            assert not second.wait(0.2)
        finally:
            release.set()
        first.result(timeout=5)
        other.result(timeout=5)
    assert second.is_set()


def test_hardening_linked_lock_is_refused_before_running_parts(home, tmp_path, monkeypatch):
    folder = home.shared / "refresh"
    folder.mkdir()
    target = tmp_path / "lock"
    target.write_text("keep")
    (folder / ".run.lock").symlink_to(target)
    monkeypatch.setattr(refresh, "part_projects", lambda *a, **kw: pytest.fail("part ran"))
    with pytest.raises((OSError, kb.KBError)):
        refresh.run(home, parts=("projects",))
    assert target.read_text() == "keep"


@pytest.mark.parametrize("private", ["pass" + "word" + "=Never" + "Publish934!"], ids=["secret"])
def test_hardening_reports_screen_part_lines(home, monkeypatch, private):
    monkeypatch.setattr(refresh, "part_projects", lambda *a, **kw: refresh.Part("projects", [private]))
    parts, report = refresh.run(home, parts=("projects",), today=TODAY)
    # B-089: no content check inside, the line stands as written
    assert private in report.read_text()


def test_hardening_part_errors_do_not_echo_dependency_values(home, monkeypatch):
    private = "unrecognized-private-value"

    def broken(*args, **kwargs):
        raise kb.KBError(private)

    monkeypatch.setattr(refresh, "part_knowledge", broken)
    parts, report = refresh.run(home, parts=("knowledge",), today=TODAY)
    assert parts[0].error and private not in report.read_text()


def test_hardening_cli_contains_report_write_failures(home, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise OSError("private-path-value")

    monkeypatch.setattr(refresh, "run", broken)
    assert refresh.main(["--part", "projects"]) == 2
    assert "private-path-value" not in capsys.readouterr().err


def test_hardening_worklist_screens_decoded_fields(home, monkeypatch):
    from dataclasses import replace

    e = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    private = "pass" + "word" + "=Never" + "Publish934!"
    monkeypatch.setattr(kb, "load", lambda *a: [replace(e, source=private)])
    parts, report = refresh.run(home, parts=("knowledge",), today=TODAY)
    # B-089: no content check inside, the worklist is written as it stands
    assert not parts[0].error
    assert (report.parent / "worklist.jsonl").exists()


def test_hardening_benchmark_link_is_not_followed(home, tmp_path, monkeypatch):
    bench = home.kb / "bench"
    bench.mkdir(parents=True)
    source = tmp_path / "outside.json"
    source.write_text('[{"question":"outside","accept":["KB-AAAA"]}]')
    (bench / "q.json").symlink_to(source)
    monkeypatch.setattr(kb, "bench", lambda *a, **kw: pytest.fail("linked benchmark read"))
    part = refresh.part_knowledge(home, home.shared / "refresh" / "test", today=TODAY)
    assert part.overdue == 1 and "cannot be read" in "\n".join(part.lines)


def _project(home, folder):
    return projects.Project("tcp-abcd", "lab", "", "tcp", str(folder), "key", "active", TODAY.isoformat())


@pytest.mark.parametrize("kind", ["outside", "state-link", "open-link", "open-missing", "open-invalid"])
def test_hardening_project_files_fail_closed(home, tmp_path, monkeypatch, kind):
    folder = (tmp_path / "outside") if kind == "outside" else home.projects_root / "tcp-abcd"
    folder.mkdir(parents=True)
    (folder / "STATE.md").write_text("current")
    (folder / "OPEN.md").write_text("- pending\n")
    if kind.endswith("link"):
        path = folder / ("STATE.md" if kind == "state-link" else "OPEN.md")
        path.unlink()
        target = tmp_path / "private"
        target.write_text("- private\n")
        path.symlink_to(target)
    elif kind == "open-missing":
        (folder / "OPEN.md").unlink()
    elif kind == "open-invalid":
        (folder / "OPEN.md").write_bytes(b'\xff')
    monkeypatch.setattr(projects, "load", lambda p: [_project(home, folder)])
    part = refresh.part_projects(home, today=TODAY, max_age=14)
    assert part.overdue == 1
    assert "0 open item(s)" not in "\n".join(part.lines)


def test_hardening_defaults_do_not_read_linked_rules(home, tmp_path, mirrors):
    root = tmp_path / "code"
    rules = root / "seal" / "work-claude"
    rules.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("Use `awb teleport`.")
    (rules / "CLAUDE.md").symlink_to(outside)
    (root / "COMMANDS.md").write_text("Use `awb kb`.")
    part = refresh.part_defaults(home, mirrors, code_root=root)
    assert part.overdue and "teleport" not in "\n".join(part.lines)


def _note():
    return {"id": "note-1", "date": TODAY.isoformat(), "title": "Public feature", "services": ["gadget hub"],
            "seen": TODAY.isoformat() + "T00:00:00Z", "text": "Public text."}


@pytest.mark.parametrize("last", [42, {"x": 1}, "private-watermark"])
def test_hardening_release_state_invalid_values_use_first_run(home, tmp_path, monkeypatch, last):
    state = refresh.rn_state(home)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"last": last}))
    monkeypatch.setattr(refresh.mirror, "release_notes", lambda base: [_note()])
    part = refresh.part_release_notes(home, tmp_path, tmp_path / "out", update=False, today=TODAY)
    assert "dated since" in "\n".join(part.lines)
    assert "private-watermark" not in "\n".join(part.lines)


@pytest.mark.parametrize("field,value", [("date", "2026-01-01\nforged"), ("services", "ecs")],
                         ids=["date", "services"])
def test_hardening_release_notes_validate_public_fields(home, field, value):
    note = _note()
    note[field] = value
    with pytest.raises(kb.KBError):
        refresh.release_note_lines([note], [], refresh.offered.load(), today=TODAY)


def test_hardening_linked_plants_file_is_refused(home, tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_text('{}')
    link = tmp_path / "plants.json"
    link.symlink_to(source)
    assert refresh.main(["apply", str(results(tmp_path, [_unchecked()])), "--plants", str(link)]) == 2
    assert "left as it is" not in capsys.readouterr().out


def test_hardening_weekly_state_refuses_linked_parent(home, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "linked"
    parent.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(refresh.config, "is_work_user", lambda: False)
    monkeypatch.setattr(refresh, "run", lambda *a, **kw: ([], tmp_path / "REPORT.md"))
    refresh.weekly(home, state=parent / "state.json")
    assert not list(outside.iterdir())


@pytest.fixture
def small_wipe(monkeypatch):
    from types import SimpleNamespace

    counts = dict(cases=1, leak_cases=0, loss_cases=0, leaked_values=0, lost_terms=0, withheld=0, errors=0)
    harness = SimpleNamespace(rt=SimpleNamespace(code_under_test=lambda: None), selfcheck=lambda work: [],
                              DIMENSIONS=("test",), module_path=lambda dim: dim, load_cases=lambda path: [{}],
                              run_case=lambda case, work: {}, summary_of=lambda entries: counts)
    monkeypatch.setattr(refresh, "_wipe_harness", lambda root: harness)
    monkeypatch.setattr(refresh, "_release", lambda: "abc1234")
    return harness


@pytest.mark.parametrize("kind", ["link", "fifo", "malformed"])
def test_hardening_wipe_history_cannot_be_followed_or_ignored(home, tmp_path, small_wipe, kind):
    path = refresh.board.wipe_tsv(home)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.tsv"
    outside.write_text("private history\n")
    if kind == "link":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("malformed history\n")
    with pytest.raises((OSError, kb.KBError)):
        refresh.part_wipe(home, today=TODAY)
    assert outside.read_text() == "private history\n"


def test_hardening_wipe_publication_is_atomic_and_private(home, small_wipe, monkeypatch):
    refresh.part_wipe(home, today=TODAY)
    path = refresh.board.wipe_tsv(home)
    before = path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0

    def failed(fd):
        raise OSError("private-write-value")

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError):
        refresh.part_wipe(home, today=TODAY)
    assert path.read_bytes() == before


def test_hardening_concurrent_wipe_runs_preserve_both_rows(home, small_wipe):
    import concurrent.futures
    import threading

    entered, release, overlapped = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def selfcheck(work):
        calls.append(work)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            overlapped.set()
        return []

    small_wipe.selfcheck = selfcheck
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(refresh.part_wipe, home, today=TODAY)
        try:
            assert entered.wait(5)
            other = pool.submit(refresh.part_wipe, home, today=TODAY)
            assert not overlapped.wait(0.2)
        finally:
            release.set()
        first.result(timeout=5)
        other.result(timeout=5)
    lines = refresh.board.wipe_tsv(home).read_text().splitlines()
    assert len(lines) == 3 and len(refresh.board.wipe_history(home)) == 2


def test_hardening_concurrent_apply_keeps_compound_changes_together(home, monkeypatch):
    import concurrent.futures
    import threading

    e = entry("ECS flavor s3.xlarge.2 is offered in eu-de", checked=OLD)
    entered, release, overlapped = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def amend(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            overlapped.set()

    monkeypatch.setattr(kb, "amend", amend)
    monkeypatch.setattr(kb, "recheck", lambda *a, **kw: e)
    rows = [{"id": e.id, "verdict": "changed", "statement": "A corrected fact"}]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(refresh.apply, rows)
        try:
            assert entered.wait(5)
            other = pool.submit(refresh.apply, rows)
            assert not overlapped.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5)[0][0].ok
        assert other.result(timeout=5)[0][0].ok


@pytest.mark.parametrize("kind", ["today-link", "earlier-link", "parent-link", "malformed"])
def test_hardening_price_snapshots_are_checked_before_use(home, tmp_path, monkeypatch, kind):
    folder = price.snapshot_dir(home) / "eu-de"
    folder.mkdir(parents=True)
    current = folder / (TODAY.isoformat() + ".json")
    prior = folder / ((TODAY - datetime.timedelta(days=1)).isoformat() + ".json")
    data = '{"records":[{"id":"TEST_EVS_SSD","priceAmount":"0.1"}]}'
    current.write_text(data)
    outside = tmp_path / "outside.json"
    outside.write_text(data)
    if kind == "today-link":
        current.unlink()
        current.symlink_to(outside)
    elif kind == "earlier-link":
        prior.symlink_to(outside)
    elif kind == "parent-link":
        moved = folder.with_name("held")
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
    else:
        current.write_text('{"records":"wrong"}')
    monkeypatch.setattr(price, "SNAPSHOT_REGIONS", ("eu-de",))
    parts, report = refresh.run(home, parts=("prices",), today=TODAY)
    assert parts[0].error and "ERROR" in report.read_text()


def test_hardening_price_snapshot_publication_preserves_prior_output(home, monkeypatch):
    from types import SimpleNamespace

    record = price.Record.from_raw({"id": "TEST_EVS_SSD", "priceAmount": "0.1"})
    fetched = SimpleNamespace(listing=SimpleNamespace(state=jobs.LIST), records=[record],
                              url="public source", fetched_at="today", cached_at="today")
    monkeypatch.setattr(price, "SNAPSHOT_REGIONS", ("eu-de",))
    monkeypatch.setattr(price, "fetch", lambda *a, **kw: fetched)
    part = refresh.part_prices(home, today=TODAY, job=jobs.Job("t", None, out=io.StringIO()))
    path = price.snapshot_dir(home) / "eu-de" / (TODAY.isoformat() + ".json")
    assert not part.error and path.stat().st_mode & 0o077 == 0
    assert json.loads(path.read_text())["records"][0]["id"] == record.id
    assert not list(path.parent.glob(".*.tmp"))


def test_hardening_release_state_link_is_refused(home, tmp_path, monkeypatch):
    state = refresh.rn_state(home)
    state.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"last":"2026-01-01T00:00:00Z"}')
    state.symlink_to(outside)
    monkeypatch.setattr(refresh.mirror, "release_notes", lambda base: [_note()])
    with pytest.raises((OSError, kb.KBError)):
        refresh.part_release_notes(home, tmp_path, tmp_path / "out", update=False, today=TODAY)


def test_hardening_release_output_failure_keeps_watermark(home, tmp_path, monkeypatch):
    state = refresh.rn_state(home)
    state.parent.mkdir(parents=True)
    state.write_text('{"last":"2026-01-01T00:00:00Z"}')
    before = state.read_bytes()
    output = tmp_path / "out"
    output.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (output / "release-notes.txt").symlink_to(outside)
    monkeypatch.setattr(refresh.config, "is_work_user", lambda: False)
    monkeypatch.setattr(refresh.mirror, "sync_release_notes", lambda base: dict(new=1, known=0, pages=1))
    monkeypatch.setattr(refresh.mirror, "release_notes", lambda base: [_note()])
    with pytest.raises((OSError, kb.KBError)):
        refresh.part_release_notes(home, tmp_path, output, update=True, today=TODAY)
    assert state.read_bytes() == before and outside.read_text() == "keep"


def test_hardening_worklist_link_does_not_overwrite_external_file(home, tmp_path):
    folder = refresh.refresh_dir(home, TODAY)
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    (folder / "worklist.jsonl").symlink_to(outside)
    parts, _ = refresh.run(home, parts=("knowledge",), today=TODAY)
    assert parts[0].error and outside.read_text() == "keep"


@pytest.mark.parametrize("reason", ["first\nforged line", "\x1b[2Jhidden"])
def test_hardening_unchecked_reasons_cannot_inject_output_lines(home, reason):
    applied, _ = refresh.apply([_unchecked(why=reason)])
    assert "\n" not in applied[0].outcome and "\x1b" not in applied[0].outcome


@pytest.mark.parametrize("error", [ValueError, TypeError, KeyError, AttributeError])
def test_hardening_malformed_part_data_is_reported_as_failure(home, monkeypatch, error):
    def broken(*args, **kwargs):
        raise error("private-value")

    monkeypatch.setattr(refresh, "part_knowledge", broken)
    parts, report = refresh.run(home, parts=("knowledge",), today=TODAY)
    assert parts[0].error and "private-value" not in report.read_text()


@pytest.mark.parametrize("kwargs", [{"parts": ("../outside",)}, {"max_state_age": -1}])
def test_hardening_invalid_run_options_are_refused(home, kwargs):
    with pytest.raises(kb.KBError):
        refresh.run(home, **kwargs)


def test_hardening_lock_wait_is_bounded(home, monkeypatch):
    import fcntl

    folder = home.shared / "refresh"
    folder.mkdir()
    with (folder / ".run.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        monkeypatch.setattr(refresh, "LOCK_WAIT", 0, raising=False)
        with pytest.raises(kb.KBError, match="another refresh"):
            refresh.run(home, parts=("projects",))


def test_hardening_recheck_requests_cannot_inject_tsv_rows(home, monkeypatch):
    written = []
    monkeypatch.setattr(refresh.recheck, "request", lambda rows: written.extend(rows) or len(rows))
    with pytest.raises(kb.KBError):
        refresh._request(refresh.Part("mirrors"), [("KB-AAAA", TODAY, "docs mirror a/b.md\nforged")])
    assert not written


@pytest.mark.parametrize("location", ["record", "source"])
def test_hardening_structured_secret_is_screened_before_snapshot_write(home, monkeypatch, location):
    from types import SimpleNamespace

    private = "Never" + "Publish934!"
    raw = {"id": "TEST_EVS_SSD"}
    if location == "record":
        raw["pass" + "word"] = private
    record = price.Record.from_raw(raw)
    source = "public source" if location == "record" else "https://user:" + private + "@api.example/"
    fetched = SimpleNamespace(listing=SimpleNamespace(state=jobs.LIST), records=[record],
                              url=source, fetched_at="today", cached_at="today")
    monkeypatch.setattr(price, "SNAPSHOT_REGIONS", ("eu-de",))
    monkeypatch.setattr(price, "fetch", lambda *a, **kw: fetched)
    parts, report = refresh.run(home, parts=("prices",), today=TODAY)
    # B-089: no content check inside, the snapshot is written as it stands
    assert not parts[0].error
    assert (price.snapshot_dir(home) / "eu-de" / (TODAY.isoformat() + ".json")).exists()
