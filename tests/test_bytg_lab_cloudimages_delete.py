"""tcp-bytg 2026-11-22-7: in run 6bq3 two entries recorded as `ims cloudimages` (the upload of an image through
/v1/cloudimages/{id}/upload, recorded as a create before the fix of ask 5) stayed live after a DELETE /v2/images/{id}
answered 204. /v1/cloudimages/ID and /v2/images/ID name one image (labadmin.KIND_SAME). Every id here is invented."""
from __future__ import annotations

from awb.tcp import labadmin
from tests.test_bytg_lab_image_delete import _image, _left
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import fake, lab_call, run_op, session, svc  # noqa: F401


def _old_entry(n, rid, path):
    return {"n": n, "label": "ims cloudimages", "name": rid, "state": "live", "at": "x",
            "delete": {"method": "DELETE", "service": "ims", "path": path, "query": {}, "region": "eu-de"}}


def test_bytg_cloudimages_entries_end_with_the_delete_of_the_image(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    rid, _name = _image(svc, uid, run)
    p = svc.paths_fn()
    record = labadmin.open_run(p, "tcp-ab2c", "test-10491")
    record["created"] += [_old_entry(2, rid, "/v1/cloudimages/%s/upload" % rid),
                          _old_entry(3, rid, "/v1/cloudimages/%s" % rid)]
    labadmin.save(p, record)
    assert len(_left(svc, uid)) == 3
    gone = lab_call(svc, uid, "DELETE", "ims", "/v2/images/%s" % rid, run)
    assert gone["status"] == 204
    assert _left(svc, uid) == []


def test_bytg_cloudimages_same_kind_only_for_the_same_id():
    a, b = "0" * 32, "1" * 32
    assert labadmin.same_resource("/v1/cloudimages/%s" % a, "/v2/images/%s" % a)
    assert labadmin.under("/v1/cloudimages/%s/upload" % a, "/v2/images/%s" % a)
    assert not labadmin.same_resource("/v1/cloudimages/%s" % a, "/v2/images/%s" % b)
    assert not labadmin.under("/v1/cloudimages/%s/upload" % a, "/v2/servers/%s" % a)
