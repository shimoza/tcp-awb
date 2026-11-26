"""The recipe disk-reader (tcp-bytg ask 2026-11-21-tcp-bytg-10): read the system volume of a stopped server through a
Linux reader ECS and give it back, on the fake answers of an invented cloud in memory (the stand-in key service of
tests/lab_fake.py). Also the reader script of awb/tcp/diskreader.py and the engine parts the recipe needs: a wait on
the status of an answer, a create that is kept, a restore that runs first in the teardown, a preflight on one object
and the user_data rendered from the run's values. Every id and value here is invented.
"""
from __future__ import annotations

import base64
import copy
import itertools
import json
import re
from pathlib import Path

import pytest

from awb import gate
from awb.tcp import diskreader, lab, labwrites
from tests.lab_fake import FakeKeyService
from tests.test_lab import PROJECT, REPO_RECIPES, evidence, project, rows, run, writes  # noqa: F401

SERVER = "%08x-1111-4222-8333-%012x" % (0x5a1b2c3d, 0x444455556666)
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
VPC = "%08x-1111-4222-8333-%012x" % (0x3c4d5e6f, 0x777788889999)
NET = "%08x-2222-4333-8444-%012x" % (0x7e6f5a4b, 0xaaaabbbbcccc)
AZ = "eu-de-01"
P = "/v1/{project_id}/"
PARAMS = ["server_id=" + SERVER]


