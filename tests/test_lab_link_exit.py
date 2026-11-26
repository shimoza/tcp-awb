"""B-094, F6 of the attacker review of 2026-11-26: `awb lab obs link` is an exit. Before it signs, the key service runs
the exit check on the object (a registered name or a secret, the gate's scan with the identifier class left out,
B-087); an object the run's own reader wrote (the disk-reader result) is signed as before.

IAM and OBS are the in-memory stand-in of tests/lab_fake.py, the key service the one of tests/test_lo_lab_obs.py. The
register is the invented one of tests/fixtures.py in the temporary vault of the service.
"""
from __future__ import annotations

import pytest

from awb import labclient
from awb.tcp import labadmin, labobs
from tests import fixtures
from tests.test_bytg_obs_link import durations, lab, link, put_object, verify
from tests.test_lo_lab_obs import BUCKET, KEY, LAB, PROJECT, SESSION, make_bucket, start_run, world

assert lab and world  # the fixtures, used by name


@pytest.fixture(autouse=True)
def register(lab):
    vault = lab.tmp / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "register.tsv").write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")


def put(w, run, key, body):
    a = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "PUT",
                            "service": "obs", "path": "/%s/%s" % (BUCKET, key), "body": body}, SESSION,
                           caller=PROJECT)
    assert a["ok"] and a["status"] == 200, a


def test_an_object_with_a_registered_name_is_refused_before_signing(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put(w, run, KEY, "notes of the visit at %s, rack 4\n" % fixtures.CUSTOMER_FORMS[0])
    a = link(w, run)
    assert not a["ok"]
    assert a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, KEY, "name")
    assert fixtures.CUSTOMER_FORMS[0] not in a["error"]
    assert durations(w) == [labadmin.CRED_SECONDS]   # no link credential was made


def test_an_object_with_a_secret_is_refused(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put(w, run, KEY, "OTC_" + "SK=" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc\n")
    a = link(w, run)
    assert not a["ok"] and a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, KEY, "secret")
    assert durations(w) == [labadmin.CRED_SECONDS]


def test_a_clean_object_is_signed(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    a = link(w, run)
    assert a["ok"], a
    assert verify(a["url"], w.fake.creds)[:2] == (BUCKET, KEY)


def test_an_id_is_no_finding(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    address = ".".join(("10", "20", "30", "40"))
    volume = "-".join(("0b1f7a2c", "3d4e", "4f50", "8a6b", "7c8d9e0f1a2b"))
    put(w, run, KEY, "server %s volume %s\n" % (address, volume))
    assert link(w, run)["ok"]


def test_the_result_of_the_run_s_reader_is_signed_as_before(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    body = ("%s\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    for key in ("files/Windows/System32/config/SYSTEM", "manifest.json"):
        # written by the reader with the lab key, not through the service: the run records no object
        w.fake.buckets[BUCKET]["objects"][key] = {"data": body, "meta": {}, "etag": '"x"'}
        a = link(w, run, key=key)
        assert a["ok"], a
    # the same key put by the session is the session's object
    put(w, run, "files/notes.txt", body.decode())
    a = link(w, run, key="files/notes.txt")
    assert not a["ok"] and a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, "files/notes.txt", "name")


def test_labclient_prints_the_refusal(lab, monkeypatch, capsys):
    w = lab
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    assert labclient.call("PUT", "obs", "/" + BUCKET)[0] == 200
    assert labclient.call("PUT", "obs", "/%s/%s" % (BUCKET, KEY), body=fixtures.PERSON_FORMS[0])[0] == 200
    capsys.readouterr()
    assert labclient.obs_main(["link", BUCKET, KEY]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "fails the exit check (name)" in err and fixtures.PERSON_FORMS[0] not in err
