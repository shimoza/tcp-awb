"""K16 and K21 of the review of 2026-10-20 (task k16-k20-host-hardening). K16: the send gate trusts the fingerprint
of a closed project only when it matches the hash close pinned on the owner side. K21: every build unit's own
/dev/shm is nosuid,nodev. Temporary folders only; no daemon socket and no unit is started."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from awb import buildrun, config, fppin, projects, review, vault
from tests.test_review_board import spawn
from tests.test_send_gate import tier3
from tests.test_send_unreadable import candidates

VERIFY = Path(__file__).resolve().parents[1] / "seal" / "verify.sh"
READABLE_TREE = review._readable_tree


def _unreadable(monkeypatch, b: Path) -> None:
    real, b_real = review._readable_tree, Path(os.path.realpath(b))

    def fake(root):
        if root == b_real:
            raise PermissionError(13, "denied")
        return real(root)

    monkeypatch.setattr(review, "_readable_tree", fake)


@pytest.fixture
def closed(home, register_path, tmp_path, monkeypatch):
    """Project a, and project b closed with a reviewed deliverable that the owner cannot read."""
    a = spawn(home, register_path)
    b = spawn(home, register_path)
    tier3(b, tmp_path)
    files = candidates(b, tmp_path / "out")
    pr = projects.close(home, b.name, force=True)
    assert "pinned on the owner side" in pr.fingerprint_note
    _unreadable(monkeypatch, b)
    return a, b, files


def _empty(home, code: str) -> None:
    path = review.fingerprint_path(home, code)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["deliverables"] = []
    path.write_text(json.dumps(data), encoding="utf-8")


def test_k16_close_pins_the_hash_of_the_fingerprint_in_the_vault(closed, home):
    _a, b, _files = closed
    pins = fppin.load(home.vault)
    assert pins[b.name] == fppin.digest(review.fingerprint_path(home, b.name).read_bytes())
    assert (os.stat(fppin.pins_path(home.vault)).st_mode & 0o777) == 0o600


def test_k16_an_emptied_fingerprint_is_refused_and_the_copy_does_not_pass(closed, home):
    a, b, files = closed
    assert review.send_check(a, files["copy"], True, workbench=home)[0] == review.SEND_REFUSE
    _empty(home, b.name)
    for key in ("copy", "plain"):
        v, message = review.send_check(a, files[key], True, workbench=home)
        assert v == review.SEND_REFUSE and b.name in message and "pinned" in message, key


def test_k16_a_fingerprint_without_a_pin_is_refused(closed, home):
    a, b, files = closed
    fppin.pins_path(home.vault).unlink()
    v, message = review.send_check(a, files["plain"], True, workbench=home)
    assert v == review.SEND_REFUSE and "awb close %s" % b.name in message


def test_k16_closing_again_keeps_the_bytes_and_the_pin(closed, home, monkeypatch):
    a, b, files = closed
    before = review.fingerprint_path(home, b.name).read_bytes()
    monkeypatch.setattr(review, "_readable_tree", READABLE_TREE)     # the work user closes it again
    pr = projects.close(home, b.name, force=True)
    _unreadable(monkeypatch, b)
    assert review.fingerprint_path(home, b.name).read_bytes() == before
    assert "pinned on the owner side" in pr.fingerprint_note
    assert review.send_check(a, files["plain"], True, workbench=home)[0] == review.SEND_WARN


def test_k16_a_forged_pin_before_close_is_a_conflict_never_a_pass(home, register_path, tmp_path, monkeypatch):
    a = spawn(home, register_path)
    b = spawn(home, register_path)
    tier3(b, tmp_path)
    files = candidates(b, tmp_path / "out")
    forged = review.fingerprint_path(home, b.name)
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(json.dumps({"version": review.FINGERPRINT_VERSION, "salt": "ab" * 16, "deliverables": []}),
                      encoding="utf-8")
    assert fppin.pin_shared(home, b.name) == fppin.PINNED
    empty = forged.read_bytes()
    pr = projects.close(home, b.name, force=True)
    assert "differs from the one pinned before" in pr.fingerprint_note
    assert fppin.load(home.vault)[b.name] == fppin.CONFLICT
    forged.write_bytes(empty)          # the session puts its empty file back after the close
    _unreadable(monkeypatch, b)
    v, _message = review.send_check(a, files["copy"], True, workbench=home)
    assert v == review.SEND_REFUSE


def test_k16_the_work_user_pins_through_the_daemon_by_code_alone(home, monkeypatch):
    from awb import vault_client

    sent = []

    def fake(sock, obj, **kw):
        sent.append((Path(sock), obj))
        return {"ok": True, "pin": fppin.PINNED}

    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(vault_client, "_request", fake)
    assert fppin.pin(home, "tcp-q7m4") == "fingerprint pinned on the owner side"
    assert sent == [(home.check_socket, {"op": "pin", "code": "tcp-q7m4"})]
    monkeypatch.setattr(vault_client, "_request", lambda *a, **k: {"ok": False, "error": "unknown op"})
    assert "not pinned" in fppin.pin(home, "tcp-q7m4")


def test_k16_the_daemon_hashes_the_shared_file_and_takes_no_hash(home):
    d = vault.Daemon(home)
    path = fppin.shared_path(home, "tcp-q7m4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"version":1}')

    def ask(obj):
        return d._answer(json.dumps(obj).encode(), False, os.getuid())

    assert ask({"op": "pin", "code": "../x"}) == {"ok": False, "error": "bad request"}
    assert ask({"op": "pin", "code": "tcp-q7m4", "sha256": "0" * 64}) == {"ok": True, "pin": fppin.PINNED}
    assert fppin.load(home.vault)["tcp-q7m4"] == fppin.digest(b'{"version":1}')
    assert ask({"op": "pin", "code": "tcp-q7m4"}) == {"ok": True, "pin": fppin.SAME}
    path.write_bytes(b'{"version":1,"deliverables":[]}')
    assert ask({"op": "pin", "code": "tcp-q7m4"}) == {"ok": True, "pin": fppin.CONFLICT}
    assert ask({"op": "pin", "code": "tcp-q7m4"})["pin"] == fppin.CONFLICT


def test_k16_a_link_in_place_of_the_fingerprint_is_not_read(home, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path = fppin.shared_path(home, "tcp-q7m4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    with pytest.raises(OSError):
        fppin.pin_shared(home, "tcp-q7m4")
    assert "tcp-q7m4" not in fppin.load(home.vault)


def test_k21_every_unit_shm_is_nosuid_and_nodev_and_verify_probes_the_same():
    opts = buildrun.SHM_FS.split(":", 1)[1].split(",")
    assert {"size=64M", "mode=1777", "nosuid", "nodev"} <= set(opts)
    assert "--property=TemporaryFileSystem=%s" % buildrun.SHM_FS in VERIFY.read_text(encoding="utf-8")