class DiskCloud(FakeKeyService):
    """An invented cloud for the recipe: one stopped server with its system volume, the calls of the recipe and the
    reader, which writes its manifest once the volume is attached to it. The knobs: `status` (the state of the
    server), `az`, `reader_writes` (False: the reader never writes the manifest), `manifest` (what it writes),
    `attach_reader` and `attach_server` (HTTP statuses of the attach calls), `lose_detach` (the first detach is made
    and its answer is lost), `order` (what happened, in order)."""

    def __init__(self):
        super().__init__()
        self.status = "SHUTOFF"
        self.az = AZ
        self.reader_writes = True
        self.manifest = {"result": "read", "files": []}
        self.attach_reader = 200
        self.attach_server = 200
        self.lose_detach = False
        self.order: list[str] = []
        self.volume = {"status": "in-use", "holder": SERVER}
        self.readers: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.buckets: dict[str, dict] = {}
        self.destroyed_volume = False
        self.ids = itertools.count(1)
        self.bodies: dict[str, dict] = {}

    @staticmethod
    def ok(status, data=None, text=""):
        return {"ok": True, "status": status, "data": data, "text": text}

    def new_id(self):
        return "%08x-6666-4777-8888-%012x" % (0x1a2b3c4d + next(self.ids), 0xbeef00 + next(self.ids))

    def volume_view(self):
        holder = self.volume["holder"]
        return {"volume": {"id": VOLUME, "status": self.volume["status"], "size": 40,
                           "attachments": [{"server_id": holder}] if holder else []}}

    def __call__(self, req):
        if req.get("op") != "call":
            return super().__call__(req)
        m, path, body = req["method"], req["path"], req.get("body")
        if path == "/v2/{project_id}/types":
            return super().__call__(req)
        self.calls.append(req)
        if self.on_call is not None:
            self.on_call(req)
        if path == P + "vpcs":
            return self.ok(200, {"vpcs": []})
        if req["service"] == "obs":
            return self.obs(m, path)
        if path == P + "cloudservers/" + SERVER and m == "GET":
            return self.ok(200, {"server": {
                "id": SERVER, "status": self.status, "OS-EXT-AZ:availability_zone": self.az,
                "metadata": {"vpc_id": VPC},
                "os-extended-volumes:volumes_attached": [{"id": VOLUME, "bootIndex": "0", "device": "/dev/sda"}]}})
        if path == P + "cloudservers/%s/os-interface" % SERVER:
            return self.ok(200, {"interfaceAttachments": [{"net_id": NET, "port_state": "ACTIVE"}]})
        if path == "/v2/cloudimages":
            return self.ok(200, {"images": [{"id": "img-0001"}]})
        if path == P + "security-groups":
            if m == "POST":
                gid = self.new_id()
                self.groups[gid] = {"deleted": False, "body": body}
                self.order.append("sg")
                return self.ok(200, {"security_group": {"id": gid}})
            return self.ok(200, {"security_groups": [{"id": g, "name": v["body"]["security_group"]["name"]}
                                                     for g, v in self.groups.items() if not v["deleted"]]})
        sg = re.match(r"^/v1/\{project_id\}/security-groups/(?P<id>[a-z0-9-]+)$", path)
        if sg:
            g = self.groups.get(sg["id"])
            if g is None or g["deleted"]:
                return self.ok(404, {"error_msg": "not found"})
            if m == "DELETE":
                g["deleted"] = True
                self.order.append("delete sg")
            return self.ok(200, {"security_group": {"id": sg["id"]}})
        if path == P + "cloudservers" and m == "POST":
            rid = self.new_id()
            self.readers[rid] = {"deleted": False, "body": body}
            self.bodies["reader"] = body
            self.order.append("reader")
            return self.ok(200, {"serverIds": [rid]})
        if path == P + "cloudservers/detail":
            return self.ok(200, {"servers": [{"id": r, "name": v["body"]["server"]["name"]}
                                             for r, v in self.readers.items() if not v["deleted"]]})
        if path == P + "cloudservers/delete":
            for item in body["servers"]:
                r = self.readers.get(item["id"])
                if r is not None:
                    r["deleted"] = True
                    self.order.append("delete reader")
                    if body.get("delete_volume") and self.volume["holder"] == item["id"]:
                        self.destroyed_volume = True      # the data disk of the reader goes with it
                        self.volume = {"status": "deleted", "holder": None}
            return self.ok(200, {"job_id": "job-1"})
        m1 = re.match(r"^/v1/\{project_id\}/cloudservers/(?P<id>[a-z0-9-]+)$", path)
        if m1 and m == "GET":
            r = self.readers.get(m1["id"])
            if r is None or r["deleted"]:
                return self.ok(404, {"error_msg": "not found"})
            return self.ok(200, {"server": {"id": m1["id"], "status": "ACTIVE"}})
        det = re.match(r"^/v1/\{project_id\}/cloudservers/(?P<id>[a-z0-9-]+)/detachvolume/(?P<vol>[a-z0-9-]+)$", path)
        if det and m == "DELETE":
            who = "server" if det["id"] == SERVER else "reader"
            if self.volume["holder"] != det["id"]:
                return self.ok(404, {"error_code": "Ecs.0010", "error_msg": "not attached"})
            self.volume = {"status": "available", "holder": None}
            self.order.append("detach " + who)
            if self.lose_detach and who == "server":
                self.lose_detach = False
                raise ConnectionResetError("no answer")
            return self.ok(200, {"job_id": "job-2"})
        att = re.match(r"^/v1/\{project_id\}/cloudservers/(?P<id>[a-z0-9-]+)/attachvolume$", path)
        if att and m == "POST":
            who = "server" if att["id"] == SERVER else "reader"
            status = self.attach_server if who == "server" else self.attach_reader
            if status != 200:
                return self.ok(status, {"error_code": "Ecs.0099", "error_msg": "refused"})
            self.volume = {"status": "in-use", "holder": att["id"]}
            self.order.append("attach " + who)
            self.bodies["attach " + who] = body
            return self.ok(200, {"job_id": "job-3"})
        if path == "/v2/{project_id}/volumes/" + VOLUME and m == "GET":
            if self.volume["status"] == "deleted":
                return self.ok(404, {"error_msg": "gone"})
            return self.ok(200, self.volume_view())
        return self.ok(404, {"error_msg": "no such path"})

    def obs(self, m, path):
        name = path[1:].split("/")[0]
        if path.endswith("/manifest.json"):
            attached_to_reader = self.volume["holder"] in self.readers or self.order.count("attach reader")
            if not (self.reader_writes and attached_to_reader):
                return self.ok(404, None, "<Error><Code>NoSuchKey</Code></Error>")
            return self.ok(200, None if m == "HEAD" else self.manifest)
        if m == "PUT":
            self.buckets[name] = {}
            self.order.append("bucket")
            return self.ok(200)
        if name not in self.buckets:
            return self.ok(404, None, "<Error><Code>NoSuchBucket</Code></Error>")
        return self.ok(200)


