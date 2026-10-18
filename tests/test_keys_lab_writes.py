"""The lab write allow list of keys.conf (task n17, gap G4 of the threat model F2): lab_writes = service:path-prefix.

Every key and code here is invented; the project code comes from tests/test_keys.py.
"""
from __future__ import annotations

import json

import pytest

from awb.tcp import keys, lab, recheckwatch, xchg
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


def test_a_write_that_matches_a_pair_is_signed(svc, gw):
    _load_with(svc, "vpc:/v1/{project_id}/vpcs, ecs:/v1/{project_id}/cloudservers")
    lab_gateway(gw)
    for args in ((), ("vpc", "/v1/{project_id}/vpcs/abc123", "DELETE"), ("ecs", SERVERS)):
        before = _sent(gw)
        a = _write(svc, *args)
        assert a["ok"] and _sent(gw) == before + 1, a      # signed and sent; the fake's own status is its own


def test_a_write_outside_the_pairs_is_refused_with_one_fixed_line(svc, gw):
    _load_with(svc, "vpc:/v1/{project_id}/vpcs")
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


def test_without_the_line_every_write_but_iam_is_signed_as_before(svc, gw):
    _load_with(svc, None)
    lab_gateway(gw)
    assert _write(svc, "ecs", SERVERS)["ok"]
    assert _write(svc, "vpc", "/v1/{project_id}/anything")["ok"]
    a = _write(svc, "iam", "/v3.0/OS-CREDENTIAL/credentials")
    assert "does not write to iam" in a["error"]


@pytest.mark.parametrize("value", ["ecs", "ecs:v1/x", "ECS:/v1/x", "iam:/v3/x", "ecs:/v1/../x", "ecs:/v1//x",
                                   "", "ecs:/v1/x,", "ecs:/v1/x,vpc"])
def test_an_invalid_lab_writes_line_refuses_the_unlock_with_its_line_number(tmp_path, value):
    conf = tmp_path / "keys.conf"
    conf.write_text("bucket_tenant = test-1\nlab_writes = %s\n" % value, encoding="utf-8")
    with pytest.raises(xchg.XchgError, match="line 2"):
        xchg.read_settings(conf)


def test_a_valid_line_is_read_as_pairs(tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_writes = ecs:/v1/{project_id}/cloudservers, obs:/\n", encoding="utf-8")
    value = xchg.read_settings(conf)["lab_writes"]
    assert xchg.lab_writes_parse(value) == [("ecs", "/v1/{project_id}/cloudservers"), ("obs", "/")]


def test_from_recipes_prints_every_recipe_write_once(capsys):
    assert keys.main(["lab-writes", "--from-recipes"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("lab_writes = ")
    pairs = xchg.lab_writes_parse(line[len("lab_writes = "):])
    assert pairs is not None and len(pairs) == len(set(pairs)) and pairs == sorted(pairs)
    # every write call of every recipe and of the recheck watch is covered, none twice
    wanted = set()

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("method") in keys.WRITE_METHODS and "service" in obj and "path" in obj:
                wanted.add((obj["service"], obj["path"]))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for f in sorted(lab.recipes_dir().glob("*.json")):
        walk(json.loads(f.read_text(encoding="utf-8")))
    for c in recheckwatch.DELETES.values():
        wanted.add((c["service"], c["path"]))
    assert wanted
    for service, path in wanted:
        assert sum(1 for s, p in pairs if s == service and path.startswith(p)) >= 1, (service, path)
    assert ("vpc", "/v1/{project_id}/vpcs") in pairs and ("ecs", "/v1/{project_id}/cloudservers") in pairs
    assert not any(s == "iam" for s, _ in pairs) and "${" not in line


def test_unlock_prints_the_count_of_pairs(capsys, monkeypatch, tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_writes = ecs:/a, vpc:/b, obs:/\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setattr(keys.config, "is_work_user", lambda: False)
    monkeypatch.setattr(keys, "unlock", lambda: {"test-1": {"roles": ["lab"], "secrets": []}})
    assert keys.main(["unlock"]) == 0
    assert "lab_writes: 3 pairs" in capsys.readouterr().out
