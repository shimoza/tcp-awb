"""UX2 of 2026-11-11: `awb owner status` lists decisions only.

One project with 12 creates kept on purpose, 9 owner items and 3 tooling items; two stopped build tasks (one by the
owner, one red). Invented codes only; the vault, the runs and the project are temporary folders.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from awb import config, ownerwait, projects
from awb.tcp import labadmin, owner
from tests import fixtures as fx

LAB = fx.TENANT_ONE
TOOLING = ["owner (tooling) the runner needs a new timer", "(tooling) widen the rebase merge",
           "Steering: pick the next UX task"]


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    lines = ["## Waiting on the owner", ""]
    lines += ["- Owner item %d: decide the region" % n for n in range(1, 10)]
    lines += ["- %s" % t for t in TOOLING]
    (Path(proj.path) / "OPEN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    now = time.time()
    run = {"id": "run-20261111-060000-abcd", "project": proj.code, "tenant": LAB, "state": "ended", "note": "",
           "started": "2026-11-11T06:00:00Z", "started_ts": now - 3600, "calls": 12,
           "created": [{"n": n, "label": "vpc vpcs", "name": "k%d" % n, "state": "kept", "kept_ts": now,
                        "reason": "stand %d" % n,
                        "delete": {"method": "DELETE", "service": "vpc", "path": "/v1/x/vpcs/%032d" % n}}
                       for n in range(12)]}
    labadmin.save(home, run)
    runs = tmp_path / "runs"
    for tid, state in (("B-101", "stopped: by_owner"), ("B-102", "red")):
        (runs / tid / "1").mkdir(parents=True)
        (runs / tid / "1" / "state").write_text(state + "\t2026-11-11T07:00:00Z\n")
    return home, proj, runs, tmp_path / "none.md"


def _lines(world, **kw):
    home, _proj, runs, queue = world
    items, parked = ownerwait.collect(home, queue, runs, **kw)
    return ownerwait.render(items, parked)


def test_the_default_list_holds_decisions_only(world):
    _home, proj, _runs, _q = world
    lines = _lines(world)
    text = "\n".join(lines)
    assert lines[0] == "7 items wait on you"
    kept = [ln for ln in lines if "kept on" in ln]
    assert len(kept) == 1 and "%s  12 creates kept on %s, listed in RESOURCES.md" % (proj.code, LAB) in kept[0]
    assert len([ln for ln in lines if ln.startswith("B-")]) == 1 and "B-102" in text and "B-101" not in text
    assert [ln.split("  ")[1] for ln in lines if "Owner item" in ln] == [
        "Owner item %d: decide the region" % n for n in range(1, 6)]
    assert "and 4 more: awb owner status --all %s" % proj.code in lines
    assert "tooling" not in text and "Steering" not in text
    fx.assert_no_fixture_name(text, "the owner list")


def test_a_task_that_ended_with_success_is_no_item(world):
    _home, _proj, runs, _q = world
    (runs / "B-101" / "1" / "state").write_text("stopped: success\t2026-11-11T07:00:00Z\n")
    assert "B-101" not in "\n".join(_lines(world))


def test_dev_prints_the_tooling_items(world):
    lines = _lines(world, view="dev")
    assert lines[0] == "3 items wait on you" and len(lines) == 4
    assert any("Steering: pick the next UX task" in ln for ln in lines)


def test_all_prints_every_source_line(world):
    _home, proj, _runs, _q = world
    lines = _lines(world, view="all")
    assert lines[0] == "26 items wait on you"
    assert len([ln for ln in lines if "kept vpc vpcs" in ln]) == 12
    assert any("B-101" in ln for ln in lines) and not any("more: awb owner status" in ln for ln in lines)
    only = _lines(world, view="all", code=proj.code)
    assert only[0] == "24 items wait on you"


def test_the_mail_follows_the_default_list(world):
    home, _proj, runs, queue = world
    sent = []
    assert ownerwait.mail(home, lambda s, b: sent.append((s, b)) or "sent", queue, runs) == "sent"
    subject, body = sent[0]
    assert subject == "Workbench: 7 items wait on you" and "more: awb owner status --all" in body
    assert "B-101" not in body and "tooling" not in body


def test_the_command_takes_dev_and_all(world, capsys, monkeypatch):
    home, proj, runs, queue = world
    monkeypatch.setattr(ownerwait, "queue_file", lambda: queue)
    monkeypatch.setenv("AWB_BUILD_RUNS", str(runs))
    monkeypatch.setattr(config, "paths", lambda: home)
    assert owner.main(["status", "--dev"]) == 0
    assert "3 items wait on you" in capsys.readouterr().out
    assert owner.main(["status", "--all", proj.code]) == 0
    assert "24 items wait on you" in capsys.readouterr().out