@pytest.fixture
def cloud(project, monkeypatch):
    monkeypatch.setenv(lab.RECIPES_ENV, str(REPO_RECIPES))
    return DiskCloud()


def go(fake, params=()):
    return run(fake, recipe="disk-reader", params=list(PARAMS) + list(params))


def recipe() -> dict:
    return lab.load(REPO_RECIPES / "disk-reader.json")


def reader_body(fake) -> dict:
    return fake.bodies["reader"]["server"]


# --------------------------------------------------------------------------- the recipe file


def test_the_recipe_is_valid_fits_its_guard_and_has_no_default_in_the_nl_region():
    r = recipe()
    assert r["name"] == "disk-reader" and lab.confirm_notes(r)
    creates = [s["id"] for s in r["steps"] if s["do"] == "create"]
    assert creates == ["bucket", "sg", "reader"] and len(creates) <= r["guard"]["resources"]
    # the bucket is kept on purpose, the other two are torn down
    assert set(r["teardown"]) == {"sg", "reader"}
    assert "eu-nl" not in json.dumps({k: p["default"] for k, p in r["params"].items()})
    assert set(r["params"]) >= {"server_id", "region", "az", "files"}
    assert r["restore"]["after"] == "detach" and r["restore"]["keep"] == ["reader"]


def test_the_list_shows_the_recipe_and_the_readme_names_it(monkeypatch):
    monkeypatch.setenv(lab.RECIPES_ENV, str(REPO_RECIPES))
    text = "\n".join(lab.list_lines())
    assert "disk-reader" in text and "system volume of a stopped server" in text
    assert "`disk-reader`" in (REPO_RECIPES / "README.md").read_text(encoding="utf-8")


def test_the_dry_run_lists_the_calls_in_order_and_makes_none(monkeypatch, capsys):
    monkeypatch.setenv(lab.RECIPES_ENV, str(REPO_RECIPES))
    seen = []
    monkeypatch.setattr(lab, "_send_default", lambda req: seen.append(req))
    assert lab.main(["dry-run", "disk-reader", "--param", "server_id=" + SERVER]) == lab.EXIT_OK
    out = capsys.readouterr().out
    assert seen == [] and "dry run: no call was made" in out
    order = ["preflight stopped", "call server:", "call nic:", "call image:", "create bucket:", "create sg:",
             "create reader:", "wait reader-up:", "call detach:", "wait detached:", "call attach:", "wait attached:",
             "wait manifest:", "call read:", "call release:", "wait released:", "call back:", "wait back-ready:",
             "teardown, first, whatever happened", "give-back:", "teardown, newest first", "\n   reader: POST", "\n   sg: DELETE"]
    at = [out.index(x) for x in order]
    assert at == sorted(at), order
    assert "/detachvolume/" in out and "/attachvolume" in out
    assert "kept after the run" in out and "publicip" not in out.replace("delete_publicip", "")
    assert "<user_data of diskreader>" in out


def test_the_write_calls_of_the_recipe_reach_the_lab_writes_line(monkeypatch):
    monkeypatch.setenv(lab.RECIPES_ENV, str(REPO_RECIPES))
    wanted = {("DELETE", "ecs", "/v1/{}/cloudservers/{}/detachvolume/{}"),
              ("POST", "ecs", "/v1/{}/cloudservers/{}/attachvolume")}
    assert wanted <= {labwrites.shape(t) for t in labwrites.triples()}


