"""`awb import --auto` (task lc-laptop-clean): the owner's job of the timer awb-import-auto takes the new files of in/
of every active project, the clean copies of `awb clean` with --trusted and the rest in wipe mode, moves the
originals into the vault and out of the bucket through the key service (`bucket_drop_in`), leaves the notice and
logs one line per run; it skips a project whose import runs. A real key service in threads, the stand-in bucket of
tests/obs_fake.py, invented names only."""
from __future__ import annotations

import fcntl
import itertools
import os

import pytest

from awb import bucket, cli, config, importauto, importcmd, jobs, projects, rulesync
from awb.tcp import keys
from tests import fixtures as fx
from tests.test_bucket_keys import GOAL, ROOT, STORE, short_dir  # noqa: F401
from tests.obs_fake import FakeOBS
from tests.test_seal import SETUP, USER, VERIFY, commands, fakebin, no_marks, run_script  # noqa: F401

AK, SK = "AKLABFAKE", "sk-lab-fake-secret"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    from awb.tcp import throttle
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)


@pytest.fixture
def own():
    with FakeOBS("awb", AK, SK) as o:
        yield o


@pytest.fixture
def svc(home, own, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={own.bucket: own.endpoint})
        s.start()
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, sorted(STORE), reader=STORE.get,
                    settings={"bucket_tenant": "test-1", "lab_bucket": "awb-lab", "owner_bucket": own.bucket,
                              "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def _base(project) -> str:
    return "%s/%s/" % (project.created[:7], project.code)


def _copies(home, cust) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in (home.outbox / cust).glob("F-*.md"))


def test_auto_imports_clean_copies_trusted_and_the_rest_wiped_then_drops_them_from_in(svc, own, home, project):
    base = _base(project)
    for key in (base, base + "in/", base + "out/"):
        own.objects[key] = b""
    own.objects[base + "in/notes.clean.md"] = ("Outcome with %s on KVM for %s.\n"
                                               % (fx.SECOND_LAST, fx.CUSTOMER_FORMS[0])).encode()
    own.objects[base + "in/raw.md"] = ("The pilot ran with %s.\n" % fx.PLANTED_CANDIDATE).encode()
    assert importauto.run(home) == 0
    text = _copies(home, project.customer)
    assert fx.SECOND_LAST in text and "KVM" in text          # trusted: no name guessed in a clean copy
    assert fx.CUSTOMER_FORMS[0] not in text and fx.CUSTOMER_CODE in text
    assert fx.PLANTED_CANDIDATE not in text                   # a file that is no clean copy is wiped
    assert base + "in/notes.clean.md" not in own.objects and base + "in/raw.md" not in own.objects
    assert len([f for f in (home.originals / project.customer).iterdir() if f.is_file()]) == 2
    assert not [f for f in home.inbox.iterdir() if f.is_file()]
    notices = [f for f in rulesync.notices_dir(home).iterdir() if f.name.startswith("input-")]
    assert notices
    (line,) = (home.vault / importauto.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert "2 file(s) imported (1 clean copies with --trusted) into %s, 2 dropped from in/" % project.code in line
    assert "notes" not in line and "raw" not in line
    assert importauto.run(home) == 0                          # nothing new: one more line, nothing imported
    lines = (home.vault / importauto.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and " 0 file(s) imported" in lines[1]


def test_auto_skips_a_project_whose_import_runs_and_import_refuses_meanwhile(svc, own, home, project, tmp_path):
    base = _base(project)
    own.objects[base + "in/notes.clean.md"] = b"ok\n"
    folder = home.vault / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / ("import-%s.lock" % project.code), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert importauto.run(home) == 0
        assert base + "in/notes.clean.md" in own.objects
        line = (home.vault / importauto.LOG_NAME).read_text(encoding="utf-8").splitlines()[-1]
        assert "1 project(s) skipped while their import runs" in line
        f = tmp_path / "a.md"
        f.write_text("ok\n", encoding="utf-8")
        with pytest.raises(importcmd.ImportRefused, match="is running"):
            importcmd.run(home, project.code, [f], tty=True)
    finally:
        os.close(fd)
    assert importauto.run(home) == 0
    assert base + "in/notes.clean.md" not in own.objects


def test_a_project_without_a_form_is_left_in_the_vault_inbox_for_his_terminal(svc, own, home, project, monkeypatch):
    base = _base(project)
    own.objects[base + "in/a.clean.md"] = b"ok\n"
    monkeypatch.setattr(importcmd, "_active_forms", lambda p, cust: 0)
    assert importauto.run(home) == 2
    line = (home.vault / importauto.LOG_NAME).read_text(encoding="utf-8").splitlines()[-1]
    assert "0 file(s) imported" in line and "1 left in the vault inbox" in line and "1 error(s)" in line
    assert base + "in/a.clean.md" in own.objects               # nothing leaves in/ before the vault holds it


def test_the_key_service_drops_a_file_of_in_only(svc, own, home, project):
    base = _base(project)
    own.objects[base + "in/a.md"] = b"x"
    own.objects[base + "out/b.md"] = b"x"
    ask = lambda **r: keys.request(svc.admin_path, r)           # noqa: E731
    for key in (base + "out/b.md", base + "in/", "2026-01/tcp-zzzz/in/a.md", base + "../in/a.md"):
        assert ask(op="bucket_drop_in", key=key)["ok"] is False, key
    assert ask(op="bucket_delete", key=base + "in/a.md")["error"] == "unknown operation"
    assert base + "out/b.md" in own.objects and base + "in/a.md" in own.objects
    bucket.client().drop_in(base + "in/a.md")
    assert base + "in/a.md" not in own.objects
    with pytest.raises(bucket.BucketError, match="only a file of in/"):
        bucket.client().drop_in(base + "out/b.md")


def test_the_key_service_never_drops_the_keep_file_of_in(svc, own, home, project):
    base = _base(project)
    own.objects[base + "in/.keep"] = b"keeps this folder visible"
    assert keys.request(svc.admin_path, {"op": "bucket_drop_in", "key": base + "in/.keep"})["ok"] is False
    assert base + "in/.keep" in own.objects


def test_auto_is_the_owners_and_takes_no_project(home, monkeypatch, capsys):
    assert cli.main(["import", "--auto", "tcp-q7m4"]) == 2
    assert "--auto takes no CODE" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["import", "--auto"]) == 2
    assert "the owner's job" in capsys.readouterr().err



def test_trusted_learns_no_form_from_a_guess_so_the_words_stay_everywhere(home, register_path, tmp_path,
                                                                         monkeypatch):
    """--trusted guessed no name but still learned the forms of the guesses and wiped them where they stood again
    (a salutation taught the words of a name); now nothing is learned from a guess."""
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    f = tmp_path / "notes.md"
    f.write_text("Dear " + "Mr %s,\nthe gateway of %s failed.\nRegards, %s\n"
                 % (fx.SECOND_PERSON, fx.SECOND_LAST, fx.SECOND_PERSON), encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True, trusted=True) == 0
    text = _copies(home, pr.customer)
    assert text.count(fx.SECOND_PERSON) == 2 and "[person" not in text
