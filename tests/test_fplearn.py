"""Task FP: the name check learns from its false positives (awb/fplearn.py), on the owner's side, through a safety
gate, without the owner marking anything. Invented hosts, ids and the names of tests/fixtures.py only."""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from awb import check, config, fplearn, hooks, vault
from tests import fixtures

LOG_LINE = ("2026-10-21T08:15:02Z web01.ops-lab.de ERROR 0x80070005 access denied, ticket INC0042137, "
            "upstream cache-2.node.eu reset (ECONNRESET), HTTP/1.1 503")
HOSTS = ("web01.ops-lab.de", "cache-2.node.eu")
SURNAME_HOST = "srv-%s-02.ops-lab.de" % fixtures.PLANTED_PERSON.split()[1].lower()
FIRST_NAME_HOST = "%s-pc01.ops-lab.de" % fixtures.LISTED_FIRST.lower()
NOW = datetime.datetime(2026, 10, 21, 3, 17, tzinfo=datetime.timezone.utc)
EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


def prompt(text: str, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": text})))
    code = hooks.main(["prompt"])
    cap = capsys.readouterr()
    return code, cap.err + cap.out


def files_with(root: Path, needle: str) -> list[Path]:
    out = []
    for f in root.rglob("*"):
        if f.is_file() and not f.is_symlink():
            try:
                if needle.encode() in f.read_bytes():
                    out.append(f)
            except OSError:
                continue
    return out


def learn(p: config.Paths, **kw) -> fplearn.Outcome:
    kw.setdefault("files", [])
    return fplearn.learn(p, **kw)


@pytest.fixture
def owner(monkeypatch):
    """The owner's own terminal: no assistant session in the environment."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def test_a_log_line_is_blocked_before_learning_and_passes_after(home, monkeypatch, capsys):
    code, err = prompt(LOG_LINE, monkeypatch, capsys)
    assert code == hooks.BLOCK and "url 2" in err
    out = learn(home)
    assert out.learned == 2 and not out.dropped
    assert {(c, s) for c, s, _, _ in fplearn.read_allow(home.vault)} == {("url", h) for h in HOSTS}
    assert prompt(LOG_LINE, monkeypatch, capsys)[0] == hooks.OK
    assert check.check_text(LOG_LINE, home.register) == []


def test_a_planted_name_in_the_same_line_stays_blocked(home, monkeypatch, capsys):
    line = "%s; reported by %s; host %s down" % (LOG_LINE, fixtures.PERSON_FORMS[0], SURNAME_HOST)
    assert prompt(line, monkeypatch, capsys)[0] == hooks.BLOCK
    learn(home)
    allowed = {s for _, s, _, _ in fplearn.read_allow(home.vault)}
    assert allowed == set(HOSTS), "only the harmless hosts are learned"
    code, err = prompt(line, monkeypatch, capsys)
    assert code == hooks.BLOCK and "registered name" in err
    left = [(h["cls"], h["length"]) for h in check.check_text(line, home.register)]
    assert ("name", len(fixtures.PERSON_FORMS[0])) in left and ("url", len(SURNAME_HOST)) in left


def test_a_first_name_is_never_learned(home, monkeypatch, capsys, tmp_path, owner):
    mirrors = tmp_path / "mirrors" / "docs"
    mirrors.mkdir(parents=True)
    (mirrors / "page.rst").write_text("the host %s and %s\n" % (FIRST_NAME_HOST, fixtures.LISTED_FIRST.lower()),
                                      encoding="utf-8")
    monkeypatch.setenv("AWB_MIRRORS", str(tmp_path / "mirrors"))
    assert prompt("host %s down" % FIRST_NAME_HOST, monkeypatch, capsys)[0] == hooks.BLOCK
    out = fplearn.learn(home)
    assert out.learned == 0 and fplearn.read_allow(home.vault) == []
    assert out.kept["url"] == 1
    assert fplearn.keep_reason(FIRST_NAME_HOST, "", None) == "first-name"
    with pytest.raises(fplearn.LearnError, match="first name is never released"):
        fplearn.allow_by_hand(home, fixtures.LISTED_FIRST, values=[])
    assert prompt("host %s down" % FIRST_NAME_HOST, monkeypatch, capsys)[0] == hooks.BLOCK


def test_a_title_a_legal_form_and_a_registered_form_stay_blocked(home):
    names = check.checker(home.register)
    assert fplearn.keep_reason("web01.ops-lab.de", "Herrn", names) == "title"
    assert fplearn.keep_reason("ops-gmbh.de", "", names) == "legal-form"
    assert fplearn.keep_reason(fixtures.CUSTOMER_DOMAIN, "", names) == "registered"
    assert fplearn.keep_reason("web01.ops-lab.de", "", names) is None


def test_the_public_corpus_teaches_a_string_the_log_rules_do_not(home, monkeypatch, capsys, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "note.md").write_text("resolver 8.8.4.4 answers\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KB", str(kb))
    p = config.paths()
    assert prompt("resolver 8.8.4.4 timed out", monkeypatch, capsys)[0] == hooks.BLOCK
    assert fplearn.log_rule("8.8.4.4") is None
    out = fplearn.learn(p, files=fplearn.corpus_files(p))
    assert [(c, s, r) for c, s, r, _ in fplearn.read_allow(p.vault)] == [("ip", "8.8.4.4", "corpus")]
    assert out.learned == 1
    assert prompt("resolver 8.8.4.4 timed out", monkeypatch, capsys)[0] == hooks.OK


def test_the_string_never_reaches_a_work_user_file(home, monkeypatch, capsys, tmp_path):
    """The prompt hook of the work side: the register lies encrypted, the check and the record go to the vault
    daemon, which writes the string inside the owner's vault alone."""
    run = Path(tempfile.mkdtemp(prefix="awb", dir="/tmp"))
    try:
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(run / "check.sock"))
        monkeypatch.setenv("AWB_ADMIN_SOCKET", str(run / "admin.sock"))
        owner_side = config.paths()
        d = vault.Daemon(owner_side)
        d.start()
        try:
            work = tmp_path / "work-vault"
            work.mkdir()
            (work / "register.tsv.gpg").write_bytes(b"sealed")
            monkeypatch.setenv("AWB_VAULT", str(work))
            code, said = prompt(LOG_LINE, monkeypatch, capsys)
            assert code == hooks.BLOCK
        finally:
            d.stop()
    finally:
        shutil.rmtree(run, ignore_errors=True)
    for host in HOSTS:
        assert host not in said
        holders = files_with(tmp_path, host)
        assert holders and all(fplearn.folder(owner_side.vault) in f.parents for f in holders), holders
        for f in holders:
            assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(fplearn.folder(owner_side.vault).stat().st_mode) == 0o700
    rows = fplearn.read_blocks(owner_side.vault, EPOCH)
    assert {r["string"] for r in rows if r["class"] == "url"} == set(HOSTS)
    assert {r["source"] for r in rows} == {"prompt"}


