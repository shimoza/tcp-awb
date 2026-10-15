"""awb/tcp/cloud.py against a stand-in API gateway that checks every signature (tests/tcp_fake.py)."""
from __future__ import annotations

import socket

import pytest

from awb import cli, config, jobs, obs
from awb.tcp import cloud
from tests.tcp_fake import FakeGateway
from tests.test_obs import fake_pass

AK, SK = "AKFAKE", "sk-fake-secret"


@pytest.fixture
def gw():
    with FakeGateway(AK, SK) as g:
        yield g


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)


def client(gw, **kw) -> cloud.Client:
    return cloud.Client(obs.Keys(kw.pop("ak", AK), kw.pop("sk", SK)), "eu-de", endpoint=gw.base, **kw)


def test_the_project_is_the_one_named_like_the_region(gw):
    c = client(gw)
    assert c.project_id() == FakeGateway.PROJECT
    assert c.expand("/v1/{project_id}/vpcs") == "/v1/%s/vpcs" % FakeGateway.PROJECT
    c.project_id()
    assert sum(1 for _, p in gw.calls if p.startswith("/v3/projects")) == 1      # asked once


def test_a_paged_list_by_marker_and_by_offset(gw):
    c = client(gw)
    vpcs = c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", limit=3)
    assert vpcs.state == jobs.LIST and [v["id"] for v in vpcs.items] == ["vpc-%02d" % i for i in range(7)]
    ports = c.list("vpc", "/v1/{project_id}/ports", "ports", paging="offset", limit=2)
    assert ports.state == jobs.LIST and len(ports.items) == 5


def test_a_limit_in_the_query_is_the_page_size(gw):
    c = client(gw)
    vpcs = c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", query={"limit": ["2"]})
    assert vpcs.state == jobs.LIST and len(vpcs.items) == 7
    pages = [p for m, p in gw.calls if "/vpcs" in p]
    assert len(pages) == 4 and all("limit=2" in p for p in pages)
    with pytest.raises(cloud.CloudError, match="is a number"):
        c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", query={"limit": "many"})


def test_empty_and_unknown_are_kept_apart(gw):
    c = client(gw)
    assert c.list("vpc", "/v1/{project_id}/empty", "items").state == jobs.EMPTY
    odd = c.list("vpc", "/v1/{project_id}/odd", "items")
    assert odd.state == jobs.UNKNOWN and "no list under items" in odd.reason
    boom = c.list("vpc", "/v1/{project_id}/boom", "items")
    assert boom.state == jobs.UNKNOWN and boom.reason == "HTTP 500"
    gw.vpcs = [{"name": "no id"}] * 3
    assert c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", limit=3).state == jobs.UNKNOWN


def test_a_wrong_key_is_an_answer_not_a_crash_and_the_key_never_shows(gw):
    gw.echo_key = True
    c = client(gw, sk="wrong-secret")
    r = c.get("iam", "/v3/projects")
    assert r.status == 401 and not r.ok
    assert AK not in r.text and "<ak>" in r.text
    with pytest.raises(cloud.CloudError, match="HTTP 401"):
        c.project_id()


def test_a_busy_service_is_asked_again_within_the_budget(gw):
    gw.busy = 2
    c = client(gw)
    listing = c.list("vpc", "/v1/{project_id}/busy", "items", paging="none")
    assert listing.state == jobs.LIST
    gw.busy = 5
    assert c.list("vpc", "/v1/{project_id}/busy", "items", paging="none").reason == "HTTP 503"


def test_no_answer_at_all_raises_without_the_key():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = cloud.Client(obs.Keys(AK, SK), endpoint="http://127.0.0.1:%d" % port, timeout=2)
    with pytest.raises(cloud.CloudError, match="no answer") as ei:
        c.get("iam", "/v3/projects")
    assert SK not in str(ei.value) and AK not in str(ei.value)


