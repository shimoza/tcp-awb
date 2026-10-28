"""Task KS (his decisions of 2026-10-28): the weekly fact-check runs Saturday 19:00 UTC, the dataset TCP Facts is
published by an owner timer every Monday 06:00 UTC (build, kb review, put --github), a failed publish shows in
`awb owner status` and a successful one is one line of the 08:00 mail.

The facts here are invented; the build and the two puts are replaced by fakes, the review and its mark are real."""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess

import pytest

from pathlib import Path

from awb import config, kb, kbreview, ownerwait
from awb.tcp import dataset, recheck
from tests.test_dataset import TODAY as DS_DAY, facts, live, new  # noqa: F401
from tests.test_recheck import READ_BLOCK
from tests.test_seal import SETUP, USER, VERIFY, WORK_JOBS, commands, dropin_lines, fakebin, run_script  # noqa: F401

DAY = "2026-11-02"


# --------------------------------------------------------------------------- the two schedules


def test_ks_the_recheck_timer_fires_saturday_19_utc_at_most_15_minutes_late(fakebin):
    timer = (WORK_JOBS / "awb-kb-recheck.timer").read_text(encoding="utf-8").splitlines()
    assert [x for x in timer if x.startswith("OnCalendar=")] == ["OnCalendar=Sat *-*-* 19:00:00 UTC"]
    delay = [x.split("=", 1)[1] for x in timer if x.startswith("RandomizedDelaySec=")]
    assert len(delay) == 1 and re.fullmatch(r"([1-9]|1[0-5])min", delay[0])
    assert "Persistent=false" in timer and "Unit=awb-kb-recheck.service" in timer
    assert "Sun" not in "\n".join(x for x in timer if not x.startswith("#"))
    if shutil.which("systemd-analyze"):
        res = subprocess.run(["systemd-analyze", "calendar", "Sat *-*-* 19:00:00 UTC"], capture_output=True, text=True)
        assert res.returncode == 0 and "Sat" in res.stdout
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert "OnCalendar=Sat *-*-* 19:00:00 UTC" in dropin_lines(res.stdout, "/etc/systemd/system/awb-kb-recheck.timer")
    assert "# Beside them the weekly read tier of the fact-check, Saturday 19:00 UTC: awb kb recheck-run --tier read (F9)" \
        in res.stdout.splitlines()
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [x[6:] for x in res.stdout.splitlines() if x.startswith("CHECK ")]
    assert "awb-kb-recheck.timer fires Saturday 19:00 UTC, at most 15 minutes late" in checks


def test_ks_the_publish_unit_runs_as_the_owner_monday_06_utc_and_verify_proves_it_enabled(fakebin):
    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        cmds = commands(res.stdout)
        unit = dropin_lines(res.stdout, "/etc/systemd/system/awb-dataset-weekly.service")
        for want in ("Type=oneshot", "User=%s" % USER, "Group=awb", "ExecStart=/usr/local/bin/awb dataset weekly",
                     "StateDirectory=awb-dataset-weekly", "StateDirectoryMode=0700",
                     "Environment=AWB_DATASET_WEEKLY=/var/lib/awb-dataset-weekly", "NoNewPrivileges=yes"):
            assert want in unit, want
        assert len([x for x in unit if x.startswith("ExecStart=")]) == 1
        assert not [x for x in unit if x.startswith("User=") and x != "User=%s" % USER]
        timer = dropin_lines(res.stdout, "/etc/systemd/system/awb-dataset-weekly.timer")
        assert [x for x in timer if x.startswith("OnCalendar=")] == ["OnCalendar=Mon *-*-* 06:00:00 UTC"]
        assert "Unit=awb-dataset-weekly.service" in timer and "WantedBy=timers.target" in timer
        reload = cmds.index("+ systemctl daemon-reload")
        assert cmds.index("+ write /etc/systemd/system/awb-dataset-weekly.timer (mode 644, root:root)") < reload
        assert cmds.index("+ systemctl enable --now awb-dataset-weekly.timer") > reload
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert "awb-dataset-weekly" not in res.stdout
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [x[6:] for x in res.stdout.splitlines() if x.startswith("CHECK ")]
    assert ("the timer awb-dataset-weekly.timer is enabled, fires Monday 06:00 UTC and runs awb dataset weekly as "
            "the owner") in checks


# --------------------------------------------------------------------------- the weekly run


def _fact(i, statement):
    return {"id": i, "statement": statement, "source": "docs", "grade": "docs", "checked": "2026-10-31"}


