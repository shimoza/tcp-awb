"""The validation levels of a fact and the loop that keeps the facts fresh (partner feedback of 2026-11-09).

A: one function computes the level V0 to V4 from grade, check block and due; the dataset and its MCP server carry it.
B: the refresh writes recheck requests (release notes, a new revision of the service description, a changed page of
   the docs mirror) and the recheck pick takes a requested entry as due until a recheck on or after its date.
C: a try run of a read block, which writes nothing back.
D: the weekly refresh as the owner's timer, Saturday 17:00 UTC, and its failure in owner status.

Every entry, id and value here is invented; the cloud is the stand-in of tests/lab_fake.py, the documentation
repositories are local git folders and nothing leaves the process.
"""
from __future__ import annotations

import datetime
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from awb import cli, config, jobs, kb, ownerwait
from awb.tcp import dataset, mirror, recheck, refresh
from awb.tcp import tcp_facts_mcp as server
from tests.test_dataset import S_ECS, TODAY as DS_DAY, facts, live, new  # noqa: F401
from tests.test_mirror import git, listing
from tests.test_recheck import (FLAVORS, OLD, PROJECT, READ_BLOCK, TENANT, Cloud, entry,  # noqa: F401
                                project)
from tests.test_seal import SETUP, USER, VERIFY, commands, dropin_lines, fakebin, run_script  # noqa: F401

TODAY = datetime.date.today()


# --------------------------------------------------------------------------- A: the level


@pytest.mark.parametrize("grade, kw, want", [
    ("live", dict(due=False, tier="read"), "V4"),
    ("live", dict(due=False, tier="write"), "V4"),
    ("live", dict(due=False, tier="decision", needs="a GPU flavor"), "V3"),
    ("live", dict(due=False, tier="read", needs="a person at the console"), "V3"),
    ("live", dict(due=False, tier="none"), "V3"),
    ("live", dict(due=False), "V3"),
    ("contract", dict(due=False, tier="read"), "V2"),
    ("docs", dict(due=False), "V1"),
    ("live", dict(due=True, tier="read"), "V0"),
    ("contract", dict(due=True), "V0"),
    ("docs", dict(due=False, refuted=True), "V0"),
    ("said", dict(due=False), ""),
    ("said", dict(due=True), "V0"),
])
def test_one_function_gives_the_level_from_grade_block_and_due(grade, kw, want):
    assert kb.level(grade, **kw) == want


def test_the_levels_are_five_with_two_lines_each_and_no_outside_confirmation():
    assert [x[0] for x in kb.LEVELS] == ["V4", "V3", "V2", "V1", "V0"]
    assert all(len(x) == 4 and x[2].endswith(".") and x[3].endswith(".") for x in kb.LEVELS)
    assert kb.level_label("V4") == "V4 kept live" and kb.level_label("V0") == "V0 recheck due"
    assert "confirm" not in " ".join(x[1] for x in kb.LEVELS)


def test_the_level_of_an_entry_reads_its_block_and_its_due(home):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    assert kb.entry_level(e, TODAY) == "V4"
    assert kb.entry_level(e, TODAY, requested={e.id: [TODAY.isoformat()]}) == "V4"     # checked that day: not due
    later = (TODAY + datetime.timedelta(days=1)).isoformat()
    assert kb.entry_level(e, TODAY, requested={e.id: [later]}, days=1) == "V0"
    assert kb.entry_level(e, TODAY, refuted={e.id: TODAY.isoformat()}) == "V0"
    bare = entry("ECS flavor s9.large.1 is offered in eu-de-02", None, checked=TODAY.isoformat())
    assert kb.entry_level(bare, TODAY) == "V3"


def _build(home, tmp_path):
    kept = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=DS_DAY.isoformat(),
                 today=DS_DAY)
    sd = new("The service description lists ECS flavors of the families s3 and c4 in eu-de", grade="contract",
             source="service description 09.10.2026 section 3.1.1")
    due = new("ECS flavor s3.xlarge.2 is offered in eu-de-01", grade="live", cls="availability",
              source="live API call, eu-de", checked=(DS_DAY - datetime.timedelta(days=40)).isoformat())
    built = dataset.build(home, out=tmp_path / "ds", today=DS_DAY, live=False)
    return built, kept, sd, due


