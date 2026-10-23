"""The lab write allow list of keys.conf (task n17, gap G4 of the threat model F2; K9 of the review of 2026-10-20):
lab_writes = METHOD service path triples, each matched whole; without the line the lab key signs no write.

Every key and code here is invented; the project code comes from tests/test_keys.py.
"""
from __future__ import annotations

import json

import pytest

from awb.tcp import keys, lab, labwrites, recheckwatch, xchg
from tests.test_keys import (PROJECT, STORE, call, calls_logged_here, gw, lab_gateway, load,  # noqa: F401
                             no_waiting, svc)

SERVERS = "/v1/{project_id}/cloudservers"


def _load_with(svc, line: str | None):
    settings = {} if line is None else {"lab_writes": line}
    return keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=settings)


def _sent(gw) -> int:
    """The calls that reached the gateway, the project id lookup left out."""
    return sum(1 for _method, path in gw.calls if not path.startswith("/v3/projects"))


def _write(svc, service="vpc", path="/v1/{project_id}/vpcs", method="POST"):
    return call(svc, role="lab", method=method, service=service, path=path, body={"x": 1}, project=PROJECT)


def test_a_write_outside_the_triples_is_refused_with_one_fixed_line(svc, gw):
    _load_with(svc, "POST vpc /v1/{project_id}/vpcs")
    lab_gateway(gw)
    before = _sent(gw)
    other_service = _write(svc, "ecs", SERVERS)
    other_path = _write(svc, "vpc", "/v1/{project_id}/security-groups")
    assert other_service["error"] == keys.LAB_WRITE_LINE % "ecs"
    assert other_path["error"] == keys.LAB_WRITE_LINE % "vpc"
    assert "security-groups" not in other_path["error"] and "vpcs" not in other_path["error"]
    assert _sent(gw) == before          # nothing was signed


def test_a_get_is_never_affected(svc, gw):
    _load_with(svc, "vpc:/v1/{project_id}/vpcs")
    before = _sent(gw)
    a = call(svc, role="read", method="GET", service="ecs", path=SERVERS)
    assert a["ok"] and _sent(gw) == before + 1, a
    lab_gateway(gw)
    a = call(svc, role="lab", method="GET", service="ecs", path=SERVERS, project=PROJECT)
    assert a["ok"] and _sent(gw) == before + 2, a


@pytest.mark.parametrize("value", ["ecs", "ecs:v1/x", "ECS:/v1/x", "iam:/v3/x", "ecs:/v1/../x", "ecs:/v1//x",
                                   "", "ecs:/v1/x,", "ecs:/v1/x,vpc"])
def test_an_invalid_lab_writes_line_refuses_the_unlock_with_its_line_number(tmp_path, value):
    conf = tmp_path / "keys.conf"
    conf.write_text("bucket_tenant = test-1\nlab_writes = %s\n" % value, encoding="utf-8")
    with pytest.raises(xchg.XchgError, match="line 2"):
        xchg.read_settings(conf)


# --------------------------------------------------------------------------- K9: whole triples, no line no write

BUCKET = PROJECT + "-lab-1"


def _recipe_line() -> str:
    """The line awb keys lab-writes --from-recipes prints for the recipes of recipes/ and the recheck watch."""
    return ", ".join(" ".join(t) for t in labwrites.triples())


