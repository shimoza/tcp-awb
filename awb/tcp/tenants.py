"""Resources on the test tenants, tracked over time (T-63).

    awb tenant setup ALIAS --domain-id ID [--region R] [--lab-key | --lab-user] [--alerts [--topic URN]]
                                                                        owner side: a new tenant in one command
    awb tenant add ALIAS --keys pass:ENTRY|file:PATH [--region R]...   owner side: a tenant and its read-only key
    awb tenant snapshot [ALIAS]...                                      owner side: list the tenant, GET only
    awb tenant list                                                     every tenant, what runs there now
    awb tenant now ALIAS                                                what runs there now, oldest first
    awb tenant history ALIAS [--project CODE] [--since YYYY-MM-DD]      what appeared, changed and went, when
    awb tenant at ALIAS YYYY-MM-DD                                      what ran there on a day
    awb tenant project CODE                                             what a project runs and ran, on every tenant

A snapshot lists the servers, disks and elastic IPs of every region of the tenant (the sources of the sweep,
awb/tcp/sweep.py) and keeps them in `<shared>/tenants/<alias>/snapshots/<date>.json`. Each resource is kept as a
neutral handle (`ecs-3`), its kind, region, creation time, state, size and its two tags: `awb-project` (the project
code) and `awb-expiry`. Names and ids are never kept in the snapshot; the ids stay in the tenant's handle file
(mode 600, owner only). A snapshot is compared with the one before it and the difference goes to
`<shared>/tenants/<alias>/events.jsonl`, one line per event, never rewritten: `seen` (in the first snapshot, with its
own creation time), `appeared`, `changed` (state, project, size) and `gone`. A listing that could not be read is
recorded as unknown for that kind and region and never read as empty: nothing is reported gone because of it.

The key of a tenant is a read-only key (list rights only) so that a daily snapshot runs without anyone: `file:PATH`
names a file of the owner (mode 600) with `ak=` and `sk=` lines, `pass:ENTRY` the entries `ENTRY/ak` and `ENTRY/sk`
of the password store. The work user never gets a key: it reads what the snapshots recorded.

`setup` (T10) does it all from the admin login that the owner put into the password store once
(`awb/admin/ALIAS/domain`, `user`, `password`): a domain-scoped password token, the group awb-read with Tenant Guest
on all projects, the user awb-read-<number of the alias> with programmatic access in it, its key written to
`awb/tenant/ALIAS/ak|sk` through `pass insert -m` (standard input), with --lab-key a key of the admin user to
`awb/tenant/ALIAS/lab/ak|sk`, with --alerts the CTS alert awb_new_access_key to the SMN topic of keys.conf, then
`add`, `awb keys unlock` and the first snapshot. Only what is missing is made; a second run says nothing to do.
It prints names and counts, never a value.

The history is kept for good. The tenants are named by aliases (test-1), never by their ids: a tenant id is an
identifier the commit gate refuses.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from awb import config, jobs, obs
from awb.tcp import sweep as _sweep

TENANTS_DIR = "tenants"
REGISTER = "tenants.tsv"
EVENTS = "events.jsonl"
SNAPSHOTS = "snapshots"
HANDLES = "handles.json"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROJECT_TAG_RE = re.compile(r"tcp-[a-z0-9]{4}")          # as tenant_api.project_tag of the console
_CODE_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
FIELDS = ("alias", "regions", "keys", "added")
PASS_REF, FILE_REF = "pass" + ":", "file" + ":"
"""The two ways a tenant names its read-only key."""


class TenantError(Exception):
    """A tenant problem. The message names aliases, kinds and counts, never a key or an id."""


@dataclass
class Tenant:
    alias: str
    regions: tuple[str, ...]
    keys: str
    added: str


def root(p: config.Paths) -> Path:
    return p.shared / TENANTS_DIR


def folder(p: config.Paths, alias: str) -> Path:
    return root(p) / alias


# --------------------------------------------------------------------------- the register of tenants


def load(p: config.Paths) -> list[Tenant]:
    try:
        lines = (root(p) / REGISTER).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) == len(FIELDS) and _ALIAS_RE.match(cols[0]):
            out.append(Tenant(cols[0], tuple(r for r in cols[1].split(",") if r), cols[2], cols[3]))
    return out


def get(p: config.Paths, alias: str) -> Tenant:
    for t in load(p):
        if t.alias == alias:
            return t
    raise TenantError("no tenant with the alias %s (awb tenant list)" % alias)


def add(p: config.Paths, alias: str, keys: str, regions: list[str], today: str | None = None) -> Tenant:
    if not _ALIAS_RE.match(alias or ""):
        raise TenantError("an alias reads like test-1: small letters, digits and dashes")
    if not keys.startswith((PASS_REF, FILE_REF)) or len(keys) < 6:
        raise TenantError("the key reads pass:ENTRY or file:PATH")
    from awb.tcp.cloud import check_region

    regions = [check_region(r) for r in (regions or ["eu-de"])]
    if any(t.alias == alias for t in load(p)):
        raise TenantError("the alias %s is taken" % alias)
    t = Tenant(alias, tuple(regions), keys, today or datetime.date.today().isoformat())
    config.make_dir(root(p), 0o770, shared=True)
    reg = root(p) / REGISTER
    new = not reg.exists()
    with open(reg, "a", encoding="utf-8") as fh:
        if new:
            fh.write("\t".join(FIELDS) + "\n")
        fh.write("\t".join([t.alias, ",".join(t.regions), t.keys, t.added]) + "\n")
    return t


def keys_of(t: Tenant) -> obs.Keys:
    if t.keys.startswith(PASS_REF):
        try:
            return obs.keys_from_reference(t.keys)
        except obs.OBSError as err:
            raise TenantError("the key of %s: %s" % (t.alias, err)) from None
    path = Path(os.path.expanduser(t.keys[5:]))
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise TenantError("the key file of %s cannot be read" % t.alias) from None
    if st.st_mode & 0o077:
        raise TenantError("the key file of %s is open to others: chmod 600 it" % t.alias)
    vals = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    ak, sk = vals.get("ak", "").strip(), vals.get("sk", "").strip()
    if not ak or not sk:
        raise TenantError("the key file of %s needs an ak= and an sk= line" % t.alias)
    return obs.Keys(ak, sk)


# --------------------------------------------------------------------------- snapshots


def _size(kind: str, raw: dict) -> str:
    if kind == "ecs":
        fl = raw.get("flavor")
        return str(fl.get("id") or fl.get("name") or "") if isinstance(fl, dict) else ""
    if kind == "evs":
        size, vtype = raw.get("size"), raw.get("volume_type")
        return ("%s GB %s" % (size, vtype or "")).strip() if size is not None else ""
    if kind == "eip":
        bw = raw.get("bandwidth_size")
        return "%s Mbit/s" % bw if bw is not None else ""
    return ""


Lister = Callable[[str, str, str, str], jobs.Listing]


def take(t: Tenant, handles: _sweep.Handles, listers: dict[str, Lister], now: datetime.datetime) -> dict:
    """One snapshot of the tenant: every source in every region. `listers[region](service, path, key, paging)`."""
    items: list[dict] = []
    unknown: dict[str, list[str]] = {}
    foreign = 0
    for region in t.regions:
        lister = listers[region]
        for kind, service, path, key, paging in _sweep.SOURCES:
            listing = lister(service, path, key, paging)
            if listing.state == jobs.UNKNOWN:
                unknown.setdefault(region, []).append(kind)
                continue
            for raw in listing.items:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    continue
                tags = _sweep.tags(raw.get("tags") or (raw.get("metadata") or {}).get("tags"))
                # a tag value is anyone's text: kept only in the form the Workbench writes, else - and counted
                project, expiry = (tags.get(_sweep.PROJECT_TAG, ""), tags.get(_sweep.EXPIRY_TAG, ""))
                if project and not _PROJECT_TAG_RE.fullmatch(project):
                    project, foreign = "-", foreign + 1
                if expiry and not _DATE_RE.fullmatch(expiry):
                    expiry, foreign = "-", foreign + 1
                items.append({
                    "handle": handles.handle(kind, raw["id"]),
                    "kind": kind,
                    "region": region,
                    "created": _sweep._created(raw),
                    "state": _sweep._state(kind, raw),
                    "size": _size(kind, raw),
                    "project": project,
                    "expiry": expiry,
                })
    items.sort(key=lambda i: (i["created"], i["handle"]))
    snap = {"alias": t.alias, "date": now.date().isoformat(), "taken": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "regions": list(t.regions), "unknown": unknown, "items": items}
    if foreign:
        snap["foreign_tags"] = foreign
    return snap


def snapshots(p: config.Paths, alias: str) -> list[Path]:
    d = folder(p, alias) / SNAPSHOTS
    try:
        return sorted(f for f in d.iterdir() if _DATE_RE.match(f.stem) and f.suffix == ".json")
    except FileNotFoundError:
        return []


def read_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff(before: dict | None, after: dict) -> list[dict]:
    """The events between two snapshots. A kind that was unknown in a region in either snapshot reports nothing
    gone there."""
    date = after["date"]
    if before is None:
        return [dict(item, event="seen", date=date) for item in after["items"]]
    old = {i["handle"]: i for i in before["items"]}
    new = {i["handle"]: i for i in after["items"]}
    blind = {(r, k) for snap in (before, after) for r, kinds in snap.get("unknown", {}).items() for k in kinds}
    out: list[dict] = []
    for h, item in new.items():
        if h not in old:
            out.append(dict(item, event="appeared", date=date))
            continue
        changed = [f for f in ("state", "project", "size", "expiry") if old[h].get(f) != item.get(f)]
        if changed:
            out.append(dict(item, event="changed", date=date, fields=changed,
                            was={f: old[h].get(f) for f in changed}))
    for h, item in old.items():
        if h not in new and (item["region"], item["kind"]) not in blind:
            out.append(dict(item, event="gone", date=date))
    return out


def record(p: config.Paths, snap: dict) -> list[dict]:
    """Keep the snapshot of the day (a second one on the same day replaces the first) and append its events."""
    d = folder(p, snap["alias"])
    config.make_dir(d / SNAPSHOTS, 0o770, shared=True)
    earlier = [s for s in snapshots(p, snap["alias"]) if s.stem < snap["date"]]
    today = [s for s in snapshots(p, snap["alias"]) if s.stem == snap["date"]]
    base = read_snapshot(today[0]) if today else (read_snapshot(earlier[-1]) if earlier else None)
    events = diff(base, snap)
    target = d / SNAPSHOTS / ("%s.json" % snap["date"])
    tmp = target.with_name("." + target.name + ".tmp")
    tmp.write_text(json.dumps(snap, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    with open(d / EVENTS, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return events


def snapshot(p: config.Paths, t: Tenant, make_lister: Callable[[Tenant, str], Lister] | None = None,
             now: datetime.datetime | None = None) -> tuple[dict, list[dict]]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    config.make_dir(folder(p, t.alias), 0o770, shared=True)
    handles = _sweep.Handles(folder(p, t.alias) / HANDLES)
    make_lister = make_lister or _cloud_lister
    snap = take(t, handles, {r: make_lister(t, r) for r in t.regions}, now)
    handles.save()
    try:
        os.chmod(folder(p, t.alias) / HANDLES, 0o600)
    except OSError:
        pass
    return snap, record(p, snap)


def _cloud_lister(t: Tenant, region: str) -> Lister:
    from awb.tcp.cloud import Client

    c = Client(keys_of(t), region, label=t.alias)
    return lambda service, path, key, paging: c.list(service, path, key, paging=paging)


# --------------------------------------------------------------------------- setting a tenant up (T10)

ADMIN_PREFIX = "awb/admin"
TENANT_PREFIX = "awb/tenant"
READ_GROUP = "awb-read"
READ_ROLE = "readonly"
LAB_GROUP = "awb-lab"
# The lab user of --lab-user (T12 part 3, P1): the system policies of the services a project's Terraform set may use
# (tf_services), by their display names in the IAM permissions reference of the docs mirror
# (identity-access-management/doc/permissions). Each is granted on the region's project only, but OBS, a global
# service, which IAM grants on the account. No IAM policy: the lab key creates no identity.
LAB_POLICIES = (("ecs", "ECS FullAccess"), ("evs", "EVS Admin"), ("vpc", "VPC FullAccess"), ("elb", "ELB FullAccess"),
                ("nat", "NAT FullAccess"), ("dns", "DNS FullAccess"), ("ims", "IMS FullAccess"),
                ("obs", "OBS OperateAccess"))
GLOBAL_SERVICES = ("obs",)
"""The system role shown as Tenant Guest: list and read rights on every service, nothing else."""
ALERT_NAME = "awb_new_access_key"
ALERT_OPERATIONS = [{"service_type": "IAM", "resource_type": "credential", "trace_names": ["createCredential"]}]
_DOMAIN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_URN_RE = re.compile(r"^urn:smn:([a-z]{2}-[a-z0-9]{2,10}):([0-9a-f]{32}):[A-Za-z0-9_-]{1,255}$")


class Store:
    """The owner's password store, by entry name. A value goes in through standard input and comes out of
    `show` only; no value is ever an argument or a log line."""

    def __init__(self, root: Path | None = None, run=subprocess.run):
        self.root = root or Path(os.environ.get("PASSWORD_STORE_DIR", "~/.password-store")).expanduser()
        self.run = run

    def has(self, entry: str) -> bool:
        return (self.root / (entry + ".gpg")).is_file()

    def show(self, entry: str) -> str:
        try:
            r = self.run(["pass", "show", entry], capture_output=True, text=True, timeout=60,
                         stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as err:
            raise TenantError("the password store cannot be read (%s)" % type(err).__name__) from None
        lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
        if not lines or not lines[0].strip():
            raise TenantError("the password store gives nothing for %s" % entry)
        return lines[0].strip()

    def insert(self, entry: str, value: str, replace: bool = False) -> None:
        try:
            r = self.run(["pass", "insert", "-m", entry] + (["-f"] if replace else []), input=value + "\n",
                         capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as err:
            raise TenantError("the password store cannot be written (%s)" % type(err).__name__) from None
        if r.returncode != 0:
            raise TenantError("pass insert of %s failed (exit %d)" % (entry, r.returncode))


class Iam:
    """Token calls to IAM and CTS with the admin login of a tenant. The password goes into the body of the token
    request only; the token stays in this object. An error names the status and the error code, never a body."""

    def __init__(self, region: str = "eu-de", endpoint: str | None = None, timeout: float = 30.0):
        self.region = region
        self.endpoint = (endpoint or os.environ.get("AWB_CLOUD_ENDPOINT") or "").rstrip("/") or None
        self.timeout = timeout
        self.token = ""

    def base(self, service: str) -> str:
        return self.endpoint or "https://%s.%s.otc.t-systems.com" % (service, self.region)

    def _send(self, method: str, service: str, path: str, body=None, query: dict | None = None,
              token: str | None = None) -> tuple[int, dict, dict]:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = self.base(service) + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json;charset=utf8")
        token = self.token if token is None else token
        if token:
            req.add_header("X-Auth-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw, headers = resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            status, raw, headers = err.code, err.read(), dict(err.headers or {})
        except (urllib.error.URLError, OSError) as err:
            raise TenantError("no answer from %s (%s)" % (service, type(err).__name__)) from None
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}, headers

    @staticmethod
    def _code(data: dict) -> str:
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("code") or "")
        return str(data.get("error_code") or "")

    def login(self, domain: str, user: str, password: str, domain_id: str, project: str | None = None) -> dict:
        """A password token, scoped to the domain or to the project named like the region. Returns the token body."""
        scope = {"project": {"name": project, "domain": {"id": domain_id}}} if project else {"domain": {"id": domain_id}}
        body = {"auth": {"identity": {"methods": ["password"], "password": {
            "user": {"name": user, "password": password, "domain": {"name": domain}}}}, "scope": scope}}
        status, data, headers = self._send("POST", "iam", "/v3/auth/tokens", body, token="")
        token = {k.lower(): v for k, v in headers.items()}.get("x-subject-token", "")
        if status != 201 or not token or not isinstance(data.get("token"), dict):
            raise TenantError("the admin login was refused (HTTP %d %s)" % (status, self._code(data)))
        if not project:
            self.token = token
        data["token"]["_id"] = token
        return data["token"]

    def call(self, method: str, service: str, path: str, body=None, query: dict | None = None,
             token: str | None = None, ok: tuple[int, ...] = (200, 201, 204)) -> tuple[int, dict]:
        status, data, _ = self._send(method, service, path, body, query, token)
        if status not in ok:
            shown = re.sub(r"[0-9a-f]{32}|[0-9a-f-]{36}", "{id}", path)
            raise TenantError("%s %s answered HTTP %d %s" % (method, shown, status, self._code(data)))
        return status, data


def read_user_name(alias: str) -> str:
    m = re.search(r"(\d+)$", alias)
    return "awb-read-%s" % (m.group(1) if m else alias)


def lab_user_name(alias: str) -> str:
    m = re.search(r"(\d+)$", alias)
    return "awb-lab-%s" % (m.group(1) if m else alias)


def _lab_user(iam: "Iam", store: "Store", domain_id: str, region: str, alias: str, prefix: str,
              done: list[str]) -> bool:
    """The lab user (P1): group awb-lab with the policies of LAB_POLICIES on the region's project (OBS on the
    account), the user awb-lab-<number> in it, its key under prefix/ak|sk. A lab key of another user there (the admin
    of --lab-key) is replaced; that key stays in IAM until the owner deletes it. True when anything changed."""
    changed = False
    _, data = iam.call("GET", "iam", "/v3/projects", query={"name": region, "domain_id": domain_id})
    project = _one(data.get("projects") or [], region)
    if not project:
        raise TenantError("IAM knows no project %s in this account" % region)
    _, data = iam.call("GET", "iam", "/v3/roles")
    by_name = {r.get("display_name"): r for r in data.get("roles") or [] if isinstance(r, dict)}
    missing = [name for _, name in LAB_POLICIES if name not in by_name]
    if missing:
        raise TenantError("IAM knows no system policy %s" % ", ".join(missing))
    _, data = iam.call("GET", "iam", "/v3/groups", query={"name": LAB_GROUP, "domain_id": domain_id})
    group = _one(data.get("groups") or [], LAB_GROUP)
    if group:
        done.append("group %s: present" % LAB_GROUP)
    else:
        _, data = iam.call("POST", "iam", "/v3/groups", {"group": {
            "name": LAB_GROUP, "domain_id": domain_id,
            "description": "Workbench lab key: the Terraform services in the lab project, no IAM"}})
        group, changed = data["group"], True
        done.append("group %s: created" % LAB_GROUP)
    for service, name in LAB_POLICIES:
        rid = by_name[name]["id"]
        where = ("/v3/domains/%s" % domain_id) if service in GLOBAL_SERVICES else ("/v3/projects/%s" % project["id"])
        path = "%s/groups/%s/roles/%s" % (where, group["id"], rid)
        status, _ = iam.call("HEAD", "iam", path, ok=(204, 404))
        if status == 404:
            iam.call("PUT", "iam", path)
            changed = True
            done.append("%s on %s: granted" % (name, "the account" if service in GLOBAL_SERVICES else region))
    name = lab_user_name(alias)
    _, data = iam.call("GET", "iam", "/v3/users", query={"name": name, "domain_id": domain_id})
    user = _one(data.get("users") or [], name)
    if user:
        done.append("user %s: present" % name)
    else:
        _, data = iam.call("POST", "iam", "/v3/users", {"user": {
            "name": name, "domain_id": domain_id, "enabled": True, "description": "Workbench lab key, no IAM"}})
        user, changed = data["user"], True
        iam.call("PUT", "iam", "/v3.0/OS-USER/users/%s" % user["id"], {"user": {"access_mode": "programmatic"}})
        done.append("user %s: created, programmatic access" % name)
    member = "/v3/groups/%s/users/%s" % (group["id"], user["id"])
    status, _ = iam.call("HEAD", "iam", member, ok=(204, 404))
    if status == 404:
        iam.call("PUT", "iam", member)
        changed = True
        done.append("user %s: put into %s" % (name, LAB_GROUP))
    has = [store.has(prefix + "/ak"), store.has(prefix + "/sk")]
    if all(has):
        _, data = iam.call("GET", "iam", "/v3.0/OS-CREDENTIAL/credentials", query={"user_id": user["id"]})
        owned = {c.get("access") for c in data.get("credentials") or [] if isinstance(c, dict)}
        if store.show(prefix + "/ak") in owned:
            done.append("lab key: present, of %s" % name)
            return changed
        _, data = iam.call("POST", "iam", "/v3.0/OS-CREDENTIAL/credentials",
                           {"credential": {"user_id": user["id"], "description": "awb lab key"}})
        cred = data.get("credential") or {}
        if not cred.get("access") or not cred.get("secret"):
            raise TenantError("IAM gave no key for the lab user")
        store.insert(prefix + "/ak", cred["access"], replace=True)
        store.insert(prefix + "/sk", cred["secret"], replace=True)
        done.append("lab key: replaced by the key of %s, written to %s; the key there before stays in IAM until "
                    "you delete it in the console" % (name, prefix))
        return True
    return _key(iam, store, user["id"], prefix, "lab key", done) or changed


def _one(items: list, name: str) -> dict | None:
    hits = [i for i in items if isinstance(i, dict) and i.get("name") == name]
    return hits[0] if hits else None


def _key(iam: Iam, store: Store, user_id: str, prefix: str, what: str, done: list[str]) -> bool:
    """The key of a user under prefix/ak and prefix/sk: created when the store has neither, checked against the
    user's keys when it has both. True when a key was created."""
    has = [store.has(prefix + "/ak"), store.has(prefix + "/sk")]
    if has[0] != has[1]:
        raise TenantError("%s holds one half of a key: fix it by hand" % prefix)
    if all(has):
        _, data = iam.call("GET", "iam", "/v3.0/OS-CREDENTIAL/credentials", query={"user_id": user_id})
        owned = {c.get("access") for c in data.get("credentials") or [] if isinstance(c, dict)}
        if store.show(prefix + "/ak") not in owned:
            raise TenantError("%s/ak holds a key that the %s does not own" % (prefix, what))
        done.append("%s: present" % what)
        return False
    _, data = iam.call("POST", "iam", "/v3.0/OS-CREDENTIAL/credentials",
                       {"credential": {"user_id": user_id, "description": "awb %s" % what}})
    cred = data.get("credential") or {}
    if not cred.get("access") or not cred.get("secret"):
        raise TenantError("IAM gave no key for the %s" % what)
    store.insert(prefix + "/ak", cred["access"])
    store.insert(prefix + "/sk", cred["secret"])
    done.append("%s: created, written to %s" % (what, prefix))
    return True