def test_names_and_paths_are_checked():
    c = cloud.Client(obs.Keys(AK, SK))
    with pytest.raises(cloud.CloudError):
        c.url("ECS!", "/v1")
    with pytest.raises(cloud.CloudError):
        c.url("ecs", "v1/no-slash")
    with pytest.raises(cloud.CloudError):
        cloud.Client(obs.Keys(AK, SK), "Frankfurt")
    assert c.url("ecs", "/v1/x", {"a": ["1", "2"]}) == "https://ecs.eu-de.otc.t-systems.com/v1/x?a=1&a=2"


# --------------------------------------------------------------------------- OBS is signed its own way (B-003)


class _Wire:
    """urlopen in place of the network: keeps every request and answers `status` with `body`."""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status, self.body, self.sent = status, body, []

    def __call__(self, req, timeout=None):
        import io
        import urllib.error

        self.sent.append(req)
        if self.status >= 300:
            raise urllib.error.HTTPError(req.full_url, self.status, "refused", {}, io.BytesIO(self.body))
        wire = self

        class Answer(io.BytesIO):
            status = wire.status

        return Answer(self.body)


@pytest.fixture
def wire(monkeypatch):
    from awb.tcp import throttle

    w = _Wire()
    monkeypatch.setattr(cloud.urllib.request, "urlopen", w)
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(throttle, "wait_turn", lambda host: 0.0)
    return w


def test_an_obs_call_goes_to_the_bucket_host_and_a_list_of_buckets_to_the_service_host():
    c = cloud.Client(obs.Keys(AK, SK))
    _, key, sub, url = c.obs_call("/tcp-ab2c-lab-1/a b.txt", {"policy": "", "max-keys": "5"})
    assert (key, sub) == ("a b.txt", "policy")
    assert url == "https://tcp-ab2c-lab-1.obs.eu-de.otc.t-systems.com/a%20b.txt?policy&max-keys=5"
    assert c.obs_call("/tcp-ab2c-lab-1")[3] == "https://tcp-ab2c-lab-1.obs.eu-de.otc.t-systems.com/"
    assert c.obs_call("/")[3] == "https://obs.eu-de.otc.t-systems.com/"
    on = cloud.Client(obs.Keys(AK, SK), endpoint="http://127.0.0.1:9")          # tests: the bucket by path
    assert on.obs_call("/tcp-ab2c-lab-1")[3] == "http://127.0.0.1:9/tcp-ab2c-lab-1/"
    assert on.obs_call("/")[3] == "http://127.0.0.1:9/"
    with pytest.raises(cloud.CloudError):
        c.obs_call("/Not_A_Bucket/x")


def test_an_obs_call_carries_the_v2_signature_and_another_service_the_gateway_one(wire):
    import base64
    import hashlib
    import hmac

    c = cloud.Client(obs.Keys(AK, SK))
    r = c.request("PUT", "obs", "/tcp-ab2c-lab-1", query={"policy": ""}, body={"Statement": []})
    assert r.status == 200
    req = wire.sent[-1]
    assert req.full_url == "https://tcp-ab2c-lab-1.obs.eu-de.otc.t-systems.com/?policy"
    text = "PUT\n\napplication/json\n%s\n/tcp-ab2c-lab-1/?policy" % req.get_header("Date")
    want = base64.b64encode(hmac.new(SK.encode(), text.encode(), hashlib.sha1).digest()).decode()
    assert req.get_header("Authorization") == "OBS %s:%s" % (AK, want)
    c.request("GET", "obs", "/")
    req = wire.sent[-1]
    text = "GET\n\n\n%s\n/" % req.get_header("Date")
    want = base64.b64encode(hmac.new(SK.encode(), text.encode(), hashlib.sha1).digest()).decode()
    assert req.get_header("Authorization") == "OBS %s:%s" % (AK, want)
    assert req.get_header("Content-type") is None
    c.request("GET", "vpc", "/v1/x")
    assert wire.sent[-1].get_header("Authorization").startswith("SDK-HMAC-SHA256 Access=%s," % AK)


