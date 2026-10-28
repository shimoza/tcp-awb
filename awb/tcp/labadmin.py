"""Lab runs with the admin logins of our two lab tenants (task LR, his decision of 2026-10-28).

    keys.conf:  lab_admin = test-10491 test-10497          the owner's line; then `awb keys unlock`
    pass:       awb/admin/<alias>/domain, user, password   the admin login of each, entered by him once

At `awb keys unlock` the key service loads the admin login of every tenant the lab_admin line names, and only of
test-10491 and test-10497: any other alias in the line refuses the unlock (test-10498, a customer tenant, a read-only
tenant). A session (the work user) then reaches these two tenants with any method on any service, IAM, STS and OBS
included, through the ops `lab_run` and `lab_call` of the call socket, and only inside an open run of its project:

    awb lab run start --tenant T [--note TEXT]     opens a run for the project (one open run per project and tenant)
    awb lab run end [--tenant T]                   deletes what the run made, newest first, and closes it
    awb lab run status                             the open runs and their leftovers

Every `lab_call` names the open run, or it is refused. The service takes the token itself (a password login at IAM:
domain scoped for iam, scoped to the region's project or to the project `scope` names for every other service, a
temporary AK/SK of the admin token for obs), keeps it in memory per tenant and scope, renews it before its expiry,
makes the call and answers it whole: status, headers and body. In the answer the password, the user and the domain
name of the login never appear ([secret], [login]); a token, a temporary AK/SK and a security token are shortened to
8 characters, "..." and the length (awb/labsecrets.py). A token or a temporary credential that an answer of the run
carried is held for the run: a later call may present its shortened form (header X-Auth-Token, or `credential` with
the shortened access key) and the service uses the whole value (the newest one when two share their shortened
form), so a script can test what a federated or assumed identity may do without ever holding it.

A create is recorded with the call that deletes it: a POST whose answer carries the id (or the name) of a new
resource (DELETE PATH/ID; ECS servers by their delete action, a VPC subnet under its VPC, a permanent access key by
its access id), and a PUT or PATCH whose path a read before it did not find (404): DELETE of the same path. A POST of
an action, a token or a search records nothing. A delete of the session that matches a recorded one marks it removed.
`end` runs the deletes that are left in reverse order (at most END_PASSES passes with a pause while some fail and
the time allows), reports created, removed and failed and closes the run; a second `end` tries the failed ones
again. A run open for more than OLD_AFTER seconds or ended with failed deletes shows in `awb owner status` (and the
08:00 mail). The records are kept by awb/labruns.py.

The runs are kept owner side in <vault>/lab-runs/<run>.json (mode 600). The call log of the run is the session's:
awb/labclient.py appends every request and answer to notes/lab-runs/<run>.log of the project, shortened the same way.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from awb import codes, labruns, labsecrets

SETTING = "lab_admin"
ALLOWED = ("test-10491", "test-10497")
"""The two lab tenants of his decision of 2026-10-28. test-10498 and every other tenant stay out."""
ADMIN_PREFIX = "awb/admin"
LOGIN_PARTS = ("domain", "user", "password")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
OLD_AFTER = labruns.OLD_AFTER
DEFAULT_REGIONS = ("eu-de", "eu-nl")
RENEW_MARGIN = 600.0
FALLBACK_LIFE = 3600.0
CRED_SECONDS = 3600
END_PAUSE = 15.0
END_BUDGET = 150.0
END_PASSES = 6
CALL_TIMEOUT = 60.0
MAX_BODY = 256 * 1024
MAX_TEXT = 1024 * 1024
NOTE_MAX = 200
RUN_RE = labruns.RUN_RE
_PROJECT_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_{}.:/=~%@+-]{0,800}$")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z0-9]{2,10}$")
_SCOPE_RE = re.compile(r"^(?:[0-9a-f]{32}|[a-z]{2}-[a-z0-9]{2,10}(?:_[A-Za-z0-9_-]{1,64})?)$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,60}$")
_ID_SEG_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f-]{36}|\{[a-z_]+\})$")
BLOCKED_HEADERS = frozenset({"authorization", "host", "x-security-token", "x-obs-security-token", "x-sdk-date",
                             "date", "content-length", "cookie", "x-subject-token"})
NOT_CREATE = frozenset({"tokens", "securitytokens", "action", "actions", "delete", "publish", "query", "search",
                        "detail", "batch", "count", "validate", "check", "logout"})
"""Last path segments of a POST that never makes a resource of its own."""

NO_RUN_LINE = ("refused: no open lab run of %s on %s: start one with `awb lab run start --tenant %s` (only on his "
               "word)")
TENANT_LINE = "refused: %s is no lab tenant: lab runs reach %s only"
SETTING_LINE = ("refused: keys.conf has no lab_admin line for %s: the owner adds `lab_admin = %s` and runs awb keys "
                "unlock")
LOGIN_LINE = "refused: the admin login of %s is not loaded: the owner runs awb keys unlock"
OPEN_LINE = "refused: run %s of %s on %s is open already: awb lab run end --tenant %s first"


class LabAdminError(Exception):
    """A step of a lab run that cannot run. The message names aliases, codes, run ids and HTTP codes, never a
    value."""


# --------------------------------------------------------------------------- the line of keys.conf


def parse(value: str) -> list[str] | None:
    """The aliases of the lab_admin line (separated by blanks or commas), or None when one is not allowed."""
    found = [a for a in re.split(r"[\s,]+", value.strip()) if a]
    if len(set(found)) != len(found) or not all(a in ALLOWED for a in found):
        return None
    return found


def of_settings(settings: dict) -> list[str]:
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return (parse(str(value)) or []) if value is not None else []


def load_logins(aliases: list[str], reader) -> dict[str, dict]:
    """{alias: {domain, user, password}} from the entries awb/admin/<alias>/<part>; `reader` takes the entry below
    awb/admin. A missing part refuses by its entry name, never its value."""
    out = {}
    for alias in aliases:
        login = {}
        for part in LOGIN_PARTS:
            try:
                login[part] = reader("%s/%s" % (alias, part))
            except Exception:
                raise LabAdminError("the admin login of %s lacks %s/%s/%s in pass" % (alias, ADMIN_PREFIX, alias, part)) \
                    from None
        out[alias] = login
    return out


def only_allowed(tenants: dict, settings: dict, readonly_aliases=()) -> dict:
    """The tenants with an admin login kept only for an alias of the lab_admin line that is no read-only tenant."""
    keep = set(of_settings(settings)) - set(readonly_aliases)
    out = {}
    for alias, t in tenants.items():
        if isinstance(t, dict) and "admin" in t and alias not in keep:
            t = {k: v for k, v in t.items() if k != "admin"}
        out[alias] = t
    return out


# --------------------------------------------------------------------------- tokens and held values (memory)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry(text) -> float:
    if isinstance(text, str):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is not None:
                return when.timestamp() - time.time()
        except ValueError:
            pass
    return FALLBACK_LIFE


class State:
    """The admin tokens and temporary credentials by (alias, scope), and the values each run's answers carried, in
    memory only; never handed over in a reload, forgotten at lock, load and stop."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.tokens: dict[tuple[str, str], tuple[object, dict, float]] = {}
        self.held: dict[str, dict[str, tuple[str, object]]] = {}

    def get(self, key: tuple[str, str], make):
        with self.lock:
            got = self.tokens.get(key)
            if got and got[2] - self.clock() > RENEW_MARGIN:
                return got[0], got[1]
            value, info, life = make()
            self.tokens[key] = (value, info, self.clock() + max(0.0, life))
            return value, info

    def drop(self, key: tuple[str, str]) -> None:
        with self.lock:
            self.tokens.pop(key, None)

    def hold(self, run: str, kind: str, full, shown: str) -> None:
        with self.lock:
            self.held.setdefault(run, {})[shown] = (kind, full)

    def find(self, run: str, kind: str, shown) -> object | None:
        with self.lock:
            got = self.held.get(run, {}).get(shown) if isinstance(shown, str) else None
            return got[1] if got and got[0] == kind else None

    def forget(self, run: str) -> None:
        with self.lock:
            self.held.pop(run, None)

    def clear(self) -> None:
        with self.lock:
            self.tokens = {}
            self.held = {}

    def values(self) -> list[str]:
        """Every token, key and security token held, for the masks of every answer of the service."""
        out: list[str] = []
        with self.lock:
            items = [v for v, _, _ in self.tokens.values()] + [v for h in self.held.values() for _, v in h.values()]
        for v in items:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, (tuple, list)):
                out.extend(x for x in v if isinstance(x, str))
        return [v for v in out if v]


