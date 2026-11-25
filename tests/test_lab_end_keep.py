"""Ask 2026-11-21-tcp-bytg-12: `awb lab run end --keep NAME` ends the run and leaves the named OBS buckets, which
go from the run's record into the kept list of the project; `awb lab kept drop NAME` deletes one later with the
proof of a run end. A name of any other kind is refused before a delete. Every name is invented."""
from __future__ import annotations

from awb import labruns
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import LAB, PROJECT, fake, lab_call, run_op, session, svc  # noqa: F401


def _made(svc, uid, bucket):
    run = run_op(svc, uid, "start")["run"]
    assert lab_call(svc, uid, "PUT", "obs", "/%s" % bucket, run)["created"] == ["obs bucket"]
    vpc = lab_call(svc, uid, "POST", "vpc", "/v1/{project_id}/vpcs", run, body={"vpc": {"name": "kp"}})
    assert vpc["created"] == ["vpc vpcs"]
    return run


def test_end_keep_leaves_the_bucket_and_deletes_the_rest(session, fake):
    svc, uid = session
    bucket = "%s-tools" % PROJECT
    _made(svc, uid, bucket)
    ended = run_op(svc, uid, "end", keep=[bucket])
    assert ended["ok"], ended
    assert [c["label"] for c in ended["removed"]] == ["vpc vpcs"] and ended["failed"] == []
    assert [c["id"] for c in ended["kept"]] == [bucket]
    assert "/%s" % bucket not in fake.deleted and len(fake.deleted) == 1
    kept = labruns.load_kept(svc.paths_fn(), PROJECT)
    assert [(k["tenant"], k["kind"], k["name"]) for k in kept] == [(LAB, "obs bucket", bucket)]
    assert kept[0]["region"] and kept[0]["run"] == ended["run"] and kept[0]["date"]
    assert all(bucket not in str(c.get("delete")) for r in labruns.load_runs(svc.paths_fn()) for c in r["created"])
    status = run_op(svc, uid, "status")
    assert [k["name"] for k in status["kept_list"]] == [bucket]


def test_a_name_of_another_kind_refuses_before_any_delete(session, fake):
    svc, uid = session
    bucket = "%s-tools" % PROJECT
    _made(svc, uid, bucket)
    for name in ("vpcs", "%s-nothing" % PROJECT):
        refused = run_op(svc, uid, "end", keep=[bucket, name])
        assert not refused["ok"] and "OBS bucket" in refused["error"], refused
    assert fake.deleted == [] and labruns.load_kept(svc.paths_fn(), PROJECT) == []
    assert len(labruns.leftovers(labruns.load_runs(svc.paths_fn())[0])) == 2


def test_kept_drop_deletes_the_bucket_and_proves_it_gone(session, fake):
    svc, uid = session
    bucket = "%s-tools" % PROJECT
    _made(svc, uid, bucket)
    run_op(svc, uid, "end", keep=[bucket])
    dropped = run_op(svc, uid, "drop", name=bucket)
    assert dropped["ok"] and dropped["dropped"] is True, dropped
    assert fake.deleted[-1] == "/%s" % bucket
    assert labruns.load_kept(svc.paths_fn(), PROJECT) == []
    again = run_op(svc, uid, "drop", name=bucket)
    assert not again["ok"] and "no kept bucket" in again["error"]
