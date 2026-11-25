"""UX10 (his yes 2026-11-12): the owner commands an assistant session of the owner user may run itself.

Allowed there: `awb start query|project --goal ... [--from DIR --trusted]` without --customer, `awb import CODE
--trusted DIR|FILE`, `awb import CODE` of the bucket folder, `awb winlab open NAME --minutes N --project CODE` and
`awb winlab close`. Still refused there: --customer, --review, --redo, the paste, the timer's job, `awb winlab add`,
`remove` and `open --public-image`, `awb tenant mode`, `awb bucket pull`. The work user is refused every one of them
as before. Every name is an invented fixture name, every folder a temporary one.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

import awb
from awb import bucket, cli, config, importcmd, projects, start, winlab
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
TEXT = "Termin am Montag, das Angebot fuer %s liegt vor.\n" % fx.CUSTOMER_FORMS[0]
MACHINE = "lab-1"
OTHER_MACHINE = "lab-2"
PROJECT = "tcp-ab2c"
FINGERPRINT = "SHA256:" + ("invented" * 6)[:43]
OTHER_FINGERPRINT = "SHA256:" + ("fixture+" * 6)[:43]
T0 = datetime.datetime(2026, 11, 5, 10, 0, tzinfo=datetime.timezone.utc)


def _at(n: int) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=n)


@pytest.fixture(autouse=True)
def session(monkeypatch):
    """An assistant session of the owner user on a host without a work user in this process and no bucket key."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _work_user(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: True)


def _lines(capsys) -> str:
    out, err = capsys.readouterr()
    return out + err


def _hold(code: str) -> str:
    return importcmd.hold_line(code)


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


@pytest.fixture
def lab(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "lab", GOAL, None, register_path)


def _copies(home, customer: str) -> list[Path]:
    folder = home.outbox / customer
    return sorted(f for f in folder.rglob("F-*") if f.is_file()) if folder.is_dir() else []


