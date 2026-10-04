"""The materials service (awb/tcp/web/materials_api.py): copy-only imports from the buckets, the original stays, the
chosen version or nothing, customer files only through the intake, answers that fit the contract.

Ported from the tests the web side wrote for the deployed service. The projects use the kinds of the repository;
the contract checks are new.
"""
import dataclasses
import hashlib
import os
import threading
from pathlib import Path

import pytest

from awb import obs, projects
from awb.tcp.web import contract
from awb.tcp.web import materials_api
from awb.tcp.web.create_api import ProjectStore
from awb.tcp.web.materials_api import Handler, Problem, ReadOBS, Server, Store, local_call
from tests import fixtures
from tests.obs_fake import FakeOBS


@pytest.fixture
def materials(home, tmp_path):
    pr = projects.spawn(home, "query", "Compare storage options", None, home.register)
    private = projects.spawn(home, "query", "Compare network options", fixtures.CUSTOMER_CODE, home.register)
    with FakeOBS("test-import", "AKFAKE", "sk-fake") as fake:
        c = obs.Client(fake.bucket, obs.Keys("AKFAKE", "sk-fake"), endpoint=fake.endpoint)

        class Sources:
            def client(self, source):
                return ReadOBS(c)

            def prefixes(self, project, source):
                return ["inbox/" if source == "brief" else project.code + "/in/"]
        writer = ProjectStore(home, tmp_path / "writer")

        def publish(row, text):
            return writer.publish_material(row["project"], {"id": row["id"], "customer": row["customer"], "text": text,
                                                            "sha256": hashlib.sha256(text.encode()).hexdigest()})
        store = Store(home, tmp_path / "materials", Sources(), publish)
        yield store, c, pr, private


def import_one(s, c, p, source="brief", text="Compare storage options.", name="task.md", rid="a" * 32):
    key = ("inbox/" if source == "brief" else p.code + "/in/") + name
    c.put_bytes(key, text.encode())
    listing = s.browse(p.code, source)
    f = next(x for x in listing["files"] if x["name"] == name)
    result = s.queue(p.code, {"request_id": rid, "files": [{"id": f["id"], "etag": f["etag"]}]})
    ident = result["imports"][0]
    s.process(ident)
    return ident, key, f


def test_public_copy_to_actual_project_and_no_delete(materials):
    s, c, p, _ = materials
    ident, key, f = import_one(s, c, p)
    rows = s.history(p.code)["items"]
    assert rows[0]["state"] == "ready", rows
    assert c.head(key) is not None
    text = s.text(p.code, ident)
    assert text["text"] == "Compare storage options."
    assert (Path(p.path) / "input" / text["filename"]).read_text() == text["text"]
    assert s.browse(p.code, "brief")["files"][0]["status"] == "imported"
    r = s.queue(p.code, {"request_id": "b" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})
    assert r["imports"] == [ident] and len(s.history(p.code)["items"]) == 1


def test_customer_uses_real_intake_only_approved_copies(materials):
    s, c, _, p = materials
    ident, key, _ = import_one(s, c, p, "customer", "Requirements for " + fixtures.CUSTOMER_FORMS[0] + ".")
    row = s.history(p.code)["items"][0]
    assert row["state"] == "ready", row
    out = s.text(p.code, ident)["text"]
    assert fixtures.CUSTOMER_FORMS[0] not in out and fixtures.CUSTOMER_CODE in out
    assert c.head(key) is not None
    assert (Path(p.path) / "input" / row["filename"]).read_text() == out


def test_public_private_content_held_and_never_published(materials):
    s, c, p, _ = materials
    ident, key, _ = import_one(s, c, p, text=fixtures.CUSTOMER_FORMS[0])
    assert s.history(p.code)["items"][0]["state"] == "held"
    assert c.head(key) is not None
    assert not (Path(p.path) / "input" / (ident + ".md")).exists()
    with pytest.raises(Problem):
        s.text(p.code, ident)


def test_unknown_customer_name_requires_review(materials):
    s, c, _, p = materials
    ident, key, _ = import_one(s, c, p, "customer", fixtures.PLANTED_CANDIDATE)
    assert s.history(p.code)["items"][0]["state"] == "held"
    assert not (Path(p.path) / "input" / (ident + ".md")).exists()
    assert c.head(key) is not None


