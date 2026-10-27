"""P1: `awb paste --own` for our own output, a refused paste is kept for a retry, a taken --as name gets a number.

The owner's rule of 2026-10-27: customer material is checked once, on the way in. Output our own tools made later
(a PoC script, a lab run on our own tenant, an answer of the key service) gets the secret check only, which
replaces in place and never refuses. Background: a 35-minute PoC log was refused for a false "iban" after false
phone and MAC hits, and it was lost because nothing kept a refused paste.
"""
from __future__ import annotations

import io
import os
import random
import stat
import string
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from awb import hooks, paste, projects, rulesync
from tests import fixtures
from tests.test_hooks import THREE_URLS, assert_clean, project  # noqa: F401 - the fixture
from tests.test_lifecycle import spawn
from tests.test_rulesync import old_project

_R = random.Random(20261027)


def _run(chars: str, n: int) -> str:
    return "".join(_R.choice(chars) for _ in range(n))


HEX_ID = _run("0123456789abcdef", 32)
KEY_ID = _run(string.ascii_uppercase, 10) + _run(string.digits, 10)
KEY_SECRET = _run(string.ascii_lowercase, 14) + _run(string.ascii_uppercase, 13) + _run(string.digits, 13)
BEARER = _run(string.ascii_letters + string.digits, 44)
WORD = "Xq7!" + "vR2m#Lp9"
IBAN_SHAPED = "DE" + "89" + "3704" + "0044" + "0532" + "0130" + "00"

OWN_LOG = (
    "2026-10-27 09:14:02 run 3 of the PoC on test-10491\n"
    "created vpc %s in eu-de\n"
    "error Common.0018 from ecs.eu-de.otc.t-systems.com (198.51.100.23), retry in 30 s\n"
    "| c0 | c1 | c2 | c3 | c4 | c5 |\n"
    "|----|----|----|----|----|----|\n"
    "| ecs-a | 10.0.1.12 | up | 4 | 16 | eu-de-01 |\n"
    "bucket ref %s and node poc-node-2.eu-de.compute\n"
    "eth0 ether 52:54:00:12:34:56, call +49 30 1234567\n"
) % (HEX_ID, IBAN_SHAPED)
SECRET_LOG = (
    "the key pair of the run\n"
    "access key %s\n"
    "secret key %s\n"
    "Authorization: Bearer %s\n"
    "password = %s\n"
    "done\n"
) % (KEY_ID, KEY_SECRET, BEARER, WORD)


def test_own_keeps_ids_error_codes_tables_addresses_and_hosts(project):  # noqa: F811
    path, n = paste.own(OWN_LOG, project, today=date(2026, 10, 27))
    assert n == 0 and path == project / "notes" / "pastes" / "2026-10-27-1.txt"
    assert path.read_text(encoding="utf-8") == OWN_LOG               # not one byte changed


def test_own_replaces_a_key_pair_a_bearer_token_and_a_password_and_keeps_the_words(project):  # noqa: F811
    path, n = paste.own(OWN_LOG + SECRET_LOG, project, name="poc-log", today=date(2026, 10, 27))
    text = path.read_text(encoding="utf-8")
    for value in (KEY_ID, KEY_SECRET, BEARER, WORD):
        assert value not in text
    assert n == 4
    assert "access key [secret]\n" in text and "secret key [secret]\n" in text
    assert text.startswith(OWN_LOG) and text.endswith("done\n")


def test_own_on_the_command_line_says_the_count_and_the_file(project, monkeypatch, capsys):  # noqa: F811
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", io.StringIO(OWN_LOG + SECRET_LOG))
    assert paste.main(["--own", "--as", "x-log"]) == 0
    out = capsys.readouterr().out
    assert "own output: 4 secrets replaced" in out and "notes/pastes/" in out and "x-log.txt" in out
    assert_clean(out, "paste output")


