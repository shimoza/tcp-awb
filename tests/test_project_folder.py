"""One identifier per project (2026-10-09): the project code is the key of its material.

Spawn makes the project's folder `<YYYY-MM>/<code>/` with in/ and out/ in the owner bucket through the key service
(op owner_folder) and never stops on the bucket; the console's creation runs the same spawn; the owner page puts a
file into in/ under an id (op web_put_in); the session's take runs the intake on the owner side, from in/ alone,
for a project with or without a customer. Two stand-in buckets play the lab and the owner bucket; every name is an
invented fixture name.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

from awb import cli, jobs, projects
from awb.tcp import keys
from awb.tcp.web import gateway
from awb.tcp.web.create_api import ProjectStore
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_web_create_api import project_payload
from tests.test_web_materials_api import materials  # noqa: F401  (the fixture of the materials service)
from tests.test_web_owner_api import OWNER, DOMAIN, Site, owner_headers

AK, SK = "AKLABFAKE", "sk-lab-fake-secret"
GOAL = "move two app clusters to managed k8s"
MONTH = date.today().isoformat()[:7]


@contextlib.contextmanager
def short_dir():
    d = tempfile.mkdtemp(prefix="awbf", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    from awb.tcp import throttle
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)


@pytest.fixture
def buckets():
    with FakeOBS("awb-lab-eu-de", AK, SK) as lab, FakeOBS("awb", AK, SK) as own:
        yield lab, own


@pytest.fixture
def svc(home, buckets, tmp_path, monkeypatch):
    lab, own = buckets
    notes: list = []
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={lab.bucket: lab.endpoint, own.bucket: own.endpoint})
        s._notify = lambda subject, message: notes.append((subject, message))
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, ["test-1/lab/ak", "test-1/lab/sk"], reader={"test-1/lab/ak": AK,
                    "test-1/lab/sk": SK}.get, settings={"bucket_tenant": "test-1", "lab_bucket": lab.bucket,
                                                         "owner_bucket": own.bucket, "max_mb": "1"})
        s.notes = notes
        try:
            yield s
        finally:
            s.stop()


def folder_keys(own, code):
    base = "%s/%s/" % (MONTH, code)
    return [k for k in (base, base + "in/", base + "out/") if k in own.objects]


# --------------------------------------------------------------------------- item 1: the folder at spawn


def test_spawn_makes_the_bucket_folder_through_the_key_service(svc, buckets, home, register_path):
    _, own = buckets
    pr = projects.spawn(home, "query", GOAL, None, register_path)
    assert len(folder_keys(own, pr.code)) == 3
    assert pr.folder_note == "bucket folder %s/%s/ with in/ and out/" % (MONTH, pr.code)
    # a second ask finds it and changes nothing
    assert projects.bucket_folder(pr.code).endswith("(there already) with in/ and out/")


def test_spawn_refuses_nothing_when_the_bucket_is_unreachable(home, register_path, capsys):
    """The key socket of the tests leads nowhere: the project is made, the line says who makes the folder later."""
    code = cli.main(["spawn", "query", "--goal", GOAL])
    out = capsys.readouterr().out
    pr = projects.load(home)[0]
    assert code == 0 and pr.state == "active"
    assert ("bucket folder not made (key service not reached); the first take or awb bucket folder %s makes it" % pr.code
            in out)


def test_spawn_with_a_locked_key_service_says_locked_and_goes_on(svc, buckets, home, register_path):
    _, own = buckets
    keys.request(svc.admin_path, {"op": "lock"})
    pr = projects.spawn(home, "query", GOAL, None, register_path)
    assert pr.folder_note.startswith("bucket folder not made (locked)")
    assert folder_keys(own, pr.code) == []
    assert projects.load(home)[0].code == pr.code


def test_the_folder_op_makes_no_folder_for_a_code_that_is_no_active_project(svc, buckets, home):
    _, own = buckets
    answer = keys.request(svc.call_path, {"op": "owner_folder", "project": "tcp-q7m4"})
    assert not answer["ok"] and own.objects == {}
    answer = keys.request(svc.call_path, {"op": "owner_folder", "project": "../x"})
    assert not answer["ok"] and own.objects == {}


def test_the_console_creation_runs_the_same_spawn_and_the_folder_appears(svc, buckets, home, tmp_path):
    _, own = buckets
    result = ProjectStore(home, tmp_path / "state").create(project_payload())
    assert "warning" not in result
    assert len(folder_keys(own, result["code"])) == 3


def test_the_console_creation_warns_when_the_folder_waits(home, tmp_path):
    result = ProjectStore(home, tmp_path / "state").create(project_payload())
    assert result["created"] and result["warning"] == ("Project created. Its bucket folder is made later, by the "
                                                       "first take.")


# --------------------------------------------------------------------------- item 3: the owner page puts into in/


def test_the_owner_page_upload_lands_in_the_project_folder_under_an_id(materials, tmp_path):
    store, buckets, pr, private = materials
    f = tmp_path / "planted brief of Northwind.md"
    f.write_text("The workloads move to eu-de.\n")
    for project in (pr, private):
        answer = store.upload(project.code, ".md", f)
        assert answer["folder"] == "in/" and answer["extension"] == ".md" and answer["size"] == f.stat().st_size
        assert answer["message"] == "The file is in the project folder in/. The session takes it with: take the " \
                                    "files from in"
        key = "%s/%s/in/%s" % (project.created[:7], project.code, answer["id"])
        assert buckets.own.head(key) is not None
        assert "Northwind" not in json.dumps(answer) and "Northwind" not in key
    # nothing was imported: the project's input/ stays as spawn made it
    assert sorted(p.name for p in (Path(pr.path) / "input").iterdir()) == [".gitkeep"]


def test_the_owner_page_upload_refuses_an_unknown_type_and_an_empty_file(materials, tmp_path):
    from awb.tcp.web.materials_api import Problem

    store, _, pr, _ = materials
    f = tmp_path / "x.exe"
    f.write_bytes(b"MZ")
    with pytest.raises(Problem) as e:
        store.upload(pr.code, ".exe", f)
    assert e.value.status == 413
    empty = tmp_path / "e.md"
    empty.write_bytes(b"")
    with pytest.raises(Problem) as e:
        store.upload(pr.code, ".md", empty)
    assert e.value.status == 413


def test_the_key_service_takes_an_upload_from_its_own_user_alone(materials, tmp_path, monkeypatch):
    store, buckets, pr, _ = materials
    f = tmp_path / "a.md"
    f.write_text("text\n")
    monkeypatch.setattr(keys.os, "getuid", lambda: 0)          # the peer is now another user than the service
    answer = keys.request(store.key_service.call_path, {"op": "web_put_in", "project": pr.code,
                                                         "extension": ".md"}, upload=f)
    assert answer == {"ok": False, "kind": "refused", "error": "refused"}
    own = store.fakes[1]
    assert not [k for k in own.objects if "/in/" in k and not k.endswith("/in/")]


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def test_the_gateway_forwards_an_upload_on_the_owner_host_alone_with_its_extension(site):
    route = "/api/projects/tcp-q7m4/materials/upload"
    token = site.owner_session()
    h = owner_headers(site, token, post=True)
    h.update({"Content-Type": "application/octet-stream", "X-AWB-Extension": ".PDF"})
    data = b"%PDF-1.4 " + b"x" * 40000                     # larger than the JSON limit of 16384 bytes
    status, _, _ = site.request(route, "POST", OWNER, h, data)
    assert status == 200
    method, path, headers, body = site.unix["materials"].calls[-1]
    assert (method, path, body) == ("POST", route, data)
    assert headers["Content-Type"] == "application/octet-stream" and headers["X-AWB-Extension"] == ".pdf"
    # the upload takes bytes alone, and the main host never forwards it
    assert site.request(route, "POST", OWNER, dict(h, **{"Content-Type": "application/json"}), data)[0] == 415
    reader = {"Cookie": "__Host-awb-session=" + site.reader_session(), "Origin": "https://" + DOMAIN,
              "Content-Type": "application/octet-stream", "X-AWB-Extension": ".pdf"}
    assert site.request(route, "POST", DOMAIN, reader, data)[:3:2] == (404, b"Not found.\n")
    assert len(site.unix["materials"].calls) == 1


def test_the_gateway_refuses_an_upload_over_25_mb_before_reading_it(site):
    route = "/api/projects/tcp-q7m4/materials/upload"
    h = owner_headers(site, site.owner_session(), post=True)
    h.update({"Content-Type": "application/octet-stream", "X-AWB-Extension": ".pdf",
              "Content-Length": str(gateway.MAX_UPLOAD + 1)})
    assert site.request(route, "POST", OWNER, h, b"")[0] == 413
    assert site.unix["materials"].calls == []


# --------------------------------------------------------------------------- item 2: the session takes from in/


def _put_in(svc, code, tmp_path, text):
    f = tmp_path / "upload.txt"
    f.write_text(text, encoding="utf-8")
    answer = keys.request(svc.call_path, {"op": "web_put_in", "project": code, "extension": ".txt"}, upload=f)
    assert answer["ok"] and answer["id"].startswith("upload-") and answer["id"].endswith(".txt"), answer
    return answer["id"]


@pytest.mark.parametrize("customer", [None, fx.CUSTOMER_CODE])
def test_a_console_upload_is_taken_with_take_the_files_from_in(svc, buckets, home, register_path, tmp_path,
                                                                monkeypatch, capsys, customer):
    """The owner page puts a brief into in/; the session's "take the files from in" runs the intake on the owner
    side: a planted person and a planted company become tokens, the customer form (when there is a customer) its
    code, a project without a customer gets no code and the session never sees a name."""
    pr = projects.spawn(home, "query", GOAL, customer, register_path)
    monkeypatch.chdir(pr.path)
    text = "Kick-off mit Herrn %s, das Angebot der %s liegt vor.%s\n" % (
        fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE, (" Kunde ist %s." % fx.CUSTOMER_FORMS[0]) if customer else "")
    _put_in(svc, pr.code, tmp_path, text)
    code = cli.main(["inbox", "take", "take", "the", "files", "from", "in"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "taken from the project's folder in/ through the intake" in out
    (copy,) = [f for f in (Path(pr.path) / "input").iterdir() if f.name != ".gitkeep"]
    got = copy.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(got + out + err, "what the session got from in/")
    assert "[person 1]" in got and "[company 1]" in got
    assert (fx.CUSTOMER_CODE in got) == bool(customer) and ("CUST-" in got) == bool(customer)
    assert cli.main(["inbox", "take", "take", "the", "files", "from", "in"]) == 1, "taken once"
    assert "holds no new file" in capsys.readouterr().err


def test_the_work_rules_name_in_as_the_place_of_material_and_the_brief_he_pastes():
    """2026-10-09: material lies in the project's in/, no customer is normal, a text he wrote and pastes is his
    brief, the hold line is for an unreadable file of in/ alone, the inbox/CUST-XXXX/ sentence is gone."""
    rules = (Path(projects.RULES_FILE)).read_text(encoding="utf-8")
    flat = " ".join(rules.split())
    assert "Material lies in the project's `in/`" in flat
    assert "No customer is normal" in flat
    assert "A text he wrote and pastes is his brief" in flat
    assert "An unreadable file of `in/` alone is one line" in flat
    assert "take the files from in" in flat or '"from in"' in flat
    assert "inbox/CUST" not in rules
    for f in ("README.md", "COMMANDS.md"):
        assert "inbox/CUST-XXXX/" not in (Path(projects.REPO) / f).read_text(encoding="utf-8"), f
