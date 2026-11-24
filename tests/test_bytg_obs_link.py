"""tcp-bytg ask 2026-11-21-16: a time-limited GET link to one object of a bucket the open lab run made (awb/tcp/labobs.py
action link, labclient.obs_link, `awb lab obs link`), and the refusal of a public ACL in a lab call that names it.

IAM and OBS are the in-memory stand-in of tests/lab_fake.py, the key service the one of tests/test_lo_lab_obs.py.
The link is checked by a verifier written here from the OBS page "Authentication of Signature in a URL" (the
security token inside the signed resource) against the credential the fake IAM handed out, and the signer against a
fixed vector. Every key, token and name here is invented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import pytest

from awb import labclient, labruns
from awb.tcp import labadmin, labobs
from tests.test_lo_lab_obs import BUCKET, KEY, LAB, OTHER, PROJECT, SESSION, lab_obs, make_bucket, start_run, world

assert world  # the fixture, used by name

OBJECT = b"invented object bytes for a reader"


def put_object(w, run, key=KEY, bucket=BUCKET):
    a = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "PUT",
                            "service": "obs", "path": "/%s/%s" % (bucket, key), "body": OBJECT.decode()},
                           SESSION, caller=PROJECT)
    assert a["ok"] and a["status"] == 200, a


def link(w, run, **extra) -> dict:
    req = dict({"op": "lab_obs", "action": "link", "tenant": LAB, "project": PROJECT, "run": run,
                "bucket": BUCKET, "key": KEY}, **extra)
    return w.svc._call_socket(req, SESSION, caller=PROJECT)


def verify(url: str, creds: dict, method: str = "GET") -> tuple[str, str, int]:
    """(bucket, key, expires) of a link whose signature holds for `method`; AssertionError otherwise."""
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    assert set(q) == {"AccessKeyId", "Expires", "Signature", "x-obs-security-token"}
    sk, token = creds[q["AccessKeyId"]]
    assert q["x-obs-security-token"] == token
    bucket, _, key = parts.path.lstrip("/").partition("/")
    resource = "/%s/%s?x-obs-security-token=%s" % (bucket, key, token)
    text = "%s\n\n\n%s\n%s" % (method, q["Expires"], resource)
    want = base64.b64encode(hmac.new(sk.encode(), text.encode(), hashlib.sha1).digest()).decode()
    assert hmac.compare_digest(want, q["Signature"])
    return bucket, urllib.parse.unquote(key), int(q["Expires"])


def durations(w) -> list[int]:
    """The duration_seconds of every temporary credential the fake IAM was asked for."""
    return [json.loads(body)["auth"]["identity"]["token"]["duration_seconds"] for body in w.iam_bodies]


@pytest.fixture
def lab(world, monkeypatch):
    w = world
    w.iam_bodies = []
    inner = w.fake.http

    def http(method, url, headers, data, timeout=None):
        if url.endswith("/v3.0/OS-CREDENTIAL/securitytokens"):
            w.iam_bodies.append(data)
        return inner(method, url, headers, data, timeout)

    monkeypatch.setattr(labadmin, "_http", http)
    return w


def test_bytg16_presign_matches_a_fixed_vector_and_the_documented_form():
    url = labobs.presign("tcp-ab2c-lab-1", "dir/a b.txt", "AKINVENTED0000000001", "sk-invented-secret",
                         "token-invented+/=", 1800000000, "eu-de")
    assert url.startswith("https://tcp-ab2c-lab-1.obs.eu-de.otc.t-systems.com/dir/a%20b.txt?AccessKeyId="
                          "AKINVENTED0000000001&Expires=1800000000&Signature=")
    assert url.endswith("&%s=%s" % (labobs.TOKEN_PARAM, "token-invented%2B%2F%3D"))
    text = "GET\n\n\n1800000000\n/tcp-ab2c-lab-1/dir/a%20b.txt?x-obs-security-token=token-invented+/="
    sig = base64.b64encode(hmac.new(b"sk-invented-secret", text.encode(), hashlib.sha1).digest()).decode()
    assert "Signature=%s&" % urllib.parse.quote(sig, safe="") in url
    assert "Signature=uuvYJLgWUXn9c1sYVwdUltWYhoo%3D&" in url
    plain = labobs.presign("b-1-x", "k", "AK00000000", "sk", None, 1, endpoint="http://127.0.0.1:9")
    assert plain.startswith("http://127.0.0.1:9/b-1-x/k?AccessKeyId=AK00000000&Expires=1&Signature=")
    assert "security-token" not in plain


def test_bytg16_link_reads_one_object_of_a_bucket_the_run_made(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    before = time.time()
    a = link(w, run)
    assert a["ok"], a
    assert a["minutes"] == 60 and a["bucket"] == BUCKET and a["key"] == KEY
    assert a["key_sha256"] == hashlib.sha256(KEY.encode()).hexdigest()[:16]
    bucket, key, expires = verify(a["url"], w.fake.creds)
    assert (bucket, key) == (BUCKET, KEY)
    assert before + 3600 - 5 <= expires <= time.time() + 3600 + 5
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


def test_bytg16_minutes_go_from_1_to_720(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    a = link(w, run, minutes=720)
    assert a["ok"] and a["minutes"] == 720 and durations(w)[-1] == 720 * 60 + labadmin.LINK_SLACK
    a = link(w, run, minutes=1)
    assert a["ok"] and durations(w)[-1] == 900
    for bad in (0, 721, "60", True, 1.5):
        a = link(w, run, minutes=bad)
        assert not a["ok"] and a["error"] == labobs.LINK_MINUTES_LINE, bad


def test_bytg16_link_is_refused_outside_the_run_s_own_buckets(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    put_object(w, run)
    # a bucket of the project the run did not make
    w.fake.buckets[PROJECT + "-older"] = {"objects": {KEY: {"data": OBJECT, "meta": {}, "etag": '"x"'}},
                                          "uploads": {}}
    a = link(w, run, bucket=PROJECT + "-older")
    assert not a["ok"] and a["error"] == labobs.LINK_BUCKET_LINE % (run, PROJECT + "-older")
    a = link(w, run, bucket=OTHER + "-lab-1")
    assert not a["ok"] and "named after the project" in a["error"]
    for exchange in w.svc.exchange_buckets():
        a = link(w, run, bucket=exchange)
        assert not a["ok"] and "exchange buckets" in a["error"]
    a = link(w, run, key="missing/thing.bin")
    assert not a["ok"] and a["error"] == "refused: obs://%s/missing/thing.bin is not there" % BUCKET
    a = link(w, run, key="../x")
    assert not a["ok"]
    a = link(w, "lab-20000101-000000-zzzz")
    assert not a["ok"] and "no open lab run" in a["error"]
    # a bucket the run made and then removed
    gone = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "DELETE",
                               "service": "obs", "path": "/%s/%s" % (BUCKET, KEY)}, SESSION, caller=PROJECT)
    assert gone["ok"]
    gone = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "DELETE",
                               "service": "obs", "path": "/" + BUCKET}, SESSION, caller=PROJECT)
    assert gone["ok"] and gone["status"] == 204
    a = link(w, run)
    assert not a["ok"] and a["error"] == labobs.LINK_BUCKET_LINE % (run, BUCKET)
    assert durations(w) == [labadmin.CRED_SECONDS]  # the run's own calls only: no link credential was made


def test_bytg16_link_is_refused_on_a_customer_tenant(lab, monkeypatch):
    w = lab
    run = start_run(w)
    make_bucket(w, run)
    monkeypatch.setattr(labadmin, "_is_customer", lambda svc, alias: True)
    a = link(w, run)
    assert not a["ok"] and a["error"] == labobs.LINK_CUSTOMER_LINE % " and ".join(labadmin.ALLOWED)


def test_bytg16_a_public_acl_stays_refused_and_names_the_link(lab):
    w = lab
    run = start_run(w)
    make_bucket(w, run)

    def call(path, headers=None, query=None, body=None, method="PUT"):
        return w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run,
                                   "method": method, "service": "obs", "path": path, "headers": headers or {},
                                   "query": query or {}, "body": body}, SESSION, caller=PROJECT)

    n = len(w.fake.calls)
    for a in (call("/%s/%s" % (BUCKET, KEY), {"x-obs-acl": "public-read"}, body="x"),
              call("/" + BUCKET, {"X-Amz-Acl": "Public-Read-Write"}),
              call("/%s/%s" % (BUCKET, KEY), {"x-obs-grant-read": "id=domain-0001,Everyone"}),
              call("/%s/%s" % (BUCKET, KEY), query={"acl": ""},
                   body="<AccessControlPolicy><AccessControlList><Grant><Grantee><Canned>Everyone</Canned>"
                        "</Grantee><Permission>READ</Permission></Grant></AccessControlList>"
                        "</AccessControlPolicy>")):
        assert not a["ok"] and a["error"] == labadmin.PUBLIC_ACL_LINE
        assert "awb lab obs link" in a["error"]
    assert len(w.fake.calls) == n
    # the error line passes the client's screen whole
    assert labclient._service_error({"ok": False, "error": labadmin.PUBLIC_ACL_LINE}) == labadmin.PUBLIC_ACL_LINE
    # a private ACL and a read of the ACL go through
    assert call("/%s/%s" % (BUCKET, KEY), {"x-obs-acl": "private"}, body="x")["ok"]
    assert call("/%s/%s" % (BUCKET, KEY), query={"acl": ""}, method="GET")["ok"]


def test_bytg16_labclient_prints_the_link_once_and_logs_no_url(lab, monkeypatch, capsys):
    w = lab
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    assert labclient.call("PUT", "obs", "/" + BUCKET)[0] == 200
    assert labclient.call("PUT", "obs", "/%s/%s" % (BUCKET, KEY), body=OBJECT.decode())[0] == 200
    capsys.readouterr()
    assert labclient.obs_main(["link", BUCKET, KEY, "--minutes", "30"]) == 0
    out, err = capsys.readouterr()
    url = out.strip()
    assert out.count("\n") == 1 and verify(url, w.fake.creds)[:2] == (BUCKET, KEY)
    assert "30 min" in err and "not logged" in err and "Signature" not in err
    a = labclient.obs_link(BUCKET, KEY)
    assert a["minutes"] == 60 and verify(a["url"], w.fake.creds)[:2] == (BUCKET, KEY)
    logs = "".join(p.read_text(encoding="utf-8") for p in (w.folder / "notes" / "lab-runs").glob("*.log"))
    assert "obs link obs://%s/<key sha256 %s> GET, 30 min" % (BUCKET, a["key_sha256"]) in logs
    assert "Signature=" not in logs and "AccessKeyId=" not in logs
    assert labclient.obs_main(["link", BUCKET, "missing.bin"]) == 2
    assert "is not there" in capsys.readouterr().err
    with pytest.raises(labclient.LabClientError):
        labclient.obs_link(BUCKET, KEY, minutes="5")