def test_versions_preserved_and_changed_object_rejected(materials):
    s, c, p, _ = materials
    first, key, f = import_one(s, c, p)
    c.put_bytes(key, b"Compare backup options.")
    newer = s.browse(p.code, "brief")["files"][0]
    assert newer["status"] == "updated"
    second = s.queue(p.code, {"request_id": "b" * 32, "files": [{"id": newer["id"], "etag": newer["etag"]}]})
    second = second["imports"][0]
    s.process(second)
    assert s.text(p.code, first)["version"] == 1 and s.text(p.code, second)["version"] == 2
    c.put_bytes(key, b"Compare network options.")
    f = s.browse(p.code, "brief")["files"][0]
    third = s.queue(p.code, {"request_id": "c" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})["imports"][0]
    c.put_bytes(key, b"Changed after selection.")
    s.process(third)
    assert s.history(p.code)["items"][0]["state"] == "failed"
    assert not (Path(p.path) / "input" / (third + ".md")).exists()


def test_cross_project_selection_and_material_read_refused(materials):
    s, c, p, other = materials
    ident, _, f = import_one(s, c, p)
    with pytest.raises(Problem):
        s.text(other.code, ident)
    with pytest.raises(Problem):
        s.queue(other.code, {"request_id": "b" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})
    with pytest.raises(Problem):
        s.browse(p.code, "customer")


def test_repeated_request_and_payload_conflict(materials):
    s, c, p, _ = materials
    ident, _, f = import_one(s, c, p)
    d = {"request_id": "a" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]}
    assert s.queue(p.code, d) == {"imports": [ident]}
    d["files"][0]["etag"] = "different"
    with pytest.raises(Problem):
        s.queue(p.code, d)


def test_failed_publication_reuses_working_copy(materials):
    s, c, p, _ = materials
    publisher = s.publisher

    def lose_response(row, text):
        publisher(row, text)
        raise OSError("interrupted response")
    s.publisher = lose_response
    ident, _, f = import_one(s, c, p)
    assert s.history(p.code)["items"][0]["state"] == "failed"
    s.publisher = publisher
    retry = s.queue(p.code, {"request_id": "b" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})["imports"][0]
    assert retry == ident
    s.process(retry)
    assert s.history(p.code)["items"][0]["state"] == "ready"
    assert len(list((Path(p.path) / "input").glob("M-*.md"))) == 1


def test_closed_project_and_changed_customer_refused(materials, home):
    s, c, p, private = materials
    c.put_bytes("inbox/task.md", b"Compare options.")
    f = s.browse(p.code, "brief")["files"][0]
    ident = s.queue(p.code, {"request_id": "a" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})["imports"][0]
    projects.close(home, p.code)
    s.process(ident)
    assert s.history(p.code)["items"][0]["state"] == "failed"
    with pytest.raises(Problem):
        s.browse(p.code, "brief")


def test_writer_rejects_symlink_and_mismatched_content(materials, tmp_path):
    s, c, p, _ = materials
    ident, _, _ = import_one(s, c, p)
    path = Path(p.path) / "input" / (ident + ".md")
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    path.symlink_to(outside)
    with pytest.raises(Exception):
        s.publisher({"project": p.code, "id": ident, "customer": "none"}, "Compare storage options.")
    assert outside.read_text() == "preserve"


def test_tampered_stage_not_sent_to_chat(materials):
    s, c, p, _ = materials
    ident, _, _ = import_one(s, c, p)
    (s.root / (ident + ".md")).write_text("tampered")
    with pytest.raises(Problem):
        s.text(p.code, ident)


def test_seal_failure_never_releases_customer_content(materials, monkeypatch):
    s, c, _, p = materials
    original = materials_api.intake.run

    def unsealed(*a, **kw):
        return dataclasses.replace(original(*a, **kw), unsealed=1)
    monkeypatch.setattr(materials_api.intake, "run", unsealed)
    ident, _, _ = import_one(s, c, p, "customer", "Requirements for " + fixtures.CUSTOMER_FORMS[0] + ".")
    assert s.history(p.code)["items"][0]["state"] == "held"
    assert not (Path(p.path) / "input" / (ident + ".md")).exists()


def test_large_and_unsupported_files_cannot_queue(materials):
    s, c, p, _ = materials
    c.put_bytes("inbox/archive.zip", b"not an archive")
    f = s.browse(p.code, "brief")["files"][0]
    assert not f["supported"]
    with pytest.raises(Problem):
        s.queue(p.code, {"request_id": "a" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})