# --------------------------------------------------------------------------- a run that passes


def test_a_run_reads_the_volume_gives_it_back_and_tears_the_reader_down(cloud, project):
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_OK, (lines, errors)
    # the volume is on the server as its system volume with the same device name
    assert cloud.volume == {"status": "in-use", "holder": SERVER} and not cloud.destroyed_volume
    assert cloud.bodies["attach server"] == {"volumeAttachment": {"volumeId": VOLUME, "device": "/dev/sda"}}
    assert cloud.order == ["bucket", "sg", "reader", "detach server", "attach reader", "detach reader",
                           "attach server", "delete reader", "delete sg"]
    body = reader_body(cloud)
    assert body["nics"] == [{"subnet_id": NET}] and body["vpcid"] == VPC and body["availability_zone"] == AZ
    assert "publicip" not in body and "eip" not in json.dumps(body).lower()
    assert body["adminPass"] == "{{random:password}}"
    rs = {r["type"]: r for r in rows(project)}
    assert rs["obs bucket"]["state"] == "kept" and rs["security group"]["state"] == "deleted"
    assert rs["ecs reader server with its system disk"]["state"] == "deleted"
    text = evidence(project)
    assert "proven back by a read" in text and "1 kept on purpose" in text
    assert any(line.startswith("kept: obs bucket") for line in lines)
    assert lab.EXIT_LEFT != code and "restore: proven back by a read" in lines


def test_the_bucket_is_kept_with_its_tags_and_never_deleted(cloud, project):
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_OK
    assert [c for c in cloud.calls if c["service"] == "obs" and c["method"] == "DELETE"] == []
    assert len(cloud.buckets) == 1 and next(iter(cloud.buckets)).startswith(PROJECT + "-lab-")


# --------------------------------------------------------------------------- the teardown gives the volume back


def test_a_failure_after_the_detach_still_gives_the_volume_back_to_the_server(cloud, project):
    cloud.reader_writes = False                 # the reader never writes its manifest: the wait runs out
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_CHECK, (lines, errors)
    assert cloud.volume == {"status": "in-use", "holder": SERVER} and not cloud.destroyed_volume
    # the restore took it from the reader and put it back before the reader was deleted
    assert cloud.order[cloud.order.index("detach server"):] == [
        "detach server", "attach reader", "detach reader", "attach server", "delete reader", "delete sg"]
    assert cloud.bodies["attach server"]["volumeAttachment"]["device"] == "/dev/sda"
    assert all(r["state"] in ("deleted", "kept") for r in rows(project))
    assert "proven back by a read" in evidence(project)


def test_a_failed_check_of_the_manifest_still_gives_the_volume_back(cloud, project):
    cloud.manifest = {"result": "failed", "reason": "the partition could not be mounted read-only"}
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_CHECK, (lines, errors)
    assert cloud.volume == {"status": "in-use", "holder": SERVER} and cloud.order[-2:] == ["delete reader", "delete sg"]
    assert cloud.order.index("attach server") < cloud.order.index("delete reader")


def test_a_failed_attach_to_the_reader_leaves_the_volume_free_and_it_goes_home(cloud, project):
    cloud.attach_reader = 409
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED
    assert cloud.volume == {"status": "in-use", "holder": SERVER}
    assert cloud.order[cloud.order.index("detach server"):] == ["detach server", "attach server", "delete reader",
                                                                 "delete sg"]


def test_a_detach_whose_answer_is_lost_is_still_given_back(cloud, project):
    cloud.lose_detach = True
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED
    assert cloud.volume == {"status": "in-use", "holder": SERVER} and "attach server" in cloud.order


