"""awb winlab list in a session of the work user (defect of 2026-11-06): the session reads no registry of its own,
so the list asks the broker and shows only the windows of its own project, never "no lab machine registered"."""
from __future__ import annotations

import pytest

from awb import winlab, winlabsession

from tests.test_wl_broker import MACHINE, OTHER, PROJECT, lab, served  # noqa: F401  (fixtures)


def test_wl_session_list_shows_the_window_of_its_own_project_from_the_broker(served, monkeypatch):
    lab = served
    winlab.open_(MACHINE, 60, PROJECT, path=lab.registry, now=lab.clock[0])
    assert not winlabsession._local_registry()
    lines = winlabsession.session_list(lab.paths.projects_root / PROJECT)
    assert lines[0].startswith("machine")
    assert lines[1].startswith("%s  %s  " % (MACHINE, PROJECT)) and len(lines[1].split()) == 3
    assert not any("registered" in ln for ln in lines)


def test_wl_session_list_of_another_project_sees_no_machine_and_says_who_opens_one(served):
    lab = served
    winlab.open_(MACHINE, 60, PROJECT, path=lab.registry, now=lab.clock[0])
    lines = winlabsession.session_list(lab.paths.projects_root / OTHER)
    assert lines == ["no lab machine has an open window for %s (the owner: awb winlab open NAME --minutes N "
                     "--project %s)" % (OTHER, OTHER)]
    assert MACHINE not in lines[0]


def test_wl_list_command_of_a_session_uses_the_broker(served, monkeypatch, capsys):
    lab = served
    winlab.open_(MACHINE, 60, PROJECT, path=lab.registry, now=lab.clock[0])
    monkeypatch.chdir(lab.paths.projects_root / PROJECT)
    assert winlab.main(["list"]) == 0
    out = capsys.readouterr().out
    assert MACHINE in out and "no lab machine registered" not in out