def test_service_peer_boundary(materials, tmp_path):
    s, c, p, _ = materials
    ident, _, _ = import_one(s, c, p)
    server = Server(str(tmp_path / "inputs.sock"), Handler)
    server.store = s
    server.web_uid = os.getuid() + 1000
    server.read_uids = {os.getuid()}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(Problem) as e:
            local_call(str(tmp_path / "inputs.sock"), "/api/projects/" + p.code + "/materials")
        assert e.value.status == 403
        with pytest.raises(Problem):
            local_call(str(tmp_path / "inputs.sock"), "/api/projects/" + p.code + "/materials/sources?source=customer")
        d = local_call(str(tmp_path / "inputs.sock"), "/internal/projects/" + p.code + "/materials/" + ident)
        assert "name" not in d and "object_key" not in d and d["text"] == "Compare storage options."
        server.read_uids = set()
        with pytest.raises(Problem):
            local_call(str(tmp_path / "inputs.sock"), "/internal/projects/" + p.code + "/materials/" + ident)
    finally:
        server.shutdown()
        server.server_close()


def test_same_content_new_etag_is_not_repeatedly_reimported(materials):
    s, c, p, _ = materials
    first, key, f = import_one(s, c, p)
    with s.db() as db:
        db.execute("UPDATE imports SET etag=? WHERE id=?", ("old-object-version", first))
    f = s.browse(p.code, "brief")["files"][0]
    assert f["status"] == "updated"
    selection = [{"id": f["id"], "etag": f["etag"]}]
    second = s.queue(p.code, {"request_id": "b" * 32, "files": selection})["imports"][0]
    s.process(second)
    assert s.history(p.code)["items"][0]["state"] == "duplicate"
    assert s.browse(p.code, "brief")["files"][0]["status"] == "imported"
    assert s.queue(p.code, {"request_id": "c" * 32, "files": selection})["imports"] == [second]
    assert len(s.history(p.code)["items"]) == 2
    assert len(list((Path(p.path) / "input").glob("M-*.md"))) == 1


def test_customer_binding_change_cannot_release_old_inputs(materials, home):
    s, c, p, _ = materials
    ident, _, _ = import_one(s, c, p)
    rows = projects.load(home)
    projects._save(home, [dataclasses.replace(r, customer=fixtures.CUSTOMER_CODE) if r.code == p.code else r
                          for r in rows])
    with pytest.raises(Problem) as e:
        s.text(p.code, ident)
    assert e.value.status == 409


def test_the_worker_runs_each_import_as_a_module_of_the_package(materials, monkeypatch):
    s, c, p, _ = materials
    c.put_bytes("inbox/task.md", b"Compare options.")
    f = s.browse(p.code, "brief")["files"][0]
    ident = s.queue(p.code, {"request_id": "a" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]})["imports"][0]
    seen = []

    class Done(Exception):
        pass

    def run(argv, **kwargs):
        seen.append(argv)
        raise Done()
    monkeypatch.setattr(materials_api.subprocess, "run", run)
    monkeypatch.setattr(materials_api.time, "sleep", lambda _: (_ for _ in ()).throw(Done()))
    with pytest.raises(Done):
        s.worker()
    assert seen and seen[0][1:4] == ["-m", "awb.tcp.web.materials_api", "--state"] and seen[0][-1] == ident


def test_the_answers_fit_the_contract(materials):
    doc = contract.build()

    def fits(value, schema):
        assert contract.validate(doc, value, contract.ref(schema)) == []

    s, c, p, private = materials
    ident, key, f = import_one(s, c, p)
    fits(s.history(p.code), "MaterialHistory")
    fits(s.browse(p.code, "brief"), "MaterialSources")
    fits(s.queue(p.code, {"request_id": "b" * 32, "files": [{"id": f["id"], "etag": f["etag"]}]}), "ImportAccepted")
    fits(s.text(p.code, ident), "MaterialText")
    held, _, _ = import_one(s, c, p, text=fixtures.CUSTOMER_FORMS[0], name="held.md", rid="c" * 32)
    fits(s.history(p.code), "MaterialHistory")
    assert s.history(p.code)["items"][0]["message"] in contract.IMPORT_MESSAGES