def setup(p: config.Paths, alias: str, domain_id: str, *, region: str = "eu-de", lab_key: bool = False,
          lab_user: bool = False, alerts: bool = False, topic: str | None = None, store: Store | None = None, iam: Iam | None = None,
          loaded: Callable[[], dict] | None = None, unlock: Callable[[], dict] | None = None,
          make_lister: Callable[[Tenant, str], Lister] | None = None) -> tuple[list[str], bool]:
    """Set a test tenant up from the admin login in the password store: the read group, the read user and its key,
    with lab_key a key of the admin user, with alerts the CTS alert of a new access key, the register, the key
    service and the first snapshot. Returns the lines to print, never a value, and whether anything changed."""
    if not _ALIAS_RE.match(alias or ""):
        raise TenantError("an alias reads like test-1: small letters, digits and dashes")
    if not _DOMAIN_ID_RE.match(domain_id or ""):
        raise TenantError("--domain-id is the 32 hex characters of the account's domain")
    if lab_key and lab_user:
        raise TenantError("--lab-key and --lab-user are two ways to the lab key: choose one")
    from awb.tcp.cloud import CloudError, check_region

    try:
        check_region(region)
    except CloudError as err:
        raise TenantError(str(err)) from None
    store = store or Store()
    iam = iam or Iam(region)
    admin = "%s/%s" % (ADMIN_PREFIX, alias)
    for part in ("domain", "user", "password"):
        if not store.has("%s/%s" % (admin, part)):
            raise TenantError("%s/%s is missing: pass insert it first" % (admin, part))
    domain, user = store.show(admin + "/domain"), store.show(admin + "/user")
    password = store.show(admin + "/password")
    try:
        tok = iam.login(domain, user, password, domain_id)
        project_tok = iam.login(domain, user, password, domain_id, project=region) if alerts else None
    finally:
        del password
    if (tok.get("domain") or {}).get("id") != domain_id:
        raise TenantError("the admin login of %s belongs to another domain" % alias)
    admin_id = (tok.get("user") or {}).get("id") or ""
    done: list[str] = []
    changed = False

    _, data = iam.call("GET", "iam", "/v3/roles", query={"name": READ_ROLE})
    role = _one(data.get("roles") or [], READ_ROLE)
    if not role:
        raise TenantError("IAM knows no role %s (Tenant Guest)" % READ_ROLE)
    _, data = iam.call("GET", "iam", "/v3/groups", query={"name": READ_GROUP, "domain_id": domain_id})
    group = _one(data.get("groups") or [], READ_GROUP)
    if group:
        done.append("group %s: present" % READ_GROUP)
    else:
        _, data = iam.call("POST", "iam", "/v3/groups", {"group": {
            "name": READ_GROUP, "domain_id": domain_id, "description": "Workbench read keys, Tenant Guest"}})
        group, changed = data["group"], True
        done.append("group %s: created" % READ_GROUP)
    grant = "/v3/OS-INHERIT/domains/%s/groups/%s/roles/%s/inherited_to_projects" % (domain_id, group["id"],
                                                                                    role["id"])
    status, _ = iam.call("HEAD", "iam", grant, ok=(204, 404))
    if status == 404:
        iam.call("PUT", "iam", grant)
        changed = True
        done.append("Tenant Guest on all projects: granted")
    else:
        done.append("Tenant Guest on all projects: present")

    name = read_user_name(alias)
    _, data = iam.call("GET", "iam", "/v3/users", query={"name": name, "domain_id": domain_id})
    reader = _one(data.get("users") or [], name)
    if reader:
        done.append("user %s: present" % name)
    else:
        _, data = iam.call("POST", "iam", "/v3/users", {"user": {
            "name": name, "domain_id": domain_id, "enabled": True, "description": "Workbench read key"}})
        reader, changed = data["user"], True
        iam.call("PUT", "iam", "/v3.0/OS-USER/users/%s" % reader["id"], {"user": {"access_mode": "programmatic"}})
        done.append("user %s: created, programmatic access" % name)
    member = "/v3/groups/%s/users/%s" % (group["id"], reader["id"])
    status, _ = iam.call("HEAD", "iam", member, ok=(204, 404))
    if status == 404:
        iam.call("PUT", "iam", member)
        changed = True
        done.append("user %s: put into %s" % (name, READ_GROUP))
    entry = "%s/%s" % (TENANT_PREFIX, alias)
    changed |= _key(iam, store, reader["id"], entry, "read key", done)
    if lab_key:
        if not admin_id:
            raise TenantError("the admin token names no user")
        changed |= _key(iam, store, admin_id, entry + "/lab", "lab key", done)
    if lab_user:
        changed |= _lab_user(iam, store, domain_id, region, alias, entry + "/lab", done)

    if alerts:
        changed |= _alert(iam, project_tok, region, topic, done)

    if not any(t.alias == alias for t in load(p)):
        add(p, alias, PASS_REF + entry, [region])
        changed = True
        done.append("tenant %s: added to the register" % alias)
    roles = {"read", "lab"} if store.has(entry + "/lab/ak") else {"read"}
    have = (loaded() if loaded else {}).get(alias) or {}
    if changed or not roles <= set(have.get("roles") or []):
        if unlock is None:
            raise TenantError("the key service needs the new keys: awb keys unlock")
        unlock()
        done.append("key service: unlocked, %s carries %s" % (alias, ", ".join(sorted(roles))))
    else:
        done.append("key service: %s carries %s" % (alias, ", ".join(sorted(roles))))

    if not snapshots(p, alias):
        snap, _ = snapshot(p, get(p, alias), make_lister)
        changed = True
        done.append("first snapshot: %d resources" % len(snap["items"]))
    _, data = iam.call("GET", "iam", "/v3/users", query={"domain_id": domain_id})
    users = len(data.get("users") or [])
    _, data = iam.call("GET", "iam", "/v3.0/OS-CREDENTIAL/credentials", query={"user_id": reader["id"]})
    keys = len(data.get("credentials") or [])
    snap = latest(p, alias)
    done.append("%s: %d users, %d key(s) of %s, %d resources in the snapshot of %s"
                % (alias, users, keys, name, len(snap["items"]) if snap else 0, snap["date"] if snap else "-"))
    if not changed:
        done.append("nothing to do: %s was set up already" % alias)
    return done, changed


