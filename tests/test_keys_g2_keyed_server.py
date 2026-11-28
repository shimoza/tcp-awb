"""G2 of the attacker review of 2026-11-28: the caller still shapes the server that gets the key.

B-092 made the script the service's own text. The server around it stayed the caller's: a private image, a key pair,
a password it knows, an injected file. The key of the tenant sits in the user data of that server and its metadata
service gives it to every process on it. So a create with a user_data_script takes only the fields of
keys.KEYED_SERVER_FIELDS, the drawn password nobody knows, one server, and an image the platform lists as public.
The platform is a stand-in of cloud.Client.request. Every key and id is invented.
"""
from __future__ import annotations

import base64
import os

import pytest

from awb.tcp import keys

PATH = "/v1/{project_id}/cloudservers"
PROJECT = "tcp-abcd"
PAIR = {"ak": "AKSHAPEFAKE", "sk": "sk-g2-fake-secret"}
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
PUBLIC = "%08x-1111-4222-8333-%012x" % (0x0a0b0c0d, 0x70b11c)
PRIVATE = "%08x-1111-4222-8333-%012x" % (0x0a0b0c0e, 0x9417a7e)
SCRIPT = {"name": "diskreader", "params": {"bucket": PROJECT + "-lab-20261128-abcd", "region": "eu-de",
                                           "volume": VOLUME, "files": ""}}
RECIPE_SERVER = {"name": PROJECT + "-lab-r", "imageRef": PUBLIC, "flavorRef": "s3.medium.1",
                 "vpcid": "vpc-1", "nics": [{"subnet_id": "net-1"}], "security_groups": [{"id": "sg-1"}],
                 "root_volume": {"volumetype": "SAS"}, "availability_zone": "eu-de-01",
                 "adminPass": "{{random:password}}", "user_data_script": SCRIPT,
                 "server_tags": [{"key": "awb-project", "value": PROJECT}], "count": 1}
"""The server of the reader step of recipes/disk-reader.json."""


class Images:
    """IMS as the check asks it: the public image is listed as gold, the private one never under that filter."""

    def __init__(self):
        self.asked, self.fail, self.answer = [], False, None

    def __call__(self, client, method, service, path, query=None, body=None, headers=None):
        from awb.tcp.cloud import CloudError, Response

        self.asked.append((client.keys.ak, client.region, method, service, path, query))
        if self.fail:
            raise CloudError("no answer from the ims endpoint", 0)
        if self.answer is not None:
            return self.answer
        if (method, service, path) == ("GET", "ims", "/v2/cloudimages") and query.get("__imagetype") == "gold":
            rows = [{"id": PUBLIC, "__imagetype": "gold", "name": "Standard_Ubuntu_22.04_latest"}]
            return Response(200, {"images": [r for r in rows if r["id"] == query.get("id")]}, "")
        return Response(404, {}, "")


@pytest.fixture
def images(monkeypatch):
    from awb.tcp import cloud

    stand_in = Images()
    monkeypatch.setattr(cloud.Client, "request", lambda c, *a, **k: stand_in(c, *a, **k))
    return stand_in


def create(server: dict, region="eu-de"):
    return keys.with_user_data({"server": server}, "POST", "ecs", PATH, PAIR, project=PROJECT, region=region)


def test_the_body_of_the_finding_is_refused(images):
    """The test of the finding: any image and a key pair of the caller around the script."""
    with pytest.raises(keys.Refused):
        create({"name": "r", "imageRef": PRIVATE, "key_name": "k", "user_data_script": SCRIPT})


@pytest.mark.parametrize("field, value", [
    ("key_name", "k"), ("personality", [{"path": "/etc/cron.d/x", "contents": "QUJD"}]),
    ("metadata", {"admin_pass": "x"}), ("publicip", {"id": "eip-1"}), ("data_volumes", [{"volumetype": "SAS"}]),
    ("extendparam", {"support_auto_recovery": "true"}), ("os:scheduler_hints", {"group": "g"}),
    ("block_device_mapping_v2", [{"uuid": PRIVATE}]), ("admin_pass", "{{random:password}}"),
])
def test_a_field_that_shapes_the_keyed_server_is_refused(images, field, value):
    with pytest.raises(keys.Refused, match="takes no %s" % field.replace(":", ".")):
        create(dict(RECIPE_SERVER, **{field: value}))
    assert images.asked == [], "refused before anything is asked"