@pytest.fixture
def weekly(home, tmp_path, monkeypatch):
    """A released dataset of one week ago and a fake build of today's: one fact amended, one added, one retired.
    The bucket put and the GitHub put are recorded; the review and its mark are the real ones."""
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    state = tmp_path / "dataset-weekly"
    state.mkdir()
    monkeypatch.setenv("AWB_DATASET_WEEKLY", str(state))
    base = kbreview.datasets_dir(home)
    base.mkdir(parents=True)
    released = base / kbreview.RELEASED
    released.write_text("\n".join(json.dumps(x) for x in (
        {"date": "2026-10-26"}, _fact("KB-0001", "old"), _fact("KB-0003", "gone"))) + "\n", encoding="utf-8")
    released.chmod(0o644)
    calls = []

    def build(p, *, today=None, force=False, **kw):
        calls.append(("build", force))
        folder = base / ("tcp-facts-" + DAY)
        folder.mkdir(exist_ok=True)
        (folder / "facts.jsonl").write_text("\n".join(json.dumps(x) for x in (
            {"dataset": "TCP Facts", "date": DAY}, _fact("KB-0001", "new"), _fact("KB-0002", "added"))) + "\n",
            encoding="utf-8")
        return dataset.Built(folder, base / (folder.name + ".zip"), DAY, 2, {}, 1, 1, {}, {}, ["built"])

    monkeypatch.setattr(dataset, "build", build)
    monkeypatch.setattr(dataset.bucket, "client", lambda: "client")
    monkeypatch.setattr(dataset, "put", lambda c, folder, replace=False: calls.append(("put", c)) or ["bucket key"])
    monkeypatch.setattr(dataset, "github_put", lambda p, folder, replace=False, pinned=None:
                        calls.append(("github", folder.name)) or ["github release"])
    return home, calls, state / ownerwait.DATASET_FILE, released


def test_ks_weekly_builds_reviews_and_puts_to_github_and_the_mail_carries_the_line(weekly, tmp_path, capsys):
    home, calls, state, released = weekly
    assert dataset.weekly(home) == 0
    assert calls == [("build", True), ("put", "client"), ("github", "tcp-facts-" + DAY)]
    line = "dataset tcp-facts-%s: 1 added, 1 amended, 1 retired" % DAY
    assert line in capsys.readouterr().out
    assert json.loads(state.read_text(encoding="utf-8"))["state"] == "published"
    assert json.loads(released.read_text(encoding="utf-8").splitlines()[0])["date"] == DAY   # the release recorded
    assert kbreview.review_path(home.shared / "datasets" / ("tcp-facts-" + DAY)).is_file()   # the owner's mark
    sent = []
    word = ownerwait.mail(home, lambda s, m: sent.append((s, m)) or "sent", tmp_path / "none.md", tmp_path / "runs")
    assert word == "sent" and len(sent) == 1 and line in sent[0][1].splitlines()
    items, _ = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert not [x for x in items if x.label == "dataset"]


def test_ks_the_success_line_is_left_out_after_24_hours():
    state = {"state": "published", "dataset": "tcp-facts-" + DAY, "added": 1, "amended": 0, "retired": 0,
             "at": "2026-11-02T06:01:00Z"}
    path = Path(os.environ["AWB_DATASET_WEEKLY"])
    path.mkdir(parents=True, exist_ok=True)
    (path / ownerwait.DATASET_FILE).write_text(json.dumps(state), encoding="utf-8")
    later = datetime.datetime(2026, 11, 3, 8, 0, tzinfo=datetime.timezone.utc)
    assert ownerwait.notes(now=later) == []


def test_ks_a_failed_put_publishes_nothing_more_and_shows_in_owner_status(weekly, tmp_path, monkeypatch, capsys):
    home, calls, state, released = weekly
    before = released.read_text(encoding="utf-8")

    def broken(p, folder, replace=False, pinned=None):
        raise dataset.DatasetError("gh release failed (exit 1)")

    monkeypatch.setattr(dataset, "github_put", broken)
    assert dataset.weekly(home) == 1
    assert released.read_text(encoding="utf-8") == before            # no release recorded
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    text = ownerwait.render(items, parked)
    assert "dataset: weekly publish failed: put: gh release failed (exit 1)" in text
    sent = []
    assert ownerwait.mail(home, lambda s, m: sent.append(m) or "sent", tmp_path / "none.md", tmp_path / "runs") == "sent"
    assert "dataset: weekly publish failed: put: gh release failed (exit 1)" in sent[0]
    assert "added" not in sent[0]
    monkeypatch.setattr(dataset, "github_put", lambda p, folder, replace=False, pinned=None: ["github release"])
    assert dataset.weekly(home) == 0                                 # the next success clears the item
    items, _ = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert not [x for x in items if x.label == "dataset"]


def test_ks_a_failed_build_stops_before_the_review_and_the_put(weekly, tmp_path, monkeypatch):
    home, calls, state, released = weekly

    def broken(p, **kw):
        raise dataset.DatasetError("no fact fit to leave: nothing to build")

    monkeypatch.setattr(dataset, "build", broken)
    assert dataset.weekly(home) == 1
    assert calls == []
    assert not list((home.shared / "datasets").glob("*.review.json"))
    items, parked = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert "dataset: weekly publish failed: build: no fact fit to leave: nothing to build" in ownerwait.render(items, parked)