def test_an_obs_error_in_xml_gives_its_code_and_message(wire):
    from tests.obs_fake import BAD_AUTHORIZATION, error_xml

    wire.status, wire.body = 400, error_xml("InvalidArgument", BAD_AUTHORIZATION)
    r = cloud.Client(obs.Keys(AK, SK)).request("PUT", "obs", "/tcp-ab2c-lab-1")
    assert r.status == 400 and r.data == {"code": "InvalidArgument", "message": BAD_AUTHORIZATION}
    assert obs.xml_error(error_xml("AccessDenied", "no")[:70]) == ("AccessDenied", "")     # cut short
    assert obs.xml_error(b"<ListAllMyBucketsResult/>") == ("", "")
    assert obs.xml_error(b'{"code": "x"}') == ("", "")


def test_the_work_user_and_a_missing_key_setting_are_refused(monkeypatch):
    monkeypatch.delenv("AWB_CLOUD_KEYS", raising=False)
    with pytest.raises(cloud.CloudError, match="AWB_CLOUD_KEYS"):
        cloud.settings()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(cloud.CloudError, match="T-100"):
        cloud.settings()


def test_the_command_line_reads_and_never_writes(gw, tmp_path, monkeypatch, capsys):
    fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": AK, "cloud/owner/sk": SK})
    monkeypatch.setenv("AWB_CLOUD_KEYS", "pass:cloud/owner")
    monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/vpcs", "--list", "vpcs", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("awb cloud: list (7)") and out.count('"id": "vpc-') == 7
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/odd", "--list", "items"]) == 2
    assert "unknown" in capsys.readouterr().out
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/boom"]) == 1
    capsys.readouterr()
    assert cli.main(["cloud", "projects"]) == 0
    out = capsys.readouterr().out
    assert "eu-de" in out and FakeGateway.PROJECT not in out and "3 project(s)" in out
    assert cli.main(["cloud", "post", "vpc", "/v1/x"]) == 2
    assert all(method == "GET" for method, _ in gw.calls)


# --------------------------------------------------------------------------- a whole OBS answer (B-005)
# The live run of 2026-10-12 got the list of buckets cut after the first one: an answer that is not JSON was kept as
# its first 300 characters only.

def bucket_list_xml(names, locations=None) -> bytes:
    from tests.obs_fake import CREATED, NS

    locations = locations or {}
    rows = "".join("<Bucket><Name>%s</Name><CreationDate>%s</CreationDate>%s</Bucket>"
                   % (n, CREATED, "<Location>%s</Location>" % locations[n] if n in locations else "") for n in names)
    return ('<?xml version="1.0" encoding="UTF-8"?><ListAllMyBucketsResult xmlns="%s"><Owner><ID>0000aaaa</ID>'
            '</Owner><Buckets>%s</Buckets></ListAllMyBucketsResult>' % (NS, rows)).encode()


def test_a_list_of_three_buckets_comes_back_whole_as_data(wire):
    from tests.obs_fake import CREATED

    names = ["tcp-ab2c-lab-1", "tcp-ab2c-lab-2", "tcp-ab2c-lab-3"]
    wire.body = bucket_list_xml(names, {"tcp-ab2c-lab-3": "eu-nl"})
    assert len(wire.body) > cloud.CUT
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "obs", "/")
    assert r.status == 200 and not r.cut and r.size == len(wire.body)
    assert r.data == {"buckets": [{"name": names[0], "created": CREATED}, {"name": names[1], "created": CREATED},
                                  {"name": names[2], "created": CREATED, "location": "eu-nl"}]}