def iam_base(region: str, endpoint: str | None) -> str:
    return endpoint.rstrip("/") if endpoint else "https://iam.%s.otc.t-systems.com" % region


def _http(method: str, url: str, headers: dict, data: bytes | None, timeout: float = CALL_TIMEOUT
          ) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(16 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as err:
        try:
            raw = err.read(16 * 1024 * 1024 + 1)
        except OSError:
            raw = b""
        return err.code, dict(err.headers or {}), raw
    except (urllib.error.URLError, OSError) as err:
        raise LabAdminError("no answer from %s (%s)" % (urllib.parse.urlsplit(url).netloc.split(".")[0] or "the API",
                                                        type(err).__name__)) from None


def fetch_token(login: dict, region: str, scope: str, endpoint: str | None = None) -> tuple[str, dict, float]:
    """(token, {project_id, domain_id}, seconds) of a password login: `scope` "domain", a project name or a project
    id. The password goes into the body of this one request only; an error names the status and the IAM code."""
    from awb.tcp import readonly

    if scope == "domain":
        sc = {"domain": {"name": login["domain"]}}
    elif re.match(r"^[0-9a-f]{32}$", scope):
        sc = {"project": {"id": scope}}
    else:
        sc = {"project": {"name": scope, "domain": {"name": login["domain"]}}}
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": login["user"], "password": login["password"], "domain": {"name": login["domain"]}}}}, "scope": sc}}
    data = json.dumps(body).encode("utf-8")
    body = None
    status, headers, raw = _http("POST", iam_base(region, endpoint) + "/v3/auth/tokens?nocatalog=true",
                                 {"Content-Type": "application/json;charset=utf8"}, data, timeout=30.0)
    data = None
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    token = {k.lower(): v for k, v in headers.items()}.get("x-subject-token", "")
    tok = parsed.get("token") if isinstance(parsed.get("token"), dict) else None
    if status != 201 or not token or tok is None:
        raise LabAdminError(readonly.refusal(status, parsed, raw or b""))
    project = tok.get("project") if isinstance(tok.get("project"), dict) else {}
    domain = tok.get("domain") if isinstance(tok.get("domain"), dict) else (project.get("domain") or {})
    info = {"project_id": project.get("id") if isinstance(project.get("id"), str) else None,
            "domain_id": domain.get("id") if isinstance(domain.get("id"), str) else None}
    return token, info, _expiry(tok.get("expires_at"))


# --------------------------------------------------------------------------- the runs (owner side, on disk)

# the records live in the core module awb/labruns.py, which the owner status reads too
runs_dir, save, load_runs, open_run = labruns.runs_dir, labruns.save, labruns.load_runs, labruns.open_run
leftovers, new_id, attention = labruns.leftovers, labruns.new_id, labruns.attention


# --------------------------------------------------------------------------- what a call creates


def _label(service: str, segs: list[str], put: bool) -> str:
    if service == "obs":
        return "obs bucket" if len(segs) == 1 else "obs object"
    words = [s for s in (segs[:-1] if put and len(segs) > 1 else segs) if not _ID_SEG_RE.match(s)]
    return "%s %s" % (service, words[-1] if words else "resource")


def _new_id(data) -> tuple[str | None, str | None]:
    """(id, name) of the new resource in an answer: top level or under its one key ({"user": {"id": ...}})."""
    if not isinstance(data, dict):
        return None, None
    inner = data
    if not isinstance(data.get("id"), (str, int)):
        dicts = [v for v in data.values() if isinstance(v, dict)]
        inner = dicts[0] if len(dicts) == 1 else {}
    rid, name = inner.get("id"), inner.get("name")
    rid = str(rid) if isinstance(rid, (str, int)) and str(rid) and re.match(r"^[A-Za-z0-9_.:-]{1,128}$", str(rid)) \
        else None
    name = name if isinstance(name, str) and re.match(r"^[A-Za-z0-9_.-]{1,128}$", name) else None
    return rid, name


def creates(method: str, service: str, path: str, query: dict, status: int, data, region: str,
            absent: bool = False) -> list[dict]:
    """The resources a call of the run made, each with the call that deletes it ([] for none). `path` is the
    expanded path; `absent` says that a read of the path before a PUT or PATCH answered 404."""
    if not 200 <= status < 300:
        return []
    segs = [s for s in path.strip("/").split("/") if s]
    if method in ("PUT", "PATCH"):
        if not absent or not segs:
            return []
        return [{"label": _label(service, segs, True), "name": segs[-1][:128],
                 "delete": {"method": "DELETE", "service": service, "path": path, "query": dict(query or {}),
                            "region": region}}]
    if method != "POST" or service == "obs" or not segs:
        return []
    last = segs[-1]
    if last.lower() in NOT_CREATE or last.endswith("action") or "auth" in [s.lower() for s in segs] \
            or "OS-AUTH" in segs:
        return []
    base = "/" + "/".join(segs)
    data = data if isinstance(data, dict) else {}

    def item(label, name, delete):
        return {"label": label, "name": name, "delete": dict(delete, region=region)}

    if service == "ecs" and len(segs) == 3 and segs[0] in ("v1", "v1.1") and last == "cloudservers":
        ids = data.get("serverIds")
        if not isinstance(ids, list):
            ids = [s.get("id") for s in data.get("servers") or [] if isinstance(s, dict)]
        ids = [i for i in ids if isinstance(i, str) and re.match(r"^[A-Za-z0-9-]{1,64}$", i)]
        return [item("ecs cloudservers", i, {"method": "POST", "service": "ecs",
                                             "path": "/v1/%s/cloudservers/delete" % segs[1],
                                             "body": {"servers": [{"id": i}], "delete_publicip": True,
                                                      "delete_volume": True}}) for i in ids]
    if service == "iam" and base.endswith("/OS-CREDENTIAL/credentials"):
        access = (data.get("credential") or {}).get("access") if isinstance(data.get("credential"), dict) else None
        if isinstance(access, str) and re.match(r"^[A-Za-z0-9]{8,64}$", access):
            return [item("iam credentials", labsecrets.short(access),
                         {"method": "DELETE", "service": "iam",
                          "path": "/v3.0/OS-CREDENTIAL/credentials/%s" % access})]
        return []
    rid, name = _new_id(data)
    if service == "iam" and base == "/v3.0/OS-USER/users" and rid:
        return [item("iam users", name or rid, {"method": "DELETE", "service": "iam", "path": "/v3/users/%s" % rid})]
    if service == "vpc" and len(segs) == 3 and segs[0] == "v1" and last == "subnets" and rid:
        vpc = (data.get("subnet") or {}).get("vpc_id") if isinstance(data.get("subnet"), dict) else None
        if isinstance(vpc, str) and re.match(r"^[A-Za-z0-9-]{1,64}$", vpc):
            return [item("vpc subnets", name or rid, {"method": "DELETE", "service": "vpc",
                                                      "path": "/v1/%s/vpcs/%s/subnets/%s" % (segs[1], vpc, rid)})]
    if rid or name:
        return [item(_label(service, segs, False), name or rid,
                     {"method": "DELETE", "service": service, "path": "%s/%s" % (base, rid or name)})]
    return []


def matches_delete(entry: dict, method: str, service: str, path: str, query: dict, body) -> bool:
    """True when a call of the session is the recorded delete of `entry` (it removed the resource itself)."""
    d = entry.get("delete") or {}
    if d.get("method") != method or d.get("service") != service:
        return False
    if d.get("path") == path and (d.get("query") or {}) == (query or {}):
        return True
    if method == "POST" and path == d.get("path") and isinstance(body, dict):
        want = {s.get("id") for s in (d.get("body") or {}).get("servers", [])}
        got = {s.get("id") for s in body.get("servers") or [] if isinstance(s, dict)}
        return bool(want) and want <= got
    return False


# --------------------------------------------------------------------------- shortening the answer


def _walk(obj, temporary: bool):
    if isinstance(obj, dict):
        return {k: _one(k, v, temporary) if isinstance(v, str) else _walk(v, temporary) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_one("value", v, temporary) if isinstance(v, str) else _walk(v, temporary) for v in obj]
    return obj


def _one(key, value: str, temporary: bool) -> str:
    snippet = {str(key): value}
    if temporary:
        snippet["security_token"] = ""
    text, counts = labsecrets.shorten(json.dumps(snippet, ensure_ascii=False))
    if not counts:
        return value
    try:
        got = json.loads(text)
        return got[str(key)] if isinstance(got.get(str(key)), str) else labsecrets.SECRET
    except (ValueError, AttributeError, KeyError):
        return labsecrets.SECRET


def shorten_answer(answer: dict, login: dict, held: list[str], drawn: list[str] = ()) -> dict:
    """The answer with the login masked ([secret], [login]), every held token or key shortened and the rest as
    awb/labsecrets.py shortens a log, field by field so that the answer stays JSON."""
    text = json.dumps(answer, ensure_ascii=False)
    for value in sorted({v for v in held if isinstance(v, str) and len(v) >= 8}, key=len, reverse=True):
        for form in (value, json.dumps(value)[1:-1]):
            text = text.replace(form, labsecrets.short(value))
    for value in drawn:
        text = text.replace(json.dumps(value)[1:-1], labsecrets.SECRET)
    for part, mark in (("password", labsecrets.SECRET), ("user", labsecrets.LOGIN), ("domain", labsecrets.LOGIN)):
        value = login.get(part)
        if isinstance(value, str) and len(value) >= 3:
            text = re.sub(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(json.dumps(value)[1:-1]), mark, text)
    answer = json.loads(text)
    temporary = bool(labsecrets._TEMP_HINT_RE.search(text))
    if labsecrets.shorten(text)[1]:
        answer = _walk(answer, temporary)
    return answer


def _temporaries(headers: dict, data) -> tuple[list[str], list[tuple[str, str, str]]]:
    """The tokens (X-Subject-Token) and temporary credentials (access, secret, securitytoken) of an answer."""
    tokens = [v for k, v in headers.items() if k.lower() == "x-subject-token" and isinstance(v, str) and v]
    creds = []
    cred = data.get("credential") if isinstance(data, dict) else None
    if isinstance(cred, dict) and all(isinstance(cred.get(k), str) and cred.get(k)
                                      for k in ("access", "secret", "securitytoken")):
        creds.append((cred["access"], cred["secret"], cred["securitytoken"]))
    return tokens, creds


# --------------------------------------------------------------------------- the key service's side


def _refused(msg: str):
    from awb.tcp import keys

    return keys.Refused(msg)


def _project(svc, req: dict, uid: int, caller) -> str:
    """The project of the call: a session only from the folder of an active project of the kind project that the
    owner granted (P0, K4), never the console; the owner names an active project."""
    from awb import projects as _projects
    from awb.tcp import keys

    project = req.get("project") or ""
    if uid != os.getuid() and uid == svc._console_uid():
        raise _refused("refused: the console runs no lab run")
    if uid != os.getuid():
        if caller is not keys._NOT_ASKED and caller != project:
            raise svc._unbound(project, caller, "a lab run runs from the folder of an active project")
        row = svc._project_row(project)
        if row is None:
            raise _refused("a lab run runs from the folder of an active project")
        if not _projects.tenant_access(row.kind) or not svc._granted(project):
            raise _refused("%s has no grant of the owner: he runs awb projects kind %s project in his own terminal"
                           % (project, project))
        if svc._customer_differs(row):
            from awb.tcp import xchg

            raise _refused(xchg.DIFFERS_LINE % project)
    if not (isinstance(project, str) and _PROJECT_RE.match(project) and svc._active_project(project)):
        raise _refused("a lab run needs the code of an active project")
    return project


def _tenant(svc, alias) -> tuple[dict, dict]:
    if not isinstance(alias, str) or alias not in ALLOWED:
        raise _refused(TENANT_LINE % (alias if isinstance(alias, str) and re.match(r"^[a-z0-9-]{1,32}$", alias)
                                      else "that tenant", " and ".join(ALLOWED)))
    if svc._readonly(alias) is not None:
        raise _refused(TENANT_LINE % (alias, " and ".join(ALLOWED)))
    with svc.lock:
        if not svc.tenants:
            raise _refused("the key service is locked: the owner runs awb keys unlock")
        listed = alias in of_settings(svc.settings)
        tenant = svc.tenants.get(alias) or {}
    if not listed:
        raise _refused(SETTING_LINE % (alias, " ".join(ALLOWED)))
    login = tenant.get("admin")
    if not isinstance(login, dict) or set(login) != set(LOGIN_PARTS):
        raise _refused(LOGIN_LINE % alias)
    return tenant, login


def _regions(svc, alias: str) -> tuple[str, ...]:
    return tuple(svc._registered().get(alias) or DEFAULT_REGIONS)


class _Auth:
    """The tokens of one tenant's admin login through the service's State."""

    def __init__(self, svc, alias: str, login: dict, region: str):
        self.svc, self.alias, self.login, self.region = svc, alias, login, region

    def token(self, scope: str) -> tuple[str, dict]:
        return self.svc.lab.get((self.alias, "%s@%s" % (scope, self.region) if scope == "domain" else scope),
                                lambda: fetch_token(self.login, self.region, scope, self.svc.endpoint))

    def drop(self, scope: str) -> None:
        self.svc.lab.drop((self.alias, "%s@%s" % (scope, self.region) if scope == "domain" else scope))

    def credential(self) -> tuple[str, str, str]:
        def make():
            token, _ = self.token("domain")
            body = {"auth": {"identity": {"methods": ["token"], "token": {"id": token,
                                                                          "duration_seconds": CRED_SECONDS}}}}
            status, _h, raw = _http("POST", self._iam() + "/v3.0/OS-CREDENTIAL/securitytokens",
                                    {"Content-Type": "application/json;charset=utf8", "X-Auth-Token": token},
                                    json.dumps(body).encode("utf-8"), timeout=30.0)
            try:
                cred = (json.loads(raw) or {}).get("credential")
            except (ValueError, AttributeError):
                cred = None
            if status != 201 or not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred.get(k)
                                                                      for k in ("access", "secret", "securitytoken")):
                raise LabAdminError("the temporary key for OBS failed: IAM answered HTTP %d" % status)
            return (cred["access"], cred["secret"], cred["securitytoken"]), {}, _expiry(cred.get("expires_at"))

        value, _ = self.svc.lab.get((self.alias, "obs@%s" % self.region), make)
        return value

    def _iam(self) -> str:
        return iam_base(self.region, self.svc.endpoint)


