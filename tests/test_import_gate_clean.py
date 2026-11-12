"""UX16 of 2026-11-11: an import, also with --trusted, leaves no home path and no blocklist word in a copy, so the
commit gate accepts the copies; the import ends with the gate's check on them. Invented names only."""

from __future__ import annotations

import pytest

from awb import bucket, config, gate, importcmd, projects, start

GOAL = "move two app clusters to managed k8s"
PLANTED = "/home/" + "qvelmar" + "/work/export"
BLOCK_WORD = "CLIENT4711"          # the shape of tests/blocklist.txt
PLAIN = "Outcome\n\nthe cluster moved to managed k8s in two steps.\n"
PLANTED_TEXT = PLAIN + "log in %s/run.log\nticket %s was closed\n" % (PLANTED, BLOCK_WORD)


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))
    blocklist = tmp_path / "temporary-blocklist.txt"
    blocklist.write_text("(?<![A-Za-z0-9])CLIENT[0-9]+(?![A-Za-z0-9])\n", encoding="utf-8")
    monkeypatch.setattr(gate, "blocklist_files", lambda: [blocklist])


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


def _project(home, capsys):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    return pr


def _copies(home, cust):
    return [x for x in (home.outbox / cust).rglob("F-*") if x.is_file()]


def _import(home, pr, tmp_path, text, capsys, **kw):
    f = tmp_path / "notes.md"
    f.write_text(text, encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True, **kw) == 0
    cap = capsys.readouterr()
    return cap.out + cap.err


@pytest.mark.parametrize("trusted", [True, False])
def test_the_copy_holds_no_home_path_and_no_blocklist_word_and_the_gate_passes(home, capsys, tmp_path, trusted):
    pr = _project(home, capsys)
    out = _import(home, pr, tmp_path, PLANTED_TEXT, capsys, trusted=trusted)
    (copy,) = _copies(home, pr.customer)
    body = copy.read_text(encoding="utf-8")
    assert "qvelmar" not in body and BLOCK_WORD not in body
    assert "[path]" in body and "[withheld]" in body
    assert gate.main(["--no-register", str(copy)]) == 0
    assert "1 copies, gate clean" in out


def test_a_file_with_neither_is_copied_as_before(home, capsys, tmp_path):
    pr = _project(home, capsys)
    _import(home, pr, tmp_path, PLAIN, capsys, trusted=True)
    (copy,) = _copies(home, pr.customer)
    body = copy.read_text(encoding="utf-8")
    assert "[path]" not in body and "[withheld]" not in body
    assert PLAIN.strip() in body


def test_redo_wipes_the_two_classes_of_the_last_import(home, capsys, tmp_path, monkeypatch):
    pr = _project(home, capsys)
    with monkeypatch.context() as m:   # an import of the old kind
        m.setattr(gate, "wipe_gate_classes", lambda t: (t, gate.Counter()))
        _import(home, pr, tmp_path, PLANTED_TEXT, capsys, trusted=True)
    (copy,) = _copies(home, pr.customer)
    assert "qvelmar" in copy.read_text(encoding="utf-8")
    assert importcmd.run(home, pr.code, [], tty=True, trusted=True, redo=True) == 0
    body = copy.read_text(encoding="utf-8")
    assert "qvelmar" not in body and "[path]" in body
    assert gate.main(["--no-register", str(copy)]) == 0


def test_the_classes_left_are_named_when_the_gate_finds_one(home, capsys, tmp_path, monkeypatch):
    pr = _project(home, capsys)
    monkeypatch.setattr(gate, "classes_left", lambda t: ["homepath"])
    out = _import(home, pr, tmp_path, PLAIN, capsys, trusted=True)
    assert "homepath" in out and "gate clean" not in out
