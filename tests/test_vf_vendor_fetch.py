"""Task VF (2026-11-10): a vendor's appliance image into a file of the session, downloaded by the key service
(`awb lab obs fetch`, labobs._fetch). The vendor host is an in-memory stand-in: no socket leaves the process. The host
names are invented example hosts; the shipped list is checked on its own at the end."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from awb import labclient, vendorhosts
from awb.tcp import doc, labobs
from tests.test_lo_lab_obs import LAB, PART, _bytes, log_path, record, start_run, wait, world  # noqa: F401

HOST = "files.vendor.example"
PORTAL = "portal.vendor.example"
URL = "https://%s/images/12_4/appliance_12_4.ova" % HOST
DATA = _bytes(PART * 3 + 777, seed=11)


class _Answer:
    def __init__(self, body: bytes, cut: int | None = None):
        self.body, self.pos, self.cut = body, 0, cut

    def read(self, n=-1):
        if self.cut is not None and self.pos >= self.cut:
            raise ConnectionResetError("cut")
        end = len(self.body) if n is None or n < 0 else self.pos + n
        if self.cut is not None:
            end = min(end, self.cut)
        out = self.body[self.pos:end]
        self.pos += len(out)
        return out

    def close(self):
        pass


class Vendor:
    """The vendor host: `files` maps a URL to (status, headers, body); `cuts` breaks the next GETs half way."""

    def __init__(self):
        self.files = {URL: (200, {"Content-Type": "application/octet-stream", "ETag": '"v1"'}, DATA)}
        self.cuts: list[int] = []
        self.requests: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, timeout=None):
        self.requests.append((method, url, dict(headers)))
        status, hdrs, body = self.files.get(url, (404, {}, b"not found"))
        hdrs = dict(hdrs)
        if status == 200:
            hdrs["Content-Length"] = str(len(body))
        if method == "HEAD":
            return status, hdrs, _Answer(b"")
        start = 0
        rng = headers.get("Range")
        if status == 200 and rng and headers.get("If-Range") in (None, hdrs.get("ETag")):
            start, status = int(rng.split("=")[1].rstrip("-")), 206
        cut = self.cuts.pop(0) if self.cuts else None
        return status, hdrs, _Answer(body[start:], cut)


@pytest.fixture
def vendor(world, tmp_path, monkeypatch):  # noqa: F811
    hosts = tmp_path / "vendor-hosts.txt"
    hosts.write_text("# test list\n%s\nportal %s\n" % (HOST, PORTAL), encoding="utf-8")
    monkeypatch.setattr(labobs, "VENDOR_FILE", hosts)
    monkeypatch.setattr(vendorhosts, "VENDOR_FILE", hosts)
    v = Vendor()
    monkeypatch.setattr(labobs, "_vendor_open", v)
    world.vendor = v
    return world


def fetch(w, run, name="appliance.ova", url=URL, **extra) -> dict:
    target = w.folder / name
    fds = [os.open(target, os.O_RDWR | os.O_CREAT, 0o600),
           os.open(log_path(w, run), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)]
    req = dict({"op": "lab_obs", "action": "fetch", "tenant": LAB, "project": "tcp-ab2c", "run": run, "url": url},
               **extra)
    try:
        return w.svc._call_socket(req, os.getuid() + 1, caller="tcp-ab2c", fds=fds)
    finally:
        for fd in fds:
            os.close(fd)


def test_vf_a_fetch_writes_the_file_and_checks_the_hash_the_vendor_publishes(vendor):
    w = vendor
    run = start_run(w)
    sha1 = hashlib.sha1(DATA).hexdigest()
    a = fetch(w, run, hash="sha1:" + sha1)
    assert a["ok"] and a["size"] == len(DATA), a
    job = wait(w, a["job"])
    assert job["state"] == "done", job
    assert (w.folder / "appliance.ova").read_bytes() == DATA
    assert job["sha256"] == hashlib.sha256(DATA).hexdigest() and job["sha1"] == sha1
    assert job["vendor_hash"] == "sha1 matches"
    log = log_path(w, run).read_text(encoding="utf-8")
    for line in ("source: " + URL, "file: appliance.ova", "size: %d bytes" % len(DATA), "sha1: " + sha1,
                 "vendor hash: sha1 matches", "state: done"):
        assert line in log, line
    assert all("Cookie" not in h and "Authorization" not in h for _m, _u, h in w.vendor.requests)


def test_vf_without_a_vendor_hash_the_computed_ones_are_recorded_and_a_wrong_one_fails(vendor):
    w = vendor
    run = start_run(w)
    job = wait(w, fetch(w, run)["job"])
    assert job["state"] == "done" and job["vendor_hash"] == "none given, computed only"
    bad = wait(w, fetch(w, run, name="second.ova", hash="sha256:" + "0" * 64)["job"])
    assert bad["state"] == "failed" and "not the one the vendor publishes" in bad["error"]
    assert not fetch(w, run, name="third.ova", hash="md5:abc")["ok"]


def test_vf_only_a_listed_host_and_a_portal_link_is_handed_to_the_owner(vendor):
    w = vendor
    run = start_run(w)
    a = fetch(w, run, url="https://elsewhere.example/x.ova")
    assert not a["ok"] and "not a vendor download host" in a["error"]
    b = fetch(w, run, url="https://%s/SoftwareDownloads" % PORTAL)
    assert not b["ok"] and "vendor portal login" in b["error"] and "hand this download to the owner" in b["error"]
    c = fetch(w, run, url=URL + "?token=abc")
    assert not c["ok"] and "hand the download to the owner" in c["error"]
    w.vendor.files[URL] = (403, {}, b"")
    d = fetch(w, run)
    assert not d["ok"] and "vendor portal login" in d["error"]
    w.vendor.files[URL] = (200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>sign in</html>")
    e = fetch(w, run)
    assert not e["ok"] and "answers a web page" in e["error"]


def test_vf_a_redirect_is_followed_only_to_a_listed_host(vendor):
    w = vendor
    run = start_run(w)
    moved = "https://%s/latest.ova" % HOST
    w.vendor.files[moved] = (302, {"Location": URL}, b"")
    a = fetch(w, run, url=moved)
    assert a["ok"], a
    assert wait(w, a["job"])["state"] == "done" and (w.folder / "appliance.ova").read_bytes() == DATA
    w.vendor.files[moved] = (302, {"Location": "https://login.elsewhere.example/sso"}, b"")
    b = fetch(w, run, name="b.ova", url=moved)
    assert not b["ok"] and "not a vendor download host" in b["error"]


def test_vf_a_file_over_the_limit_is_refused_before_a_byte_moves(vendor, monkeypatch):
    w = vendor
    run = start_run(w)
    monkeypatch.setattr(labobs, "GB", 1000)
    a = fetch(w, run)
    assert not a["ok"] and "over the limit of 16 GB of a fetch" in a["error"]
    assert [m for m, _u, _h in w.vendor.requests] == ["HEAD"]


def test_vf_a_broken_download_goes_on_with_a_range_request(vendor):
    w = vendor
    run = start_run(w)
    w.vendor.cuts = [PART + 5]
    job = wait(w, fetch(w, run)["job"])
    assert job["state"] == "done", job
    assert (w.folder / "appliance.ova").read_bytes() == DATA
    gets = [h for m, _u, h in w.vendor.requests if m == "GET"]
    assert gets[1].get("Range") == "bytes=%d-" % (PART + 5) and gets[1].get("If-Range") == '"v1"'


def test_vf_the_command_fetches_lists_the_hosts_and_shows_the_job(vendor, monkeypatch, capsys):
    w = vendor
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    assert labclient.obs_main(["hosts"]) == 0
    out = capsys.readouterr().out
    assert HOST in out and "%s  (portal" % PORTAL in out
    sha1 = hashlib.sha1(DATA).hexdigest()
    assert labclient.obs_main(["fetch", URL, str(w.folder / "fw.ova"), "--sha1", sha1.upper()]) == 0
    job = capsys.readouterr().out.split()[1]
    assert labclient.obs_wait(job, every=0.01)["state"] == "done"
    assert labclient.obs_main(["status", job]) == 0
    out = capsys.readouterr().out
    assert "fetch %s -> fw.ova  done" % URL in out and "vendor hash: sha1 matches" in out
    with pytest.raises(labclient.LabClientError, match="a fetch writes only inside the project folder"):
        labclient.obs_fetch(URL, w.tmp / "x.ova")
    with pytest.raises(labclient.LabClientError, match="name one vendor hash"):
        labclient.obs_fetch(URL, w.folder / "y.ova", sha1=sha1, sha256="0" * 64)
    assert labclient.run_main(["end"]) == 0


def test_vf_the_shipped_lists_name_the_watchguard_hosts():
    hosts, portals = labobs.vendor_hosts()
    assert "cdn.watchguard.com" in hosts and "software.watchguard.com" in portals
    assert not set(hosts) & set(portals)
    assert "www.watchguard.com" in doc.allowed_hosts()
    assert not set(hosts) & set(doc.allowed_hosts()), "the download hosts are a list of their own"
    assert Path(labobs.VENDOR_FILE).name == "vendor-hosts.txt"
