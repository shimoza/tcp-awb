"""tcp-bytg ask 2026-11-21-tcp-bytg-20: STATE.md and OPEN.md pass the start hook with reserved test domains and
private lab addresses, and `awb state rotate` moves the old dated layers to history/ and writes the Pinned block."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from awb import hooks, state, status

CODE = "tcp-q7m4"


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    (root / "deliverables").mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- kind: lab\n- goal: a small lab\n" % (CODE, CODE),
                                   encoding="utf-8")
    (root / "OPEN.md").write_text("# Open items\n\n- size the backup\n", encoding="utf-8")
    return root


def _start_context(root: Path, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(root)})))
    assert hooks.main(["session-start"]) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _state_section(text: str) -> str:
    return text.split("## STATE.md")[1].split("## OPEN.md")[0]


def test_reserved_domains_and_private_addresses_pass_the_start_hook(project, monkeypatch, capsys):
    (project / "STATE.md").write_text(
        "Status: the lab forest runs\nNext: join the host\n\nforest corp.example.test, host dc1.lab.local, "
        "addresses 10.0.0.5, 172.20.1.9, 192.168.4.4, 100.100.1.1 and fd00::5:1\n", encoding="utf-8")
    text = _start_context(project, monkeypatch, capsys)
    assert "corp.example.test" in _state_section(text)
    assert "withheld" not in _state_section(text)


def test_a_public_address_or_a_real_looking_domain_is_still_withheld(project, monkeypatch, capsys):
    # DMZ 2: the start shows STATE.md as written, public addresses and domains included
    for line in ("the gateway is 8.8.4.4", "see portal.vendor-corp.com", "see 100.200.1.1", "see 172.32.0.1"):
        (project / "STATE.md").write_text("Status: x\nNext: y\n\n" + line + "\n", encoding="utf-8")
        text = _start_context(project, monkeypatch, capsys)
        assert "withheld" not in _state_section(text) and line in _state_section(text), line


def _layer(day: str, extra: str = "") -> str:
    return "### %s step\n\n- did the work of %s\n%s\n" % (day, day, extra)


def _state_text() -> str:
    return ("# State of tcp-q7m4\n\nStatus: building\nNext: the network\n\n## Now\n\nwork goes on\n\n"
            + _layer("2026-11-20") + _layer("2026-11-21", "pinned: Limit: no change on Fridays\n")
            + _layer("2026-11-22") + _layer("2026-11-23") + _layer("2026-11-24"))


def test_rotate_moves_old_layers_keeps_the_top_and_writes_pinned(project):
    (project / "STATE.md").write_text(_state_text(), encoding="utf-8")
    moved, files = state.rotate(project, 3)
    assert moved == 2
    assert files == ["history/STATE-2026-11-20.md", "history/STATE-2026-11-21.md"]
    text = (project / "STATE.md").read_text(encoding="utf-8")
    assert text.startswith("# State of tcp-q7m4\n\nStatus: building\nNext: the network\n")
    assert "2026-11-20" not in text and "2026-11-21" not in text
    for day in ("2026-11-22", "2026-11-23", "2026-11-24"):
        assert "### %s step" % day in text
    assert status.pinned(text) == ["- Limit: no change on Fridays"]
    assert status.pin_shape(text) is None
    old = (project / "history" / "STATE-2026-11-21.md").read_text(encoding="utf-8")
    assert "did the work of 2026-11-21" in old


def test_a_second_rotate_changes_nothing(project):
    (project / "STATE.md").write_text(_state_text(), encoding="utf-8")
    state.rotate(project, 3)
    before = {p: p.read_bytes() for p in [project / "STATE.md", *(project / "history").iterdir()]}
    assert state.rotate(project, 3) == (0, [])
    after = {p: p.read_bytes() for p in [project / "STATE.md", *(project / "history").iterdir()]}
    assert before == after


def test_a_pinned_line_of_another_kind_is_refused_and_nothing_is_written(project):
    text = _state_text() + "pinned: Mood: calm\n"
    (project / "STATE.md").write_text(text, encoding="utf-8")
    with pytest.raises(state.Refused):
        state.rotate(project, 3)
    assert (project / "STATE.md").read_text(encoding="utf-8") == text
    assert not (project / "history").exists()