def test_ks_the_work_user_may_not_run_the_weekly_publish(weekly, monkeypatch, capsys):
    home, calls, state, _ = weekly
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert dataset.main(["weekly"]) == 2
    assert calls == [] and not state.exists()
    assert "the owner's" in capsys.readouterr().err
    with pytest.raises(kbreview.ReviewError):                       # the refusal of the review stays
        kbreview.review(home)


# --------------------------------------------------------------------------- a fact leaves only when invalidated


def _aged(statement: str, days: int, today: datetime.date) -> kb.Entry:
    """A live availability fact (30 days of validity) checked `days` ago."""
    return new(statement, grade="live", cls="availability", source="live API call, eu-de",
               checked=(today - datetime.timedelta(days=days)).isoformat(), today=today)


def test_ks_an_expired_fact_stays_in_the_build_marked_recheck_due(home, facts, tmp_path):
    due = _aged("ECS flavor s3.xlarge.2 is offered in eu-de-01", 40, DS_DAY)
    built = dataset.build(home, out=tmp_path / "ds", today=DS_DAY, live=False)
    rows = {r["id"]: r for r in kbreview.read_facts(built.folder / "facts.jsonl")}
    assert rows[due.id]["recheck_due"] is True and rows[due.id]["checked"] == due.checked
    assert not [r for i, r in rows.items() if i != due.id and r["recheck_due"]]
    facts_md = (built.folder / "facts.md").read_text(encoding="utf-8")
    assert "(%s, live, checked %s, recheck due)" % (due.id, due.checked) in facts_md
    readme = (built.folder / "README.md").read_text(encoding="utf-8")
    assert "| %s | live | %s | recheck due |" % (due.id, due.checked) in readme
    assert json.loads((built.folder / "MANIFEST.json").read_text(encoding="utf-8"))["recheck_due"] == 1


def test_ks_a_retired_superseded_or_refuted_fact_leaves_the_build(home, facts, tmp_path):
    gone = _aged("ECS flavor s3.small.1 is offered in eu-de-02", 40, DS_DAY)
    newer = _aged("ECS flavor s3.medium.4 is offered in eu-de-03", 0, DS_DAY)
    kb.retire(gone.id, "superseded by %s" % newer.id, today=DS_DAY)
    old_retired = _aged("ECS flavor s3.medium.2 is offered in eu-de-02", 2, DS_DAY)
    kb.retire(old_retired.id, "the flavor was withdrawn", today=DS_DAY)
    refuted = _aged("ECS flavor s3.medium.8 is offered in eu-de-01", 40, DS_DAY)
    path = recheck.refuted_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("%s\t%s\n" % (refuted.id, (DS_DAY - datetime.timedelta(days=1)).isoformat()), encoding="utf-8")
    built = dataset.build(home, out=tmp_path / "ds", today=DS_DAY, live=False)
    ids = {r["id"] for r in kbreview.read_facts(built.folder / "facts.jsonl")}
    assert newer.id in ids and not {gone.id, old_retired.id, refuted.id} & ids
    assert any("contradicted by a recheck 1" in line for line in built.lines)
    kb.recheck(refuted.id, grade="live", source="live API call, eu-de", checked=DS_DAY, today=DS_DAY)
    built = dataset.build(home, out=tmp_path / "ds", today=DS_DAY, live=False, force=True)
    assert refuted.id in {r["id"] for r in kbreview.read_facts(built.folder / "facts.jsonl")}   # checked after it


def test_ks_a_refuted_verdict_is_recorded_for_the_build(home):
    recheck.record_refuted("KB-AAAA", datetime.date(2026, 10, 24))
    recheck.record_refuted("KB-AAAA", datetime.date(2026, 10, 31))
    recheck.record_refuted("KB-BBBB", datetime.date(2026, 10, 24))
    assert recheck.refuted() == {"KB-AAAA": "2026-10-31", "KB-BBBB": "2026-10-24"}


def test_ks_the_recheck_takes_a_fact_expiring_in_3_days_and_skips_one_in_20(home):
    today = datetime.date.today()
    soon = _aged("ECS flavor s9.medium.1 is offered in eu-de-01", 27, today)
    later = _aged("ECS flavor s9.large.2 is offered in eu-de-02", 10, today)
    for e in (soon, later):
        kb.how(e.id, READ_BLOCK)
    assert kb.get(soon.id).expires == (today + datetime.timedelta(days=3)).isoformat()
    assert kb.get(later.id).expires == (today + datetime.timedelta(days=20)).isoformat()
    picked = recheck.pick("read", today=today)
    assert [e.id for e in picked.run] == [soon.id]
    assert later.id not in {e.id for e in picked.run + picked.no_block + picked.other_tier + picked.owner}
    assert recheck.DUE_DAYS == 8