def test_the_default_mode_still_masks_and_refuses_what_it_did(project):  # noqa: F811
    """Successor of tests/test_hooks.py::test_t8_awb_paste_masks_the_soft_classes_and_refuses_a_name: the same
    checks, and a name taken today now gets a number instead of a refusal."""
    listing = ("-rw-r----- 1 awb awb     0 2026-10-07 06:02 0001-req.txt\n"
               "last login 2026-10-07 06:02:11 from 198.51.100.23\n" + THREE_URLS +
               "eth0 ether 52:54:00:12:34:56 and call +49 30 1234567\n")
    path, counts, outside = paste.paste(listing, project, today=date(2026, 10, 8))
    assert path == project / "notes" / "pastes" / "2026-10-08-1.txt"
    assert counts == {"ip": 1, "url": 3, "mac": 1, "phone": 1} and outside == 0
    text = path.read_text(encoding="utf-8")
    assert "[ip]" in text and text.count("[url]") == 3 and "[phone]" in text and "[mac]" in text
    for value in ("198.51.100.23", "qrtvb", "52:54:00", "1234567"):
        assert value not in text
    assert "0001-req.txt" in text and "06:02:11" in text
    second, _, _ = paste.paste("plain text\n", project, today=date(2026, 10, 8))
    assert second.name == "2026-10-08-2.txt"
    named, _, _ = paste.paste("plain text\n", project, name="tf-error", today=date(2026, 10, 8))
    assert named.name == "2026-10-08-tf-error.txt"
    again, _, _ = paste.paste("plain text\n", project, name="tf-error", today=date(2026, 10, 8))
    assert again.name == "2026-10-08-tf-error-2.txt"
    before = sorted(p.name for p in path.parent.iterdir())
    with pytest.raises(paste.PasteError) as err:
        paste.paste("the offer for %s\n%s" % (fixtures.CUSTOMER_FORMS[0], THREE_URLS), project,
                    today=date(2026, 10, 8))
    assert "registered name 1" in str(err.value)
    assert_clean(str(err.value), "paste message")
    with pytest.raises(paste.PasteError):
        paste.paste("write to ops@qrtvb.net\n", project, today=date(2026, 10, 8))
    with pytest.raises(paste.PasteError):
        paste.paste(SECRET_LOG, project, today=date(2026, 10, 8))
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_a_taken_name_is_said_in_one_line_and_written_with_a_number(project, monkeypatch, capsys):  # noqa: F811
    monkeypatch.chdir(project)
    for _ in range(2):
        monkeypatch.setattr(sys, "stdin", io.StringIO("plain text\n"))
        assert paste.main(["--as", "build-log"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "awb paste: the name build-log is taken today, written as build-log-2"
    assert "build-log-2.txt" in out[2]


def _refuse(project, monkeypatch, capsys, args=(), body=None):  # noqa: F811
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body or "call %s now\n%s" % (fixtures.PERSON_FORMS[0], OWN_LOG)))
    assert paste.main(list(args)) == 2
    cap = capsys.readouterr()
    assert "nothing was written" in cap.err
    assert_clean(cap.out + cap.err, "paste output")
    kept = list(paste.keep_dir().iterdir())
    assert len(kept) == 1
    stamp = kept[0].name.split("-")[0] + "-" + kept[0].name.split("-")[1]
    return stamp, kept[0], cap.err


def test_a_refused_paste_is_kept_owner_only_outside_the_project_and_names_the_retry(project, monkeypatch,  # noqa: F811
                                                                                     capsys):
    stamp, path, err = _refuse(project, monkeypatch, capsys, ["--as", "poc-log"])
    assert paste.STAMP_RE.fullmatch(stamp) and path.name == "%s-poc-log.txt" % stamp
    assert "`awb paste --retry %s`" % stamp in err and "`awb paste --own --retry %s`" % stamp in err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.parent == Path(os.environ["XDG_STATE_HOME"]) / "awb" / "refused-pastes"
    assert project not in path.parents
    assert fixtures.PERSON_FORMS[0] in path.read_text(encoding="utf-8")   # the raw text, as it came
    assert not (project / "notes" / "pastes").exists() or not list((project / "notes" / "pastes").iterdir())


def test_a_retry_pastes_the_kept_text_and_deletes_it(project, monkeypatch, capsys):  # noqa: F811
    stamp, path, _ = _refuse(project, monkeypatch, capsys, ["--as", "poc-log"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert paste.main(["--retry", stamp]) == 2                        # the entry check refuses it again
    assert "`awb paste --own --retry %s`" % stamp in capsys.readouterr().err
    assert path.is_file() and len(list(path.parent.iterdir())) == 1  # kept once, not twice
    assert paste.main(["--retry", "../../etc"]) == 2


def test_a_paste_the_name_check_could_not_run_on_is_kept(project, monkeypatch, capsys):  # noqa: F811
    def down(*_a, **_k):
        raise hooks.Unavailable("the vault is locked")
    monkeypatch.setattr(hooks, "name_hits", down)
    stamp, path, err = _refuse(project, monkeypatch, capsys, body="plain text\n")
    assert "the name check did not run" in err and "`awb paste --retry %s`" % stamp in err


def test_a_kept_file_older_than_seven_days_goes_at_the_next_paste(project, monkeypatch, capsys):  # noqa: F811
    stamp, path, _ = _refuse(project, monkeypatch, capsys)
    old = path.parent / "20261001-080000-old-run.txt"
    old.write_text("x\n", encoding="utf-8")
    week = time.time() - 8 * 86400
    os.utime(old, (week, week))
    monkeypatch.setattr(sys, "stdin", io.StringIO("plain text\n"))
    assert paste.main([]) == 0
    assert not old.exists() and path.exists()


def test_a_new_project_carries_the_own_output_rule_and_stays_within_its_lines(home, register_path):
    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "- " + projects.OWN_OUTPUT_RULE in text.splitlines()
    assert "awb paste --own --as x-log < /tmp/x.log" in text and "Never pipe a script straight into" in text
    assert len(text.splitlines()) <= projects.MAX_CLAUDE_LINES
    assert rulesync.sync_project(Path(pr.path)) == []


def test_the_sync_adds_the_own_output_rule_once(tmp_path):
    root = old_project(tmp_path)
    rulesync.sync_project(root)
    lines = (root / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    at = lines.index("- RESOURCES.md lists what the project uses, with platform ids and codes.")
    assert lines[at + 1] == "- " + projects.OWN_OUTPUT_RULE
    assert rulesync.sync_project(root) == []
    assert sum("awb paste --own" in line for line in (root / "CLAUDE.md").read_text().splitlines()) == 1