def test_the_dataset_carries_the_level_in_every_record_line_table_and_manifest(home, facts, tmp_path):
    built, kept, sd, due = _build(home, tmp_path)
    rows = {json.loads(x)["id"]: json.loads(x) for x in (built.folder / "facts.jsonl").read_text().splitlines()[1:]}
    assert all("level" in r for r in rows.values())
    assert rows[kept.id]["level"] == "V4" and rows[facts.id]["level"] == "V3"
    assert rows[sd.id]["level"] == "V2" and rows[due.id]["level"] == "V0"
    assert {r["level"] for r in rows.values() if r["grade"] == "docs"} == {"V1"}
    facts_md = (built.folder / "facts.md").read_text()
    assert "(%s, V4 kept live, checked %s)" % (kept.id, DS_DAY.isoformat()) in facts_md
    assert "- %s (%s, V3 live, checked %s)" % (S_ECS, facts.id, DS_DAY.isoformat()) in facts_md
    assert "(%s, V0 recheck due, live, checked %s)" % (due.id, due.checked) in facts_md
    topic = (built.folder / "topics" / "ecs.md").read_text()
    assert "(%s, V4 kept live, checked" % kept.id in topic
    manifest = json.loads((built.folder / "MANIFEST.json").read_text())
    assert manifest["levels"] == {"V4": 1, "V3": 2, "V2": 1, "V1": 1, "V0": 1}
    for name in ("README.md", "PROMPT.md"):
        text = (built.folder / name).read_text()
        assert "## Validation levels" in text, name
        assert "| V4 kept live | %s | %s | 1 |" % kb.LEVELS[0][2:] in text, name
        assert "| V3 live | %s | %s | 2 |" % kb.LEVELS[1][2:] in text, name
        assert "| V0 recheck due | %s | %s | 1 |" % kb.LEVELS[4][2:] in text, name
    assert "levels: V4 1, V3 2, V2 1, V1 1, V0 1" in built.lines
    if shutil.which("pdftotext"):
        pdf_text = subprocess.run(["pdftotext", "-layout", str(built.folder / "HOW-TO.pdf"), "-"],
                                  capture_output=True, text=True, check=True).stdout
        assert "Validation levels" in pdf_text and "V4 kept live" in pdf_text and "V0 recheck due" in pdf_text


def test_the_mcp_answer_carries_the_level_and_a_search_asks_for_a_minimum(home, facts, tmp_path):
    built, kept, sd, due = _build(home, tmp_path)
    ds = server.Dataset(built.folder)
    res = server.call_tool(ds, "tcp_facts_search", {"query": "ecs flavor offered eu-de"})
    hits = json.loads(res["content"][0]["text"])["facts"]
    by_id = {h["id"]: h for h in hits}
    assert by_id[kept.id]["level"] == "V4" and by_id[kept.id]["level_name"] == "kept live"
    assert by_id[due.id]["level"] == "V0"
    res = server.call_tool(ds, "tcp_facts_search", {"query": "ecs flavor offered eu-de", "min_level": "V3"})
    answer = json.loads(res["content"][0]["text"])
    assert answer["min_level"] == "V3" and answer["facts"]
    assert {h["level"] for h in answer["facts"]} <= {"V3", "V4"} and kept.id in {h["id"] for h in answer["facts"]}
    res = server.call_tool(ds, "tcp_facts_search", {"query": "ecs", "min_level": "V5"}, arg_errors_are_results=True)
    assert res["isError"] is True
    schema = next(t for t in server.TOOLS if t["name"] == "tcp_facts_search")["inputSchema"]
    assert schema["properties"]["min_level"]["enum"] == ["V4", "V3", "V2", "V1", "V0"]