def _recipe_writes() -> list[dict]:
    """Every write call of the recipes of recipes/ and of the recheck watch, as the recipe writes it."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("method") in keys.WRITE_METHODS and "service" in obj and "path" in obj:
                found.append(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for f in sorted(lab.recipes_dir().glob("*.json")):
        walk(json.loads(f.read_text(encoding="utf-8")))
    found.extend(recheckwatch.DELETES.values())
    return found


def _filled(path: str) -> str:
    """A recipe path with each ${name} filled as a run fills it: an invented id, a bucket of the project."""
    return "/".join((BUCKET if seg == "${name}" else "c0ffee-%d" % n) if seg.startswith("${") else seg
                    for n, seg in enumerate(path.split("/")))


def test_a_write_that_matches_a_triple_is_signed(svc, gw):
    _load_with(svc, "POST vpc /v1/{project_id}/vpcs, DELETE vpc /v1/{project_id}/vpcs/{id}, "
                    "POST ecs /v1/{project_id}/cloudservers")
    lab_gateway(gw)
    for args in ((), ("vpc", "/v1/{project_id}/vpcs/abc123", "DELETE"), ("ecs", SERVERS)):
        before = _sent(gw)
        a = _write(svc, *args)
        assert a["ok"] and _sent(gw) == before + 1, a      # signed and sent; the fake's own status is its own


def test_an_ecs_v1_write_the_line_does_not_name_is_refused(svc, gw):
    """K9: the triple of the server create no longer passes every write under /v1/{project_id}/cloudservers."""
    _load_with(svc, _recipe_line())
    lab_gateway(gw)
    before = _sent(gw)
    for method, path in (("POST", SERVERS + "/action"), ("PUT", SERVERS + "/srv-1"),
                         ("POST", SERVERS + "/srv-1/os-reset-password"), ("POST", SERVERS + "/delete/extra"),
                         ("POST", "/v1/{project_id}/cloudservers-x"), ("POST", SERVERS + "/")):
        a = _write(svc, "ecs", path, method)
        assert a["error"] == keys.LAB_WRITE_LINE % "ecs", (method, path, a)
    assert _sent(gw) == before


def test_an_obs_write_the_line_does_not_name_is_refused(svc, gw):
    """K9: no obs:/ prefix any more: an object write, a bucket write with another query and a query left out of a
    triple are all refused; the bucket writes of the recipes pass the list."""
    _load_with(svc, _recipe_line())
    lab_gateway(gw)
    before = _sent(gw)
    refused = [("PUT", "/%s/object.txt" % BUCKET, None), ("POST", "/%s" % BUCKET, None),
               ("DELETE", "/%s/object.txt" % BUCKET, None), ("PUT", "/" + BUCKET, {"acl": ""}),
               ("PUT", "/" + BUCKET, {"policy": "", "acl": ""}), ("DELETE", "/" + BUCKET, {"policy": ""}),
               ("PUT", "/", None)]
    for method, path, query in refused:
        a = call(svc, role="lab", method=method, service="obs", path=path, query=query, project=PROJECT)
        assert a["error"] == keys.LAB_WRITE_LINE % "obs", (method, path, query, a)
    assert _sent(gw) == before
    for query in (None, {"policy": ""}, {"lifecycle": ""}):
        a = call(svc, role="lab", method="PUT", service="obs", path="/" + BUCKET, query=query, project=PROJECT)
        assert a.get("error") != keys.LAB_WRITE_LINE % "obs", (query, a)


def test_without_the_line_no_write_is_signed(svc, gw):
    """K9: a keys.conf without lab_writes signs no write of the lab key; reads go on."""
    _load_with(svc, None)
    lab_gateway(gw)
    before = _sent(gw)
    for args in (("ecs", SERVERS), ("vpc", "/v1/{project_id}/vpcs"), ("vpc", "/v1/{project_id}/vpcs/abc", "DELETE")):
        a = _write(svc, *args)
        assert a["error"] == keys.LAB_WRITES_MISSING_LINE, a
    assert _sent(gw) == before
    a = _write(svc, "iam", "/v3.0/OS-CREDENTIAL/credentials")
    assert not a["ok"]
    a = call(svc, role="lab", method="GET", service="ecs", path=SERVERS, project=PROJECT)
    assert a["ok"] and _sent(gw) == before + 1, a


def test_a_write_with_another_method_is_refused(svc, gw):
    _load_with(svc, "POST ecs /v1/{project_id}/cloudservers, DELETE vpc /v1/{project_id}/vpcs/{id}")
    lab_gateway(gw)
    before = _sent(gw)
    for method, service, path in (("DELETE", "ecs", SERVERS), ("PUT", "ecs", SERVERS), ("PATCH", "ecs", SERVERS),
                                  ("POST", "vpc", "/v1/{project_id}/vpcs/abc"),
                                  ("PUT", "vpc", "/v1/{project_id}/vpcs/abc")):
        a = _write(svc, service, path, method)
        assert a["error"] == keys.LAB_WRITE_LINE % service, (method, service, path, a)
    assert _sent(gw) == before


def test_a_template_segment_is_one_segment_never_a_prefix():
    t = ("DELETE", "vpc", "/v1/{project_id}/vpcs/{id}")
    assert xchg.lab_write_matches(t, "DELETE", "vpc", "/v1/{project_id}/vpcs/abc-1", {})
    assert xchg.lab_write_matches(t, "DELETE", "vpc", "/v1/0a1b2c/vpcs/abc-1", None)
    for path in ("/v1/{project_id}/vpcs", "/v1/{project_id}/vpcs/", "/v1/{project_id}/vpcs/abc/subnets/x",
                 "/v1/{project_id}/vpcs/abc/", "/v1//vpcs/abc", "/v2/{project_id}/vpcs/abc"):
        assert not xchg.lab_write_matches(t, "DELETE", "vpc", path, {}), path
    assert not xchg.lab_write_matches(t, "DELETE", "ecs", "/v1/{project_id}/vpcs/abc", {})
    assert not xchg.lab_write_matches(t, "DELETE", "vpc", "/v1/{project_id}/vpcs/abc", {"force": "true"})
    assert not xchg.lab_write_matches(t, "DELETE", "vpc", "/v1/{project_id}/vpcs/abc", "force")
    q = ("PUT", "obs", "/{name}?policy")
    assert xchg.lab_write_matches(q, "PUT", "obs", "/" + BUCKET, {"policy": ""})
    assert not xchg.lab_write_matches(q, "PUT", "obs", "/" + BUCKET, {})
    assert not xchg.lab_write_matches(q, "PUT", "obs", "/" + BUCKET, {"policy": "", "acl": ""})


def test_every_real_recipe_write_passes_the_line_of_the_recipes():
    triples = xchg.lab_writes_parse(_recipe_line())
    writes = _recipe_writes()
    assert triples and writes
    for c in writes:
        path, query = _filled(c["path"]), c.get("query") or {}
        assert "${" not in path
        assert sum(1 for t in triples if xchg.lab_write_matches(t, c["method"], c["service"], path, query)) == 1, c


def test_the_real_recipe_writes_are_signed_through_the_key_service(svc, gw):
    _load_with(svc, _recipe_line())
    lab_gateway(gw)
    for c in _recipe_writes():
        if c["service"] == "obs":
            continue                 # the obs recipe runs end to end in tests/test_lab.py
        before = _sent(gw)
        a = call(svc, role="lab", method=c["method"], service=c["service"], path=_filled(c["path"]),
                 query=c.get("query") or {}, body={"x": 1}, project=PROJECT)
        assert a["ok"] and _sent(gw) == before + 1, (c, a)


@pytest.mark.parametrize("value", ["POST ecs", "GET ecs /v1/x",
                                   "post ecs /v1/x", "POST iam /v3/x", "POST ecs v1/x", "POST ecs /v1/../x",
                                   "POST ecs /v1//x", "POST ecs /v1/x{id}", "POST ecs /v1/{id", "POST ecs /v1/{ID}",
                                   "POST ecs /v1/x?", "POST ecs /v1/x?ACL", "POST ecs /v1/x extra",
                                   "POST ecs /v1/x,", ""])
def test_a_line_that_is_no_list_of_triples_refuses_the_unlock(tmp_path, value):
    conf = tmp_path / "keys.conf"
    conf.write_text("bucket_tenant = test-1\nlab_writes = %s\n" % value, encoding="utf-8")
    with pytest.raises(xchg.XchgError, match="line 2.*METHOD service path"):
        xchg.read_settings(conf)


def test_a_valid_line_is_read_as_triples(tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_writes = POST ecs /v1/{project_id}/cloudservers, PUT obs /{name}?policy, PUT obs /\n",
                    encoding="utf-8")
    value = xchg.read_settings(conf)["lab_writes"]
    assert xchg.lab_writes_parse(value) == [("POST", "ecs", "/v1/{project_id}/cloudservers"),
                                            ("PUT", "obs", "/{name}?policy"), ("PUT", "obs", "/")]


def test_from_recipes_prints_every_recipe_write_once_as_a_triple(capsys):
    assert keys.main(["lab-writes", "--from-recipes"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("lab_writes = ")
    triples = xchg.lab_writes_parse(line[len("lab_writes = "):])
    assert triples is not None and len(triples) == len(set(triples)) and triples == sorted(triples)
    assert triples == labwrites.triples()
    assert ("POST", "ecs", "/v1/{project_id}/cloudservers") in triples
    assert ("POST", "ecs", "/v1/{project_id}/cloudservers/delete") in triples
    assert ("PUT", "obs", "/{name}?policy") in triples and ("PUT", "obs", "/{name}?lifecycle") in triples
    assert not any(s == "iam" for _, s, _ in triples) and "${" not in line and "obs /," not in line + ","
    # each triple is used by a write of a recipe or of the recheck watch: nothing on the line nobody needs
    used = {labwrites.shape((c["method"], c["service"], labwrites.template(c["path"], c.get("query"))))
            for c in _recipe_writes()}
    assert {labwrites.shape(t) for t in triples} == used and len(used) == len(triples)


def test_a_placeholder_inside_a_segment_has_no_triple():
    assert labwrites.template("/v1/{project_id}/vpcs/${vpc_id}/subnets/${id}") == \
        "/v1/{project_id}/vpcs/{vpc_id}/subnets/{id}"
    assert labwrites.template("/${name}", {"lifecycle": "", "acl": ""}) == "/{name}?acl&lifecycle"
    with pytest.raises(labwrites.LabWritesError):
        labwrites.template("/v1/{project_id}/servers-${id}")


def test_unlock_prints_the_count_of_triples_or_that_no_write_is_signed(capsys, monkeypatch, tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_writes = POST ecs /a, DELETE vpc /b/{id}, PUT obs /{name}\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setattr(keys.config, "is_work_user", lambda: False)
    monkeypatch.setattr(keys, "unlock", lambda: {"test-1": {"roles": ["lab"], "secrets": []}})
    assert keys.main(["unlock"]) == 0
    assert "lab_writes: 3 triples" in capsys.readouterr().out
    conf.write_text("bucket_tenant = test-1\n", encoding="utf-8")
    assert keys.main(["unlock"]) == 0
    assert "lab_writes: no line, the lab key signs no write" in capsys.readouterr().out


def test_an_old_form_line_unlocks_reads_and_signs_no_write(svc, gw, tmp_path, monkeypatch, capfd):
    """A deploy must not take TCP from every session: the old service:path-prefix line of before K9 lets the unlock
    go on and says so in one line, a read passes and every lab write is refused with that line."""
    conf = tmp_path / "keys.conf"
    conf.write_text("bucket_tenant = test-1\nlab_writes = vpc:/v1/{project_id}/vpcs, obs:/\n", encoding="utf-8")
    settings = xchg.read_settings(conf)                    # no XchgError: the unlock is not stopped
    assert settings["lab_writes"] == "vpc:/v1/{project_id}/vpcs, obs:/"
    summary = keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=settings)
    assert "test-1" in summary
    assert keys.LAB_WRITES_OLD_LINE in capfd.readouterr().err       # the line of the service's log
    before = _sent(gw)
    a = call(svc, method="GET", service="vpc", path="/v1/{project_id}/vpcs")
    assert a["ok"] and _sent(gw) == before + 1, a
    lab_gateway(gw)
    for service, path in (("vpc", "/v1/{project_id}/vpcs"), ("obs", "/%s" % BUCKET), ("ecs", SERVERS)):
        a = _write(svc, service, path, "PUT" if service == "obs" else "POST")
        assert a["error"] == keys.LAB_WRITES_OLD_LINE, a
    assert _sent(gw) == before + 1
    a = call(svc, role="lab", method="GET", service="ecs", path=SERVERS, project=PROJECT)
    assert a["ok"], a
    # awb keys unlock reports the same one line
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setattr(keys.config, "is_work_user", lambda: False)
    monkeypatch.setattr(keys, "unlock", lambda: {"test-1": {"roles": ["lab"], "secrets": []}})
    capfd.readouterr()
    assert keys.main(["unlock"]) == 0
    assert keys.LAB_WRITES_OLD_LINE in capfd.readouterr().out
