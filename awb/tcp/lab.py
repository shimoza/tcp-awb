"""Lab recipes: a live check on a test tenant as one command (raise, check, write evidence, always tear down).

    awb lab list                                             the recipes, their question and their cost guard
    awb lab dry-run RECIPE [--param K=V]... [--project CODE] [--tenant ALIAS]
                                                             the calls a run would make; no call is made
    awb lab run RECIPE --project CODE --tenant ALIAS [--param K=V]...

A recipe is a JSON file under `recipes/` of the repository (recipes/README.md holds the format). It names the
question it answers, its parameters with their defaults, the steps as calls through the key service with the lab
role (create, call, wait), the checks (a field of an answer, a state reached within a time, an expected refusal),
the teardown of every resource it creates, a time budget and a cost guard (the most resources and the longest
lifetime it may create). RECIPE is the name of a recipe there or the path of a recipe file.

A run works for one project of the kind project from a session. Before the first write it checks the project's
row (active, kind project), the owner's grant where this user can read it and then the grant as the key service
sees it, with one read call. Every resource it creates carries the tags awb-project (the project code) and
awb-expiry (an ISO date, the end of the guard's lifetime), in the body of its create call, in a tag call right
after it or, for a service that takes no tags at create, in its name (`<code>-lab-<expiry>-<4 letters>`). Before
each create the run writes a row in the project's RESOURCES.md in the state creating, with a neutral handle
(`ecs-3`, awb/tcp/sweep.py) and the name it gives the resource (the name carries the project code). When the answer
holds the id, the row gets it in full and the state live. When the answer is lost (no answer, a timeout or a 5xx)
or holds no id, the find of the step looks the resource up by that name: found, the row gets its id; a find that
answers without it proves the row not created; a find that cannot tell keeps the row live with a line for the owner.
A create the platform refuses with a 4xx made nothing and its row goes again.

The teardown always runs: after the checks, on a failed check, on an error, when the time budget runs out and on
Ctrl-C or SIGTERM (a second signal during the teardown is noted and does not stop it). It goes through every row the
run wrote, newest first. A row without an id is looked up by its name once more: found, it is deleted like the
others; still not there, the row stays not created with both finds as proof. It deletes the resources and proves
each one gone with a read. A row whose resource is proven gone is set to deleted. A resource it cannot delete or
cannot place keeps its row as live with one line for the owner; the command then exits 4.

The evidence goes to `evidence/lab-<recipe>-<date>-<n>.md` of the project: the question, the parameters, every
call (method, service, path, the answer's status and the fields the steps and checks read; for a refused or failed
call also its error code and the first 200 characters of its error message), the check results, the teardown proof,
the time and the resource count. Ids appear as handles; a field whose name or value looks like a key, a token or a
secret is withheld, and an error message in which the name check finds a name (or that it cannot check) too. When
every check passed and nothing is left, the evidence and the output end with a ready `awb kb add` line with the
grade live and the evidence path as its source.

A recipe whose calls fill a field with `{{secret:NAME}}` needs that secret of the tenant in the key service. The
run asks the key service for the names of the tenant's secrets (never a value) after the grant read and before its
first create; a name that is missing stops it there with one line, so it makes nothing.

A recipe whose create bodies name an EVS disk type (`volumetype` or `volume_type`, such as the system disk of a
server) gets the disk types read with the read key (GET evs /v2/{project_id}/types) after the secrets and before its
first create. A type the answer does not list, a type whose RESKEY:availability_zones leaves out the recipe's AZ and a
type that lists the AZ under os-vendor-extended:sold_out_availability_zones stop it there with one line, so it makes
nothing (run 3 of 2026-10-12 made three resources before the server create answered Ecs.0044 for a sold-out SATA).

Exit codes: 0 every check passed and everything is gone; 1 a check failed and everything is gone; 2 refused
before the first call, a usage error or a run that stopped (an error, the time budget, a signal) with everything
gone; 4 a resource is left live (its row in RESOURCES.md says what to do).
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import secrets
import shlex
import signal
import string
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

RECIPES_DIR = Path(__file__).resolve().parent.parent.parent / "recipes"
RECIPES_ENV = "AWB_RECIPES"
MAX_RESOURCES = 12
"""The most resources any recipe may create, whatever its guard says."""
MAX_LIFETIME = 120
"""Minutes: the longest any recipe may keep a resource, whatever its guard says."""
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
DOS = ("create", "call", "wait")
COSTS = ("none", "small", "medium")
GONE_WITHIN = 180
EVERY = 10
EXIT_OK, EXIT_CHECK, EXIT_STOPPED, EXIT_LEFT = 0, 1, 2, 4
PROBE = {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs", "query": {"limit": "1"}}
"""The read call that asks the key service whether this project may reach the tenant (its grant)."""
DISK_TYPES = {"method": "GET", "service": "evs", "path": "/v2/{project_id}/types"}
"""The read of the EVS disk types before the first create (the Cinder-shaped list under volume_types)."""
DISK_KEYS = ("volumetype", "volume_type")
OFFERED_IN = "RESKEY:availability_zones"
SOLD_OUT_IN = "os-vendor-extended:sold_out_availability_zones"
"""Where a disk type in the answer names the AZs that offer it and the AZs where it is sold out (live, KB-KYR6)."""

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_VAR_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_KIND_RE = re.compile(r"^[a-z]{2,5}$")
_REF_RE = re.compile(r"\$\{([a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?)\}")
_WHOLE_RE = re.compile(r"^\$\{(tags):(kv|list|dict)\}$")
_INT_RE = re.compile(r"^\$\{int:([a-z][a-z0-9_]*)\}$")
_SECRETISH = re.compile(r"(?i)(key|secret|token|passw|credential|auth|private|cert)")
_SECRET_REF_RE = re.compile(r"\{\{secret:([a-z0-9][a-z0-9_.-]{0,40})\}\}")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")
ERROR_FORMS = (("error_code", "error_msg"), ("code", "message"), ("error.code", "error.message"))
"""Where the TCP APIs put the code and the message of an error, in this order."""
ERROR_CHARS = 200
BUILTINS = ("project", "tenant", "region", "expiry", "expiry8", "rand", "name")
TOP_KEYS = {"name", "question", "about", "params", "vars", "budget", "guard", "steps", "checks", "teardown", "kb"}


class LabError(Exception):
    """A recipe or a run that is refused. The message names the recipe part, never a value of a key."""


class Stop(Exception):
    """The run stops here (an error or the time budget); the teardown follows."""


class CheckFailed(Exception):
    """A check did not hold; the teardown follows."""


class Interrupted(BaseException):
    """Ctrl-C or SIGTERM during the run; the teardown follows."""


# --------------------------------------------------------------------------- recipes


def recipes_dir() -> Path:
    return Path(os.environ.get(RECIPES_ENV) or RECIPES_DIR)


def find(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".json" and p.is_file():
        return p
    if not _NAME_RE.match(name_or_path or ""):
        raise LabError("a recipe is named like ecs-boot-ssh (awb lab list) or given as a .json file")
    p = recipes_dir() / ("%s.json" % name_or_path)
    if not p.is_file():
        raise LabError("no recipe of that name (awb lab list)")
    return p


def load(path: Path) -> dict:
    try:
        recipe = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LabError("the recipe file is not readable JSON") from None
    validate(recipe)
    return recipe


def _refs(obj) -> set[str]:
    if isinstance(obj, str):
        return {m.group(1).split(":", 1)[-1] if m.group(1).startswith(("base64:", "int:")) else m.group(1)
                for m in _REF_RE.finditer(obj) if not m.group(1).startswith("tags:")}
    if isinstance(obj, dict):
        return set().union(*(_refs(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_refs(v) for v in obj)) if obj else set()
    return set()


def _check_call(call, where: str) -> None:
    if not isinstance(call, dict) or not set(call) <= {"method", "service", "path", "query", "body"}:
        raise LabError("%s: a call has method, service, path and maybe query and body" % where)
    if call.get("method") not in METHODS:
        raise LabError("%s: the method is one of %s" % (where, ", ".join(METHODS)))
    if not isinstance(call.get("service"), str) or not re.match(r"^[a-z][a-z0-9-]{1,30}$", call["service"]):
        raise LabError("%s: a service reads like ecs or vpc" % where)
    if not isinstance(call.get("path"), str) or not call["path"].startswith("/"):
        raise LabError("%s: a path starts with a slash" % where)
    if "query" in call and not (isinstance(call["query"], dict)
                                and all(isinstance(v, str) for v in call["query"].values())):
        raise LabError("%s: a query is an object of strings" % where)


def validate(r: dict) -> None:
    """Refuse a recipe that is malformed, refers to a value it does not have, creates a resource without a
    teardown or without the tags; also one that breaks its own cost guard or the Workbench ceiling."""
    if not isinstance(r, dict) or not set(r) <= TOP_KEYS:
        raise LabError("a recipe holds only %s" % ", ".join(sorted(TOP_KEYS)))
    for k in ("name", "question", "params", "budget", "guard", "steps", "checks", "teardown"):
        if k not in r:
            raise LabError("the recipe has no %s" % k)
    if not isinstance(r["name"], str) or not _NAME_RE.match(r["name"]):
        raise LabError("a recipe name reads like ecs-boot-ssh")
    if not isinstance(r["question"], str) or not r["question"].strip():
        raise LabError("the question is a sentence")
    params = r["params"]
    if not isinstance(params, dict):
        raise LabError("params is an object")
    for k, p in params.items():
        if not _VAR_RE.match(k) or (k in BUILTINS and k != "region") or not isinstance(p, dict) \
                or not isinstance(p.get("default"), str):
            raise LabError("param %s: a parameter has a plain name and a default string" % k)
        if not set(p) <= {"default", "pattern", "help", "choices"}:
            raise LabError("param %s: a parameter has default, pattern, help and choices only" % k)
        if "pattern" in p:
            try:
                re.compile(p["pattern"])
            except (re.error, TypeError):
                raise LabError("param %s: the pattern is not a regular expression" % k) from None
        if "choices" in p and not (isinstance(p["choices"], dict) and p["default"] in p["choices"]
                                   and all(isinstance(c, dict) for c in p["choices"].values())):
            raise LabError("param %s: choices map each allowed value to its values; the default is one" % k)
    known = set(BUILTINS) | set(params)
    for p in params.values():
        for c in (p.get("choices") or {}).values():
            known |= set(c)
    rvars = r.get("vars") or {}
    if not isinstance(rvars, dict) or not all(_VAR_RE.match(k) and isinstance(v, str) for k, v in rvars.items()):
        raise LabError("vars is an object of plain names and strings")
    known |= set(rvars)
    budget, guard = r["budget"], r["guard"]
    if not (isinstance(budget, dict) and isinstance(budget.get("minutes"), int)
            and isinstance(budget.get("teardown_minutes"), int) and budget["minutes"] > 0
            and budget["teardown_minutes"] > 0):
        raise LabError("budget has minutes and teardown_minutes, whole numbers")
    if not (isinstance(guard, dict) and isinstance(guard.get("resources"), int)
            and isinstance(guard.get("lifetime_minutes"), int)):
        raise LabError("guard has resources and lifetime_minutes, whole numbers")
    steps = r["steps"]
    if not isinstance(steps, list) or not steps:
        raise LabError("steps is a list")
    ids: list[str] = []
    creates: list[str] = []
    for n, s in enumerate(steps, start=1):
        where = "step %d" % n
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not _ID_RE.match(s["id"]):
            raise LabError("%s: a step has an id like vpc or wait-subnet" % where)
        sid = s["id"]
        where = "step %s" % sid
        if sid in ids:
            raise LabError("%s: the id is used twice" % where)
        if s.get("do") not in DOS:
            raise LabError("%s: do is create, call or wait" % where)
        allowed = {"id", "do", "says", "call", "save", "confirm"}
        if s["do"] == "create":
            allowed |= {"resource", "id_field", "id_value", "find", "tags", "name"}
        if s["do"] == "wait":
            allowed |= {"until", "within", "every", "fail_on"}
        if not set(s) <= allowed:
            raise LabError("%s: unknown keys for a %s step" % (where, s["do"]))
        _check_call(s.get("call"), where)
        if s["do"] == "wait" and s["call"]["method"] not in ("GET", "HEAD", "POST"):
            raise LabError("%s: a wait reads" % where)
        missing = _refs(s["call"]) - known
        if missing:
            raise LabError("%s: refers to %s, which no earlier step gives" % (where, ", ".join(sorted(missing))))
        save = s.get("save") or {}
        if not isinstance(save, dict) or not all(_VAR_RE.match(k) and isinstance(v, str) for k, v in save.items()):
            raise LabError("%s: save maps a plain name to a field path" % where)
        if s["do"] == "create":
            if not _VAR_RE.match(sid):
                raise LabError("%s: a create step is named like a value (letters, digits, underscores): later "
                               "steps refer to its id as ${%s}" % (where, sid.replace("-", "_")))
            res = s.get("resource")
            if not (isinstance(res, dict) and _KIND_RE.match(str(res.get("kind", "")))
                    and isinstance(res.get("type"), str) and res.get("cost") in COSTS):
                raise LabError("%s: resource has kind (2 to 5 letters), type and cost (%s)" % (where, ", ".join(COSTS)))
            if len([k for k in ("id_field", "id_value") if isinstance(s.get(k), str)]) != 1:
                raise LabError("%s: id_field names where the answer holds the new id; id_value gives it in its "
                               "place" % where)
            rname = s.get("name", "${name}")
            if not (isinstance(rname, str) and ("${name}" in rname or "${project}" in rname)):
                raise LabError("%s: the name of the resource carries ${name} or ${project}, so it holds the project "
                               "code" % where)
            if rname not in json.dumps(s["call"]) + str(s.get("id_value", "")):
                raise LabError("%s: the call gives the resource its name %s" % (where, rname))
            if _refs(rname) - known:
                raise LabError("%s: the name refers to a value it does not have" % where)
            find_ = s.get("find")
            if find_ is not None:
                if not (isinstance(find_, dict) and set(find_) <= {"call", "list", "field", "status", "within"}
                        and (isinstance(find_.get("field"), str)
                             or (isinstance(find_.get("status"), list) and find_["status"]
                                 and all(isinstance(x, int) for x in find_["status"]) and "list" not in find_
                                 and "id_value" in s))):
                    raise LabError("%s: find has a call and a field (with list, the field of the item named like the "
                                   "resource) or, for an id_value, the statuses that say it exists" % where)
                if "list" in find_ and not isinstance(find_["list"], str):
                    raise LabError("%s: the list of a find is a field path" % where)
                if "within" in find_ and not (isinstance(find_["within"], int) and find_["within"] >= 0):
                    raise LabError("%s: within of a find is a number of seconds" % where)
                _check_call(find_.get("call"), where + " find")
                if find_["call"]["method"] not in ("GET", "HEAD"):
                    raise LabError("%s: a find reads" % where)
                if _refs(find_["call"]) - known:
                    raise LabError("%s: the find refers to a value it does not have" % where)
            tags = s.get("tags")
            body = json.dumps(s["call"].get("body"))
            if tags == "body":
                if "${tags:" not in body:
                    raise LabError("%s: tags in the body need ${tags:kv}, ${tags:list} or ${tags:dict} there" % where)
            elif tags == "name":
                if "${name}" not in json.dumps(s["call"]):
                    raise LabError("%s: tags in the name need ${name} in the call" % where)
            elif isinstance(tags, dict):
                _check_call(tags, where + " tags")
                if "${tags:" not in json.dumps(tags.get("body")):
                    raise LabError("%s: the tag call carries ${tags:kv}, ${tags:list} or ${tags:dict}" % where)
                if _refs(tags) - known - {"id"}:
                    raise LabError("%s: the tag call refers to a value it does not have" % where)
            else:
                raise LabError("%s: every resource carries the project and expiry tags: tags is body, name or a "
                               "call" % where)
            creates.append(sid)
            known.add(sid)
        if s["do"] == "wait":
            until = s.get("until")
            if not (isinstance(until, dict) and isinstance(until.get("field"), str)
                    and len(set(until) & {"equals", "in", "contains"}) == 1):
                raise LabError("%s: until has a field and one of equals, in, contains" % where)
            if not isinstance(s.get("within"), int) or s["within"] <= 0:
                raise LabError("%s: within is a number of seconds" % where)
        known |= set(save)
        ids.append(sid)
    checks = r["checks"]
    if not isinstance(checks, list) or not checks:
        raise LabError("checks is a list with at least one check")
    cids = set()
    for c in checks:
        if not (isinstance(c, dict) and isinstance(c.get("id"), str) and _ID_RE.match(c["id"])
                and isinstance(c.get("says"), str) and c.get("step") in ids and c["id"] not in cids):
            raise LabError("a check has its own id, says what it checks and names a step of the recipe")
        cids.add(c["id"])
        kinds = [k for k in ("reached", "status", "equals", "contains", "present") if k in c]
        if len(kinds) != 1:
            raise LabError("check %s: one of reached, status, equals, contains, present" % c["id"])
        if kinds[0] in ("equals", "contains", "present") and not isinstance(c.get("field"), str):
            raise LabError("check %s: a field check names the field" % c["id"])
        if kinds[0] == "reached" and next(s for s in steps if s["id"] == c["step"])["do"] != "wait":
            raise LabError("check %s: reached is the check of a wait step" % c["id"])
        if kinds[0] == "status" and not (isinstance(c["status"], list) and c["status"]
                                         and all(isinstance(x, int) for x in c["status"])):
            raise LabError("check %s: status is a list of HTTP statuses" % c["id"])
    teardown = r["teardown"]
    if not isinstance(teardown, dict) or set(teardown) != set(creates):
        raise LabError("teardown has one entry for every create step and no other")
    for sid, t in teardown.items():
        where = "teardown %s" % sid
        if not (isinstance(t, dict) and set(t) <= {"call", "gone", "confirm"} and isinstance(t.get("gone"), dict)):
            raise LabError("%s: an entry has a call and a gone proof" % where)
        _check_call(t.get("call"), where)
        g = t["gone"]
        if not set(g) <= {"call", "status", "field", "in", "within"}:
            raise LabError("%s: gone has call, status, field, in and within" % where)
        _check_call(g.get("call"), where + " gone")
        if g["call"]["method"] not in ("GET", "HEAD"):
            raise LabError("%s: the gone proof is a read" % where)
        if not (isinstance(g.get("status"), list) and g["status"]) and not ("field" in g and "in" in g):
            raise LabError("%s: gone names the statuses or the field values that prove the resource gone" % where)
        if (_refs(t["call"]) | _refs(g["call"])) - known - {"id"}:
            raise LabError("%s: refers to a value it does not have" % where)
    if len(creates) > guard["resources"]:
        raise LabError("cost guard: the recipe creates %d resources and its guard allows %d"
                       % (len(creates), guard["resources"]))
    if guard["resources"] > MAX_RESOURCES or guard["lifetime_minutes"] > MAX_LIFETIME:
        raise LabError("cost guard: a recipe may create at most %d resource(s) for at most %d minutes"
                       % (MAX_RESOURCES, MAX_LIFETIME))
    if budget["minutes"] + budget["teardown_minutes"] > guard["lifetime_minutes"]:
        raise LabError("cost guard: the time budget and the teardown (%d minutes) exceed the lifetime of the guard "
                       "(%d minutes)" % (budget["minutes"] + budget["teardown_minutes"], guard["lifetime_minutes"]))
    kb = r.get("kb")
    if kb is not None and not (isinstance(kb, dict) and kb.get("scope") in ("tcp", "hcs")
                               and isinstance(kb.get("tags"), list) and kb["tags"]
                               and kb.get("class") in ("availability", "api", "stable")
                               and isinstance(kb.get("statement"), str)):
        raise LabError("kb has scope, tags, class and statement")


def confirm_notes(r: dict) -> list[tuple[str, str]]:
    """(where, note) of every step or teardown the recipe marks "to confirm live"."""
    out = [(s["id"], s["confirm"]) for s in r["steps"] if s.get("confirm")]
    out += [("teardown " + k, t["confirm"]) for k, t in r["teardown"].items() if t.get("confirm")]
    return out


def secrets_of(r: dict, env: dict[str, str]) -> list[str]:
    """The names of the tenant secrets the calls of a recipe fill with {{secret:NAME}}, with the values of `env`."""
    calls = []
    for s in r["steps"]:
        calls += [s["call"], (s.get("find") or {}).get("call")]
        calls.append(s["tags"] if isinstance(s.get("tags"), dict) else None)
    for t in r["teardown"].values():
        calls += [t["call"], t["gone"]["call"]]
    text = json.dumps([expand(c, dict(env, id="<id>"), loose=True) for c in calls if c])
    return sorted(set(_SECRET_REF_RE.findall(text)))


def disks_of(r: dict, env: dict[str, str]) -> list[tuple[str, str]]:
    """(disk type, AZ) of every disk the create bodies of a recipe name under DISK_KEYS, with the values of `env`;
    the AZ is the availability_zone of the nearest object around it, else the parameter az, else empty."""
    found: set[tuple[str, str]] = set()

    def walk(obj, az: str) -> None:
        if isinstance(obj, list):
            for v in obj:
                walk(v, az)
        elif isinstance(obj, dict):
            here = obj.get("availability_zone")
            az = here if isinstance(here, str) and here else az
            for k in DISK_KEYS:
                if isinstance(obj.get(k), str) and obj[k]:
                    found.add((obj[k], az))
            for v in obj.values():
                walk(v, az)
    for s in r["steps"]:
        if s["do"] == "create":
            walk(expand(s["call"].get("body"), env, loose=True), str(env.get("az") or ""))
    return sorted(found)


def _zones(value) -> set[str]:
    if isinstance(value, str):
        return {z.strip() for z in value.split(",") if z.strip()}
    if isinstance(value, list):
        return {z.strip() for z in value if isinstance(z, str) and z.strip()}
    return set()


def disk_refusal(answer: dict, disks: list[tuple[str, str]]) -> str | None:
    """None when every (disk type, AZ) is listed, offered in its AZ and not sold out there in the answer of
    DISK_TYPES, else the line that stops the run. Entries of one name count together; the fields are read under
    extra_specs and, failing that, beside it."""
    data = answer.get("data") if answer.get("ok") else None
    types = data.get("volume_types") if isinstance(data, dict) else None
    if answer.get("status") != 200 or not isinstance(types, list):
        return "the disk types could not be read (%s), so no create was tried" % (
            "status %s" % answer.get("status") if answer.get("ok") else "no answer from the key service")
    offered: dict[str, set[str]] = {}
    sold: dict[str, set[str]] = {}
    for t in types:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str):
            continue
        spec = t.get("extra_specs") if isinstance(t.get("extra_specs"), dict) else {}
        offered.setdefault(t["name"], set()).update(_zones(spec.get(OFFERED_IN, t.get(OFFERED_IN))))
        sold.setdefault(t["name"], set()).update(_zones(spec.get(SOLD_OUT_IN, t.get(SOLD_OUT_IN))))
    for name, az in disks:
        shown = name if _CODE_RE.match(name) else "of the recipe"
        if name not in offered:
            return "the disk type %s is not in the list of disk types of the tenant, so no create was tried" % shown
        if az and offered[name] and az not in offered[name]:
            return "the disk type %s is not offered in %s, so no create was tried" % (shown, az)
        if (az and az in sold[name]) or (not az and sold[name]):
            return "the disk type %s is sold out in %s, so no create was tried; set another type in the recipe" % (
                shown, az or ", ".join(sorted(sold[name])))
    return None


def error_of(answer: dict) -> tuple[str, str]:
    """(code, message) of the error in an answer, in the forms of ERROR_FORMS (also inside the one object a
    wrapper such as badRequest holds); empty strings when it carries none. Raw: the caller cleans both."""
    data = answer.get("data")
    if data is None and isinstance(answer.get("text"), str) and answer["text"].lstrip().startswith("{"):
        try:
            data = json.loads(answer["text"])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return "", ""
    places = [data]
    if len(data) == 1 and isinstance(next(iter(data.values())), dict):
        places.append(next(iter(data.values())))
    for place in places:
        for code_at, msg_at in ERROR_FORMS:
            code, msg = dig(place, code_at), dig(place, msg_at)
            code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
            msg = msg if isinstance(msg, str) else ""
            if code or msg:
                return code.strip(), msg.strip()
    return "", ""


def params_of(r: dict, given: list[str]) -> dict[str, str]:
    """The parameters with their defaults, then what --param K=V gives; refused when a value leaves its pattern or
    its choices (part of the cost guard: a flavor or a size outside the recipe's range)."""
    out = {k: p["default"] for k, p in r["params"].items()}
    for item in given:
        if "=" not in item:
            raise LabError("a parameter reads K=V")
        k, v = item.split("=", 1)
        if k not in r["params"]:
            raise LabError("the recipe has no parameter %s" % (k if _VAR_RE.match(k) else "of that name"))
        out[k] = v
    for k, v in out.items():
        p = r["params"][k]
        if "pattern" in p and not re.fullmatch(p["pattern"], v):
            raise LabError("cost guard: the value of %s is outside what the recipe allows" % k)
        if "choices" in p and v not in p["choices"]:
            raise LabError("the value of %s is not one of %s" % (k, ", ".join(sorted(p["choices"]))))
        if not re.fullmatch(r"[A-Za-z0-9_.:/=+-]{0,200}", v):
            raise LabError("the value of %s holds characters a call does not take" % k)
    return out


# --------------------------------------------------------------------------- values


def dig(obj: Any, path: str) -> Any:
    """`a.b.0.c` in an answer; None when a part is absent."""
    for part in path.split(".") if path else []:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def tags_of(project: str, expiry: str, form: str):
    if form == "kv":
        return [{"key": "awb-project", "value": project}, {"key": "awb-expiry", "value": expiry}]
    if form == "list":
        return ["awb-project=%s" % project, "awb-expiry=%s" % expiry]
    return {"awb-project": project, "awb-expiry": expiry}


def expand(obj, env: dict[str, str], *, loose: bool = False, depth: int = 0):
    """Fill ${var}, ${base64:var}, a whole ${int:var} (a number) and a whole ${tags:FORM} value; a value that
    holds ${...} itself is filled too.
    `loose` (the dry run) shows a value no step has given yet as <var>."""
    if depth > 5:
        raise Stop("a value refers to itself")
    if isinstance(obj, dict):
        return {k: expand(v, env, loose=loose, depth=depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand(v, env, loose=loose, depth=depth) for v in obj]
    if not isinstance(obj, str):
        return obj
    m = _WHOLE_RE.match(obj)
    if m:
        return tags_of(env.get("project", "<project>"), env.get("expiry", "<expiry>"), m.group(2))
    m = _INT_RE.match(obj)
    if m:
        value = expand("${%s}" % m.group(1), env, loose=loose, depth=depth)
        if value.lstrip("-").isdigit():
            return int(value)
        if loose:
            return value
        raise Stop("the value of %s is not a whole number" % m.group(1))

    def one(m):
        ref = m.group(1)
        var = ref.split(":", 1)[1] if ref.startswith("base64:") else ref
        if var not in env:
            if loose:
                return "<base64 of %s>" % var if var != ref else "<%s>" % var
            raise Stop("no value for %s" % var)
        value = expand(str(env[var]), env, loose=loose, depth=depth + 1)
        return base64.b64encode(value.encode("utf-8")).decode("ascii") if var != ref else value
    return _REF_RE.sub(one, obj)


# --------------------------------------------------------------------------- the run


@dataclass
class Resource:
    step: str
    kind: str
    type: str
    cost: str
    id: str                 # empty while the create runs and when no answer and no find gave one
    handle: str
    name: str = ""
    state: str = "live"     # creating, live, deleted or not created
    proof: str = ""
    owner_line: str = ""
    lost: str = ""          # what the create answered when its answer was lost (no answer or a 5xx)


@dataclass
class CallRecord:
    n: int
    phase: str
    step: str
    method: str
    service: str
    path: str
    status: str
    fields: dict = field(default_factory=dict)
    polls: int = 1
    error_code: str = ""    # the error code of a refused or failed answer, cleaned; empty when it carries none


def _send_default(req: dict) -> dict:
    from awb.tcp import keys

    return keys.request(keys.call_socket(), req)


class Run:
    """One run of a recipe. `send(req) -> answer` is the key service (a stand-in in the tests); `clock`, `sleep`
    and `now` are the time."""

    def __init__(self, recipe: dict, params: dict[str, str], project: str, tenant: str, folder: Path,
                 handles, *, send: Callable[[dict], dict] | None = None, clock=time.monotonic, sleep=time.sleep,
                 now: datetime.datetime | None = None, out=print, err=None):
        self.r = recipe
        self.params = params
        self.project = project
        self.tenant = tenant
        self.folder = Path(folder)
        self.handles = handles
        self.send = send or _send_default
        self.clock = clock
        self.sleep = sleep
        self.started_at = now or datetime.datetime.now(datetime.timezone.utc)
        self.out = out
        self.err = err or (lambda line: print(line, file=sys.stderr))
        self.region = params.get("region") or "eu-de"
        lifetime = datetime.timedelta(minutes=recipe["guard"]["lifetime_minutes"])
        expiry = (self.started_at + lifetime).date()
        rand = "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))
        self.env: dict[str, str] = {"project": project, "tenant": tenant, "region": self.region,
                                    "expiry": expiry.isoformat(), "expiry8": expiry.strftime("%Y%m%d"), "rand": rand}
        self.env["name"] = "%s-lab-%s-%s" % (project, self.env["expiry8"], rand)
        self.env.update(params)
        for k, p in recipe["params"].items():
            self.env.update((p.get("choices") or {}).get(params[k], {}))
        for k, v in (recipe.get("vars") or {}).items():
            self.env[k] = v
        self.calls: list[CallRecord] = []
        self.checks: list[tuple[dict, str, str]] = []      # (check, got, passed|failed|not run)
        self.made: list[Resource] = []
        self.results: dict[str, dict] = {}
        self.outcome = ""
        self.reason = ""
        self.deadline = 0.0
        self.t0 = 0.0
        self.t_end = 0.0
        self.signals_in_teardown = 0
        self.in_teardown = False
        self.critical = False
        self.pending = ""

    # ----------------------------------------------------------------- calls

    def _req(self, call: dict, role: str = "lab") -> dict:
        c = expand(call, self.env)
        return {"op": "call", "tenant": self.tenant, "role": role, "method": c["method"], "service": c["service"],
                "path": c["path"], "region": self.region, "query": c.get("query") or {}, "body": c.get("body"),
                "project": self.project}

    def _call(self, phase: str, step: str, call: dict, role: str = "lab", record: CallRecord | None = None,
              timed: bool = True):
        if timed and phase == "run" and self.deadline and self.clock() > self.deadline:
            raise Stop("the time budget of %d minutes ran out" % self.r["budget"]["minutes"])
        req = self._req(call, role)
        try:
            answer = self.send(req)
        except Exception as exc:                         # KeysError and the like: no answer at all
            answer = {"ok": False, "lost": True, "error": "no answer from the key service (%s)" % type(exc).__name__}
        if not isinstance(answer, dict):
            answer = {"ok": False, "lost": True, "error": "the key service gave no readable answer"}
        status = ("%d" % int(answer.get("status") or 0)) if answer.get("ok") else \
            self._clean("error", str(answer["error"])) if answer.get("lost") else \
            "refused: %s" % self._clean("error", str(answer.get("error") or "refused"))
        if record is None:
            record = CallRecord(len(self.calls) + 1, phase, step, req["method"], req["service"],
                                self.handles.mask(req["path"]), status)
            self.calls.append(record)
        else:
            record.polls += 1
            record.status = status
        self._keep_error(record, answer, phase)
        return answer, record

    def _keep_error(self, record: CallRecord, answer: dict, phase: str) -> None:
        """The error code and the start of the error message of a refused or failed answer, in the fields of its
        call: cleaned like every field, the message also through the name check. A 404 of the teardown proves a
        resource gone and keeps nothing; a later answer of a poll replaces what an earlier one kept."""
        record.fields.pop("error_code", None)
        record.fields.pop("error_msg", None)
        record.error_code = ""
        st = self.status(answer)
        if not answer.get("ok") or 200 <= st < 300 or (phase == "teardown" and st == 404):
            return
        code, msg = error_of(answer)
        if code:
            code = self._clean("error_code", code) if _CODE_RE.match(code) else "<withheld>"
            if code != "<withheld>" and not self._names_clean(code):
                code = "<withheld>"
            record.fields["error_code"] = record.error_code = code
        if msg:
            msg = self._clean("error_msg", msg[:ERROR_CHARS], limit=ERROR_CHARS)
            if msg != "<withheld>" and not self._names_clean(msg):
                msg = "<withheld by the name check>"
            record.fields["error_msg"] = msg

    def _names_clean(self, text: str) -> bool:
        """True when the name check finds nothing in `text`; False when it finds a name or cannot run."""
        from awb import check, config

        try:
            return not check.check_text(text, config.paths().register)
        except Exception:                   # CheckUnavailable, RegisterError: unchecked text is not kept
            return False

    def _said(self, record: CallRecord) -> str:
        """The status of a call for a stop line, with the error code when the answer carried one."""
        return "%s, error_code %s" % (record.status, record.error_code) if record.error_code else record.status

    @staticmethod
    def status(answer: dict) -> int:
        return int(answer.get("status") or 0) if answer.get("ok") else 0

    def _clean(self, name: str, value, limit: int = 120) -> str:
        """A value for the evidence: withheld when its field name or its text looks like a key, a token or a
        secret; ids as handles."""
        from awb import gate

        if _SECRETISH.search(name):
            return "<withheld>"
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = re.sub(r"[\r\n\t|]+", " ", text)
        if len(text) > limit:
            text = text[:limit - 3] + "..."
        if any(gate.DETECTORS[d](text) for d in ("secret", "token", "private-key")) or "<secret>" in text:
            return "<withheld>"
        return self.handles.mask(text)

    def _read(self, record: CallRecord, answer: dict, path: str):
        path = expand(path, self.env)
        value = dig(answer.get("data"), path)
        record.fields[self.handles.mask(path)] = self._clean(path, value)
        return value

    # ----------------------------------------------------------------- RESOURCES.md

    def _rows_path(self) -> Path:
        return self.folder / "RESOURCES.md"

    def _row(self, res: Resource, note: str) -> str:
        return "| %s | %s | %s | %s | %s | %s | %s | %s |\n" % (
            res.handle, res.id or "unknown", res.type, self.region, res.cost, self.env["expiry"], res.state,
            note.replace("|", "/"))

    def _write_row(self, res: Resource, note: str) -> None:
        from awb import projects

        path = self._rows_path()
        if not path.exists():
            path.write_text(projects._resources(self.project), encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        row = self._row(res, note)
        for i, line in enumerate(lines):
            cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
            if cells and cells[0] == res.handle:
                lines[i] = row
                break
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(row)
        tmp = path.with_name(".RESOURCES.md.tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.replace(tmp, path)

    def _label(self) -> str:
        return "awb lab run %s %s" % (self.r["name"], self.started_at.date().isoformat())

    def _drop_row(self, res: Resource) -> None:
        """The row of a create the platform refused (a 4xx: nothing was made) goes again."""
        path = self._rows_path()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = [x for x in lines if not (x.strip().startswith("|")
                                         and x.strip().strip("|").split("|")[0].strip() == res.handle)]
        tmp = path.with_name(".RESOURCES.md.tmp")
        tmp.write_text("".join(keep), encoding="utf-8")
        os.replace(tmp, path)

    def _open(self, step: dict, name: str) -> Resource:
        """The row of a resource about to be created, written before its create call: state creating, the name it
        gets, no id yet. Its handle stands for a placeholder until the id is known."""
        res = step["resource"]
        handle = self.handles.handle(res["kind"], "pending:%s:%s" % (self.env["rand"], step["id"]), name)
        self.handles.save()
        r = Resource(step["id"], res["kind"], res["type"], res["cost"], "", handle, name, state="creating")
        self.made.append(r)
        self._write_row(r, "%s: creating, named %s" % (self._label(), name))
        return r

    def _place(self, res: Resource, rid: str) -> None:
        """The id of a resource is known: its handle stands for it and its row says live with the id."""
        self.handles.rebind(res.handle, rid)
        self.handles.save()
        res.id, res.state, res.owner_line = rid, "live", ""
        self._write_row(res, self._label())

    def _unplaced(self, res: Resource, why: str) -> None:
        """A resource that may exist and has no id: its row stays live with a line for the owner."""
        res.state = "live"
        res.owner_line = ("%s: find it by its name %s on %s and delete it, then set this row to deleted"
                          % (why, res.name, self.tenant))
        self._write_row(res, "%s: %s" % (self._label(), res.owner_line))

    # ----------------------------------------------------------------- steps

    def _create(self, step: dict) -> None:
        if len(self.made) >= self.r["guard"]["resources"]:
            raise Stop("cost guard: the run would create more than %d resources" % self.r["guard"]["resources"])
        # a signal between the row and the end of the create would lose a resource: it waits until the row is right
        self.critical = True
        try:
            res = self._open(step, expand(step.get("name", "${name}"), self.env))
            answer, rec = self._call("run", step["id"], step["call"])
            st = self.status(answer)
            # no answer, a timeout of the platform (status 0) or a 5xx: the resource may exist
            lost = bool(answer.get("lost")) or (bool(answer.get("ok")) and (st == 0 or st >= 500))
            if not lost and not 200 <= st < 300:
                # the platform refused (a 3xx or a 4xx) or the key service did and sent nothing: nothing was made,
                # the row goes again
                self.made.remove(res)
                self._drop_row(res)
                self.handles.drop(res.handle)
                self.handles.save()
                raise Stop("the create %s answered %s" % (step["id"], self._said(rec)))
            if "id_value" in step:
                # an id the recipe names itself (a bucket): made when the answer is 2xx
                rid = expand(step["id_value"], self.env) if 200 <= st < 300 else None
            else:
                rid = self._read(rec, answer, step["id_field"]) if 200 <= st < 300 else None
            rid = rid if isinstance(rid, str) and rid else None
            if rid is None:
                rid = self._settle(step, res, rec.status, lost)
            else:
                self._place(res, rid)
        finally:
            self.critical = False
        if self.pending:
            raise Interrupted(self.pending)
        self.env[step["id"]] = rid
        self.results[step["id"]] = {"answer": answer, "record": rec}
        tags = step["tags"]
        if isinstance(tags, dict):
            saved, self.env = self.env, dict(self.env, id=rid)
            try:
                ta, trec = self._call("run", step["id"] + " tags", tags)
            finally:
                self.env = saved
            if not 200 <= self.status(ta) < 300:
                raise Stop("the tags of %s (%s) could not be set: %s" % (res.handle, step["id"], self._said(trec)))

    def _settle(self, step: dict, res: Resource, said: str, lost: bool) -> str:
        """A create whose answer holds no id: the find by name places it, proves it not created or cannot tell.
        Returns the id; raises Stop when there is none."""
        if lost:
            res.lost = said
        if not step.get("find"):
            if lost:
                if "id_value" in step:
                    # its name is its id: the teardown deletes it if it is there and proves it gone
                    rid = expand(step["id_value"], self.env)
                    self._place(res, rid)
                    return rid
                self._unplaced(res, "the create got %s and may have made it" % said)
                raise Stop("the create %s got %s and the step has no find" % (step["id"], said))
            self._unplaced(res, "created without an id in its answer")
            raise Stop("%s answered without an id" % step["id"])
        rid, verdict, proof = self._find(step, res.name, step["find"].get("within", 60))
        if rid:
            self._place(res, rid)
            return rid
        if verdict == "absent" and lost:
            res.state, res.proof = "not created", "the create got %s; %s" % (said, proof)
            self._write_row(res, "%s: not created, %s" % (self._label(), res.proof))
            raise Stop("the create %s got %s and the find by name proves nothing was made" % (step["id"], said))
        if lost:
            self._unplaced(res, "the create got %s and the find could not tell (%s)" % (said, proof))
            raise Stop("the create %s got %s and the find could not tell" % (step["id"], said))
        self._unplaced(res, "created without an id in its answer and %s" % proof)
        raise Stop("%s answered without an id and the find found none" % step["id"])

    def _find(self, step: dict, name: str, within: float, phase: str = "run") -> tuple[str | None, str, str]:
        """Look a resource up by its name with the find of its step, every EVERY seconds for `within` seconds.
        Returns (id, verdict, proof): verdict found, absent (the last answer was a list without the name on a page
        that is not full; for a find of statuses also a 404) or unknown (no answer that proves either)."""
        f = step["find"]
        end = self.clock() + within
        rec = None
        c = expand(f["call"], self.env)
        what = "the find by name %s (%s %s %s)" % (name, c["method"], c["service"], c["path"])
        while True:
            # not timed: a resource may exist already and must get its row before the budget stops the run
            answer, rec = self._call(phase, step["id"] + " find", f["call"], record=rec, timed=False)
            st = self.status(answer)
            verdict, value = "unknown", None
            if 200 <= st < 300:
                if "status" in f:
                    verdict = "found" if st in f["status"] else "unknown"
                    value = expand(step["id_value"], self.env) if verdict == "found" else None
                elif "list" in f:
                    items = dig(answer.get("data"), expand(f["list"], self.env))
                    if isinstance(items, list):
                        hit = next((x for x in items if isinstance(x, dict) and x.get("name") == name), None)
                        value = dig(hit, f["field"]) if hit is not None else None
                        # a full page without it proves nothing: it may be on the next one
                        limit = str((c.get("query") or {}).get("limit", ""))
                        full = limit.isdigit() and len(items) >= int(limit)
                        verdict = "found" if isinstance(value, str) and value else "unknown" if full else "absent"
                        rec.fields["named like the resource"] = "yes" if hit is not None else "no"
                else:
                    # a field alone: an answer without it may be another shape, it proves nothing
                    value = self._read(rec, answer, f["field"])
                    verdict = "found" if isinstance(value, str) and value else "unknown"
            elif st == 404 and "status" in f:
                # the read of the resource itself (a bucket) says it is not there; a 404 of a list may be its path
                verdict = "absent"
            if verdict == "found":
                return value, verdict, "%s found it" % what
            if self.clock() + EVERY > end:
                if verdict == "absent":
                    return None, verdict, "%s answered %s without it after %d s" % (what, rec.status, within)
                return None, verdict, "%s answered %s" % (what, rec.status)
            self.sleep(EVERY)

    def _cond(self, until: dict, value) -> bool:
        if "equals" in until:
            return value == until["equals"]
        if "in" in until:
            return value in until["in"]
        return isinstance(value, str) and expand(until["contains"], self.env) in value

    def _wait(self, step: dict) -> None:
        until = step["until"]
        every = step.get("every", EVERY)
        own_end = self.clock() + step["within"]
        end = min(own_end, self.deadline or float("inf"))
        rec = None
        reached, answer = False, {}
        while True:
            answer, rec = self._call("run", step["id"], step["call"], record=rec)
            value = dig(answer.get("data"), expand(until["field"], self.env)) if answer.get("ok") else None
            if self._cond(until, value):
                reached = True
                break
            if value is not None and value in (step.get("fail_on") or []):
                break
            if self.clock() + every > end:
                break
            self.sleep(every)
        shown = "contains %s: %s" % (until["contains"], "yes" if reached else "no") if "contains" in until else \
            self._clean(until["field"], dig(answer.get("data"), expand(until["field"], self.env)))
        rec.fields[self.handles.mask(expand(until["field"], self.env))] = shown
        self.results[step["id"]] = {"answer": answer, "record": rec, "reached": reached}
        if not reached and end < own_end and self.clock() + every > end:
            raise Stop("the time budget of %d minutes ran out in %s" % (self.r["budget"]["minutes"], step["id"]))
        if not reached and not any(c.get("step") == step["id"] and "reached" in c for c in self.r["checks"]):
            raise Stop("%s did not reach its state within %d s" % (step["id"], step["within"]))

    def _plain(self, step: dict) -> None:
        answer, rec = self._call("run", step["id"], step["call"])
        self.results[step["id"]] = {"answer": answer, "record": rec}
        st = self.status(answer)
        if not 200 <= st < 300 and not any(c.get("step") == step["id"] and "status" in c for c in self.r["checks"]):
            raise Stop("%s answered %s" % (step["id"], self._said(rec)))

    def _save(self, step: dict) -> None:
        res = self.results.get(step["id"]) or {}
        for var, path in (step.get("save") or {}).items():
            value = self._read(res["record"], res["answer"], path)
            if value is None or isinstance(value, (dict, list)):
                raise Stop("%s: the answer holds no %s" % (step["id"], path))
            self.env[var] = str(value)

    def _check(self, c: dict) -> None:
        res = self.results.get(c["step"]) or {}
        answer = res.get("answer") or {}
        if "reached" in c:
            got = "reached" if res.get("reached") else "not reached"
            ok = bool(res.get("reached"))
        elif "status" in c:
            got = res["record"].status
            ok = self.status(answer) in c["status"]
        else:
            path = expand(c["field"], self.env)
            value = dig(answer.get("data"), path) if 200 <= self.status(answer) < 300 else None
            if "contains" in c:
                want = expand(c["contains"], self.env)
                ok = isinstance(value, str) and want in value
                got = "contains it" if ok else "does not contain it"
            elif "present" in c:
                ok = (value not in (None, "", [], {})) == bool(c["present"])
                got = "present" if value not in (None, "", [], {}) else "absent"
            else:
                want = expand(c["equals"], self.env) if isinstance(c["equals"], str) else c["equals"]
                ok = value == want
                got = self._clean(path, value)
            if "contains" not in c:
                res["record"].fields[self.handles.mask(path)] = self._clean(path, value)
        self.checks.append((c, got, "passed" if ok else "failed"))
        if not ok:
            raise CheckFailed(c["id"])

    def _expected(self, c: dict) -> str:
        if "reached" in c:
            return "reached within the wait"
        if "status" in c:
            return "HTTP " + " or ".join(str(s) for s in c["status"])
        if "contains" in c:
            return "%s contains %s" % (c["field"], self._clean("x", expand(c["contains"], self.env, loose=True)))
        if "present" in c:
            return "%s %s" % (c["field"], "present" if c["present"] else "absent")
        return "%s = %s" % (c["field"], self._clean(c["field"], expand(c["equals"], self.env, loose=True)
                                                     if isinstance(c["equals"], str) else c["equals"]))

    # ----------------------------------------------------------------- the whole

    def probe(self) -> str | None:
        """The grant as the key service sees it: one read; None when it lets the project through, else its
        refusal."""
        answer, rec = self._call("grant", "grant", PROBE, role="read")
        if not answer.get("ok"):
            return str(answer.get("error") or "refused")
        return None

    def missing_secret(self) -> str | None:
        """The secrets the recipe fills, asked of the key service by name before the first create: the names of
        the tenant's secrets come back (op tenants), never a value. None when every one is there, else the line
        that stops the run."""
        names = secrets_of(self.r, self.env)
        if not names:
            return None
        try:
            answer = self.send({"op": "tenants"})
        except Exception as exc:
            return "the key service did not list the secrets of %s (%s)" % (self.tenant, type(exc).__name__)
        if not isinstance(answer, dict) or not answer.get("ok") or not isinstance(answer.get("tenants"), dict):
            said = answer.get("error") if isinstance(answer, dict) else None
            return "the key service did not list the secrets of %s (%s)" % (
                self.tenant, self._clean("error", str(said or "no list")))
        tenant = answer["tenants"].get(self.tenant)
        have = tenant.get("secrets") if isinstance(tenant, dict) else None
        missing = [n for n in names if not isinstance(have, list) or n not in have]
        if not missing:
            return None
        if len(missing) == 1:
            return "the secret %s is missing for %s" % (missing[0], self.tenant)
        return "the secrets %s are missing for %s" % (", ".join(missing), self.tenant)

    def disk_check(self) -> str | None:
        """The disk types the create bodies name, read with the read key before the first create: None when each
        is offered and not sold out in its AZ (or the recipe names none, then no call), else the line that stops
        the run."""
        disks = disks_of(self.r, self.env)
        if not disks:
            return None
        answer, rec = self._call("preflight", "disk types", DISK_TYPES, role="read", timed=False)
        return disk_refusal(answer, disks)

    def _on_signal(self, signum, frame):
        if self.in_teardown:
            self.signals_in_teardown += 1
            self.err("awb lab: a signal during the teardown is noted; the teardown runs to its end")
            return
        if self.critical:
            self.pending = signal.Signals(signum).name
            return
        raise Interrupted(signal.Signals(signum).name)

    def execute(self) -> int:
        self.t0 = self.clock()
        self.deadline = self.t0 + self.r["budget"]["minutes"] * 60
        old = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, self._on_signal)
        try:
            try:
                for step in self.r["steps"]:
                    {"create": self._create, "wait": self._wait, "call": self._plain}[step["do"]](step)
                    for c in self.r["checks"]:
                        if c["step"] == step["id"]:
                            self._check(c)
                    self._save(step)
                self.outcome = "passed"
            except CheckFailed as exc:
                self.outcome, self.reason = "check failed", str(exc)
            except Stop as exc:
                self.outcome, self.reason = "stopped", str(exc)
            except (Interrupted, KeyboardInterrupt) as exc:
                self.outcome, self.reason = "stopped", "interrupted (%s)" % (str(exc) or "SIGINT")
            except Exception as exc:             # a fault of the runner itself still tears down
                self.outcome, self.reason = "stopped", "an error in the run (%s)" % type(exc).__name__
        except (Interrupted, KeyboardInterrupt):
            # a signal that came while an outcome above was being taken
            self.outcome, self.reason = self.outcome or "stopped", self.reason or "interrupted"
        finally:
            # the teardown runs whatever happened above; a signal from here on is only noted
            self.in_teardown = True
            self.outcome = self.outcome or "stopped"
            try:
                self.teardown()
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
                self.t_end = self.clock()
        done = {c[0]["id"] for c in self.checks}
        for c in self.r["checks"]:
            if c["id"] not in done:
                self.checks.append((c, "-", "not run"))
        if any(r.state == "live" for r in self.made):
            return EXIT_LEFT
        return {"passed": EXIT_OK, "check failed": EXIT_CHECK}.get(self.outcome, EXIT_STOPPED)

    def teardown(self) -> None:
        """Go through every row the run wrote, newest first: place a row without an id by its name, delete every
        resource and prove each gone with a read. Never raises."""
        end = self.clock() + self.r["budget"]["teardown_minutes"] * 60
        for res in reversed(self.made):
            try:
                self._tear(res, end)
            except BaseException as exc:            # a fault here leaves the row live, the next one still runs
                res.state, res.proof = "live", "teardown error (%s)" % type(exc).__name__
            if res.state == "deleted":
                self._write_row(res, "%s: deleted, %s" % (self._label(), res.proof))
            elif res.state == "not created":
                self._write_row(res, "%s: not created, %s" % (self._label(), res.proof))
            else:
                if not res.owner_line:
                    t = self.r["teardown"][res.step]
                    c = expand(t["call"], dict(self.env, id=res.id or "<id>"), loose=True)
                    res.owner_line = ("LEFT LIVE: delete it with awb cloud call %s %s %s --tenant %s --role lab%s "
                                      "--project %s, then set this row to deleted"
                                      % (c["method"], c["service"], c["path"], self.tenant,
                                         " --body FILE" if c.get("body") is not None else "", self.project))
                self._write_row(res, "%s: %s (%s)" % (self._label(), res.owner_line, res.proof or "not proven gone"))

    def _look_again(self, res: Resource, end: float) -> bool:
        """A row without an id, looked up by its name once more before the teardown gives up on it. True when the
        find places it: it is deleted like the others."""
        step = next(s for s in self.r["steps"] if s["id"] == res.step)
        if not step.get("find"):
            res.state, res.proof = "live", res.proof or "no id to delete and no find by name"
            return False
        # a row proven not created is read once; one that may exist is looked for as long as the find allows
        within = 0 if res.state == "not created" else min(step["find"].get("within", 60), max(end - self.clock(), 0))
        rid, verdict, proof = self._find(step, res.name, within, phase="teardown")
        if rid:
            res.proof = ""
            self._place(res, rid)
            return True
        if verdict == "absent" and res.state == "not created":
            res.proof = "%s; at the teardown %s" % (res.proof, proof)
        elif verdict == "absent" and res.lost:
            # the create got no answer, the find could not tell then and answers without it now
            res.state, res.proof = "not created", "the create got %s; at the teardown %s" % (res.lost, proof)
        else:
            res.state = "live"
            res.proof = res.proof or "no id to delete: %s" % proof
        return False

    def _tear(self, res: Resource, end: float) -> None:
        if not res.id and not self._look_again(res, end):
            return
        t = self.r["teardown"][res.step]
        g = t["gone"]
        within = min(g.get("within", GONE_WITHIN), max(end - self.clock(), EVERY))
        stop_at = self.clock() + within
        saved = self.env
        self.env = dict(saved, id=res.id)
        try:
            deleted, drec, grec = False, None, None
            while True:
                if not deleted:
                    answer, drec = self._call("teardown", res.handle, t["call"], record=drec)
                    st = self.status(answer)
                    deleted = 200 <= st < 300 or st == 404
                if deleted:
                    answer, grec = self._call("teardown", res.handle + " gone", g["call"], record=grec)
                    st = self.status(answer)
                    gone = answer.get("ok") and (st in (g.get("status") or [])
                                                 or ("field" in g and 200 <= st < 300
                                                     and dig(answer.get("data"), g["field"]) in g["in"]))
                    if gone:
                        res.state = "deleted"
                        res.proof = "the read answered %s" % (st if st in (g.get("status") or []) else
                                                              "%s %s" % (g["field"], dig(answer.get("data"),
                                                                                         g["field"])))
                        grec.fields["gone"] = "yes"
                        return
                if self.clock() + EVERY > stop_at:
                    res.state = "live"
                    res.proof = ("the delete answered %s" % drec.status) if not deleted else \
                        "still there after %d s (the read answered %s)" % (within, grec.status if grec else "-")
                    if grec is not None:
                        grec.fields["gone"] = "no"
                    return
                self.sleep(EVERY)
        finally:
            self.env = saved

    # ----------------------------------------------------------------- evidence

    def evidence_path(self) -> Path:
        folder = self.folder / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        stem = "lab-%s-%s-" % (self.r["name"], self.started_at.date().isoformat())
        taken = [int(p.stem[len(stem):]) for p in folder.glob(stem + "*.md") if p.stem[len(stem):].isdigit()]
        return folder / ("%s%d.md" % (stem, max(taken + [0]) + 1))

    def kb_line(self, evidence: Path) -> str | None:
        kb = self.r.get("kb")
        if not kb or self.outcome != "passed" or any(r.state == "live" for r in self.made):
            return None
        statement = expand(kb["statement"], self.env)
        source = "live lab run of the recipe %s on the test tenant %s, %s" % (
            self.r["name"], self.tenant, evidence.relative_to(self.folder).as_posix())
        argv = ["awb", "kb", "add", "--scope", kb["scope"]]
        for t in kb["tags"]:
            argv += ["--tag", t]
        argv += ["--grade", "live", "--class", kb["class"], "--source", source, statement]
        return " ".join(shlex.quote(a) for a in argv)

    def evidence(self, path: Path) -> str:
        r = self.r
        left = [x for x in self.made if x.state == "live"]
        nots = [x for x in self.made if x.state == "not created"]
        minutes = (self.t_end - self.t0) / 60.0
        lines = ["# Lab run %s" % r["name"], "",
                 "Question: %s" % expand(r["question"], self.env, loose=True), "",
                 "- result: %s%s" % (self.outcome, (" (%s)" % self.reason) if self.reason else ""),
                 "- teardown: %s" % ("every resource deleted and proven gone" if not left else
                                     "%d resource(s) LEFT LIVE: %s" % (len(left), ", ".join(x.handle for x in left))),
                 "- project %s, tenant %s, region %s" % (self.project, self.tenant, self.region),
                 "- started %s, took %.1f minutes, %d resource(s) created, %d call(s)%s"
                 % (self.started_at.strftime("%Y-%m-%d %H:%M UTC"), minutes,
                    len([x for x in self.made if x.state != "not created"]), sum(c.polls for c in self.calls),
                    "; %d create(s) proven not made" % len(nots) if nots else ""),
                 "- cost guard: at most %d resource(s) for at most %d minutes; time budget %d minutes plus %d for the "
                 "teardown" % (r["guard"]["resources"], r["guard"]["lifetime_minutes"], r["budget"]["minutes"],
                               r["budget"]["teardown_minutes"]),
                 "", "## Parameters", "", "| parameter | value |", "|---|---|"]
        lines += ["| %s | %s |" % (k, self._clean(k, v)) for k, v in sorted(self.params.items())]
        lines += ["", "## Calls", "",
                  "Every call went through the key service. A wait or a teardown read shows the number of its polls "
                  "and its last answer.", "",
                  "| n | phase | step | method | service | path | status | polls | fields read |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in self.calls:
            fields = "; ".join("%s = %s" % (k, v) for k, v in c.fields.items()) or "-"
            lines.append("| %d | %s | %s | %s | %s | `%s` | %s | %d | %s |" % (
                c.n, c.phase, c.step, c.method, c.service, c.path, c.status, c.polls, fields))
        lines += ["", "## Checks", "", "| check | says | expected | got | result |", "|---|---|---|---|---|"]
        for c, got, result in self.checks:
            lines.append("| %s | %s | %s | %s | %s |" % (c["id"], c["says"], self._expected(c), got, result))
        lines += ["", "## Teardown", "", "| handle | type | state | proof |", "|---|---|---|---|"]
        for x in reversed(self.made):
            lines.append("| %s | %s | %s | %s |" % (x.handle, x.type, x.state, x.proof or "-"))
        if not self.made:
            lines.append("| - | nothing was created | - | - |")
        notes = confirm_notes(r)
        if notes:
            lines += ["", "## To confirm live", "",
                      "Shapes of the recipe that no earlier live call proved; this run is their first evidence.", ""]
            ran = {c.step.split(" ")[0] for c in self.calls} | {"teardown " + x.step for x in self.made}
            for where, note in notes:
                lines.append("- %s (%s): %s" % (where, "ran" if where in ran else "not reached", note))
        kb = self.kb_line(path)
        lines += ["", "## Knowledge base", ""]
        if kb:
            lines += ["Every check passed and nothing is left; the fact can go in as live:", "", "```", kb, "```"]
        elif self.outcome == "check failed":
            lines.append("A check failed. A negative needs two recorded attempts: run the recipe again and add the "
                         "fact with two --tried naming both evidence files.")
        else:
            lines.append("No fact: the run did not finish its checks or left a resource.")
        # every id met in this run as its handle, also one read before its handle existed
        return self._scrub(self.handles.mask("\n".join(lines) + "\n"))

    @staticmethod
    def _scrub(text: str) -> str:
        """The last guard: a line in which the gate still finds a secret, a token or a private key is withheld."""
        from awb import gate

        out = []
        for line in text.splitlines():
            if any(gate.DETECTORS[d](line) for d in ("secret", "token", "private-key")):
                line = "(a line withheld: the gate found a secret class in it)"
            out.append(line)
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- dry run and list


def dry_run_lines(r: dict, params: dict[str, str], project: str = "tcp-xxxx", tenant: str = "ALIAS") -> list[str]:
    env = {"project": project, "tenant": tenant, "region": params.get("region") or "eu-de", "expiry": "<expiry>",
           "expiry8": "<expiry8>", "rand": "<rand>", "name": "%s-lab-<expiry8>-<rand>" % project}
    env.update(params)
    for k, p in r["params"].items():
        env.update((p.get("choices") or {}).get(params[k], {}))
    env.update(r.get("vars") or {})
    out = ["recipe %s: %s" % (r["name"], expand(r["question"], env, loose=True)),
           "cost guard: at most %d resource(s) for at most %d minutes; budget %d minutes plus %d for the teardown"
           % (r["guard"]["resources"], r["guard"]["lifetime_minutes"], r["budget"]["minutes"],
              r["budget"]["teardown_minutes"]),
           "0. grant: GET vpc /v1/{project_id}/vpcs (read key)"]
    needed = secrets_of(r, env)
    if needed:
        out.append("0. secrets: the tenant holds %s in the key service (asked by name before the first create)"
                   % ", ".join(needed))
    disks = disks_of(r, env)
    if disks:
        out.append("0. disk types: GET evs /v2/{project_id}/types (read key); stop before the first create unless "
                   "%s" % "; ".join("%s is listed%s and not sold out%s" % (n, " and offered in %s" % az if az else "",
                                                                         " there" if az else " in any AZ")
                                    for n, az in disks))
    confirm = dict(confirm_notes(r))
    creates = []
    for n, s in enumerate(r["steps"], start=1):
        c = expand(s["call"], env, loose=True)
        line = "%d. %s %s: %s %s %s" % (n, s["do"], s["id"], c["method"], c["service"], c["path"])
        if c.get("query"):
            line += " ?" + "&".join("%s=%s" % kv for kv in sorted(c["query"].items()))
        if s["do"] == "wait":
            u = s["until"]
            how = next(k for k in ("equals", "in", "contains") if k in u)
            line += " until %s %s %s within %d s" % (u["field"], how, json.dumps(u[how]), s["within"])
        out.append(line)
        if c.get("body") is not None:
            out.append("   body: %s" % json.dumps(c["body"], sort_keys=True))
        if s["do"] == "create":
            creates.append(s["id"])
            tags = s["tags"]
            if isinstance(tags, dict):
                t = expand(tags, dict(env, id="<id>"), loose=True)
                out.append("   tags: %s %s %s" % (t["method"], t["service"], t["path"]))
            else:
                out.append("   tags: in the %s" % tags)
            out.append("   row in RESOURCES.md before the call: %s (%s, cost %s) creating, named %s"
                       % (s["resource"]["kind"], s["resource"]["type"], s["resource"]["cost"],
                          expand(s.get("name", "${name}"), env, loose=True)))
            if s.get("find"):
                f = expand(s["find"]["call"], env, loose=True)
                out.append("   find by name when the answer holds no id: %s %s %s%s" % (
                    f["method"], f["service"], f["path"],
                    (" ?" + "&".join("%s=%s" % kv for kv in sorted(f["query"].items()))) if f.get("query") else ""))
            else:
                out.append("   no find by name: a lost answer leaves the row live for the owner")
        for ch in r["checks"]:
            if ch["step"] == s["id"]:
                out.append("   check %s: %s" % (ch["id"], ch["says"]))
        if s["id"] in confirm:
            out.append("   to confirm live: %s" % confirm[s["id"]])
    out.append("teardown, newest first, each proven gone with a read:")
    for sid in reversed(creates):
        t = r["teardown"][sid]
        c = expand(t["call"], dict(env, id="<%s>" % sid), loose=True)
        g = expand(t["gone"]["call"], dict(env, id="<%s>" % sid), loose=True)
        out.append("   %s: %s %s %s, then %s %s %s" % (sid, c["method"], c["service"], c["path"], g["method"],
                                                     g["service"], g["path"]))
        if t.get("confirm"):
            out.append("   to confirm live: %s" % t["confirm"])
    out.append("dry run: no call was made")
    return out


def list_lines() -> list[str]:
    out = []
    folder = recipes_dir()
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            r = load(p)
        except LabError as err:
            out.append("%-22s refused: %s" % (p.stem, err))
            continue
        out.append("%-22s %s" % (r["name"], expand(r["question"], {k: v["default"] for k, v in r["params"].items()}
                                                   | {"region": r["params"].get("region", {}).get("default",
                                                                                                  "eu-de")},
                                                   loose=True)))
        out.append("%-22s guard: at most %d resource(s) for %d minutes; %d step(s) to confirm live"
                   % ("", r["guard"]["resources"], r["guard"]["lifetime_minutes"], len(confirm_notes(r))))
    if not out:
        out.append("no recipe under recipes/")
    return out


# --------------------------------------------------------------------------- preflight and command line


def project_folder(code: str) -> Path:
    """The folder of an active project of the kind project that may reach a tenant; refused otherwise. Where this
    user can read the owner's grants, the grant must be there too."""
    from awb import codes, config, grants, projects, review

    if not codes.is_project_code(code or ""):
        raise LabError("a project code reads like tcp-q7m4")
    p = config.paths()
    try:
        rows = projects.load(p)
    except Exception:
        raise LabError("the project register cannot be read") from None
    row = next((x for x in rows if x.code == code and x.state == "active"), None)
    if row is None:
        raise LabError("%s is not an active project" % code)
    if not projects.tenant_access(row.kind):
        raise LabError("%s is a query: it has no test tenant; the owner switches it with awb projects kind %s project"
                       % (code, code))
    if grants.readable(p) and not grants.granted(p, code):
        raise LabError("%s has no grant of the owner: he runs awb projects kind %s project in his own terminal"
                       % (code, code))
    folder = Path(row.path)
    if not (folder / "SCOPE.md").is_file():
        raise LabError("the folder of %s has no SCOPE.md" % code)
    here = review.find_project()
    if (here / "SCOPE.md").is_file() and here.resolve() != folder.resolve():
        raise LabError("this session works in another project's folder; run the recipe from the folder of %s" % code)
    return folder


def run(name: str, project: str, tenant: str, given: list[str], *, send=None, clock=time.monotonic,
        sleep=time.sleep, now=None, out=print, err=None) -> int:
    from awb import config
    from awb.tcp import sweep

    err = err or (lambda line: print(line, file=sys.stderr))
    try:
        r = load(find(name))
        params = params_of(r, given)
        if not re.match(r"^[a-z][a-z0-9-]{1,30}$", tenant or ""):
            raise LabError("a tenant alias reads like test-1")
        folder = project_folder(project)
        handles = sweep.Handles(sweep.handles_path(config.paths()))
    except (LabError, sweep.SweepError) as e:
        err("awb lab: %s" % e)
        return EXIT_STOPPED
    except Exception as e:      # an OSError of the host and the like: one line and stopped, never a traceback
        err("awb lab: the preflight failed before any call (%s); nothing was created" % type(e).__name__)
        return EXIT_STOPPED
    job = Run(r, params, project, tenant, folder, handles, send=send or _send_default, clock=clock, sleep=sleep,
              now=now, out=out, err=err)
    refusal = job.probe()
    if refusal is not None:
        err("awb lab: the grant check through the key service failed: %s" % refusal)
        return EXIT_STOPPED
    missing = job.missing_secret()
    if missing is not None:
        err("awb lab: %s" % missing)
        return EXIT_STOPPED
    disk = job.disk_check()
    if disk is not None:
        err("awb lab: %s" % disk)
        return EXIT_STOPPED
    out("awb lab: %s on %s for %s: %d step(s), at most %d resources, budget %d minutes"
        % (r["name"], tenant, project, len(r["steps"]), r["guard"]["resources"], r["budget"]["minutes"]))
    code = job.execute()
    path = job.evidence_path()
    path.write_text(job.evidence(path), encoding="utf-8")
    rel = path.relative_to(folder).as_posix()
    for c, got, result in job.checks:
        out("check %s: %s (%s)" % (c["id"], result, c["says"]))
    out("result: %s%s" % (job.outcome, (" (%s)" % job.reason) if job.reason else ""))
    left = [x for x in job.made if x.state == "live"]
    if left:
        for x in left:
            err("awb lab: %s (%s) is LEFT LIVE; its row in RESOURCES.md says what to do" % (x.handle, x.type))
    else:
        out("teardown: %d resource(s) deleted and proven gone" % len(job.made))
    out("evidence: %s" % rel)
    kb = job.kb_line(path)
    if kb:
        out("knowledge base (run it when the fact is worth keeping):")
        out(kb)
    return code


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb lab", description="Lab recipes: a live check on a test tenant in one command.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    sub.add_parser("list", help="the recipes, their question and their cost guard")
    d = sub.add_parser("dry-run", help="the calls a run would make; no call is made")
    d.add_argument("recipe")
    d.add_argument("--param", action="append", default=[], metavar="K=V")
    d.add_argument("--project", default="tcp-xxxx")
    d.add_argument("--tenant", default="ALIAS")
    rn = sub.add_parser("run", help="raise, check, write the evidence and always tear down")
    rn.add_argument("recipe")
    rn.add_argument("--project", required=True)
    rn.add_argument("--tenant", required=True)
    rn.add_argument("--param", action="append", default=[], metavar="K=V")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_STOPPED
    if not args.command:
        ap.print_usage(sys.stderr)
        return EXIT_STOPPED
    if args.command == "list":
        print("\n".join(list_lines()))
        return EXIT_OK
    if args.command == "dry-run":
        try:
            r = load(find(args.recipe))
            print("\n".join(dry_run_lines(r, params_of(r, args.param), args.project, args.tenant)))
        except LabError as e:
            print("awb lab: %s" % e, file=sys.stderr)
            return EXIT_STOPPED
        return EXIT_OK
    return run(args.recipe, args.project, args.tenant, args.param)


if __name__ == "__main__":
    sys.exit(main())