def test_a_fact_of_an_older_dataset_without_a_level_is_left_out_of_a_minimum_search(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "facts.jsonl").write_text("\n".join(json.dumps(x) for x in (
        {"dataset": "TCP Facts", "date": "2026-10-01"},
        {"id": "KB-AAAA", "statement": "ECS flavor s9.medium.1 is offered", "grade": "live"})) + "\n")
    ds = server.Dataset(folder)
    assert server.facts_search(ds, "ecs flavor")["count"] == 1
    assert server.facts_search(ds, "ecs flavor", min_level="V0")["count"] == 0


# --------------------------------------------------------------------------- B: requests and the pick


def test_a_request_is_written_once_and_makes_the_entry_due_until_a_recheck_on_or_after_its_date(home):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    assert e.id not in {x.id for x in kb.due(home, TODAY, recheck.DUE_DAYS, requested=recheck.requested_dates())}
    tomorrow = TODAY + datetime.timedelta(days=1)
    assert recheck.request([(e.id, tomorrow, "release note 2026-11-01 Key Management Service")]) == 1
    assert recheck.request([(e.id, TODAY, "release note 2026-11-01 Key Management Service")]) == 0   # kept once
    path = home.shared / "recheck" / "requested.tsv"
    assert path.read_text() == "%s\t%s\trelease note 2026-11-01 Key Management Service\n" % (e.id, tomorrow)
    assert e.id in {x.id for x in kb.due(home, TODAY, recheck.DUE_DAYS, requested=recheck.requested_dates())}
    assert e.id not in {x.id for x in kb.due(home, TODAY, 0, requested=recheck.requested_dates())}   # not yet
    p = recheck.pick("read", today=TODAY)
    assert [x.id for x in p.run] == [e.id]
    kb.recheck(e.id, grade="live", source="recheck run", checked=tomorrow, today=tomorrow)
    assert not recheck.pick("read", today=tomorrow).run


def test_a_requested_entry_without_a_block_goes_to_the_list_without_a_block(home):
    bare = entry("ECS flavor s9.large.1 is offered in eu-de-02", None, checked=TODAY.isoformat())
    recheck.request([(bare.id, TODAY, "docs mirror elastic-cloud-server/api-ref.rst")])
    assert not recheck.pick("read", today=TODAY).no_block              # checked that day: the request is met
    recheck.request([(bare.id, TODAY + datetime.timedelta(days=1), "docs mirror cloud-eye/api-ref.rst")])
    p = recheck.pick("read", today=TODAY)
    assert [x.id for x in p.no_block] == [bare.id] and not p.run


def test_a_weekly_run_takes_a_requested_entry_and_clears_it(project, home):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    recheck.request([(e.id, TODAY + datetime.timedelta(days=1), "service description 2026-11-01")])
    lines: list[str] = []
    code = recheck.run("read", [], PROJECT, TENANT, send=Cloud(), out=lines.append, err=lines.append,
                       now=datetime.datetime.combine(TODAY + datetime.timedelta(days=1), datetime.time(19),
                                                     datetime.timezone.utc))
    assert code == 0, lines
    assert kb.get(e.id).checked == (TODAY + datetime.timedelta(days=1)).isoformat()


def kms(statement):
    return kb.add(statement, scope="tcp", tags=["kms"], grade="live", cls="availability",
                  source="live API call, eu-de", force_new=True)


def test_the_release_notes_part_requests_every_matched_entry_and_the_report_counts_them(home, tmp_path, monkeypatch):
    ids = [kms("key rotation interval number %d is set through the API" % i).id for i in range(17)]
    note = {"id": "2026-10-16-n", "date": "2026-10-16", "title": "key rotation interval",
            "text": "The key rotation interval is set through the API.", "services": ["Key Management Service"],
            "seen": "2026-10-19T10:00:00Z"}
    later = dict(note, id="2026-11-30-m", date="2026-11-30")
    monkeypatch.setattr(mirror, "release_notes", lambda b: [note, later])
    part = refresh.part_release_notes(home, tmp_path / "mirrors", tmp_path / "out", update=False,
                                      today=datetime.date(2026, 10, 19))
    rows = recheck.requests()
    assert {i for i, _, _ in rows} == set(ids)                         # all 17, not only the 15 shown
    assert {d for _, d, r in rows if "2026-10-16" in r} == {"2026-10-19"}
    assert {d for _, d, r in rows if "2026-11-30" in r} == {"2026-11-30"}   # announced: due from its date
    assert part.requested == {"release note": 34}
    assert refresh.request_line([part]).startswith("requested: release note 34, service description 0, docs mirror 0")
    again = refresh.part_release_notes(home, tmp_path / "mirrors", tmp_path / "out", update=False,
                                       today=datetime.date(2026, 10, 19))
    assert len(recheck.requests()) == 34 and any("0 new" in x for x in again.lines)