def _base(svc, service: str, region: str) -> str:
    return svc.endpoint.rstrip("/") if svc.endpoint else "https://%s.%s.otc.t-systems.com" % (service, region)


def _send(svc, auth: _Auth, run: str, method: str, service: str, path: str, query: dict, body, headers: dict,
          scope: str | None, held_token: str | None, held_cred) -> tuple[int, dict, bytes, str]:
    """One call with the admin login (or a held token or credential of the run). Returns (status, headers, raw,
    the expanded path); a 401 of an admin token takes a new one once."""
    from awb import obs, sign
    from awb.tcp import cloud

    for attempt in (0, 1):
        tscope = "domain" if service == "iam" else (scope or auth.region)
        expanded = path
        if "{project_id}" in expanded:
            _, info = auth.token(scope or auth.region)
            expanded = expanded.replace("{project_id}", info.get("project_id") or "")
        if "{domain_id}" in expanded:
            _, info = auth.token("domain")
            expanded = expanded.replace("{domain_id}", info.get("domain_id") or "")
        hdrs = dict(headers)
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
            hdrs.setdefault("Content-Type", "application/octet-stream" if service == "obs" else "application/json")
        else:
            data = None
            if service != "obs":
                hdrs.setdefault("Content-Type", "application/json")
        if service == "obs":
            ak, sk, st = held_cred or auth.credential()
            client, key, sub, url = cloud.Client(obs.Keys(ak, sk), auth.region, endpoint=svc.endpoint).obs_call(
                expanded, query)
            hdrs["x-obs-security-token"] = st
            client.sign(method, key, hdrs, sub)
        else:
            url = _base(svc, service, auth.region) + expanded
            q = urllib.parse.urlencode(query or {}, doseq=True)
            url += "?" + q if q else ""
            if held_cred:
                hdrs["X-Security-Token"] = held_cred[2]
                hdrs = sign.sign(method, url, hdrs, data or b"", held_cred[0], held_cred[1])
            elif held_token:
                hdrs["X-Auth-Token"] = held_token
            else:
                hdrs["X-Auth-Token"] = auth.token(tscope)[0]
        status, rh, raw = _http(method, url, hdrs, data)
        if status == 401 and attempt == 0 and not held_token and not held_cred:
            if service == "obs":
                auth.svc.lab.drop((auth.alias, "obs@%s" % auth.region))
            auth.drop(tscope)
            continue
        return status, rh, raw, expanded
    return status, rh, raw, expanded


