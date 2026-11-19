"""Tests of Q-0019 S3, S4 and S7: the register of packages, the stop hook question, the sweep, the refresh part and
the board line. Invented projects and the stand-in price API of test_solutions only."""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from awb import cli, packages, projects, sessions
from awb.tcp import solutions
from awb.tcp import board, refresh
from tests import test_kbpattern as tk
from tests import test_solutions as ts

TODAY = date.today()
THIRD = "tcp-m5vd"


def register(home, *codes_and_folders):
    home.projects_register.parent.mkdir(parents=True, exist_ok=True)
    projects._save(home, [projects.Project(code, "query", projects.NO_CUSTOMER, "tcp", str(folder),
                                           projects.memory_key(str(folder)), "active", "2026-11-01")
                          for code, folder in codes_and_folders])


def transcript(path: Path, *calls) -> Path:
    """A transcript with one assistant line per call: ("write", PATH) or ("cmd", COMMAND)."""
    lines = []
    for kind, value in calls:
        use = ({"type": "tool_use", "name": "Write", "input": {"file_path": value, "content": "x"}} if kind == "write"
               else {"type": "tool_use", "name": "Bash", "input": {"command": value}})
        lines.append(json.dumps({"type": "assistant", "message": {"content": [use]}}))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(l + "\n" for l in lines))
    return path


def row(**kw):
    out = {"slug": "cad-on-tcp", "kind": "offer", "tags": "gpu,vdi", "state": "released", "version": "v1",
           "released": "2026-11-19", "project": ts.SOLUTIONS, "counts": "changed=1;new=0", "checked": "2026-11-19"}
    out.update(kw)
    return out


def test_the_register_keeps_one_row_per_package_and_feeds_the_board(home):
    assert packages.line(home) == "packages: none registered"
    packages.record(row(), home)
    packages.record(row(state="stale", counts="changed=1;new=3;price=0"), home)
    packages.record(row(slug="azure-vm-migration", state="draft", version="", counts=""), home)
    assert [r["slug"] for r in packages.rows(home)] == ["azure-vm-migration", "cad-on-tcp"]
    line = packages.line(home)
    assert line == "packages: azure-vm-migration - draft, cad-on-tcp v1 stale (changed 1, new 3)"
    assert board.packages_line(home) == line
    with pytest.raises(ValueError):
        packages.record(row(counts="Zyxwo=1"), home)
    with pytest.raises(ValueError):
        packages.record(row(project="not-a-code"), home)


def test_build_status_and_release_write_the_register(home, monkeypatch):
    d, pkg, _, _ = ts.solutions_project(home, monkeypatch)
    solutions.build("cad-on-tcp", project=d, fetcher=ts.fetcher(), pdf=False)
    assert packages.rows(home)[0]["state"] == "draft"
    solutions.release("cad-on-tcp", project=d)
    r = packages.rows(home)[0]
    assert (r["state"], r["version"], r["project"], r["tags"]) == ("released", "v1", ts.SOLUTIONS, "gpu,vdi")


def _source(home):
    d = tk.project(home)
    (d / "deliverables").mkdir(exist_ok=True)
    return d


def test_the_stop_hook_asks_once_per_batch_of_changed_deliverables(home, tmp_path):
    packages.record(row(), home)
    d = _source(home)
    t = transcript(tmp_path / "t.jsonl", ("write", str(d / "deliverables" / "offer.md")))
    msg = packages.due(home, d, t, "s1")
    assert msg and "cad-on-tcp" in msg and "awb kb pattern add" in msg and "1 deliverable" in msg
    assert packages.due(home, d, t, "s1") is None                         # the same change never asks twice
    transcript(t, ("write", str(d / "deliverables" / "sizing.md")), ("cmd", "awb kb pattern add --package x y"))
    assert packages.due(home, d, t, "s1") is None                         # it wrote cards after the change
    transcript(t, ("write", str(d / "notes" / "scratch.md")))
    assert packages.due(home, d, t, "s1") is None                         # not a deliverable
    transcript(t, ("write", str(d / "deliverables" / "plan.md")))
    assert packages.due(home, d, t, "s1")                                  # a new batch asks again


def test_the_stop_hook_skips_other_tags_a_retired_package_and_the_solutions_project(home, tmp_path):
    d = _source(home)
    t = transcript(tmp_path / "t.jsonl", ("write", str(d / "deliverables" / "offer.md")))
    assert packages.due(home, d, t, "a") is None                          # no package registered
    packages.record(row(tags="sap"), home)
    assert packages.due(home, d, t, "b") is None
    packages.record(row(state="retired"), home)
    assert packages.due(home, d, t, "c") is None
    packages.record(row(project=tk.CODE), home)
    assert packages.due(home, d, t, "d") is None