def test_an_interrupt_after_the_detach_gives_the_volume_back(cloud, project):
    def stop(req):
        if req["method"] == "POST" and req["path"].endswith("/attachvolume") and SERVER not in req["path"] \
                and cloud.order.count("attach reader") == 0 and cloud.volume["status"] == "available":
            raise KeyboardInterrupt
    cloud.on_call = stop
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED
    assert cloud.volume == {"status": "in-use", "holder": SERVER} and not cloud.destroyed_volume


def test_a_volume_that_cannot_be_given_back_keeps_the_reader_and_exits_4(cloud, project):
    cloud.attach_server = 500                   # the server refuses its volume, every time
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_LEFT, (lines, errors)
    assert not cloud.destroyed_volume
    # the reader is not deleted: no delete call at all of a server, so the volume is never touched by one
    assert [c for c in cloud.calls if c["path"].endswith("/cloudservers/delete")] == []
    rs = {r["type"]: r for r in rows(project)}
    assert rs["ecs reader server with its system disk"]["state"] == "live"
    assert "NOT DELETED" in rs["ecs reader server with its system disk"]["note"]
    assert any("NOT BACK" in e for e in errors)
    assert "NOT BACK" in evidence(project)


def test_the_restore_does_nothing_when_the_detach_was_never_reached(cloud, project):
    cloud.status = "SHUTOFF"
    cloud.attach_reader = 409
    cloud.on_call = lambda req: (_ for _ in ()).throw(KeyboardInterrupt) \
        if req["path"].endswith("/os-interface") else None
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED
    assert "detach server" not in cloud.order and "attach server" not in cloud.order
    assert [c for c in cloud.calls if "volumes" in c["path"] and c["service"] == "evs"] == []
    assert "not needed, the step detach was not reached" in evidence(project)


# --------------------------------------------------------------------------- refused before any write


@pytest.mark.parametrize("status", ["ACTIVE", "BUILD", "REBOOT", "SHUTOFF "])
def test_a_server_that_is_not_stopped_is_refused_before_anything_is_made(cloud, project, status):
    cloud.status = status
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED
    assert writes(cloud) == [] and cloud.order == [] and rows(project) == []
    assert any("does not hold" in e and "is stopped (SHUTOFF)" in e and "so no create was tried" in e
               for e in errors), errors
    assert lines == []


def test_a_server_in_another_az_is_refused_before_anything_is_made(cloud, project):
    cloud.az = "eu-de-02"
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_STOPPED and writes(cloud) == [] and cloud.order == []
    assert any("does not hold" in e and "in the AZ of the run" in e for e in errors), errors


def test_an_unknown_server_is_refused_before_anything_is_made(cloud, project):
    code, lines, errors = go(cloud, ["server_id=%08x-0000-4000-8000-%012x" % (0xdeadbeef, 1)])
    cloud_calls = [c for c in cloud.calls if c["method"] != "GET"]
    assert code == lab.EXIT_STOPPED and cloud_calls == []


@pytest.mark.parametrize("given", ["server_id=web-1", "az=eu-de-09", "region=us-east", "files=../x",
                                   "files=/etc/passwd", "files=a//b", "files=Windows/Panther/", "files=a/./b",
                                   "flavor=p2.large.2", "flavor=s3.4xlarge.4", "agency=a b"])
def test_a_value_outside_its_pattern_is_refused_before_any_call(cloud, project, given):
    code, lines, errors = go(cloud, [given])
    assert code == lab.EXIT_STOPPED and cloud.calls == [] and errors


# --------------------------------------------------------------------------- the user_data


REFS = ("{{secret:obs-ak}}", "{{secret:obs-sk}}")


def script_text(fake) -> str:
    """The user data the run asked for: the run names the script and its parameters (user_data_script), the key
    service renders the text, fills the references and makes the base64 user_data of it."""
    from awb.tcp import keys

    server = reader_body(fake)
    assert "user_data" not in server and "user_data_text" not in server and "agency_name" not in server
    spec = server["user_data_script"]
    assert spec["name"] == "diskreader" and set(spec["params"]) == set(diskreader.PARAMS)
    return keys.script_text(spec, PROJECT, "eu-de")