def _parse(service: str, status: int, raw: bytes) -> tuple[object, str, bool]:
    from awb import obs

    if not raw:
        return None, "", False
    try:
        return json.loads(raw), "", False
    except ValueError:
        pass
    if service == "obs":
        if status < 300:
            found = obs.xml_result(raw)
            if found is not None:
                return found, "", False
        else:
            code, msg = obs.xml_error(raw[:4096])
            if code or msg:
                return {"code": code, "message": msg}, raw[:MAX_TEXT].decode("utf-8", "replace"), len(raw) > MAX_TEXT
    return None, raw[:MAX_TEXT].decode("utf-8", "replace"), len(raw) > MAX_TEXT


def _check_call(req: dict) -> tuple[str, str, str, dict, object, dict]:
    method = str(req.get("method") or "GET").upper()
    service, path = req.get("service"), req.get("path")
    if method not in METHODS:
        raise _refused("a method is one of %s" % ", ".join(METHODS))
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        raise _refused("a service name reads like ecs or vpc")
    if not isinstance(path, str) or not _PATH_RE.match(path) or ".." in path or "//" in path:
        raise _refused("a path is plain: /v3/users or /v1/{project_id}/vpcs")
    query = req.get("query") or {}
    if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, (str, list)) for k, v in query.items()):
        raise _refused("a query is an object of strings")
    body = req.get("body")
    if body is not None and (not isinstance(body, (dict, list, str)) or len(json.dumps(body)) > MAX_BODY):
        raise _refused("a body is JSON or a text of at most %d bytes" % MAX_BODY)
    headers = req.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and _HEADER_RE.match(k) and isinstance(v, str)
                                                and len(v) < 8192 and "\n" not in v for k, v in headers.items()):
        raise _refused("headers are an object of plain names and one-line values")
    return method, service, path, query, body, headers