def _folder(tmp_path: Path) -> Path:
    d = tmp_path / "export"
    (d / "sub").mkdir(parents=True)
    (d / "a.md").write_text(TEXT, encoding="utf-8")
    (d / "sub" / "b.md").write_text(TEXT, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- awb start


def test_ux10_start_without_a_customer_runs_in_a_session(home, capsys):
    assert cli.main(["start", "query", "--goal", GOAL]) == 0
    (pr,) = projects.load(home)
    assert pr.kind == "query"
    out = _lines(capsys)
    assert "the bucket folder in/ cannot be read here" in out
    assert out.strip().splitlines()[-1] == "open: awb projects open %s" % pr.code


def test_ux10_start_from_a_folder_with_trusted_runs_in_a_session(home, capsys, tmp_path):
    assert cli.main(["start", "project", "--goal", GOAL, "--from", str(_folder(tmp_path)), "--trusted"]) == 0
    (pr,) = projects.load(home)
    out = _lines(capsys)
    assert "files taken: 2" in out and "trusted: no name is guessed" in out
    assert len(_copies(home, pr.code)) == 2


def test_ux10_start_of_the_bucket_folder_in_a_session_imports_it(home, capsys, monkeypatch, tmp_path):
    f = tmp_path / "brief.md"
    f.write_text(TEXT, encoding="utf-8")
    monkeypatch.setattr(importcmd, "_from_bucket", lambda p, code: [f])
    assert start.start(home, "project", GOAL) == 0
    (pr,) = projects.load(home)
    assert "files taken: 1" in _lines(capsys)
    assert len(_copies(home, pr.code)) == 1


@pytest.mark.parametrize("extra", [["--customer", "new"], ["--customer", "CUST-Q7M4"], ["--review-tier", "2"],
                                   ["--from", "FOLDER"]])
def test_ux10_start_with_a_customer_a_tier_or_an_untrusted_folder_is_refused_in_a_session(home, capsys, tmp_path,
                                                                                          extra):
    argv = ["start", "project", "--goal", GOAL] + [str(_folder(tmp_path)) if a == "FOLDER" else a for a in extra]
    assert cli.main(argv) == 2
    assert "awb start runs on the owner side, in your own terminal" in _lines(capsys)
    assert not projects.load(home)


def test_ux10_start_stays_refused_for_the_work_user(home, capsys, monkeypatch, tmp_path):
    _work_user(monkeypatch)
    for argv in (["start", "query", "--goal", GOAL],
                 ["start", "project", "--goal", GOAL, "--from", str(_folder(tmp_path)), "--trusted"]):
        assert cli.main(argv) == 2
        assert "awb start runs on the owner side, in your own terminal" in _lines(capsys)
    assert not projects.load(home)


# --------------------------------------------------------------------------- awb import


def test_ux10_import_of_trusted_files_and_a_trusted_folder_runs_in_a_session(home, lab, capsys, tmp_path):
    f = tmp_path / "one.md"
    f.write_text(TEXT, encoding="utf-8")
    assert cli.main(["import", lab.code, "--trusted", str(f)]) == 0
    assert "1 file(s) from the command line" in _lines(capsys)
    assert cli.main(["import", lab.code, "--trusted", str(_folder(tmp_path))]) == 0
    out = _lines(capsys)
    assert "2 file(s) from the command line" in out and "trusted: no name is guessed" in out
    assert len(_copies(home, lab.code)) == 3


def test_ux10_import_of_the_bucket_folder_runs_in_a_session_and_never_takes_the_vault_inbox(home, project,
                                                                                           monkeypatch, capsys):
    home.inbox.mkdir(parents=True, exist_ok=True)
    (home.inbox / "waiting.txt").write_text(TEXT, encoding="utf-8")
    assert cli.main(["import", project.code]) == 2
    assert "the bucket folder in/ cannot be read here" in _lines(capsys)
    with FakeOBS() as fake:
        monkeypatch.setattr(bucket, "client", lambda: fake.client())
        folder, _ = bucket.ensure_folder(fake.client(), project)
        assert cli.main(["import", project.code]) == 0
        assert "no new file in the bucket folder in/" in _lines(capsys)
        fake.objects[folder + "in/brief.txt"] = TEXT.encode("utf-8")
        assert cli.main(["import", project.code]) == 0
        out = _lines(capsys)
        assert "1 file(s) from the bucket folder in/ of %s" % project.code in out
        fx.assert_no_fixture_name(out, "the output of awb import")
        with pytest.raises(bucket.BucketError, match="assistant session"):
            bucket.fetch(home, fake.client(), project.code)      # outside the import door: still refused
    assert (home.inbox / "waiting.txt").is_file(), "the vault inbox is never taken from a session"
    assert len(_copies(home, project.customer)) == 1


@pytest.mark.parametrize("extra", [["FILE"], ["--trusted"], ["--review"], ["--redo"], ["--customer", "new"],
                                   ["--trusted", "FILE", "--customer", "new"], ["--trusted", "FILE", "--review"],
                                   ["--trusted", "FILE", "--redo"]])
def test_ux10_import_without_its_two_forms_is_refused_in_a_session(home, lab, capsys, tmp_path, extra):
    f = tmp_path / "one.md"
    f.write_text(TEXT, encoding="utf-8")
    argv = ["import", lab.code] + [str(f) if a == "FILE" else a for a in extra]
    assert cli.main(argv) == 2
    assert _lines(capsys).strip() == _hold(lab.code)
    assert not _copies(home, lab.code)


def test_ux10_the_paste_the_timer_job_and_the_forms_prompt_stay_refused_in_a_session(home, lab, capsys):
    assert cli.main(["import", lab.code, "--paste"]) == 2
    assert "--paste" in _lines(capsys)
    assert cli.main(["import", "--auto"]) == 2
    assert "the owner's job" in _lines(capsys)
    with pytest.raises(importcmd.ImportRefused, match="has no form in the register"):
        importcmd.ask_forms(home, "CUST-ZZ9Z", read=lambda: "a form\n", tty=True)


def test_ux10_import_stays_refused_for_the_work_user(home, lab, capsys, monkeypatch, tmp_path):
    _work_user(monkeypatch)
    f = tmp_path / "one.md"
    f.write_text(TEXT, encoding="utf-8")
    for argv in (["import", lab.code], ["import", lab.code, "--trusted", str(f)]):
        assert cli.main(argv) == 2
        assert _lines(capsys).strip() == _hold(lab.code)
    assert not _copies(home, lab.code)


# --------------------------------------------------------------------------- awb winlab


@pytest.fixture
def registry(tmp_path, monkeypatch):
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setattr(winlab, "post_notice", lambda *a, **k: None)
    monkeypatch.delenv("CLAUDECODE")
    winlab.add(MACHINE, 2201, FINGERPRINT, True, today="2026-11-05")
    monkeypatch.setenv("CLAUDECODE", "1")
    return conf.parent / winlab.FILE_NAME


def test_ux10_winlab_open_and_close_run_in_a_session(registry, capsys):
    assert "window open for %s" % PROJECT in winlab.open_(MACHINE, 30, PROJECT, now=T0, extend=True)
    assert winlab.allowed(MACHINE, PROJECT, now=_at(1))["name"] == MACHINE
    winlab.close(MACHINE, now=_at(2))
    with pytest.raises(winlab.WinlabError):
        winlab.allowed(MACHINE, PROJECT, now=_at(3))
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 0
    assert cli.main(["winlab", "close", MACHINE]) == 0
    assert "window" in capsys.readouterr().out


def test_ux10_winlab_add_remove_and_public_image_stay_refused_in_a_session(registry, capsys):
    before = registry.read_text(encoding="utf-8")
    for argv, line in ((["add", OTHER_MACHINE, "--port", "2202", "--fingerprint", OTHER_FINGERPRINT,
                         "--no-customer-data"], winlab.OWNER_LINE),
                       (["remove", MACHINE], winlab.OWNER_LINE)):
        assert cli.main(["winlab"] + argv) == 1, argv
        assert line in capsys.readouterr().err
    # --public-image is gone (B-080): the option is no option and the call has no such argument
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT, "--public-image"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    with pytest.raises(TypeError):
        winlab.open_(MACHINE, 30, PROJECT, now=T0, public_image=True)
    assert registry.read_text(encoding="utf-8") == before


def test_ux10_winlab_open_and_close_stay_refused_for_the_work_user(registry, capsys, monkeypatch):
    before = registry.read_text(encoding="utf-8")
    _work_user(monkeypatch)
    for argv in (["open", MACHINE, "--minutes", "30", "--project", PROJECT], ["close", MACHINE]):
        assert cli.main(["winlab"] + argv) == 1, argv
        assert winlab.OWNER_LINE in capsys.readouterr().err
    assert registry.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- what stays his own terminal's


def test_ux10_tenant_mode_and_bucket_pull_stay_refused_in_a_session(home, project, capsys):
    from awb.tcp import readonly

    with pytest.raises(readonly.ReadonlyError) as err:
        readonly.set_mode("test-1", "write", "a note")
    assert str(err.value) == readonly.MODE_OWNER_LINE
    assert cli.main(["bucket", "pull", project.code]) == 2
    assert "awb bucket pull never runs inside an assistant session" in _lines(capsys)
    with pytest.raises(bucket.BucketError, match="assistant session"):
        bucket.owner_terminal("a get of a file of in/")
    with bucket.import_door():
        bucket.owner_terminal("a get of a file of in/")         # the import door alone lets it pass
    with pytest.raises(bucket.BucketError, match="assistant session"):
        bucket.owner_terminal("a get of a file of in/")         # and it closes behind the import


def test_ux10_register_add_is_refused_in_a_session_and_writes_nothing(home, register_path, capsys):
    from awb import register

    before = register_path.read_bytes()
    assert cli.main(["register", "add", "CUST-ZZ9Z", "CUST", fx.SECOND_COMPANY]) == 2
    out = _lines(capsys)
    assert "awb register add: never inside an assistant session; run it in your own terminal" in out
    assert fx.SECOND_COMPANY not in out
    assert register_path.read_bytes() == before
    assert not [e for e in register.load(register_path) if e.code == "CUST-ZZ9Z"]