def test_the_daemon_applies_the_learned_list_for_the_work_user(home, monkeypatch):
    fplearn.write_allow(home.vault, [("url", HOSTS[0], "log-host", "2026-10-21")])
    run = Path(tempfile.mkdtemp(prefix="awb", dir="/tmp"))
    try:
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(run / "check.sock"))
        monkeypatch.setenv("AWB_ADMIN_SOCKET", str(run / "admin.sock"))
        d = vault.Daemon(config.paths())
        d.start()
        try:
            remote = check.RemoteCheck(run / "check.sock")
            text = "host %s and %s down" % HOSTS
            assert [h["cls"] for h in remote.scan(text)] == ["url"]
        finally:
            d.stop()
    finally:
        shutil.rmtree(run, ignore_errors=True)


def test_the_safety_gate_drops_a_list_change_that_loses_a_planted_name(home, monkeypatch, capsys):
    assert prompt("host %s down; %s" % (SURNAME_HOST, LOG_LINE), monkeypatch, capsys)[0] == hooks.BLOCK
    real = fplearn.decide

    def broken(cands, names, files):
        # a learning rule broken on purpose: it takes every string for harmless
        return {key: "log-host" for key in real(cands, names, files)}

    monkeypatch.setattr(fplearn, "decide", broken)
    out = learn(home)
    assert out.dropped and out.learned == 0
    assert fplearn.read_allow(home.vault) == []
    assert "the safety gate dropped the change" in out.line("2026-W43")
    assert fixtures.PLANTED_PERSON.split()[1] not in out.line("2026-W43")
    assert prompt("host %s down" % HOSTS[0], monkeypatch, capsys)[0] == hooks.BLOCK


def test_the_safety_gate_runs_the_red_team_pack_and_the_planted_sets():
    values = fplearn.planted_values() + fplearn.pack_values()
    assert len(fplearn.pack_values()) >= 405
    assert fplearn.safety_gate(set(), {("url", h) for h in HOSTS}, values) == []
    planted_host = "%s.ops-lab.de" % fixtures.SECOND_LAST.lower()
    assert fixtures.SECOND_LAST in fplearn.safety_gate(set(), {("url", planted_host)}, values)