def serve_call(svc, req: dict, uid: int, caller) -> dict:
    """The op lab_call: one call of a session inside its open run."""
    from awb.tcp import keys

    project = _project(svc, req, uid, caller)
    alias = req.get("tenant")
    tenant, login = _tenant(svc, alias)
    method, service, path, query, body, headers = _check_call(req)
    p = svc.paths_fn()
    with svc.lab.lock:
        run = open_run(p, project, alias)
    if run is None or req.get("run") != run["id"]:
        raise _refused(NO_RUN_LINE % (project, alias, alias))
    regions = _regions(svc, alias)
    region = req.get("region") or regions[0]
    if region not in regions:
        raise _refused("the region is not one of the tenant's")
    scope = req.get("scope")
    if scope is not None and (not isinstance(scope, str) or not _SCOPE_RE.match(scope)):
        raise _refused("a scope is a project name (eu-de, eu-de_<suffix>) or its id")
    held_token = held_cred = None
    extra = {}
    for k, v in headers.items():
        low = k.lower()
        if low == "x-auth-token":
            held_token = svc.lab.find(run["id"], "token", v)
            if held_token is None:
                raise _refused("refused: X-Auth-Token takes only a token an answer of this run carried, in its "
                               "shortened form")
        elif low in BLOCKED_HEADERS or low.startswith("x-obs-security"):
            raise _refused("refused: the header %s is set by the key service" % k)
        else:
            extra[k] = v
    if req.get("credential") is not None:
        held_cred = svc.lab.find(run["id"], "cred", req.get("credential"))
        if held_cred is None:
            raise _refused("refused: credential takes only the shortened access key of a temporary key this run got")
    drawn: list[str] = []
    if isinstance(body, (dict, list)):
        body = keys.fill(body, tenant.get("secrets", {}), drawn=drawn)
    auth = _Auth(svc, alias, login, region)
    absent = False
    try:
        if method in ("PUT", "PATCH") and not held_token and not held_cred:
            st, _h, _r, _e = _send(svc, auth, run["id"], "HEAD" if service == "obs" else "GET", service, path,
                                   query, None, {}, scope, None, None)
            absent = st == 404
        status, rh, raw, expanded = _send(svc, auth, run["id"], method, service, path, query, body, extra, scope,
                                          held_token, held_cred)
    except LabAdminError as err:
        svc._log(uid, alias, "admin", method, service, region, "failed", project)
        raise _refused(str(err)) from None
    data, text, cut = _parse(service, status, raw)
    tokens, creds = _temporaries(rh, data)
    with svc.lab.lock:
        for t in tokens:
            svc.lab.hold(run["id"], "token", t, labsecrets.short(t))
        for c in creds:
            svc.lab.hold(run["id"], "cred", c, labsecrets.short(c[0]))
        run = open_run(p, project, alias) or run
        made = creates(method, service, expanded, query, status, data, region, absent)
        n = len(run.get("created", []))
        for i, m in enumerate(made, start=1):
            run.setdefault("created", []).append(dict(m, n=n + i, state="live", at=_now()))
        if method in ("DELETE", "POST") and (200 <= status < 300 or status == 404):
            for c in run.get("created", []):
                if c.get("state") in ("live", "failed") and matches_delete(c, method, service, expanded, query, body):
                    c["state"] = "removed"
        run["calls"] = int(run.get("calls") or 0) + 1
        save(p, run)
    svc._log(uid, alias, "admin", method, service, region, status, project)
    answer = {"ok": True, "run": run["id"], "status": status, "headers": rh, "data": data, "text": text, "cut": cut,
              "path": expanded, "created": [m["label"] for m in made]}
    return shorten_answer(answer, login, svc.lab.values() + [v for c in creds for v in c] + tokens, drawn)


