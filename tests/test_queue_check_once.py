"""Q-0011 (UXR12), then DMZ 4 (B-088): `awb queue` ran the name check over every field of every row and later kept a
verdict per row and register stamp so that two calls in a row did not run out of the daemon's 600 per minute. Inside
there is no content check any more: the queue sends no row to the name check, keeps no verdict and no key and needs no
register stamp. The tests keep their names and assert that.

Also the structural test of finding 12: the modules that call the name check are listed here, so a new caller
fails until it is added on purpose."""
from __future__ import annotations

import ast
import csv
import datetime as dt
import os
from pathlib import Path

import pytest

from awb import check, cli, config, ledger, vault, vault_client, workqueue
from tests import fixtures as fx
from tests.test_vault import fast_gpg, serving, sockets, vp  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
VERDICTS = "queue-verdicts.tsv"


@pytest.fixture
def queue(vp, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "work-profile" / "queue.tsv"
    monkeypatch.setenv("AWB_WORK_QUEUE", str(path))
    monkeypatch.setattr(config, "paths", lambda: vp)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    clock = [dt.datetime(2026, 11, 11, 12, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(workqueue, "now", lambda: clock[0])
    return path


@pytest.fixture
def screened(monkeypatch):
    """Every text the queue sends to the name check: none."""
    calls: list[str] = []
    real = check.check_text

    def count(text, *args, **kw):
        calls.append(text)
        return real(text, *args, **kw)

    monkeypatch.setattr(check, "check_text", count)
    return calls


def new_process():
    """A new `awb queue` run: nothing of the last one stays in memory, and nothing was kept in memory."""
    assert not hasattr(workqueue, "_CHECKED")


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def add(what="check the export", step="review the result", who="desk"):
    return cli.main(["queue", "add", what, "--next", step, "--who", who])


def plant(path, n):
    """`n` rows written straight to the file, as a hand edit or a migration leaves them."""
    at = "2026-11-11T12:00:00+00:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    workqueue._write(path, [workqueue._new("Q-%04d" % i, "check the export %d" % i, "review the result", "desk",
                                           "", at) for i in range(1, n + 1)])


def test_a_list_after_add_checks_no_row_again(queue, screened, capsys):
    assert add() == 0 and add("check the draft") == 0
    assert screened == [], "add checks no row"
    new_process()
    assert cli.main(["queue", "list"]) == 0
    assert cli.main(["queue", "list", "--mine"]) == 0
    assert screened == []
    assert "check the draft" in capsys.readouterr().out


def test_add_and_next_check_only_the_new_or_changed_row(queue, screened, capsys):
    plant(queue, 5)
    assert cli.main(["queue", "list"]) == 0
    assert screened == []
    new_process()
    assert add() == 0
    assert cli.main(["queue", "next", "Q-0003", "send the export", "--who", "owner"]) == 0
    assert cli.main(["queue", "done", "Q-0002"]) == 0
    assert cli.main(["queue", "list"]) == 0
    assert screened == []
    assert not queue.with_name(VERDICTS).exists()


def test_a_hand_edited_row_is_checked_again_and_a_name_still_refuses(queue, screened, capsys):
    plant(queue, 3)
    assert cli.main(["queue", "list"]) == 0
    new_process()
    queue.write_text(queue.read_text().replace("check the export 2", "call " + fx.PERSON_FORMS[0]))
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    output = capsys.readouterr()
    assert "call " + fx.PERSON_FORMS[0] in output.out and output.err == ""
    assert screened == []


def test_a_changed_register_checks_every_row_again(queue, vp, screened):  # noqa: F811
    plant(queue, 3)
    workqueue.load(queue)
    new_process()
    assert workqueue.load(queue)
    assert screened == []
    with vp.register.open("a", encoding="utf-8") as stream:
        stream.write("PART-NEW2\tPART\treview the result\t2026-11-11\tactive\n")
    new_process()
    assert len(workqueue.load(queue)) == 3
    assert screened == []