@pytest.mark.parametrize("password", ["Known-Pass-4711", "{{secret:ecs-admin}}", "", None, 7])
def test_only_the_drawn_password_goes_with_the_script(images, password):
    with pytest.raises(keys.Refused, match="drawn password"):
        create(dict(RECIPE_SERVER, adminPass=password))


@pytest.mark.parametrize("count", [2, 0, "1", True, None, 1.5])
def test_the_keyed_server_is_one_server(images, count):
    with pytest.raises(keys.Refused, match="count 1"):
        create(dict(RECIPE_SERVER, count=count))


@pytest.mark.parametrize("field, value", [
    ("root_volume", {"volumetype": "SAS", "extendparam": {"snapshotId": "s"}}), ("root_volume", "SAS"),
    ("nics", [{"subnet_id": "n", "binding:profile": {}}]), ("nics", {"subnet_id": "n"}),
    ("security_groups", [{"id": "sg", "name": "x"}]), ("security_groups", ["sg"]),
])
def test_the_parts_of_the_keyed_server_are_plain(images, field, value):
    with pytest.raises(keys.Refused, match="takes in %s only" % field):
        create(dict(RECIPE_SERVER, **{field: value}))


def test_an_image_that_is_not_a_public_one_is_refused(images):
    for image in (PRIVATE, "img-0001", "", None, 7, PUBLIC.upper()):
        with pytest.raises(keys.Refused, match="public image of the platform"):
            create(dict(RECIPE_SERVER, imageRef=image))
    asked = [a for a in images.asked if a[5]["id"] == PRIVATE]
    assert asked == [(PAIR["ak"], "eu-de", "GET", "ims", "/v2/cloudimages", {"id": PRIVATE, "__imagetype": "gold"})]


def test_an_image_check_without_an_answer_refuses(images):
    from awb.tcp.cloud import Response

    images.fail = True
    with pytest.raises(keys.Refused, match="public image of the platform"):
        create(dict(RECIPE_SERVER))
    images.fail = False
    for answer in (Response(500, None, "oops"), Response(200, {"images": [{"id": PUBLIC, "__imagetype": "private"}]}, ""),
                   Response(200, {"images": [{"id": PRIVATE, "__imagetype": "gold"}]}, ""), Response(200, [], "")):
        images.answer = answer
        with pytest.raises(keys.Refused, match="public image of the platform"):
            create(dict(RECIPE_SERVER))
    with pytest.raises(keys.Refused, match="public image of the platform"):
        keys.with_user_data({"server": dict(RECIPE_SERVER)}, "POST", "ecs", PATH, PAIR, project=PROJECT)


def test_the_server_of_the_recipe_still_gets_the_script_with_the_key(images):
    body, filled = create(dict(RECIPE_SERVER))
    server = body["server"]
    assert filled and "user_data_script" not in server
    assert {k: v for k, v in server.items() if k != "user_data"} == {k: v for k, v in RECIPE_SERVER.items()
                                                                   if k != "user_data_script"}
    text = base64.b64decode(server["user_data"]).decode("utf-8")
    assert PAIR["ak"] in text and PAIR["sk"] in text and "{{secret" not in text
    assert len(images.asked) == 1


def test_a_caller_text_keeps_its_own_server(images):
    """No key goes into a user_data_text, so its server stays the caller's."""
    body, filled = create({"name": "x", "imageRef": PRIVATE, "key_name": "k", "user_data_text": "#!/bin/sh\n"})
    assert not filled and body["server"]["key_name"] == "k" and images.asked == []


def test_the_service_refuses_the_shaped_server_before_the_platform_sees_it(images, tmp_path):
    s = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock", log_dir=tmp_path / "log")
    s.tenants = {"test-1": {"roles": {"lab": dict(PAIR)}}}
    s.settings = {"lab_writes": "POST ecs " + PATH}
    s._registered = lambda: {"test-1": ("eu-de",)}
    s._active_project = lambda code: code == PROJECT
    req = {"tenant": "test-1", "role": "lab", "method": "POST", "service": "ecs", "path": PATH, "region": "eu-de",
           "project": PROJECT}
    with pytest.raises(keys.Refused, match="takes no key_name"):
        s._call(dict(req, body={"server": dict(RECIPE_SERVER, key_name="k")}), os.getuid())
    with pytest.raises(keys.Refused, match="public image of the platform"):
        s._call(dict(req, body={"server": dict(RECIPE_SERVER, imageRef=PRIVATE)}), os.getuid())
    assert not [a for a in images.asked if a[2] == "POST"] and s.keyed == {}
