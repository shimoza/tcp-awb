"""Q-0011 (UXR12): `awb queue` checks a row once when it is added or changed and keeps the verdict per register
stamp; a later call checks only rows with no verdict or an old stamp. Before, every call ran the name check over
every field of every row (about 260 checks) and two calls in a row ran out of the daemon's 600 per minute.

Also the structural test of finding 12: the modules that call the name check are listed here, so a new caller
fails until it is added on purpose."""
from __future__ import annotations

import ast
import csv
import datetime as dt
import hashlib
import hmac
import os
from pathlib import Path

import pytest

from awb import cli, config, ledger, vault, vault_client, workqueue
from tests import fixtures as fx
from tests.test_vault import fast_gpg, serving, sockets, vp  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def queue(vp, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "work-profile" / "queue.tsv"
    monkeypatch.setenv("AWB_WORK_QUEUE", str(path))
    monkeypatch.setattr(config, "paths", lambda: vp)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    clock = [dt.datetime(2026, 11, 11, 12, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(workqueue, "now", lambda: clock[0])
    workqueue._CHECKED.clear()
    yield path
    workqueue._CHECKED.clear()


@pytest.fixture
def screened(monkeypatch):
    """Every row the queue sends to the name check, by id."""
    calls: list[str] = []
    real = ledger.screen_fields

    def count(fields, reg, **kw):
        calls.append(dict(fields)["id"])
        return real(fields, reg, **kw)

    monkeypatch.setattr(ledger, "screen_fields", count)
    return calls


def new_process():
    """A new `awb queue` run: nothing of the last one stays in memory."""
    workqueue._CHECKED.clear()


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def add(what="check the export", step="review the result", who="desk"):
    return cli.main(["queue", "add", what, "--next", step, "--who", who])


def plant(path, n):
    """`n` rows written straight to the file, as a hand edit or a migration leaves them: no verdict yet."""
    at = "2026-11-11T12:00:00+00:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    workqueue._write(path, [workqueue._new("Q-%04d" % i, "check the export %d" % i, "review the result", "desk",
                                           "", at) for i in range(1, n + 1)])


def test_a_list_after_add_checks_no_row_again(queue, screened, capsys):
    assert add() == 0 and add("check the draft") == 0
    assert len(screened) == 2, "add checks only the row it adds"
    new_process()
    screened.clear()
    assert cli.main(["queue", "list"]) == 0
    assert cli.main(["queue", "list", "--mine"]) == 0
    assert screened == []
    assert "check the draft" in capsys.readouterr().out


def test_add_and_next_check_only_the_new_or_changed_row(queue, screened, capsys):
    plant(queue, 5)
    assert cli.main(["queue", "list"]) == 0
    assert len(screened) == 5
    new_process()
    screened.clear()
    assert add() == 0
    assert screened == ["Q-0006"]
    new_process()
    screened.clear()
    assert cli.main(["queue", "next", "Q-0003", "send the export", "--who", "owner"]) == 0
    assert screened == ["Q-0003"]
    new_process()
    screened.clear()
    assert cli.main(["queue", "done", "Q-0002"]) == 0
    assert cli.main(["queue", "list"]) == 0
    assert screened == []
    assert "Q-0002" not in workqueue._read_verdicts(queue.with_name(workqueue.VERDICTS))


def test_a_hand_edited_row_is_checked_again_and_a_name_still_refuses(queue, screened, capsys):
    plant(queue, 3)
    assert cli.main(["queue", "list"]) == 0
    new_process()
    screened.clear()
    queue.write_text(queue.read_text().replace("check the export 2", "call " + fx.PERSON_FORMS[0]))
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 1
    output = capsys.readouterr()
    assert not output.out
    fx.assert_no_fixture_name(output.out + output.err)
    assert screened == ["Q-0002"]
    new_process()
    screened.clear()
    assert cli.main(["queue", "list"]) == 1, "a row with a hit never earns a verdict"
    assert screened == ["Q-0002"]


def test_a_changed_register_checks_every_row_again(queue, vp, screened):  # noqa: F811
    plant(queue, 3)
    workqueue.load(queue)
    new_process()
    screened.clear()
    workqueue.load(queue)
    assert screened == []
    with vp.register.open("a", encoding="utf-8") as stream:
        stream.write("PART-NEW2\tPART\treview the result\t2026-11-11\tactive\n")
    new_process()
    with pytest.raises(ledger.Refused):
        workqueue.load(queue)
    assert screened == ["Q-0001"]


def test_a_planted_verdict_certifies_nothing(queue, capsys):
    """Planted: a verdict file next to the queue with the unkeyed row hash and with an HMAC under a key of its
    own. Neither lets a secret through."""
    assert add() == 0
    planted = "pass" + "word=" + "invented-value-8273"
    row = dict(rows(queue)[0], next=planted)
    workqueue._write(queue, [row])
    stamp = workqueue._register_stamp(ledger.register_path())
    assert stamp
    for mac in (workqueue._row_key(row, stamp),
                hmac.new(b"k" * 32, workqueue._row_key(row, stamp).encode(), hashlib.sha256).hexdigest()):
        queue.with_name(workqueue.VERDICTS).write_text("%s\t%s\n" % (row["id"], mac))
        new_process()
        capsys.readouterr()
        assert cli.main(["queue", "list"]) == 1
        output = capsys.readouterr()
        assert not output.out and planted not in output.err