def test_the_keys_and_the_tags_of_a_bucket_come_as_data(wire):
    from tests.obs_fake import NS

    wire.body = ('<ListBucketResult xmlns="%s"><Name>tcp-ab2c-lab-1</Name><Prefix>in/</Prefix>'
                 '<IsTruncated>true</IsTruncated><NextMarker>in/b.txt</NextMarker>'
                 '<Contents><Key>in/a.txt</Key><Size>12</Size><ETag>"abc"</ETag>'
                 '<LastModified>2026-10-03T07:58:12.000Z</LastModified></Contents>'
                 '<Contents><Key>in/b.txt</Key><Size>0</Size><ETag>"def"</ETag></Contents>'
                 '<CommonPrefixes><Prefix>in/sub/</Prefix></CommonPrefixes></ListBucketResult>' % NS).encode()
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "obs", "/tcp-ab2c-lab-1", query={"prefix": "in/"})
    assert r.data == {"name": "tcp-ab2c-lab-1", "prefix": "in/", "truncated": True, "next_marker": "in/b.txt",
                      "prefixes": ["in/sub/"],
                      "keys": [{"key": "in/a.txt", "size": 12, "etag": "abc", "modified": "2026-10-03T07:58:12.000Z"},
                               {"key": "in/b.txt", "size": 0, "etag": "def", "modified": ""}]}
    wire.body = ('<Tagging xmlns="%s"><TagSet><Tag><Key>awb-project</Key><Value>tcp-ab2c</Value></Tag></TagSet>'
                 '</Tagging>' % NS).encode()
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "obs", "/tcp-ab2c-lab-1", query={"tagging": ""})
    assert r.data == {"tags": [{"key": "awb-project", "value": "tcp-ab2c"}]}
    assert obs.xml_result(b"<Other/>") is None and obs.xml_result(b"<ListBucketResult") is None


def test_a_long_answer_that_is_not_json_says_it_was_cut_and_how_long_the_whole_was(wire):
    wire.body = b"<html><body>" + b"x" * 4000 + b"</body></html>"
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "vpc", "/v1/x")
    assert r.data is None and r.cut and r.size == len(wire.body) and len(r.text) == cloud.CUT
    assert cloud.cut_line(r.size) == ("awb cloud: the answer is not JSON and was cut to its first 300 of %d bytes"
                                      % len(wire.body))
    wire.body = b"short and plain"
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "vpc", "/v1/x")
    assert not r.cut and r.text == "short and plain" and r.size == 15
    wire.body = b'{"items": [' + b", ".join(b'"%d"' % i for i in range(200)) + b"]}"
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "vpc", "/v1/x")
    assert not r.cut and len(r.data["items"]) == 200


class _Router:
    """urlopen in place of the network for the bucket sweep: the list of buckets and the tags of each (a dict, None
    for no tags, "denied" for a 403); a bucket not in `tags` is gone."""

    def __init__(self, names, tags, locations=None):
        self.list = bucket_list_xml(names, locations)
        self.tags, self.sent = tags, []

    def __call__(self, req, timeout=None):
        import io
        import urllib.error
        import urllib.parse

        from tests.obs_fake import NS, error_xml

        self.sent.append(req.full_url)
        host = urllib.parse.urlsplit(req.full_url).netloc
        bucket = host.split(".obs.")[0] if ".obs." in host else ""
        if not bucket:
            status, body = 200, self.list
        elif bucket not in self.tags:
            status, body = 404, error_xml("NoSuchBucket", "gone")
        elif self.tags[bucket] is None:
            status, body = 404, error_xml("NoSuchTagSet", "no tags")
        elif self.tags[bucket] == "denied":
            status, body = 403, error_xml("AccessDenied", "no")
        else:
            status, body = 200, ('<Tagging xmlns="%s"><TagSet>%s</TagSet></Tagging>' % (NS, "".join(
                "<Tag><Key>%s</Key><Value>%s</Value></Tag>" % kv for kv in self.tags[bucket].items()))).encode()
        if status >= 300:
            raise urllib.error.HTTPError(req.full_url, status, "refused", {}, io.BytesIO(body))

        class Answer(io.BytesIO):
            pass
        Answer.status = status
        return Answer(body)


