"""awb/obs.py against a local stand-in that checks every signature (tests/obs_fake.py)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awb import obs
from tests.obs_fake import FakeOBS

AWKWARD = "2026-09/tcp-q7m4/in/Offer v2 (final) Größe & more.txt"


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


def test_put_head_get_list_copy_delete(fake, tmp_path):
    c = fake.client()
    c.put_bytes(AWKWARD, b"one\n", "text/plain")
    assert fake.objects[AWKWARD] == b"one\n"
    assert c.head(AWKWARD) is not None
    assert c.get(AWKWARD, tmp_path / "x").read_bytes() == b"one\n"
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(3 * obs.CHUNK + 7))
    c.put_file("2026-09/tcp-q7m4/out/big.bin", src)
    assert fake.objects["2026-09/tcp-q7m4/out/big.bin"] == src.read_bytes()
    c.copy(AWKWARD, "2026-09/tcp-q7m4/in/copy.txt")
    assert fake.objects["2026-09/tcp-q7m4/in/copy.txt"] == b"one\n"
    listed = c.list("2026-09/tcp-q7m4/")
    assert sorted(k for k, _, _ in listed.objects) == sorted(fake.objects)
    assert dict((k, s) for k, s, _ in listed.objects)[AWKWARD] == 4
    c.delete(AWKWARD)
    assert AWKWARD not in fake.objects


def test_a_missing_object(fake, tmp_path):
    c = fake.client()
    assert c.head("2026-09/nothing") is None
    with pytest.raises(obs.OBSError) as ei:
        c.get("2026-09/nothing", tmp_path / "x")
    assert ei.value.status == 404 and ei.value.code == "NoSuchKey"


def test_a_wrong_secret_is_refused_and_never_shown(fake):
    c = fake.client(keys=obs.Keys(fake.ak, "not-the-secret"))
    with pytest.raises(obs.OBSError) as ei:
        c.put_bytes("x.txt", b"x")
    assert ei.value.status == 403 and ei.value.code == "SignatureDoesNotMatch"
    assert fake.ak not in str(ei.value) and "not-the-secret" not in str(ei.value)
    assert "x.txt" not in fake.objects


def test_every_page_of_a_listing(fake):
    c = fake.client()
    for i in range(7):
        c.put_bytes("2026-09/tcp-q7m4/in/f%d.txt" % i, b"x")
    for code in ("tcp-aaaa", "tcp-bbbb", "tcp-cccc"):
        c.put_bytes("2026-10/%s/" % code, b"")
        c.put_bytes("2026-10/%s/in/a.txt" % code, b"x")
    assert len(c.list("2026-09/", page=2).objects) == 7
    assert c.list("2026-10/", "/", page=2).prefixes == ["2026-10/tcp-aaaa/", "2026-10/tcp-bbbb/",
                                                          "2026-10/tcp-cccc/"]
    assert c.list("", "/", page=1).prefixes == ["2026-09/", "2026-10/"]


def test_an_unreachable_endpoint(tmp_path):
    c = obs.Client("awb", obs.Keys("a", "b"), endpoint="http://127.0.0.1:9", timeout=2)
    with pytest.raises(obs.OBSError) as ei:
        c.head("x")
    assert ei.value.status == 0 and "cannot be reached" in str(ei.value)


def test_keys_hide_their_values():
    assert "secret-value" not in repr(obs.Keys("access-id", "secret-value"))
    assert "access-id" not in repr(obs.Keys("access-id", "secret-value"))


@pytest.mark.parametrize("ref", [None, "", "pass:", "AKIA1234567890", "file:/tmp/keys"])
def test_a_key_setting_must_name_the_password_store(ref):
    with pytest.raises(obs.OBSError, match="pass:<entry"):
        obs.keys_from_reference(ref)


def fake_pass(tmp_path: Path, monkeypatch, entries: dict[str, str]) -> None:
    """A `pass` on PATH that answers `pass show NAME` from `entries`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pass"
    cases = "".join('  "%s") echo "%s";;\n' % (k, v) for k, v in entries.items())
    script.write_text('#!/bin/sh\n[ "$1" = show ] || exit 2\ncase "$2" in\n%s  *) exit 1;;\nesac\n' % cases,
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", "%s:%s" % (bindir, os.environ.get("PATH", "")))


def test_keys_come_from_the_password_store(tmp_path, monkeypatch):
    fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": "AKFROMSTORE", "cloud/owner/sk": "sk-from-store"})
    keys = obs.keys_from_reference("pass:cloud/owner")
    assert (keys.ak, keys.sk) == ("AKFROMSTORE", "sk-from-store")
    with pytest.raises(obs.OBSError) as ei:
        obs.keys_from_reference("pass:cloud/other")
    assert "sk-from-store" not in str(ei.value)