def _alert(iam: Iam, tok: dict | None, region: str, topic: str | None, done: list[str]) -> bool:
    project_id = ((tok or {}).get("project") or {}).get("id") or ""
    if not project_id:
        raise TenantError("the project token of %s names no project" % region)
    path = "/v3/%s/notifications" % project_id
    _, data = iam.call("GET", "cts", path + "/smn", query={"notification_name": ALERT_NAME}, token=tok["_id"])
    if _one([dict(n, name=n.get("notification_name")) for n in data.get("notifications") or []
             if isinstance(n, dict)], ALERT_NAME):
        done.append("CTS alert %s: present" % ALERT_NAME)
        return False
    if not topic:
        from awb.tcp import xchg

        try:
            topic = xchg.read_settings().get("notify_topic") or ""
        except xchg.XchgError as err:
            raise TenantError(str(err)) from None
    m = _URN_RE.match(topic or "")
    if not m:
        raise TenantError("--alerts needs an SMN topic: notify_topic in keys.conf or --topic URN")
    if m.group(2) != project_id:
        raise TenantError("the SMN topic belongs to another project: create a topic on this tenant and give "
                          "--topic URN")
    iam.call("POST", "cts", path, {"notification_name": ALERT_NAME, "operation_type": "customized",
                                   "operations": ALERT_OPERATIONS, "topic_id": topic}, token=tok["_id"])
    done.append("CTS alert %s: created" % ALERT_NAME)
    return True