def test_a_planted_verdict_certifies_nothing(queue, capsys):
    """A verdict file next to the queue is not read and not written: a row shows as written, with or without one."""
    assert add() == 0
    planted = "pass" + "word=" + "invented-value-8273"
    row = dict(rows(queue)[0], next=planted)
    workqueue._write(queue, [row])
    queue.with_name(VERDICTS).write_text("%s\t%s\n" % (row["id"], "0" * 64))
    new_process()
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    output = capsys.readouterr()
    assert planted in output.out and output.err == ""
    assert queue.with_name(VERDICTS).read_text() == "%s\t%s\n" % (row["id"], "0" * 64)


def test_the_key_lives_in_the_owner_state_folder_never_beside_the_queue(queue):
    assert add() == 0
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "awb" / "queue-verdict.key").exists()
    assert not any("key" in p.name for p in queue.parent.iterdir())
    assert not queue.with_name(VERDICTS).exists()


def test_a_key_others_can_read_keeps_verdicts_in_the_process_only(queue, screened):
    key = Path(os.environ["XDG_STATE_HOME"]) / "awb" / "queue-verdict.key"
    key.parent.mkdir(parents=True, mode=0o700)
    key.write_bytes(b"x" * 32)
    key.chmod(0o644)
    plant(queue, 2)
    workqueue.load(queue)
    workqueue.load(queue)
    new_process()
    workqueue.load(queue)
    assert screened == []
    assert not queue.with_name(VERDICTS).exists()
    assert key.read_bytes() == b"x" * 32


def test_the_verdict_file_is_a_reserved_queue_name(queue, monkeypatch):
    monkeypatch.setenv("AWB_WORK_QUEUE", str(queue.with_name(VERDICTS)))
    assert add() == 2


def test_verdicts_before_a_refusal_are_kept(queue, screened):
    plant(queue, 4)
    queue.write_text(queue.read_text().replace("check the export 3", "call " + fx.PERSON_FORMS[0]))
    assert len(workqueue.load(queue)) == 4
    assert screened == []
    new_process()
    assert any(fx.PERSON_FORMS[0] in r["what"] for r in workqueue.load(queue))
    assert screened == []


# --------------------------------------------------------------------------- the register behind the vault daemon


@pytest.fixture
def remote(queue, vp, monkeypatch):  # noqa: F811
    """The owner's case on the host: the register is not readable here, the vault daemon checks."""
    monkeypatch.setattr(ledger, "register_path", lambda p=None: None)
    ops: list[str] = []
    real = vault_client._request

    def request(sock, obj, **kw):
        ops.append(obj.get("op"))
        return real(sock, obj, **kw)

    monkeypatch.setattr(vault_client, "_request", request)
    return ops


def test_with_the_daemon_two_calls_in_a_row_stay_within_the_rate(queue, vp, remote, capsys):  # noqa: F811
    """The failure of 2026-11-18: a second call in the same minute answered "name or safety check unavailable".
    DMZ 4: the queue sends the daemon no request at all, so no rate can run out."""
    plant(queue, 10)
    with serving(vp, rate_limit=60, rate_window=60.0):
        for _ in range(5):
            new_process()
            remote.clear()
            assert cli.main(["queue", "list"]) == 0, capsys.readouterr().err
            assert remote == []
        new_process()
        remote.clear()
        assert add() == 0
        assert remote == []


def test_with_the_daemon_a_changed_register_checks_again(queue, vp, remote, capsys):  # noqa: F811
    plant(queue, 2)
    with serving(vp):
        assert cli.main(["queue", "list"]) == 0
        new_process()
        remote.clear()
        assert cli.main(["queue", "list"]) == 0
        assert "check" not in remote
        with vp.register.open("a", encoding="utf-8") as stream:
            stream.write("PART-NEW2\tPART\tcheck the export 2\t2026-11-11\tactive\n")
        new_process()
        capsys.readouterr()
        assert cli.main(["queue", "list"]) == 0
        assert "check" not in remote and remote == []
        assert "check the export 2" in capsys.readouterr().out


def test_without_a_daemon_revision_no_verdict_is_reused(queue, vp, remote, monkeypatch, capsys):  # noqa: F811
    """An older daemon (unknown op) or a daemon that cannot say: the queue asks it nothing and keeps no verdict."""
    plant(queue, 2)
    monkeypatch.setattr(vault.Daemon, "_CHECK_OPS", {k: v for k, v in vault.Daemon._CHECK_OPS.items()
                                                    if k != "revision"})
    with serving(vp):
        assert vault_client.register_revision(vp.check_socket) == ""
        for _ in range(2):
            new_process()
            remote.clear()
            assert cli.main(["queue", "list"]) == 0
            assert remote.count("check") == 0
    assert not queue.with_name(VERDICTS).exists()


