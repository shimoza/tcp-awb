"""A deleted image is no leftover of a lab run (tcp-bytg ask 5, 2026-11-21): `awb lab run status` listed an image
as live twice although it was deleted with 204. The upload of its data was recorded as a second create and the
delete by another API version matched neither. Every id here is invented."""
from __future__ import annotations

from awb.tcp import labadmin
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import fake, lab_call, run_op, session, svc  # noqa: F401


def _image(svc, uid, run):
    a = lab_call(svc, uid, "POST", "ims", "/v2/images", run, body={"name": "lr-image", "disk_format": "vhd"})
    assert a["ok"] and a["created"] == ["ims images"], a
    return a["data"]["image"]["id"], a["data"]["image"]["name"]


def _left(svc, uid):
    return [(c["label"], c.get("name")) for r in run_op(svc, uid, "status")["runs"] for c in r.get("left") or []]


def test_bytg_the_upload_of_an_image_is_no_second_create(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    rid, name = _image(svc, uid, run)
    for path in ("/v2/images/%s/file" % rid, "/v1/cloudimages/%s/upload" % rid, "/v2/images/%s/tags/lab" % rid):
        a = lab_call(svc, uid, "PUT", "ims", path, run, body={"x": 1})
        assert a["ok"] and a["created"] == [], (path, a)
    assert _left(svc, uid) == [("ims images", name)]
    assert labadmin.creates("PUT", "ims", "/v2/images/%s/file" % rid, {}, 204, None, "eu-de", absent=True) == []


def test_bytg_an_image_deleted_by_another_api_version_is_removed_with_what_lives_in_it(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    rid, _name = _image(svc, uid, run)
    # an entry recorded below the image before this fix, as the record of run 6bq3 holds it
    p = svc.paths_fn()
    record = labadmin.open_run(p, "tcp-ab2c", "test-10491")
    record["created"].append({"n": 2, "label": "ims images", "name": "file", "state": "live", "at": "x",
                              "delete": {"method": "DELETE", "service": "ims", "path": "/v2/images/%s/file" % rid,
                                         "query": {}, "region": "eu-de"}})
    labadmin.save(p, record)
    assert len(_left(svc, uid)) == 2
    fake.store["/v1.1/images/%s" % rid] = {}
    gone = lab_call(svc, uid, "DELETE", "ims", "/v1.1/images/%s" % rid, run)
    assert gone["status"] == 204
    assert _left(svc, uid) == []
    fake.deleted.clear()
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and ended["failed"] == [] and fake.deleted == [], ended


def test_bytg_a_delete_in_a_later_run_ends_the_leftover_of_an_earlier_one(session, fake):
    svc, uid = session
    first = run_op(svc, uid, "start")["run"]
    rid, name = _image(svc, uid, first)
    fake.fail.add("/v2/images/%s" % rid)
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and len(ended["failed"]) == 1
    assert _left(svc, uid) == [("ims images", name)]
    fake.fail.clear()
    second = run_op(svc, uid, "start")["run"]
    assert lab_call(svc, uid, "DELETE", "ims", "/v2/images/%s" % rid, second)["status"] == 204
    assert _left(svc, uid) == []
    old = next(r for r in labadmin.load_runs(svc.paths_fn()) if r["id"] == first)
    assert old["created"][0]["state"] == "removed" and old["created"][0]["removed_in"] == second


def test_bytg_the_same_id_under_another_kind_or_an_object_of_a_bucket_is_not_matched():
    entry = {"delete": {"method": "DELETE", "service": "ims", "path": "/v2/images/" + "a" * 32}}
    assert labadmin.matches_delete(entry, "DELETE", "ims", "/v1.1/images/" + "a" * 32, {}, None)
    assert not labadmin.matches_delete(entry, "DELETE", "ims", "/v2/members/" + "a" * 32, {}, None)
    assert not labadmin.matches_delete(entry, "DELETE", "evs", "/v1.1/images/" + "a" * 32, {}, None)
    named = {"delete": {"method": "DELETE", "service": "iam", "path": "/v3/groups/lr-group"}}
    assert not labadmin.matches_delete(named, "DELETE", "iam", "/v3.0/groups/lr-group", {}, None)
    obj = {"delete": {"method": "DELETE", "service": "obs", "path": "/lr-bucket/key"}}
    assert not labadmin.matches_delete(obj, "DELETE", "obs", "/lr-bucket", {}, None)