# --------------------------------------------------------------------------- questions


def events(p: config.Paths, alias: str) -> list[dict]:
    try:
        lines = (folder(p, alias) / EVENTS).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest(p: config.Paths, alias: str, on: str | None = None) -> dict | None:
    snaps = [s for s in snapshots(p, alias) if on is None or s.stem <= on]
    return read_snapshot(snaps[-1]) if snaps else None


def age_days(created: str, today: datetime.date) -> str:
    try:
        return str((today - datetime.date.fromisoformat(created[:10])).days)
    except ValueError:
        return "?"


def flags(item: dict, today: datetime.date) -> str:
    out = []
    if item.get("project") in (None, "", "-"):
        out.append("no project")
    exp = item.get("expiry", "")
    if exp and _DATE_RE.match(exp) and exp < today.isoformat():
        out.append("expired")
    if item.get("state") in _sweep.IDLE_STATES.get(item.get("kind", ""), ()):
        out.append("idle")
    return ", ".join(out)


def _row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()


def table(rows: list[list[str]], head: list[str]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    return [_row(head, widths)] + [_row([str(c) for c in r], widths) for r in rows]


def now_lines(p: config.Paths, alias: str, today: datetime.date) -> list[str]:
    snap = latest(p, alias)
    if snap is None:
        return ["%s: no snapshot yet (awb tenant snapshot %s)" % (alias, alias)]
    rows = [[i["handle"], i["kind"], i["region"], i.get("size", ""), i.get("project") or "-", i["created"][:10],
             age_days(i["created"], today), i.get("expiry") or "-", i.get("state", ""), flags(i, today)]
            for i in snap["items"]]
    out = ["%s: %d resources in the snapshot of %s" % (alias, len(rows), snap["date"])]
    for region, kinds in sorted(snap.get("unknown", {}).items()):
        out.append("  not readable in %s: %s (counted as unknown, not as none)" % (region, ", ".join(kinds)))
    if snap.get("foreign_tags"):
        out.append("  %d tag value(s) not in the Workbench form, kept as -" % snap["foreign_tags"])
    if rows:
        out += table(rows, ["handle", "kind", "region", "size", "project", "created", "days", "expiry", "state",
                            "flags"])
    return out


def history_lines(p: config.Paths, alias: str, project: str | None = None, since: str | None = None) -> list[str]:
    evs = [e for e in events(p, alias) if (not project or e.get("project") == project)
           and (not since or e.get("date", "") >= since)]
    if not evs:
        return ["%s: no event%s" % (alias, " for %s" % project if project else "")]
    rows = []
    for e in evs:
        what = e["event"]
        if what == "changed":
            what += " " + ", ".join("%s %s -> %s" % (f, (e.get("was") or {}).get(f) or "-", e.get(f) or "-")
                                    for f in e.get("fields", []))
        rows.append([e["date"], e["handle"], e["kind"], e.get("project") or "-", e.get("created", "")[:10], what])
    return table(rows, ["date", "handle", "kind", "project", "created", "event"])


def project_lines(p: config.Paths, code: str, today: datetime.date) -> list[str]:
    out = []
    for t in load(p):
        snap = latest(p, t.alias)
        running = [i for i in (snap or {}).get("items", []) if i.get("project") == code]
        past = [e for e in events(p, t.alias) if e.get("project") == code]
        if not running and not past:
            continue
        out.append("%s: %d running now, %d events" % (t.alias, len(running), len(past)))
        out += ["  " + line for line in history_lines(p, t.alias, project=code)]
    return out or ["%s: nothing on any tenant" % code]


def list_lines(p: config.Paths, today: datetime.date) -> list[str]:
    rows = []
    for t in load(p):
        snap = latest(p, t.alias)
        items = (snap or {}).get("items", [])
        kinds = {}
        for i in items:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        rows.append([t.alias, ",".join(t.regions), (snap or {}).get("date", "never"), str(len(items)),
                     " ".join("%s %d" % kv for kv in sorted(kinds.items())) or "-",
                     str(sum(1 for i in items if i.get("project") in (None, "", "-")))])
    if not rows:
        return ["no tenant yet (awb tenant add ALIAS --keys file:PATH)"]
    return table(rows, ["tenant", "regions", "snapshot", "resources", "by kind", "no project"])


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb tenant", description="Resources on the test tenants, tracked over time.")
    sub = ap.add_subparsers(dest="command")
    a = sub.add_parser("add", help="a tenant and its read-only key (owner side)")
    a.add_argument("alias")
    a.add_argument("--keys", required=True)
    a.add_argument("--region", action="append", default=[])
    su = sub.add_parser("setup", help="a new tenant from its admin login in pass, in one command (owner side)")
    su.add_argument("alias")
    su.add_argument("--domain-id", required=True)
    su.add_argument("--region", default="eu-de")
    su.add_argument("--lab-key", action="store_true", help="also a key of the admin user, the lab role")
    su.add_argument("--lab-user", action="store_true",
                    help="the lab role as its own user awb-lab-N: the Terraform services in the region's project, no "
                         "IAM; replaces a lab key of the admin")
    su.add_argument("--alerts", action="store_true", help="the CTS alert of a new access key")
    su.add_argument("--topic", help="the SMN topic urn of the alert (default: notify_topic of keys.conf)")
    s = sub.add_parser("snapshot", help="list the tenants now, GET only (owner side)")
    s.add_argument("alias", nargs="*")
    sub.add_parser("list", help="every tenant and what runs there now")
    n = sub.add_parser("now", help="what runs on a tenant now")
    n.add_argument("alias")
    h = sub.add_parser("history", help="what appeared, changed and went")
    h.add_argument("alias")
    h.add_argument("--project")
    h.add_argument("--since")
    t_ = sub.add_parser("at", help="what ran on a tenant on a day")
    t_.add_argument("alias")
    t_.add_argument("date")
    pr = sub.add_parser("project", help="what a project runs and ran on every tenant")
    pr.add_argument("code")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    today = datetime.date.today()
    try:
        if args.command == "add":
            if config.is_work_user():
                raise TenantError("tenants are added on the owner side")
            t = add(p, args.alias, args.keys, args.region)
            print("tenant %s added (%s); take the first snapshot: awb tenant snapshot %s"
                  % (t.alias, ",".join(t.regions), t.alias))
        elif args.command == "setup":
            if config.is_work_user():
                raise TenantError("tenants are set up on the owner side")
            from awb.tcp import keys as _keys

            def loaded() -> dict:
                try:
                    answer = _keys.request(_keys.call_socket(), {"op": "tenants"}, timeout=10)
                except _keys.KeysError:
                    return {}
                return answer.get("tenants") or {} if answer.get("ok") else {}

            def unlock() -> dict:
                try:
                    return _keys.unlock()
                except _keys.KeysError as err:
                    raise TenantError("awb keys unlock: %s" % err) from None

            lines, _ = setup(p, args.alias, args.domain_id, region=args.region, lab_key=args.lab_key,
                             lab_user=args.lab_user, alerts=args.alerts, topic=args.topic, loaded=loaded,
                             unlock=unlock)
            print("\n".join(lines))
        elif args.command == "snapshot":
            if config.is_work_user():
                raise TenantError("snapshots are taken on the owner side, with the tenant's read-only key")
            chosen = [get(p, a) for a in args.alias] if args.alias else load(p)
            if not chosen:
                raise TenantError("no tenant yet (awb tenant add ALIAS --keys file:PATH)")
            for t in chosen:
                snap, evs = snapshot(p, t)
                counts = {}
                for e in evs:
                    counts[e["event"]] = counts.get(e["event"], 0) + 1
                unknown = sum(len(v) for v in snap["unknown"].values())
                print("%s: %d resources, events %s%s" % (t.alias, len(snap["items"]),
                      " ".join("%s %d" % kv for kv in sorted(counts.items())) or "none",
                      "; %d listing(s) not readable" % unknown if unknown else ""))
        elif args.command == "list":
            print("\n".join(list_lines(p, today)))
        elif args.command == "now":
            get(p, args.alias)
            print("\n".join(now_lines(p, args.alias, today)))
        elif args.command == "history":
            get(p, args.alias)
            if args.since and not _DATE_RE.match(args.since):
                raise TenantError("--since reads YYYY-MM-DD")
            print("\n".join(history_lines(p, args.alias, args.project, args.since)))
        elif args.command == "at":
            get(p, args.alias)
            if not _DATE_RE.match(args.date):
                raise TenantError("the date reads YYYY-MM-DD")
            snap = latest(p, args.alias, on=args.date)
            if snap is None:
                print("%s: no snapshot on or before %s" % (args.alias, args.date))
            else:
                rows = [[i["handle"], i["kind"], i["region"], i.get("size", ""), i.get("project") or "-",
                         i["created"][:10], i.get("state", "")] for i in snap["items"]]
                print("%s on %s (snapshot of %s): %d resources" % (args.alias, args.date, snap["date"], len(rows)))
                if rows:
                    print("\n".join(table(rows, ["handle", "kind", "region", "size", "project", "created", "state"])))
        elif args.command == "project":
            if not _CODE_RE.match(args.code):
                raise TenantError("a project code reads like tcp-q7m4")
            print("\n".join(project_lines(p, args.code, today)))
    except (TenantError, _sweep.SweepError) as err:
        print("awb tenant: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb tenant: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