def test_cited_pages_follow_the_source_lines_of_the_mirror():
    repos = {"key-management-service", "regions-and-endpoints"}
    src = ("docs mirror: key-management-service/api-ref/source/a.rst and umn/source/b.rst; "
           "regions-and-endpoints/doc/source/index.rst")
    assert refresh.cited_pages(src, repos) == {"key-management-service/api-ref/source/a.rst",
                                               "key-management-service/umn/source/b.rst",
                                               "regions-and-endpoints/doc/source/index.rst"}
    assert refresh.cited_pages("live API call, key-management-service/api-ref/source/a.rst", repos) == set()


def test_the_mirrors_part_requests_contract_entries_on_a_new_revision_and_entries_of_changed_pages(
        home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    base = tmp_path / "mirrors"
    repos = listing(tmp_path)
    mirror.sync_docs(base, repos, jobs.Job("a", None, out=io.StringIO()))
    ecs = tmp_path / "origin" / "elastic-cloud-server"
    (ecs / "api-ref.rst").write_text("second\n", encoding="utf-8")
    git("commit", "-q", "-am", "two", cwd=ecs)
    cited = kb.add("ECS flavors are listed with GET on the flavors path", scope="tcp", tags=["ecs"], grade="docs",
                   cls="api", source="docs mirror: elastic-cloud-server/api-ref.rst")
    other = kb.add("A VPC carries up to five subnets in the user guide example", scope="tcp", tags=["vpc"],
                   grade="docs", cls="api", source="docs mirror: virtual-private-cloud/api-ref.rst")
    contract = kb.add("The service description lists ECS in section 3.1.1", scope="tcp", tags=["ecs"],
                      grade="contract", cls="stable", source="service description 09.10.2026 section 3.1.1")
    old = base / "service-description" / "2026-10-09" / "sd.txt"
    monkeypatch.setattr(mirror, "current_sd", lambda b: old)
    monkeypatch.setattr(mirror, "sync_sd", lambda b: {"revision": "01.11.2026", "state": "new",
                                                      "current": "2026-11-01"})
    monkeypatch.setattr(mirror, "list_repos", lambda: jobs.Listing.of(repos))
    part = refresh.part_mirrors(base, update=True, job=jobs.Job("b", None, out=io.StringIO()), p=home, today=TODAY)
    rows = {(i, r) for i, _, r in recheck.requests()}
    assert (cited.id, "docs mirror elastic-cloud-server/api-ref.rst") in rows
    assert (contract.id, "service description 2026-11-01") in rows
    assert other.id not in {i for i, _ in rows}
    assert part.requested == {"service description": 1, "docs mirror": 1}
    assert "documentation: 1 page(s) changed in 1 repository(ies)" in part.lines


def test_an_update_of_the_mirror_names_the_changed_paths_and_keeps_them_out_of_the_manifest(tmp_path):
    base = tmp_path / "mirrors"
    repos = listing(tmp_path, names=("cloud-eye",), archived=())
    mirror.sync_docs(base, repos, jobs.Job("a", None, out=io.StringIO()))
    origin = tmp_path / "origin" / "cloud-eye"
    (origin / "new.rst").write_text("x\n", encoding="utf-8")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "two", cwd=origin)
    changed: dict = {}
    mirror.sync_docs(base, repos, jobs.Job("b", None, out=io.StringIO()), changed=changed)
    assert changed == {"cloud-eye": ["new.rst"]}
    assert "changed" not in mirror.load_manifest(base / "docs")["repos"]["cloud-eye"]
    assert mirror.changed_paths({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == ["b", "c"]
    assert mirror.changed_paths(None, {"a": "1"}) is None


# --------------------------------------------------------------------------- C: the try run


def test_a_try_run_checks_an_entry_that_is_not_due_and_writes_nothing_back(project, home, tmp_path, capsys):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    before = (home.kb / "entries" / (e.id + ".md")).read_bytes()
    lines: list[str] = []
    cloud = Cloud(s9="sellout")
    code = recheck.try_run([e.id], PROJECT, TENANT, out_dir=tmp_path / "try", send=cloud, out=lines.append,
                           err=lines.append)
    assert code == 0, lines
    assert lines[0] == "%s: refuted (refutes holds, confirms does not)" % e.id
    assert lines[1].startswith("  confirms: ") and lines[1].endswith(": fails")
    assert lines[2].startswith("  refutes: ") and lines[2].endswith(": holds")
    assert lines[3].startswith("  control: ") and lines[3].endswith(": holds")
    assert [r["method"] for r in cloud.calls] == ["GET"] and cloud.calls[0]["role"] == "read"
    assert (home.kb / "entries" / (e.id + ".md")).read_bytes() == before
    assert not (home.shared / "recheck" / "state.tsv").exists()
    assert not (home.shared / "recheck" / "refuted.tsv").exists()
    assert not list((project / "evidence").glob("recheck-*"))
    runs = list((tmp_path / "try" / "evidence").glob("recheck-*"))
    assert len(runs) == 1 and (runs[0] / ("%s.md" % e.id)).is_file()
    assert "refuted" in (runs[0] / "RESULT.md").read_text() and "s9.medium.1" in (runs[0] / "calls.log").read_text()


def test_a_try_run_proves_a_block_before_it_is_attached(project, home, tmp_path):
    bare = entry("ECS flavor s9.medium.1 is offered in eu-de-01", None, checked=TODAY.isoformat())
    lines: list[str] = []
    assert recheck.try_run([bare.id], PROJECT, TENANT, out_dir=tmp_path / "t", send=Cloud(), out=lines.append,
                           err=lines.append) == 0
    assert lines[0] == "%s: not tried, no read block (tier none, no block)" % bare.id
    lines.clear()
    assert recheck.try_run([bare.id], PROJECT, TENANT, out_dir=tmp_path / "t", block_text=READ_BLOCK,
                           send=Cloud(), out=lines.append, err=lines.append) == 0
    assert lines[0] == "%s: confirmed (confirms holds, refutes does not)" % bare.id
    assert not kb.get(bare.id).check                                   # the block is not attached
    lines.clear()
    broken = READ_BLOCK.replace("control: ", "controls: ")
    assert recheck.try_run([bare.id], PROJECT, TENANT, out_dir=tmp_path / "t", block_text=broken, send=Cloud(),
                           out=lines.append, err=lines.append) == 2
    assert lines and "known key" in lines[0]


def test_the_try_flag_reaches_the_try_run_and_refuses_what_it_cannot_do(project, home, tmp_path, monkeypatch,
                                                                         capsys):
    from awb.tcp import lab
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    monkeypatch.setattr(lab, "_send_default", Cloud())
    assert cli.main(["kb", "recheck-run", "--try", "--id", e.id, "--project", PROJECT, "--tenant", TENANT,
                     "--out", str(tmp_path / "o")]) == 0
    assert "%s: confirmed" % e.id in capsys.readouterr().out
    assert cli.main(["kb", "recheck-run", "--try", "--project", PROJECT, "--tenant", TENANT]) == 2
    assert "names its entries with --id" in capsys.readouterr().err
    assert cli.main(["kb", "recheck-run", "--try", "--tier", "write", "--id", e.id]) == 2
    assert cli.main(["kb", "recheck-run", "--out", str(tmp_path / "o")]) == 2
    assert not (home.shared / "recheck" / "state.tsv").exists()


# --------------------------------------------------------------------------- D: the weekly refresh


def test_the_refresh_unit_runs_as_the_owner_saturday_17_utc_and_verify_proves_it_enabled(fakebin):
    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        cmds = commands(res.stdout)
        unit = dropin_lines(res.stdout, "/etc/systemd/system/awb-refresh-weekly.service")
        for want in ("Type=oneshot", "User=%s" % USER, "ExecStart=/usr/local/bin/awb refresh weekly",
                     "StateDirectory=awb-refresh-weekly", "StateDirectoryMode=0700",
                     "Environment=AWB_REFRESH_WEEKLY=/var/lib/awb-refresh-weekly", "NoNewPrivileges=yes"):
            assert want in unit, want
        assert len([x for x in unit if x.startswith("ExecStart=")]) == 1
        timer = dropin_lines(res.stdout, "/etc/systemd/system/awb-refresh-weekly.timer")
        assert [x for x in timer if x.startswith("OnCalendar=")] == ["OnCalendar=Sat *-*-* 17:00:00 UTC"]
        assert "Unit=awb-refresh-weekly.service" in timer and "WantedBy=timers.target" in timer
        reload = cmds.index("+ systemctl daemon-reload")
        assert cmds.index("+ write /etc/systemd/system/awb-refresh-weekly.timer (mode 644, root:root)") < reload
        assert cmds.index("+ systemctl enable --now awb-refresh-weekly.timer") > reload
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [x[6:] for x in res.stdout.splitlines() if x.startswith("CHECK ")]
    assert ("the timer awb-refresh-weekly.timer is enabled, fires Saturday 17:00 UTC and runs awb refresh weekly as "
            "the owner") in checks


def test_a_failed_weekly_refresh_shows_in_owner_status_until_a_run_goes_through(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    ownerwait.refresh_state_path().parent.mkdir(parents=True, exist_ok=True)     # the unit's StateDirectory
    calls = []

    def broken(p, **kw):
        calls.append(kw)
        return [refresh.Part("mirrors", error="MirrorError: no answer"), refresh.Part("prices")], tmp_path / "R.md"

    monkeypatch.setattr(refresh, "run", broken)
    assert refresh.main(["weekly"]) == 1
    assert calls[0]["update"] is True
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert "refresh: weekly run failed: mirrors: MirrorError: no answer" in ownerwait.render(items, parked)

    def crash(p, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(refresh, "run", crash)
    assert refresh.weekly(home) == 1
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert "refresh: weekly run failed: the run broke off (RuntimeError)" in ownerwait.render(items, parked)
    part = refresh.Part("release-notes", overdue=3, requested={"release note": 2})
    monkeypatch.setattr(refresh, "run", lambda p, **kw: ([part], tmp_path / "R.md"))
    assert refresh.weekly(home) == 0                                  # overdue is the report's, not a failure
    items, _ = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert not [x for x in items if x.label == "refresh"]
    state = json.loads(ownerwait.refresh_state_path().read_text())
    assert state["state"] == "done" and state["requested"]["release note"] == 2


def test_the_work_user_may_not_run_the_weekly_refresh(home, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(refresh, "run", lambda p, **kw: pytest.fail("the refresh ran"))
    assert refresh.main(["weekly"]) == 2
    assert "the owner's" in capsys.readouterr().err
    assert not Path(ownerwait.refresh_state_path()).exists()


def test_a_fact_a_trigger_hit_is_v0_in_the_dataset_until_it_is_checked_again(home, facts, tmp_path):
    kept = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK,
                 checked=(DS_DAY - datetime.timedelta(days=2)).isoformat(), today=DS_DAY)
    recheck.request([(kept.id, (DS_DAY - datetime.timedelta(days=1)).isoformat(), "release note 2026-10-07 ecs")])
    recheck.request([(facts.id, DS_DAY.isoformat(), "release note 2026-10-08 ecs")])   # checked that day: stays
    built = dataset.build(home, out=tmp_path / "ds", today=DS_DAY, live=False)
    rows = {json.loads(x)["id"]: json.loads(x) for x in (built.folder / "facts.jsonl").read_text().splitlines()[1:]}
    assert rows[kept.id]["level"] == "V0" and rows[kept.id]["recheck_due"] is True
    assert rows[facts.id]["level"] == "V3" and rows[facts.id]["recheck_due"] is False
