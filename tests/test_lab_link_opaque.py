"""Ask 2026-11-27-tcp-bytg-1: `awb lab obs link` of an installer (an opaque object: a NUL byte in its first bytes, as
in the header of an msi) is signed without the name check, like an object over TEXT_LIMIT. The
decoded bytes of a vendor msi asked the vault daemon thousands of times and every link ran into its rate limit.
A text object still gets the exit check (tests/test_lab_link_exit.py)."""
from __future__ import annotations

from awb import gate
from awb.tcp import labobs
from tests import fixtures
from tests.test_bytg_obs_link import lab, link, verify
from tests.test_lab_link_exit import put, register
from tests.test_lo_lab_obs import BUCKET, KEY, make_bucket, start_run, world

assert lab and world and register  # the fixtures, used by name


def _no_scan(*_a, **_k):
    raise AssertionError("an opaque object was scanned")


def test_a_binary_object_is_signed_without_the_name_check(lab, monkeypatch):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put(w, run, KEY, "MZ\x00\x00 installer bytes %s\x00" % fixtures.CUSTOMER_FORMS[0])
    monkeypatch.setattr(gate, "_scan_decoded", _no_scan)
    a = link(w, run)
    assert a["ok"], a
    assert verify(a["url"], w.fake.creds)[:2] == (BUCKET, KEY)


def test_a_text_object_still_gets_the_exit_check(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put(w, run, KEY, "notes of the visit at %s\n" % fixtures.CUSTOMER_FORMS[0])
    a = link(w, run)
    assert not a["ok"] and a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, KEY, "name")
