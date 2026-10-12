"""A stand-in key service for the lab recipes: one object that answers a request of the call socket the way
awb/tcp/keys.py answers it (ok, status, data, text; a refusal as ok false with an error), over a small invented
cloud kept in memory.

Every path takes the form /v1/{project_id}/<collection>[/<id>[/tags/action]]: a POST to a collection creates an item
under the singular of its name (widgets: widget) with a fresh id and the status BUILD; a GET of an item answers it
(ACTIVE after `polls_to_active` reads) or 404 once it is deleted; a GET of the collection lists the items not deleted
with their id and name (the find by name); a DELETE marks it deleted; a tag call answers 204.
GET /v1/{project_id}/vpcs is the grant probe. GET /v2/{project_id}/types answers the EVS disk types of
`volume_types` (by default SAS and SSD offered in eu-de-01 to eu-de-03 and SATA sold out in all three, the shape
read live on 2026-10-12) or `types_status` when that is set. The knobs: refuse (the probe's refusal), fail_create (collection:
status or "lost" for no answer at all; nothing is made), made_but (collection: status or "lost"; the item is made,
then the answer is that status or none), list_fail (the next so many GETs of a collection answer 500), fail_delete
(collections whose DELETE answers 500), never_active, plant (fields put into every item read), on_call (a function
run before each answer, for a signal), forbidden (a GET of /v1/{project_id}/forbidden answers 403 or this
status), refused_body (the body of a refused create, by default an error_msg alone) and secrets (tenant alias: the
names of its secrets, which the operation tenants lists as the key service does, never a value). Every id and value
here is invented.
"""
from __future__ import annotations

import itertools
import re

_ITEM = re.compile(r"^/v1/\{project_id\}/(?P<coll>[a-z]+)(?:/(?P<id>[a-z0-9-]+)(?P<tags>/tags/action)?)?$")


class FakeKeyService:
    def __init__(self, *, polls_to_active: int = 1):
        self.items: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.refuse = ""
        self.fail_create: dict[str, int | str] = {}
        self.made_but: dict[str, int | str] = {}
        self.list_fail = 0
        self.fail_delete: set[str] = set()
        self.never_active = False
        self.polls_to_active = polls_to_active
        self.plant: dict = {}
        self.forbidden = 403
        self.on_call = None
        self.refused_body: dict = {"error_msg": "refused"}
        self.secrets: dict[str, list[str]] = {}
        zones = "eu-de-01,eu-de-02,eu-de-03"
        self.volume_types: list[dict] = [
            {"name": n, "id": "%08x-5555-4666-8777-%012x" % (i, 0xd15c), "is_public": True,
             "extra_specs": dict({"RESKEY:availability_zones": zones},
                                 **({"os-vendor-extended:sold_out_availability_zones": zones} if n == "SATA" else {}))}
            for i, n in enumerate(("SAS", "SSD", "SATA"), start=1)]
        self.types_status = 0
        self._n = itertools.count(1)

    def created(self) -> list[str]:
        return list(self.items)

    def live(self) -> list[str]:
        return [i for i, it in self.items.items() if not it["deleted"]]

    def __call__(self, req: dict) -> dict:
        self.calls.append(req)
        if self.on_call is not None:
            self.on_call(req)
        if req.get("op") == "tenants":
            return {"ok": True, "tenants": {a: {"roles": ["lab", "read"], "secrets": sorted(names), "regions": []}
                                            for a, names in self.secrets.items()}}
        if req.get("op") != "call":
            return {"ok": False, "error": "unknown operation"}
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        method, path = req["method"], req["path"]
        if method == "GET" and path == "/v1/{project_id}/vpcs":
            return {"ok": True, "status": 200, "data": {"vpcs": []}, "text": ""}
        if method == "GET" and path == "/v2/{project_id}/types":
            if self.types_status:
                return {"ok": True, "status": self.types_status, "data": {"error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {"volume_types": self.volume_types}, "text": ""}
        if method == "GET" and path == "/v1/{project_id}/forbidden":
            return {"ok": True, "status": self.forbidden, "data": {"error_msg": "denied"}, "text": ""}
        m = _ITEM.match(path)
        if not m:
            return {"ok": True, "status": 404, "data": {"error_msg": "no such path"}, "text": ""}
        coll, rid, tags = m.group("coll"), m.group("id"), m.group("tags")
        one = coll[:-1]
        if rid is None and method == "POST":
            if coll in self.fail_create:
                if self.fail_create[coll] == "lost":
                    raise ConnectionResetError("no answer")
                return {"ok": True, "status": self.fail_create[coll], "data": self.refused_body, "text": ""}
            rid = "%08x-0000-4000-8000-%012x" % (0x1a2b3c4d + next(self._n), 0xabcdef0 + len(self.items))
            body = (req.get("body") or {}).get(one) or {}
            self.items[rid] = {"coll": coll, "deleted": False, "reads": 0, "body": body}
            if coll in self.made_but:
                if self.made_but[coll] == "lost":
                    raise ConnectionResetError("no answer")
                return {"ok": True, "status": self.made_but[coll], "data": {"error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {one: {"id": rid, "status": "BUILD"}}, "text": ""}
        if rid is None and method == "GET":
            if self.list_fail > 0:
                self.list_fail -= 1
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            items = [{"id": i, "name": it["body"].get("name")} for i, it in self.items.items()
                     if it["coll"] == coll and not it["deleted"]]
            return {"ok": True, "status": 200, "data": {coll: items}, "text": ""}
        item = self.items.get(rid or "")
        if item is None or item["coll"] != coll:
            return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
        if tags:
            item["tags"] = (req.get("body") or {}).get("tags")
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "DELETE":
            if coll in self.fail_delete:
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            item["deleted"] = True
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "GET":
            if item["deleted"]:
                return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
            item["reads"] += 1
            status = "ACTIVE" if item["reads"] >= self.polls_to_active and not self.never_active else "BUILD"
            data = {one: dict({"id": rid, "status": status, "size": "small"}, **self.plant)}
            return {"ok": True, "status": 200, "data": data, "text": ""}
        return {"ok": True, "status": 405, "data": {"error_msg": "method"}, "text": ""}


class Clock:
    """Time that moves only when the run sleeps or a test moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