def test_a_missing_red_team_pack_drops_the_change(home, monkeypatch, capsys):
    monkeypatch.setattr(fplearn, "_repo_roots", lambda: [])
    assert prompt(LOG_LINE, monkeypatch, capsys)[0] == hooks.BLOCK
    out = learn(home, values=None)
    assert out.learned == 0 and "not found" in out.note
    assert fplearn.read_allow(home.vault) == []


def test_the_list_never_relaxes_a_name_or_a_mail(home):
    mail = "ops@web01.ops-lab.de"
    fplearn.write_allow(home.vault, [("name", fixtures.PERSON_FORMS[0], "x", "x"), ("mail", mail, "x", "x"),
                                     (fplearn.ANY, mail, "hand", "x")])
    assert fplearn.load_allow(home.vault) == frozenset({(fplearn.ANY, mail)})
    text = "%s wrote from %s" % (fixtures.PERSON_FORMS[0], mail)
    assert {h["cls"] for h in check.check_text(text, home.register)} == {"name", "mail"}


def test_the_weekly_line_counts_only_and_goes_once_a_week(home, monkeypatch, capsys):
    assert prompt(LOG_LINE + " " + fixtures.PERSON_FORMS[0], monkeypatch, capsys)[0] == hooks.BLOCK
    fplearn.record("gate", extra={"secret": 1, "name": 2}, p=home)
    sent = []
    out = learn(home)
    line = fplearn.weekly(home, out, now=NOW, send=lambda subject, body: sent.append(body))
    assert line == sent[0]
    assert "blocks 2 (gate 1, prompt 1); learned 2; kept blocked 4 (name 3, secret 1)" in line
    for host in HOSTS:
        assert host not in line
    fixtures.assert_no_fixture_name(line, "the weekly line")
    assert fplearn.weekly(home, out, now=NOW + datetime.timedelta(days=3), send=sent.append) is None
    assert fplearn.weekly(home, out, now=NOW + datetime.timedelta(days=7), send=lambda s, b: sent.append(b))
    assert len(sent) == 2


def test_allow_by_hand_and_the_list_are_the_owners_terminal_alone(home, monkeypatch, capsys, owner):
    monkeypatch.setattr(fplearn, "pack_values", lambda: [])
    assert check.main(["allow", "web01.ops-lab.de"]) == 0
    assert "web01.ops-lab.de" not in capsys.readouterr().out
    assert check.main(["allow", "--list"]) == 0
    assert "*\tweb01.ops-lab.de\thand" in capsys.readouterr().out
    assert check.check_text("host web01.ops-lab.de down", home.register) == []
    monkeypatch.setenv("CLAUDECODE", "1")
    assert check.main(["allow", "--list"]) == 2
    cap = capsys.readouterr()
    assert "web01" not in cap.out + cap.err and "own terminal" in cap.err
    assert check.main(["allow", "cache-2.node.eu"]) == 2


def test_awb_check_and_the_gate_record_their_blocks(home, tmp_path, capsys):
    f = tmp_path / "log.txt"
    f.write_text(LOG_LINE + "\n", encoding="utf-8")
    assert check.main([str(f)]) == 1
    from awb import gate

    msg = tmp_path / "msg.txt"
    msg.write_text("fix token ghp_%s\n" % ("a1B2c3D4e5" * 4), encoding="utf-8")
    assert gate.main(["--message", str(msg)]) == 1
    capsys.readouterr()
    rows = fplearn.read_blocks(home.vault, EPOCH)
    assert {r["string"] for r in rows if r["source"] == "check"} == set(HOSTS)
    gate_rows = [r for r in rows if r["source"] == "gate"]
    assert gate_rows and {r["string"] for r in gate_rows} == {"-"}


def test_the_support_log_set_measured_before_and_after(tmp_path, monkeypatch):
    from calibration.fp import support_logs

    for name in ("AWB_VAULT", "AWB_SHARED"):
        monkeypatch.setenv(name, os.environ[name])
    got = support_logs.measure(tmp_path)
    assert got["before"] == got["lines"], "every line of the set is blocked before the learning"
    assert got["after"]["harmless"] < got["before"]["harmless"]
    assert got["after"]["planted"] == got["lines"]["planted"], "a planted name passed after the learning"
    assert got["dropped"] == 0