def test_the_stop_hook_carries_the_question(home, tmp_path, monkeypatch, capsys):
    from awb import hooks

    packages.record(row(), home)
    d = _source(home)
    t = transcript(tmp_path / "t.jsonl", ("write", str(d / "deliverables" / "offer.md")))
    monkeypatch.setattr(hooks, "_session_folder", lambda data: d)
    monkeypatch.setattr(hooks, "draft_gate", lambda data, root: [])
    code = hooks.hook_stop({"transcript_path": str(t), "session_id": "s9", "cwd": str(d)})
    assert code == hooks.BLOCK and "Pattern cards before you stop" in capsys.readouterr().err
    hooks.hook_stop({"transcript_path": str(t), "session_id": "s9", "cwd": str(d)})
    assert "Pattern cards" not in capsys.readouterr().err


def test_the_sweep_counts_projects_and_asks_only_the_changed_ones(home, monkeypatch, capsys):
    d, pkg, _, _ = ts.solutions_project(home, monkeypatch)
    (pkg / "PACKAGE.md").write_text((pkg / "PACKAGE.md").read_text().replace("tags: gpu, vdi\n",
                                                                             "tags: gpu, vdi\nterms: FSLogix, vGPU\n"))
    src = home.projects_root / tk.CODE
    (src / "notes").mkdir(exist_ok=True)
    (src / "notes" / "hosts.md").write_text("Profiles on FSLogix, one vGPU per host.\n")
    (src / "input").mkdir(exist_ok=True)
    (src / "input" / "raw.md").write_text("FSLogix\n")
    third = home.projects_root / THIRD
    (third / "notes").mkdir(parents=True)
    (third / "notes" / "x.md").write_text("Nothing about it.\n")
    other = tk.project(home, tk.OTHER, resource="vtrqz-host-02", goal="plorvex", total="912.30")
    (other / "STATE.md").write_text("We size vGPU hosts.\n")
    register(home, (ts.SOLUTIONS, d), (tk.CODE, src), (THIRD, third), (tk.OTHER, other))
    sessions.claim(home, tk.OTHER, "session-1")
    noted, started = [], []
    found = solutions.sweep("cad-on-tcp", project=d, p=home, noter=lambda c, f: noted.append(c),
                            starter=lambda c, f: started.append(c))
    assert {r["code"]: (r["files"], r["action"]) for r in found} == {
        tk.CODE: (1, "side session contrib-cad-on-tcp"), tk.OTHER: (1, "note")}
    assert noted == [tk.OTHER] and started == [tk.CODE]
    again = solutions.sweep("cad-on-tcp", project=d, p=home, noter=lambda c, f: pytest.fail("asked twice"),
                            starter=lambda c, f: pytest.fail("asked twice"))
    assert {r["action"] for r in again} == {"no change since the last sweep"}
    later = time.time() + 5
    os.utime(src / "notes" / "hosts.md", (later, later))
    dry = solutions.sweep("cad-on-tcp", project=d, p=home, dry_run=True)
    assert [r["action"] for r in dry if r["code"] == tk.CODE] == ["side session contrib-cad-on-tcp (dry run)"]
    code = cli.main(["solutions", "sweep", "cad-on-tcp", "--dry-run", "--project", str(d)])
    out = capsys.readouterr().out
    assert code == 0 and "%s  1 file(s)" % tk.CODE in out and "FSLogix" not in out and "vGPU" not in out


def test_the_sweep_finds_its_solutions_project_by_the_register(home, monkeypatch, tmp_path):
    d, pkg, _, _ = ts.solutions_project(home, monkeypatch)
    register(home, (ts.SOLUTIONS, d))
    solutions.status("cad-on-tcp", project=d, offline=True, p=home)
    monkeypatch.chdir(tmp_path)
    assert solutions.sweep("cad-on-tcp", p=home, dry_run=True) == []


def test_the_refresh_part_runs_the_status_of_every_package(home, monkeypatch):
    d, pkg, _, _ = ts.solutions_project(home, monkeypatch)
    register(home, (ts.SOLUTIONS, d))
    part = refresh.part_packages(home, today=TODAY)
    assert part.lines == ["no package registered"] and part.overdue == 0
    solutions.build("cad-on-tcp", project=d, fetcher=ts.fetcher(), pdf=False)
    solutions.release("cad-on-tcp", project=d)
    monkeypatch.setattr(solutions, "_price_fetcher", lambda: ts.fetcher("9.99"))
    part = refresh.part_packages(home, today=TODAY)
    assert part.lines == ["cad-on-tcp  v1  stale  changed 0, retired 0, expired 0, new 0, price 1, service 0, age 0"
                          "  OVERDUE"]
    assert part.overdue == 1
    assert "price=1" in packages.rows(home)[0]["counts"]
    assert "packages" in refresh.PARTS