def _end(svc, run: dict, alias: str, login: dict, p) -> dict:
    """The deletes of what is left, newest first, in passes while some fail and the time allows."""
    deadline = time.monotonic() + END_BUDGET
    calls = []
    for n in range(END_PASSES):
        failed = 0
        for c in sorted(leftovers(run), key=lambda c: -int(c.get("n") or 0)):
            d = c["delete"]
            auth = _Auth(svc, alias, login, d.get("region") or _regions(svc, alias)[0])
            try:
                status, _h, raw, _e = _send(svc, auth, run["id"], d["method"], d["service"], d["path"],
                                            d.get("query") or {}, d.get("body"), {}, None, None, None)
            except LabAdminError as err:
                status, raw = 0, str(err).encode()
            ok = 200 <= status < 300 or status == 404
            c["state"] = "removed" if ok else "failed"
            if not ok:
                failed += 1
                data, _t, _c = _parse(d["service"], status, raw)
                err = data.get("error") if isinstance(data, dict) else None
                code = (err.get("code") if isinstance(err, dict) else None) or (
                    data.get("error_code") or data.get("code") if isinstance(data, dict) else None)
                c["error"] = "HTTP %d%s" % (status, " %s" % code if isinstance(code, str) and
                                            re.match(r"^[A-Za-z0-9._]{1,40}$", code) else "")
            else:
                c.pop("error", None)
            calls.append({"method": d["method"], "service": d["service"], "path": d["path"], "status": status,
                          "label": c["label"]})
        save(p, run)
        if not failed or n == END_PASSES - 1 or time.monotonic() + END_PAUSE > deadline:
            break
        time.sleep(END_PAUSE)
    return {"calls": calls}