def test_the_buckets_of_the_region_come_with_their_tags_for_the_sweep(wire, monkeypatch):
    from tests.obs_fake import CREATED

    tagged = {"awb-project": "tcp-ab2c", "awb-expiry": "2026-12-31"}
    router = _Router(["tcp-ab2c-lab-1", "tcp-ab2c-lab-2", "tcp-ab2c-gone", "tcp-ab2c-other"],
                     {"tcp-ab2c-lab-1": tagged, "tcp-ab2c-lab-2": None, "tcp-ab2c-other": tagged},
                     {"tcp-ab2c-other": "eu-nl"})
    monkeypatch.setattr(cloud.urllib.request, "urlopen", router)
    listing = cloud.Client(obs.Keys(AK, SK)).buckets()
    assert listing.state == jobs.LIST
    assert list(listing.items) == [
        {"id": "tcp-ab2c-lab-1", "name": "tcp-ab2c-lab-1", "created": CREATED,
         "tags": [{"key": "awb-project", "value": "tcp-ab2c"}, {"key": "awb-expiry", "value": "2026-12-31"}]},
        {"id": "tcp-ab2c-lab-2", "name": "tcp-ab2c-lab-2", "created": CREATED, "tags": []}]
    assert not any("tcp-ab2c-other" in url for url in router.sent)          # a bucket of another region
    assert "https://tcp-ab2c-lab-1.obs.eu-de.otc.t-systems.com/?tagging" in router.sent
    router.tags["tcp-ab2c-lab-2"] = "denied"
    listing = cloud.Client(obs.Keys(AK, SK)).buckets()
    assert listing.state == jobs.UNKNOWN and "HTTP 403" in listing.reason        # unknown, never a shorter list


class _Endless:
    """An answer without end: a read of all of it is a failure, a bounded read gets what it asks for."""

    status = 200

    def __init__(self):
        self.asked = 0

    def read(self, n=-1):
        assert n is not None and n >= 0, "the whole answer was read"
        self.asked += n
        return b"<" + b"x" * (n - 1) if n else b""

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_an_answer_is_read_up_to_its_bound_and_marked_cut(monkeypatch):
    """Review of 2026-10-13: the key service read a whole answer into memory, so a GET of a large object of a
    project bucket (awb cloud call GET obs /BUCKET/KEY) could take the owner's daemon down."""
    import io
    import urllib.error

    from awb.tcp import throttle

    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(throttle, "wait_turn", lambda host: 0.0)
    answer = _Endless()
    monkeypatch.setattr(cloud.urllib.request, "urlopen", lambda req, timeout=None: answer)
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "obs", "/tcp-ab2c-lab-1/big.bin")
    assert r.status == 200 and r.data is None and r.cut and r.size > cloud.MAX_READ
    assert answer.asked <= cloud.MAX_READ + 1 and len(r.text) <= cloud.CUT
    assert "more than %d bytes" % cloud.MAX_READ in cloud.cut_line(r.size)

    def refused(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "refused", {}, _Endless())

    monkeypatch.setattr(cloud.urllib.request, "urlopen", refused)
    r = cloud.Client(obs.Keys(AK, SK)).request("GET", "obs", "/tcp-ab2c-lab-1/big.bin")
    assert r.status == 403 and r.data is None and r.cut


def test_the_retry_after_of_a_busy_gateway_is_waited_through_jobs_pause(monkeypatch):
    """F1 section 5: the Retry-After of a 429 was a time.sleep of the client's own, up to 30 s for real in the
    suite. It goes through `jobs.pause` now, as the retry wait after it does. No server: a stand-in urlopen answers
    429 with Retry-After 7, then 200. Planted: a client that sleeps by itself records no 7."""
    import email.message
    import io
    import urllib.error

    from awb.tcp import throttle

    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(throttle, "wait_turn", lambda host: 0.0)
    waits, sent = [], []
    monkeypatch.setattr(jobs, "pause", waits.append)

    def busy_once(req, timeout=None):
        sent.append(req.full_url)
        if len(sent) == 1:
            headers = email.message.Message()
            headers["Retry-After"] = "7"
            raise urllib.error.HTTPError(req.full_url, 429, "busy", headers, io.BytesIO(b"{}"))
        answer = _Endless()
        answer.read = lambda n=-1: b'{"servers": []}'
        return answer

    monkeypatch.setattr(cloud.urllib.request, "urlopen", busy_once)
    r = cloud.Client(obs.Keys(AK, SK)).get("ecs", "/v1/servers")
    assert r.status == 200 and r.data == {"servers": []}
    assert len(sent) == 2 and waits == [7.0, 2.0]
