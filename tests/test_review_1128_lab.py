"""G3, G4 and G11 of the attacker review of 2026-11-28 (awb/tcp/labadmin.py, awb/tcp/labobs.py,
awb/tcp/bucketgrant.py).

G3: an OBS lab call is fenced like every other OBS path of the key service: a granted bucket only for its holder, the
exchange buckets never, on a lab tenant else only a bucket named after the project or one the run made.
G4: `awb lab obs link` signs the service's own copy of the bytes it checked; the name of an object decides nothing;
an object the check cannot read needs the owner's word; the result of the disk reader is checked like any object.
G11: a grant says that nothing checks what the bucket holds and names its own end for `awb close`.

IAM and OBS are the in-memory stand-in of tests/lab_fake.py, the key service the one of tests/test_lo_lab_obs.py,
the register the invented one of tests/fixtures.py. Every name, key and word here is invented.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from awb import config, gate, labclient, labruns
from awb.tcp import bucketgrant, labadmin, labobs
from tests import fixtures
from tests.test_bytg_obs_link import OBJECT, durations, lab, link, put_object, verify
from tests.test_lab_link_exit import put, register
from tests.test_lo_lab_obs import BUCKET, KEY, LAB, OTHER, PROJECT, SESSION, make_bucket, start_run, world

assert lab and world and register  # the fixtures, used by name

GRANTED = "own-bucket"
WORD = "the installer is the vendor's, link it"
NAME = fixtures.CUSTOMER_FORMS[0]


def call(w, run, method, path, **extra) -> dict:
    return w.svc._call_socket(dict({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run,
                                    "method": method, "service": "obs", "path": path}, **extra),
                              SESSION, caller=PROJECT)


def grant(w, code, bucket=GRANTED):
    assert bucketgrant.grant(w.svc.paths_fn(), LAB, bucket, code, "the bucket belongs to " + code, "2026-11-27",
                             say=lambda _line: None, exchange=set())


def plant(w, bucket, key="x", data=b"owner bytes"):
    w.fake.buckets.setdefault(bucket, {"objects": {}, "uploads": {}})["objects"][key] = {
        "data": data, "meta": {}, "etag": '"x"'}


def obs_calls(w, bucket) -> list:
    return [c for c in w.fake.calls if c[1].startswith("/" + bucket)]


# --------------------------------------------------------------------------- G3: the fence of a lab call


def test_g3_a_granted_bucket_is_refused_for_another_project_on_the_lab_call_path(world):
    w = world
    grant(w, OTHER)
    plant(w, GRANTED)
    run = start_run(w)
    for method in ("GET", "PUT", "DELETE", "HEAD"):
        a = call(w, run, method, "/%s/x" % GRANTED, **({"body": "new"} if method == "PUT" else {}))
        assert not a["ok"] and a["error"] == bucketgrant.REFUSED_LINE % GRANTED, method
    a = call(w, run, "GET", "/" + GRANTED, query={"tagging": ""})
    assert not a["ok"] and a["error"] == bucketgrant.REFUSED_LINE % GRANTED
    assert obs_calls(w, GRANTED) == []
    assert w.fake.buckets[GRANTED]["objects"]["x"]["data"] == b"owner bytes"


def test_g3_the_holder_of_a_grant_reaches_its_bucket(world):
    w = world
    grant(w, PROJECT)
    plant(w, GRANTED)
    run = start_run(w)
    a = call(w, run, "GET", "/%s/x" % GRANTED)
    assert a["ok"] and a["status"] == 200, a


def test_g3_a_copy_source_header_names_no_bucket_of_another_project(world):
    w = world
    grant(w, OTHER)
    plant(w, GRANTED)
    run = start_run(w)
    make_bucket(w, run)
    for header in ("x-obs-copy-source", "X-Amz-Copy-Source"):
        for value in ("/%s/x" % GRANTED, "%s/x" % GRANTED, "%2F" + GRANTED + "%2Fx"):
            a = call(w, run, "PUT", "/%s/copy" % BUCKET, headers={header: value})
            assert not a["ok"] and a["error"] == bucketgrant.REFUSED_LINE % GRANTED, (header, value)
    assert "copy" not in w.fake.buckets[BUCKET]["objects"]


def test_g3_an_exchange_bucket_is_refused_on_the_lab_call_path(world):
    w = world
    svc = w.service("x", {"lab_bucket": "the-exchange"})
    plant(w, "the-exchange")
    run = start_run(w, svc=svc)
    a = svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "GET",
                          "service": "obs", "path": "/the-exchange/x"}, SESSION, caller=PROJECT)
    assert not a["ok"] and a["error"] == "the exchange buckets are reached through awb xchg only"
    assert obs_calls(w, "the-exchange") == []


def test_g3_on_a_lab_tenant_a_lab_call_reaches_only_the_project_s_buckets_and_those_the_run_made(world):
    w = world
    run = start_run(w)
    plant(w, "owner-archive")                  # a bucket of the owner on the tenant, under no project's name
    plant(w, OTHER + "-lab-1")
    for bucket in ("owner-archive", OTHER + "-lab-1"):
        for method, extra in (("GET", {}), ("PUT", {"body": "new"}), ("DELETE", {})):
            a = call(w, run, method, "/%s/x" % bucket, **extra)
            assert not a["ok"] and a["error"] == labadmin.OBS_FENCE_LINE % (PROJECT, bucket), (bucket, method)
    # the create of a bucket that is there answers 200 on the platform: it never turns it into one the run made
    a = call(w, run, "PUT", "/owner-archive")
    assert not a["ok"] and a["error"] == labadmin.OBS_EXISTS_LINE % ("owner-archive", PROJECT)
    a = call(w, run, "PUT", "/" + OTHER + "-new")
    assert not a["ok"] and a["error"] == labadmin.OBS_FENCE_LINE % (PROJECT, OTHER + "-new")
    assert w.fake.buckets["owner-archive"]["objects"]["x"]["data"] == b"owner bytes"
    assert OTHER + "-new" not in w.fake.buckets
    # a new bucket of another name is the run's own once the run made it, and the list of buckets stays
    a = call(w, run, "PUT", "/site-test-1")
    assert a["ok"] and a["created"] == ["obs bucket"], a
    assert call(w, run, "PUT", "/site-test-1/index.html", body="hello")["status"] == 200
    assert call(w, run, "GET", "/site-test-1/index.html")["status"] == 200
    assert call(w, run, "GET", "/")["ok"]


# --------------------------------------------------------------------------- G4: the exit check of a link


def copy_of(w, run, key=KEY) -> bytes:
    return w.fake.buckets[labobs.link_bucket(run)]["objects"][key]["data"]


def test_g4_the_result_of_the_run_s_reader_gets_the_exit_check(lab):
    """Replaces test_the_result_of_the_run_s_reader_is_signed_as_before (tests/test_lab_link_exit.py): an object
    under files/ or manifest.json with no put recorded in the run was signed unread; a put with the lab key or a
    leased key leaves no record either."""
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    body = ("%s\n" % NAME).encode()
    for key in ("files/x.md", "files/Windows/System32/config/SYSTEM", "manifest.json"):
        plant(w, BUCKET, key, body)             # put past the service: the run records no object
        a = link(w, run, key=key)
        assert not a["ok"] and a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, key, "name"), key
    assert durations(w) == [labadmin.CRED_SECONDS]   # no link credential was made
    plant(w, BUCKET, "manifest.json", b'{"files": [], "result": "read"}\n')
    assert link(w, run, key="manifest.json")["ok"]


def test_g4_the_name_of_an_object_decides_nothing(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    for key in ("notes.zip", "notes.png", "notes.pdf"):
        put(w, run, key, "notes of the visit at %s, rack 4\n" % NAME)
        a = link(w, run, key=key)
        assert not a["ok"] and a["error"] == labobs.LINK_EXIT_LINE % (BUCKET, key, "name"), key


def test_g4_a_clean_object_is_signed_as_the_copy_the_service_holds(lab):
    """Replaces test_a_clean_object_is_signed (tests/test_lab_link_exit.py): the link reads the checked copy in the
    link bucket of the run, under the same key, never the object of the session."""
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    a = link(w, run)
    assert a["ok"] and a["bucket"] == BUCKET and a["key"] == KEY, a
    assert verify(a["url"], w.fake.creds)[:2] == (labobs.link_bucket(run), KEY)
    assert copy_of(w, run) == OBJECT


def test_g4_a_put_after_the_link_changes_nothing_the_link_reads(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    a = link(w, run, minutes=720)
    assert a["ok"], a
    held = labobs.link_bucket(run)
    put(w, run, KEY, "the real one: %s\n" % NAME)
    assert w.fake.buckets[BUCKET]["objects"][KEY]["data"] != OBJECT
    assert verify(a["url"], w.fake.creds)[:2] == (held, KEY) and copy_of(w, run) == OBJECT
    # the session reaches the copy on no path of a lab call or a lab obs transfer
    for method, extra in (("PUT", {"body": NAME}), ("DELETE", {}), ("GET", {})):
        b = call(w, run, method, "/%s/%s" % (held, KEY), **extra)
        assert not b["ok"] and b["error"] == labadmin.OBS_FENCE_LINE % (PROJECT, held), method
    b = call(w, run, "PUT", "/%s/x" % BUCKET, headers={"x-obs-copy-source": "/%s/%s" % (held, KEY)})
    assert not b["ok"]
    b = link(w, run, bucket=held)
    assert not b["ok"]
    assert copy_of(w, run) == OBJECT
    # a second link of the changed object is checked again and refused; the first copy stays what was checked
    b = link(w, run)
    assert not b["ok"] and b["error"] == labobs.LINK_EXIT_LINE % (BUCKET, KEY, "name")
    assert copy_of(w, run) == OBJECT


def test_g4_the_copy_and_its_bucket_go_with_the_end_of_the_run(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    assert link(w, run)["ok"] and link(w, run)["ok"]
    held = labobs.link_bucket(run)
    record = labruns.open_run(w.svc.paths_fn(), PROJECT, LAB)
    mine = [(c["label"], c["delete"]["path"]) for c in record["created"] if c["label"].startswith("link ")]
    assert mine == [(labobs.LINK_BUCKET_LABEL, "/" + held), (labobs.LINK_OBJECT_LABEL, "/%s/%s" % (held, KEY))]
    a = w.svc._call_socket({"op": "lab_run", "action": "end", "tenant": LAB, "project": PROJECT, "run": run},
                           SESSION, caller=PROJECT)
    assert a["ok"], a
    assert held not in w.fake.buckets and BUCKET not in w.fake.buckets


def _no_scan(*_a, **_k):
    raise AssertionError("an opaque object was scanned")


def test_g4_an_object_the_check_cannot_read_needs_the_owner_s_word(lab, monkeypatch):
    """Replaces test_a_binary_object_is_signed_without_the_name_check (tests/test_lab_link_opaque.py): an installer
    (a NUL byte in its first bytes) is still not scanned, and is signed only with the owner's word, which the run
    record keeps."""
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put(w, run, KEY, "MZ\x00\x00 installer bytes %s\x00" % NAME)
    put(w, run, "doc.txt", "%PDF-1.7 " + NAME)
    w.fake.virtual[(BUCKET, "big.txt")] = gate.TEXT_LIMIT + 1
    plant(w, BUCKET, "big.txt", NAME.encode())
    monkeypatch.setattr(gate, "_scan_decoded", _no_scan)
    for key, why in ((KEY, "no text"), ("doc.txt", "no text"), ("big.txt", "over 8 MiB")):
        a = link(w, run, key=key)
        assert not a["ok"] and a["error"].startswith("refused: obs://%s/%s is %s" % (BUCKET, key, why)), a
        assert "needs the owner's word" in a["error"] and NAME not in a["error"]
    assert durations(w) == [labadmin.CRED_SECONDS]
    for bad in ("", "  ", 7, "a\x00b"):
        assert not link(w, run, owner_word=bad)["ok"], bad
    a = link(w, run, owner_word=WORD)
    assert a["ok"], a
    assert verify(a["url"], w.fake.creds)[:2] == (BUCKET, KEY)          # in place: no copy of what was not read
    assert labobs.link_bucket(run) not in w.fake.buckets
    entry = labruns.open_run(w.svc.paths_fn(), PROJECT, LAB)["links"][-1]
    assert entry["word"] == WORD and entry["unread"] == "no text" and entry["bucket"] == BUCKET
    assert link(w, run, key="big.txt", owner_word=WORD)["ok"]
    logs = [p.read_text(encoding="utf-8") for p in (w.tmp / "log").rglob("*") if p.is_file()]
    assert any("unread, owner's word" in t for t in logs)


def test_g4_link_reads_one_object_of_a_bucket_the_run_made(lab):
    """Replaces test_bytg16_link_reads_one_object_of_a_bucket_the_run_made (tests/test_bytg_obs_link.py): the same
    link, signed for the copy in the link bucket of the run."""
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    before = datetime.now(timezone.utc).timestamp()
    a = link(w, run)
    assert a["ok"], a
    assert a["minutes"] == 60 and a["bucket"] == BUCKET and a["key"] == KEY
    assert a["key_sha256"] == hashlib.sha256(KEY.encode()).hexdigest()[:16]
    bucket, key, expires = verify(a["url"], w.fake.creds)
    assert (bucket, key) == (labobs.link_bucket(run), KEY)
    after = datetime.now(timezone.utc).timestamp()
    assert before + 3600 - 5 <= expires <= after + 3600 + 5   # the link expiry, not a speed bound
    with pytest.raises(AssertionError):
        verify(a["url"], w.fake.creds, method="PUT")
    # a credential of its own that outlives the link, never the held one of the run's calls
    assert durations(w)[-1] == 3600 + labadmin.LINK_SLACK
    held = w.svc.lab.tokens.get((LAB, "obs@eu-de"))
    ak = urllib.parse.parse_qs(urllib.parse.urlsplit(a["url"]).query)["AccessKeyId"][0]
    assert held is not None and held[0][0] != ak and ak not in w.svc.lab.values()
    # the URL, its signature and the secret key are in no log and no record
    sk, st = w.fake.creds[ak]
    record = labruns.open_run(w.svc.paths_fn(), PROJECT, LAB)
    entry = record["links"][-1]
    assert set(entry) == {"at", "bucket", "key_sha256", "minutes", "expires"}
    assert entry["key_sha256"] == a["key_sha256"] and entry["minutes"] == 60 and entry["bucket"] == BUCKET
    calls = [p.read_text(encoding="utf-8") for p in (w.tmp / "log").rglob("*") if p.is_file()]
    assert any("obs-link" in t and a["key_sha256"] in t for t in calls)
    assert not any(KEY in t for t in calls)
    texts = calls + [p.read_text(encoding="utf-8") for p in (w.tmp / "vault").rglob("*.json")]
    for t in texts:
        assert st not in t and sk not in t and ak not in t and "Signature=" not in t


def test_g4_labclient_prints_the_link_once_and_logs_no_url(lab, monkeypatch, capsys):
    """Replaces test_bytg16_labclient_prints_the_link_once_and_logs_no_url (tests/test_bytg_obs_link.py): the same
    commands; the URL names the link bucket of the run, the answer and the log the bucket of the session."""
    w = lab
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    assert labclient.call("PUT", "obs", "/" + BUCKET)[0] == 200
    assert labclient.call("PUT", "obs", "/%s/%s" % (BUCKET, KEY), body=OBJECT.decode())[0] == 200
    capsys.readouterr()
    assert labclient.obs_main(["link", BUCKET, KEY, "--minutes", "30"]) == 0
    out, err = capsys.readouterr()
    url = out.strip()
    run = labruns.open_run(w.svc.paths_fn(), PROJECT, LAB)["id"]
    held = labobs.link_bucket(run)
    assert out.count("\n") == 1 and verify(url, w.fake.creds)[:2] == (held, KEY)
    assert "30 min" in err and "not logged" in err and "Signature" not in err
    a = labclient.obs_link(BUCKET, KEY)
    assert a["minutes"] == 60 and a["bucket"] == BUCKET and verify(a["url"], w.fake.creds)[:2] == (held, KEY)
    logs = "".join(p.read_text(encoding="utf-8") for p in (w.folder / "notes" / "lab-runs").glob("*.log"))
    assert "obs link obs://%s/<key sha256 %s> GET, 30 min" % (BUCKET, a["key_sha256"]) in logs
    assert "Signature=" not in logs and "AccessKeyId=" not in logs and held not in logs
    assert labclient.obs_main(["link", BUCKET, "missing.bin"]) == 2
    assert "is not there" in capsys.readouterr().err
    with pytest.raises(labclient.LabClientError):
        labclient.obs_link(BUCKET, KEY, minutes="5")


# --------------------------------------------------------------------------- G11: what a grant says and its end


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return SimpleNamespace(vault=tmp_path / "vault")


def test_g11_a_new_grant_says_that_nothing_checks_what_the_bucket_holds(vault):
    said = []
    assert bucketgrant.grant(vault, "tcp-10503", GRANTED, PROJECT, "his bucket", "2026-11-27", say=said.append,
                             exchange=set())
    assert said == [bucketgrant.ENTRY_LINE % (GRANTED, PROJECT)]
    assert "no entry check" in said[0] and "awb lab obs put writes into it" in said[0]
    said.clear()
    assert not bucketgrant.grant(vault, "tcp-10503", GRANTED, PROJECT, "his bucket", "2026-11-27", say=said.append,
                                 exchange=set())
    assert said == []                                   # granted already: said once


def test_g11_a_project_s_grants_are_named_with_their_revoke_command_for_awb_close(vault):
    give = lambda tenant, bucket, code: bucketgrant.grant(   # noqa: E731
        vault, tenant, bucket, code, "his bucket", "2026-11-27", say=lambda _line: None, exchange=set())
    assert give("tcp-10503", "shared-images", PROJECT) and give("tcp-10491", GRANTED, PROJECT)
    assert give("tcp-10503", "other-images", OTHER)
    assert [(e["tenant"], e["bucket"]) for e in bucketgrant.of_project(vault, PROJECT)] == [
        ("tcp-10491", GRANTED), ("tcp-10503", "shared-images")]
    assert bucketgrant.close_lines(vault, PROJECT) == [
        bucketgrant.CLOSE_LINE % (PROJECT, GRANTED, "tcp-10491", "tcp-10491", GRANTED),
        bucketgrant.CLOSE_LINE % (PROJECT, "shared-images", "tcp-10503", "tcp-10503", "shared-images")]
    assert bucketgrant.close_lines(vault, PROJECT)[0].endswith("awb tenant bucket-revoke tcp-10491 " + GRANTED)
    assert bucketgrant.revoke(vault, "tcp-10491", GRANTED) and bucketgrant.revoke(vault, "tcp-10503", "shared-images")
    assert bucketgrant.close_lines(vault, PROJECT) == []
    assert len(bucketgrant.close_lines(vault, OTHER)) == 1