def test_the_revision_is_opaque_stable_and_new_per_daemon(vp):  # noqa: F811
    with serving(vp):
        first = vault_client.register_revision(vp.check_socket)
        assert first and first == vault_client.register_revision(vp.check_socket)
        answer = vault_client._request(vp.check_socket, {"op": "revision"})
        assert set(answer) == {"ok", "revision"}
        for form in fx.ALL_REGISTERED:
            assert form.lower() not in first.lower()
        before = vp.register.read_text(encoding="utf-8")
        vp.register.write_text(before + "PART-NEW2\tPART\tcheck the draft\t2026-11-11\tactive\n", encoding="utf-8")
        second = vault_client.register_revision(vp.check_socket)
        assert second and second != first
    with serving(vp):
        assert vault_client.register_revision(vp.check_socket) not in ("", second)
    assert vault_client.register_revision(vp.check_socket) == "", "no daemon: no revision, no error"


# --------------------------------------------------------------------------- who may call the name check

CHECK_CALLS = frozenset(("check_text", "check_file", "check_remote", "record_remote", "checker", "hit_indexes",
                         "screen_fields", "withhold"))
"""The calls that run the name check. `screen` counts when it is called on the module `ledger` or `check`."""

ALLOWED_CALLERS = frozenset((
    "awb/bucket.py", "awb/calibrate.py", "awb/career.py", "awb/check.py", "awb/fplearn.py", "awb/hooks.py",
    "awb/images.py", "awb/kb.py", "awb/labclient.py", "awb/ledger.py", "awb/pdf.py",
    "awb/projectfacts.py",
    "awb/review.py", "awb/tcp/ask.py", "awb/tcp/dataset.py", "awb/tcp/doc.py", "awb/tcp/keys_exchange.py",
    "awb/tcp/lab.py", "awb/tcp/owner.py", "awb/tcp/recheck.py", "awb/tcp/web/chat_service.py",
    "awb/tcp/web/create_api.py", "awb/tcp/web/materials_api.py", "awb/tcp/xchg.py", "awb/translate.py",
    "awb/writing.py",
))
"""Every module that calls the name check today. The rule (finding 12 of the review of 2026-11-18, then DMZ 4 of
2026-11-25): customer data is checked once when it enters and a text that leaves is checked at the exit; our own
records inside run no content check. A new caller is a decision: add it here on purpose, with the reason in the
commit, or check the text where it enters instead."""


def _callers(source: str) -> set[str]:
    """The check calls of one module, imported aliases of them included."""
    tree = ast.parse(source)
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("awb.check", "awb.ledger", "awb.vault_client"):
            for name in node.names:
                if name.name in CHECK_CALLS or name.name == "screen":
                    alias[name.asname or name.name] = name.name
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and (f.id in CHECK_CALLS or f.id in alias):
            found.add(alias.get(f.id, f.id))
        elif isinstance(f, ast.Attribute) and (
                f.attr in CHECK_CALLS or f.attr == "screen" and isinstance(f.value, ast.Name)
                and f.value.id in ("ledger", "check")):
            found.add(f.attr)
    return found


def test_only_the_listed_modules_call_the_name_check():
    callers = {p.relative_to(ROOT).as_posix() for p in sorted((ROOT / "awb").rglob("*.py"))
               if _callers(p.read_text(encoding="utf-8"))}
    assert callers - ALLOWED_CALLERS == set(), "a new caller of the name check: check once at entry instead"
    assert ALLOWED_CALLERS - callers == set(), "a module no longer calls the name check: take it off the list"


def test_the_caller_walk_can_fail():
    """Planted: a direct call, a call through the module, an imported alias. A comment or a string is no call."""
    assert _callers("from awb import check\ncheck.check_text(t, r)\n") == {"check_text"}
    assert _callers("from awb.ledger import screen_fields as sf\nsf([], None)\n") == {"screen_fields"}
    assert _callers("from awb import ledger\nledger.screen(t, r)\n") == {"screen"}
    assert _callers("# check_text(t, r)\nx = 'ledger.screen(t)'\nscreen(t)\n") == set()