def test_the_key_lives_in_the_owner_state_folder_never_beside_the_queue(queue):
    assert add() == 0
    key = Path(os.environ["XDG_STATE_HOME"]) / "awb" / "queue-verdict.key"
    assert key.stat().st_mode & 0o777 == 0o600 and key.stat().st_size == 32
    assert key.parent.stat().st_mode & 0o077 == 0
    assert not any("key" in p.name for p in queue.parent.iterdir())
    text = queue.with_name(workqueue.VERDICTS).read_text()
    assert text.count("\n") == 1 and text.startswith("Q-0001\t")


def test_a_key_others_can_read_keeps_verdicts_in_the_process_only(queue, screened):
    key = Path(os.environ["XDG_STATE_HOME"]) / "awb" / "queue-verdict.key"
    key.parent.mkdir(parents=True, mode=0o700)
    key.write_bytes(b"x" * 32)
    key.chmod(0o644)
    plant(queue, 2)
    workqueue.load(queue)
    workqueue.load(queue)
    assert len(screened) == 2, "within one process the verdicts still hold"
    new_process()
    workqueue.load(queue)
    assert len(screened) == 4
    assert not queue.with_name(workqueue.VERDICTS).exists()


def test_the_verdict_file_is_a_reserved_queue_name(queue, monkeypatch):
    monkeypatch.setenv("AWB_WORK_QUEUE", str(queue.with_name(workqueue.VERDICTS)))
    assert add() == 2


def test_verdicts_before_a_refusal_are_kept(queue, screened):
    plant(queue, 4)
    queue.write_text(queue.read_text().replace("check the export 3", "call " + fx.PERSON_FORMS[0]))
    with pytest.raises(ledger.Refused):
        workqueue.load(queue)
    assert screened == ["Q-0001", "Q-0002", "Q-0003"]
    new_process()
    screened.clear()
    with pytest.raises(ledger.Refused):
        workqueue.load(queue)
    assert screened == ["Q-0003"]


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
    A rate of 60 per minute is enough for a first check of 10 rows and then any number of calls."""
    plant(queue, 10)
    with serving(vp, rate_limit=60, rate_window=60.0):
        for _ in range(5):
            new_process()
            remote.clear()
            assert cli.main(["queue", "list"]) == 0, capsys.readouterr().err
        assert remote == ["revision"]
        new_process()
        remote.clear()
        assert add() == 0
        assert remote.count("revision") == 1 and 0 < remote.count("check") <= 5


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
        assert cli.main(["queue", "list"]) == 1
        assert "check" in remote
        fx.assert_no_fixture_name(capsys.readouterr().err)


def test_without_a_daemon_revision_no_verdict_is_reused(queue, vp, remote, monkeypatch, capsys):  # noqa: F811
    """An older daemon (unknown op) or a daemon that cannot say: every row is checked, as before."""
    plant(queue, 2)
    monkeypatch.setattr(vault.Daemon, "_CHECK_OPS", {k: v for k, v in vault.Daemon._CHECK_OPS.items()
                                                    if k != "revision"})
    with serving(vp):
        assert vault_client.register_revision(vp.check_socket) == ""
        for _ in range(2):
            new_process()
            remote.clear()
            assert cli.main(["queue", "list"]) == 0
            assert remote.count("check") >= 2
    assert not queue.with_name(workqueue.VERDICTS).exists()


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
    "awb/bucket.py", "awb/calibrate.py", "awb/career.py", "awb/check.py", "awb/cli.py", "awb/devask.py",
    "awb/english.py", "awb/fplearn.py", "awb/hooks.py", "awb/images.py", "awb/kb.py", "awb/labclient.py",
    "awb/ledger.py", "awb/opensweep.py", "awb/ownerwait.py", "awb/pdf.py", "awb/period.py", "awb/projectfacts.py",
    "awb/projects.py", "awb/review.py", "awb/side.py", "awb/tcp/ask.py", "awb/tcp/dataset.py", "awb/tcp/doc.py",
    "awb/tcp/keys_exchange.py", "awb/tcp/lab.py", "awb/tcp/owner.py", "awb/tcp/recheck.py",
    "awb/tcp/web/chat_service.py", "awb/tcp/web/create_api.py", "awb/tcp/web/materials_api.py",
    "awb/tcp/web/projects_api.py", "awb/tcp/xchg.py", "awb/translate.py",
    "awb/workqueue.py", "awb/writing.py",
))
"""Every module that calls the name check today. The rule (finding 12 of the review of 2026-11-18): customer data is
checked once when it enters; our own output gets the secret check only. A new caller is a decision: add it here on
purpose, with the reason in the commit, or check the text where it enters instead."""


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