def test_the_user_data_carries_no_secret_and_nothing_from_a_public_bucket(cloud, project):
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_OK
    text = script_text(cloud)
    # the text names the two reserved secrets once each and holds no key; the key service fills them
    assert [text.count(r) for r in REFS] == [1, 1]
    bare = text
    for ref in REFS:
        bare = bare.replace(ref, "")
    for detector in ("secret", "token", "private-key"):
        assert not gate.DETECTORS[detector](bare), detector
    assert "{{" not in bare and "adminPass" not in text and "password" not in text.lower()
    # no address at all: no metadata service, no public bucket; the bucket and region come from the run
    assert re.findall(r"https?://[^\s\"')]+", text) == []
    assert "169.254" not in text and "securitykey" not in text and "x-obs-security-token" not in text
    assert "github" not in text and "s3.amazonaws" not in text
    cfg = json.loads(re.search(r"CFG = json\.loads\((.*)\)\n", text).group(1).encode().decode("unicode_escape")
                     if False else json.loads(re.search(r"CFG = json\.loads\((.*)\)\n", text).group(1)))
    assert cfg["bucket"] == next(iter(cloud.buckets)) and cfg["region"] == "eu-de" and cfg["volume"] == VOLUME
    assert cfg["files"][:len(diskreader.FIXED)] == list(diskreader.FIXED)
    compile(text, "reader", "exec")
    # no plain text of a key anywhere in what the run sent or wrote: the evidence and the output carry no script
    sent = json.dumps(cloud.calls)
    assert "securitytoken" not in sent and "BEGIN" not in sent
    shown = evidence(project) + "\n".join(lines)
    assert "obs-ak" not in shown and "obs-sk" not in shown and "AWB-READER" not in shown and "CFG" not in shown


def test_the_user_data_carries_the_files_of_the_parameter(cloud, project):
    code, lines, errors = go(cloud, ["files=Windows/Panther/setupact.log:Windows/Temp/x.log"])
    assert code == lab.EXIT_OK
    text = script_text(cloud)
    cfg = json.loads(json.loads(re.search(r"CFG = json\.loads\((.*)\)\n", text).group(1)))
    assert cfg["files"][-2:] == ["Windows/Panther/setupact.log", "Windows/Temp/x.log"]


def test_the_files_parameter_pattern_and_the_reader_take_the_same_paths():
    pattern = recipe()["params"]["files"]["pattern"]
    good = ["", "Windows/Panther/setupact.log", "a:b", "Windows/Temp/x.log", "a.b/c-d_e"]
    bad = ["../x", "/etc/x", "a//b", "a/", ":a", "a::b", "a/./b", ".", "a/../b", "a b", "a*", "a;b", "a\nb"]
    for v in good:
        assert re.fullmatch(pattern, v), v
        diskreader.files_of(v)
    for v in bad:
        assert not re.fullmatch(pattern, v), v
    for v in ("../x", "/etc/x", "a//b", "a/", "a/./b", "a/../b", "a*", "a;b"):
        with pytest.raises(diskreader.ReaderError):
            diskreader.files_of(v)
    with pytest.raises(diskreader.ReaderError):
        diskreader.files_of(":".join("f%d" % i for i in range(diskreader.MAX_FILES + 1)))


@pytest.mark.parametrize("bucket, region, volume", [("Bad Name", "eu-de", VOLUME), ("tcp-ab2c-x", "us-east", VOLUME),
                                                    ("tcp-ab2c-x", "eu-de", "not-an-id"), ("", "eu-de", VOLUME)])
def test_the_script_is_not_made_from_a_value_that_is_not_one(bucket, region, volume):
    with pytest.raises(diskreader.ReaderError):
        diskreader.render(bucket, region, volume)


# --------------------------------------------------------------------------- the reader script itself


