"""UX review 2026-10-24, changes 1 and 3: `awb start --from DIR [--trusted]` imports the whole folder inside the
start and ends with the app line; the import timer runs every 2 minutes and a session hears of an auto import with
one line at its next prompt. Invented names only, temporary folders."""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest

from awb import bucket, cli, config, importcmd, projects, rulesync, start
from tests import fixtures as fx
from tests.test_hooks import run_hook
from tests.test_import_auto import GOAL, ROOT
from tests.test_seal import SETUP, USER, VERIFY, commands, fakebin, no_marks, run_script  # noqa: F401


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _lines(capsys) -> str:
    out, err = capsys.readouterr()
    return out + err


def _export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    (root / "a" / "b").mkdir(parents=True)
    (root / "top.md").write_text("The pilot ran on KVM for %s.\n" % fx.SECOND_LAST, encoding="utf-8")
    (root / "a" / "mid.md").write_text("Second file, nothing special.\n", encoding="utf-8")
    (root / "a" / "b" / "deep.md").write_text("Third file in a subfolder.\n", encoding="utf-8")
    os.symlink(root / "top.md", root / "link.md")
    return root


def test_from_walks_the_subfolders_and_skips_links(tmp_path):
    files, skipped = start.walk_from(str(_export(tmp_path)))
    assert {f.name for f in files} == {"top.md", "mid.md", "deep.md"} and skipped == 1


def test_from_imports_every_file_of_the_folder_trusted_and_ends_with_the_app_line(home, capsys, tmp_path):
    root = _export(tmp_path)
    assert start.start(home, "project", GOAL, None, from_dir=str(root), trusted=True) == 0
    (pr,) = projects.load(home)
    out, err = capsys.readouterr()
    assert "files taken: 3" in out and "copies written: 3" in out and "trusted:" in out
    assert "not a regular file" not in out + err
    last = out.strip().splitlines()
    assert last[-2] == "open: awb projects open %s" % pr.code
    assert last[-1] == "app: SSH host awb-10491, folder %s" % pr.path
    assert fx.SECOND_LAST in "\n".join(f.read_text(encoding="utf-8") for f in (home.outbox / pr.code).glob("F-*.md"))


def test_from_without_trusted_runs_the_wipe_mode_import(home, capsys, tmp_path):
    assert start.start(home, "query", GOAL, None, from_dir=str(_export(tmp_path))) == 0
    (pr,) = projects.load(home)
    text = "\n".join(f.read_text(encoding="utf-8") for f in (home.outbox / pr.code).glob("F-*.md"))
    assert "KVM" in text and "trusted:" not in _lines(capsys)


def test_from_is_checked_before_anything_is_made(home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    for bad in (str(empty), str(tmp_path / "missing")):
        with pytest.raises(start.StartError):
            start.start(home, "project", GOAL, None, from_dir=bad)
    with pytest.raises(start.StartError, match="--trusted goes with --from"):
        start.start(home, "project", GOAL, None, trusted=True)
    assert not projects.load(home)


def test_without_from_the_start_ends_with_the_open_line_as_before(home, capsys):
    assert start.start(home, "project", GOAL, None) == 0
    (pr,) = projects.load(home)
    assert capsys.readouterr().out.strip().splitlines()[-1] == "open: awb projects open %s" % pr.code


def test_the_command_line_takes_from_and_trusted(home, capsys, tmp_path):
    assert cli.main(["start", "project", "--goal", GOAL, "--from", str(_export(tmp_path)), "--trusted"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("app: SSH host awb-10491, folder ")


def test_an_auto_import_posts_a_notice_and_the_next_prompt_prints_one_line(home, register_path, monkeypatch, capsys,
                                                                          tmp_path):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    payload = {"cwd": pr.path, "session_id": "a-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    f = tmp_path / "notes.clean.md"
    f.write_text("Outcome on KVM.\n", encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=False, trusted=True, auto=True) in (0, 1)
    capsys.readouterr()
    (fid,) = [x.stem for x in (home.outbox / pr.customer).glob("F-*.md")]
    notices = [json.loads(x.read_text(encoding="utf-8")) for x in rulesync.notices_dir(home).glob("input-*.json")]
    assert notices and notices[-1]["auto"] is True
    code, out, _ = run_hook("prompt", dict(payload, prompt="go on"), monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "new input: 1 files (%s)" % fid
    assert run_hook("prompt", dict(payload, prompt="go on"), monkeypatch, capsys) == (0, "", "")


def test_a_manual_import_keeps_its_older_notice_line(home, tmp_path):
    (home.outbox / fx.CUSTOMER_CODE).mkdir(parents=True, exist_ok=True)
    (home.outbox / fx.CUSTOMER_CODE / "F-ABCD.md").write_text("x", encoding="utf-8")
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    rulesync.post_input(home, fx.CUSTOMER_CODE, root.name, ["F-ABCD"])
    assert rulesync.input_notices(home, "s", root) == [rulesync.INPUT % (1, fx.CUSTOMER_CODE)]


def test_a_long_auto_notice_shows_eight_ids_and_the_rest_as_a_count(home, tmp_path):
    (home.outbox / fx.CUSTOMER_CODE).mkdir(parents=True, exist_ok=True)
    ids = ["F-AAA%s" % a for a in "ABCDEFGHIJ"]
    for i in ids:
        (home.outbox / fx.CUSTOMER_CODE / (i + ".md")).write_text("x", encoding="utf-8")
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    rulesync.post_input(home, fx.CUSTOMER_CODE, root.name, ids, auto=True)
    (line,) = rulesync.input_notices(home, "s", root)
    assert line == "new input: 10 files (%s, +2 more)" % ", ".join(ids[:8])


def test_setup_installs_the_import_timer_every_two_minutes_and_verify_proves_it_enabled(fakebin):
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    out = res.stdout.splitlines()
    unit = out.index("+ write /etc/systemd/system/awb-import-auto.service (mode 644, root:root)")
    body = [x[6:] for x in itertools.takewhile(lambda x: x.startswith("    | "), out[unit + 1:])]
    for line in ("Type=oneshot", "User=%s" % USER, "Group=awb", "ExecStart=/usr/local/bin/awb import --auto",
                 "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "ProtectHome=read-only"):
        assert line in body, line
    timer = out.index("+ write /etc/systemd/system/awb-import-auto.timer (mode 644, root:root)")
    body = [x[6:] for x in itertools.takewhile(lambda x: x.startswith("    | "), out[timer + 1:])]
    assert "OnUnitActiveSec=2min" in body and "OnUnitActiveSec=10min" not in body
    assert "Unit=awb-import-auto.service" in body
    assert "+ systemctl enable --now awb-import-auto.timer" in commands(res.stdout)
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [x[6:] for x in res.stdout.splitlines() if x.startswith("CHECK ")]
    assert "the timer awb-import-auto.timer is enabled and runs awb import --auto as the owner" in checks