def serve_run(svc, req: dict, uid: int, caller) -> dict:
    """The op lab_run: start, end or status of the runs of a project."""
    action = req.get("action")
    if action not in ("start", "end", "status"):
        raise _refused("a lab run action is start, end or status")
    p = svc.paths_fn()
    if action == "status":
        if uid == os.getuid() and not req.get("project"):
            project = None
        else:
            project = _project(svc, req, uid, caller)
        rows = []
        for run in load_runs(p):
            if project is not None and run.get("project") != project:
                continue
            left = leftovers(run)
            if run.get("state") == "open" or left:
                rows.append({"run": run["id"], "project": run["project"], "tenant": run["tenant"],
                             "state": run["state"], "started": run.get("started"), "calls": run.get("calls", 0),
                             "minutes": int((time.time() - float(run.get("started_ts") or time.time())) // 60),
                             "left": [{"label": c["label"], "name": c.get("name"), "state": c["state"],
                                       "error": c.get("error")} for c in left]})
        return {"ok": True, "runs": rows}
    project = _project(svc, req, uid, caller)
    alias = req.get("tenant")
    _tenant_, login = _tenant(svc, alias)
    with svc.lab.lock:
        run = open_run(p, project, alias)
        if action == "start":
            if run is not None:
                raise _refused(OPEN_LINE % (run["id"], project, alias, alias))
            note = req.get("note") or ""
            note = re.sub(r"[\x00-\x1f\x7f]+", " ", note)[:NOTE_MAX] if isinstance(note, str) else ""
            now = time.time()
            run = {"id": new_id(), "project": project, "tenant": alias, "note": note, "state": "open",
                   "started": _now(), "started_ts": now, "calls": 0, "created": []}
            save(p, run)
            svc._log(uid, alias, "admin", "START", "lab-run", "-", "ok", project)
            return {"ok": True, "run": run["id"], "tenant": alias, "project": project, "started": run["started"]}
        if run is None:
            named = req.get("run")
            run = next((r for r in load_runs(p) if r.get("project") == project and r.get("tenant") == alias
                        and r.get("state") == "ended" and any(c.get("state") == "failed" for c in leftovers(r))
                        and (named in (None, r["id"]))), None)
            if run is None:
                raise _refused("refused: no open lab run of %s on %s and none with failed deletes" % (project, alias))
    done = _end(svc, run, alias, login, p)
    with svc.lab.lock:
        run["state"] = "ended"
        run["ended"] = _now()
        save(p, run)
        svc.lab.forget(run["id"])
    created = run.get("created", [])
    svc._log(uid, alias, "admin", "END", "lab-run", "-", "%d/%d" % (
        sum(1 for c in created if c["state"] == "removed"), len(created)), project)
    answer = {"ok": True, "run": run["id"], "tenant": alias, "project": project, "created": len(created),
              "removed": [{"label": c["label"], "name": c.get("name")} for c in created if c["state"] == "removed"],
              "failed": [{"label": c["label"], "name": c.get("name"), "error": c.get("error"),
                          "delete": "%s %s %s" % (c["delete"]["method"], c["delete"]["service"], c["delete"]["path"])}
                         for c in created if c["state"] == "failed"],
              "calls": done["calls"]}
    return shorten_answer(answer, login, svc.lab.values())