def load_script(tmp_path, monkeypatch, key=None):
    """The rendered script with its main() cut off, run as a module whose mount point is a folder of the test. With
    `key` (ak, sk) the references are filled the way the key service fills them."""
    from awb.tcp import keys

    params = {"bucket": "tcp-ab2c-lab-20261125-abcd", "region": "eu-de", "volume": VOLUME,
              "files": "Windows/Panther/setupact.log"}
    text = diskreader.from_params(params)
    if key:
        script = {"name": "diskreader", "params": params}
        body, filled = keys.with_user_data({"server": {"user_data_script": script}}, "POST", "ecs",
                                           "/v1/{project_id}/cloudservers", key)
        assert filled
        text = base64.b64decode(body["server"]["user_data"]).decode("utf-8")
    assert text.rstrip().endswith("main()")
    ns: dict = {"__name__": "reader"}
    exec(compile(text.rstrip()[:-len("main()")], "reader", "exec"), ns)
    ns["MOUNT"] = str(tmp_path / "mnt")
    return ns


def test_the_script_finds_the_files_without_regard_to_case(tmp_path, monkeypatch):
    ns = load_script(tmp_path, monkeypatch)
    root = tmp_path / "mnt"
    for rel in ("windows/inf/SetupAPI.dev.log", "windows/inf/setupapi.app.log", "windows/inf/other.log",
                "windows/system32/CONFIG/system", "windows/system32/winevt/logs/System.evtx"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    found = {Path(p).relative_to(root).as_posix() for p in ns["resolve"]("Windows/INF/setupapi*.log")}
    assert found == {"windows/inf/SetupAPI.dev.log", "windows/inf/setupapi.app.log"}
    assert [Path(p).name for p in ns["resolve"]("Windows/System32/config/SYSTEM")] == ["system"]
    assert [Path(p).name for p in ns["resolve"]("Windows/System32/winevt/Logs/System.evtx")] == ["System.evtx"]
    assert ns["resolve"]("Windows/System32/config/SOFTWARE") == []
    assert ns["resolve"]("Windows/INF") == []                    # a folder is no file
    assert ns["safe_key"]("Windows/Temp/ä x.log") == "files/Windows/Temp/__x.log"


def test_the_script_signs_a_put_the_obs_way_and_never_sends_the_secret_key(tmp_path, monkeypatch):
    import hashlib
    import hmac

    # the key service fills the two references with the key of the call; the script then signs with that key only
    ns = load_script(tmp_path, monkeypatch, {"ak": "AKINVENTED", "sk": "SKINVENTED"})
    assert ns["AK"] == "AKINVENTED" and ns["SK"] == "SKINVENTED"
    sent = {}

    class Conn:
        def __init__(self, host, timeout=0):
            sent["host"] = host
            sent["headers"], sent["body"] = {}, b""

        def putrequest(self, method, path):
            sent["method"], sent["path"] = method, path

        def putheader(self, k, v):
            sent["headers"][k] = v

        def endheaders(self):
            pass

        def send(self, block):
            sent["body"] += block

        def getresponse(self):
            return type("R", (), {"status": 200, "read": lambda self: b""})()

        def close(self):
            pass

    monkeypatch.setattr(ns["http"].client, "HTTPSConnection", Conn)
    assert ns["put"]("manifest.json", data=b"{}") == 200
    h = sent["headers"]
    assert sent["host"] == "tcp-ab2c-lab-20261125-abcd.obs.eu-de.otc.t-systems.com"
    assert sent["method"] == "PUT" and sent["path"] == "/manifest.json" and sent["body"] == b"{}"
    assert "x-obs-security-token" not in h and h["Authorization"].startswith("OBS AKINVENTED:")
    text = "PUT\n\n%s\n%s\n/tcp-ab2c-lab-20261125-abcd/manifest.json" % (h["Content-Type"], h["Date"])
    sig = base64.b64encode(hmac.new(b"SKINVENTED", text.encode(), hashlib.sha1).digest()).decode()
    assert h["Authorization"] == "OBS AKINVENTED:" + sig
    assert "SKINVENTED" not in json.dumps(sent, default=str)


# --------------------------------------------------------------------------- the engine parts


def test_the_engine_refuses_a_malformed_wait_keep_and_restore():
    def bad(change, said):
        r = copy.deepcopy(recipe())
        change(r)
        with pytest.raises(lab.LabError, match=said):
            lab.validate(r)

    def step(r, sid):
        return next(s for s in r["steps"] if s["id"] == sid)

    bad(lambda r: step(r, "manifest")["until"].update(field="x"), "until")
    bad(lambda r: step(r, "manifest").update(until={"status": []}), "list of HTTP statuses")
    bad(lambda r: step(r, "bucket").update(keep="x"), "keep is the reason")
    # a kept create has no teardown entry and any other create must have one
    bad(lambda r: r["teardown"].update(bucket=r["teardown"]["sg"]), "teardown has one entry")
    bad(lambda r: r["teardown"].pop("sg"), "teardown has one entry")
    bad(lambda r: r["restore"].update(after="nowhere"), "after names a step")
    bad(lambda r: r["restore"].update(keep=["bucket", "nowhere"]), "keep names create steps")
    bad(lambda r: r["restore"].pop("proof"), "restore holds")
    bad(lambda r: r["restore"]["read"].update(method="POST"), "the read is a GET")
    bad(lambda r: r["restore"]["actions"][0].update(when=[]), "an action has its own id")
    bad(lambda r: r["restore"]["actions"][0]["then"].update(equals=3), "a condition has a field")
    bad(lambda r: r["restore"]["actions"][1].update(call={"method": "POST", "service": "ecs",
                                                        "path": "/x/${nothing}"}), "no step gives")
    bad(lambda r: r["preflight"][0].update(value="x"), "belong to a read of a list")
    bad(lambda r: r["preflight"][0].pop("equals"), "equals is a string")


def test_a_wait_on_a_status_ends_on_the_status_and_runs_out_without_it(cloud, project):
    cloud.reader_writes = False
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_CHECK
    text = evidence(project)
    assert "| manifest-put | the reader writes manifest.json into the bucket | reached within the wait | not reached | failed |" \
        in text
    heads = [c for c in cloud.calls if c["method"] == "HEAD" and c["path"].endswith("manifest.json")]
    assert len(heads) > 5 and all(h["service"] == "obs" for h in heads)


def test_a_kept_resource_is_found_by_its_name_when_its_answer_was_lost(cloud, project):
    # a bucket whose create answer is lost is placed by the find, then kept like the others
    real = cloud.obs

    def lose(m, path):
        out = real(m, path)
        if m == "PUT" and not getattr(lose, "done", False):
            lose.done = True
            raise ConnectionResetError("no answer")
        return out
    cloud.obs = lose
    code, lines, errors = go(cloud)
    assert code == lab.EXIT_OK, (lines, errors)
    assert {r["type"]: r["state"] for r in rows(project)}["obs bucket"] == "kept"


def test_the_userdata_form_is_a_name_the_engine_knows_and_renders_from_the_run_values():
    assert lab._refs("${userdata:diskreader}") == set()
    with pytest.raises(lab.Stop):
        lab.expand("${userdata:nothing}", {})
    assert lab.expand("${userdata:diskreader}", {}, loose=True) == "<user_data of diskreader>"
    env = {"name": "tcp-ab2c-lab-20261125-abcd", "region": "eu-de", "sys_volume": VOLUME, "files": ""}
    text = base64.b64decode(lab.expand("${userdata:diskreader}", env)).decode()
    assert text.startswith("#!/usr/bin/env python3") and VOLUME in text
    with pytest.raises(lab.Stop):
        lab.expand("${userdata:diskreader}", dict(env, sys_volume=""))
