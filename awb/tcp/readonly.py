"""Read-only tenants (task RT): a tenant of a customer or partner where nothing may change, only read.

    awb tenant add-readonly ALIAS --domain-id ID [--login | --key] [--region R]...   owner: register it
    awb tenant regions ALIAS --region R [--region R]...                             owner: change its regions
    awb tenant projects ALIAS [--project CODE]                                       a session: what it reads
    awb tenant remove ALIAS                                                          owner: forget it
    awb tenant grant ALIAS CODE, awb tenant revoke ALIAS CODE                        owner: one project reads it

A read-only tenant holds one of two things in the owner's password store, entered by him with `pass insert`, never
by a session:

    awb/tenant/<alias>/ak, awb/tenant/<alias>/sk                          a read AK/SK (--key)
    awb/tenant/<alias>/login/domain, login/user, login/password          a console login (--login)

It is registered by one line of keys.conf, `readonly = ALIAS:key|login:DOMAIN-ID:REGION[+REGION]`, entries
separated by commas (an old line of one region still reads; add-readonly writes eu-de+eu-nl unless --region names
others, and `awb tenant regions` changes them without the login), which travels to the key service with `awb keys unlock` like every other setting. The key
service allows GET and HEAD only on such a tenant, for every role and every caller, whatever rights the user has in
the tenant; any other method, a body, a lease, a lab key and a secret are refused with one line. A session reaches
it only from a project the owner granted the kind project (awb projects kind) and this tenant
(`awb tenant grant ALIAS CODE`, `<vault>/readonly-grants.json`, mode 600); a grant of the project alone is not
enough. The console's inventory never reads it.

For a login the key service asks IAM for a token scoped to the project named like the region (POST /v3/auth/tokens,
password authentication, `nocatalog`), or, when the call names a project (`scope`: a region project or a
sub-project like eu-de_<suffix>, by name or id), scoped to that project by its id, one token per project. Before
it does, it lists the projects the login or key can see (GET /v3/auth/projects, kept LIST_LIFE seconds) and
refuses a project of another domain and a project whose region (the name before `_`) is not one of the tenant's;
the call then goes to the endpoints of that region. A read key sends the project as X-Project-Id. The token is
kept in memory only, uses it until RENEW_MARGIN seconds before its
expiry and then asks again; an answer 401 drops it and asks once more. The token goes as X-Auth-Token and is taken
out of every answer; it is never logged, never handed over in a reload and never shown. IAM refuses a password
login when login protection with MFA is on: the service then says MFA_LINE and never asks for a code. OBS takes a
key, so a login-only tenant has no OBS.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from awb import codes, config

SETTING = "readonly"
AUTHS = ("key", "login")
LOGIN_PARTS = ("domain", "user", "password")
METHODS = ("GET", "HEAD")
DEFAULT_REGIONS = ("eu-de", "eu-nl")
"""The regions add-readonly writes when --region names none."""
PROJECTS_PATH = "/v3/auth/projects"
"""The projects the login or key can see (IAM): the list a scope is checked against."""
LIST_LIFE = 300.0
"""Seconds the key service keeps the project list of a read-only tenant."""
RENEW_MARGIN = 600.0
"""Seconds before the expiry of a token at which the key service takes a new one."""
FALLBACK_LIFE = 3600.0
"""Seconds a token is used when IAM names no readable expiry."""
GRANTS_NAME = "readonly-grants.json"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_DOMAIN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z0-9]{2,10}$")
_CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_SCOPE_NAME_RE = re.compile(r"^[a-z]{2}-[a-z0-9]{2,10}(_[A-Za-z0-9_-]{1,64})?$")
_IAM_CODE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_MFA_RE = re.compile(r"(?i)\bmfa\b|totp|passcode|login protection|verification code|multi-factor|virtual mfa")

METHOD_LINE = "refused: %s is a read-only tenant: GET and HEAD only"
ROLE_LINE = "refused: %s is a read-only tenant: the read role only"
BODY_LINE = "refused: %s is a read-only tenant: a call carries no body"
LEASE_LINE = "refused: %s is a read-only tenant: no lease"
CONSOLE_LINE = "refused: %s is a read-only tenant: the console's inventory does not read it"
GRANT_LINE = ("refused: %s is not granted the read-only tenant %s: the owner runs awb tenant grant %s %s in his own "
              "terminal")
OBS_LINE = "refused: %s holds a login only: OBS takes a key, ask for a read-only AK/SK"
UNREGISTERED_LINE = "refused: %s holds a login but is not a registered read-only tenant (awb tenant add-readonly)"
MFA_LINE = "this login needs MFA: ask for a read-only AK/SK"
SCOPE_LINE = "refused: a project is named by its name (eu-de, eu-de_<suffix>) or its id of 32 hex characters"
UNKNOWN_PROJECT_LINE = ("refused: %s is no project the read-only tenant %s can see: awb tenant projects %s lists "
                        "them")
OTHER_DOMAIN_LINE = "refused: the project %s belongs to another domain than the read-only tenant %s"
NO_REGION_LINE = "refused: the project %s names no region"
REGION_LINE = ("refused: the project %s is in %s, not a region of the read-only tenant %s (%s): the owner adds it with "
               "awb tenant regions %s --region ...")
CONSOLE_ONLY_LINE = ("this login has console access only and IAM refuses it an API token (IAM.0081): ask for "
                     "programmatic access or a read-only AK/SK")


class ReadonlyError(Exception):
    """A step of a read-only tenant that cannot run. The message names aliases, kinds and HTTP codes, never a
    value."""


# --------------------------------------------------------------------------- the line of keys.conf


def parse(value: str) -> dict[str, dict] | None:
    """{alias: {"auth", "domain_id", "regions"}} of the readonly line, or None when it is not a list of valid
    entries. An empty line is no tenant."""
    out: dict[str, dict] = {}
    if not value.strip():
        return out
    for item in value.split(","):
        parts = item.strip().split(":")
        if len(parts) != 4:
            return None
        alias, auth, domain_id, regions = parts
        found = [r for r in regions.split("+")]
        if (not _ALIAS_RE.match(alias) or auth not in AUTHS or not _DOMAIN_ID_RE.match(domain_id) or not found
                or not all(_REGION_RE.match(r) for r in found) or alias in out):
            return None
        out[alias] = {"auth": auth, "domain_id": domain_id, "regions": tuple(found)}
    return out


def render(entries: dict[str, dict]) -> str:
    return ", ".join("%s:%s:%s:%s" % (a, e["auth"], e["domain_id"], "+".join(e["regions"]))
                     for a, e in sorted(entries.items()))


def of_settings(settings: dict) -> dict[str, dict]:
    """The read-only tenants of the settings the key service holds; nothing when the line is missing or broken."""
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return (parse(str(value)) or {}) if value is not None else {}


def write_setting(entries: dict[str, dict], path: Path | None = None) -> Path:
    """Replace the readonly line of keys.conf (or add it; drop it for no entry), every other line kept, mode 600."""
    from awb.tcp import xchg

    path = path or xchg.settings_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [ln for ln in lines if ln.strip().partition("=")[0].strip() != SETTING]
    if entries:
        kept.append("%s = %s" % (SETTING, render(entries)))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".keys.conf.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


# --------------------------------------------------------------------------- the grants of a tenant to projects


def grants_path(p: config.Paths) -> Path:
    return p.vault / GRANTS_NAME


def load_grants(p: config.Paths) -> dict[str, list[str]]:
    """{alias: [project codes]} the owner granted; nothing when the file cannot be read."""
    try:
        data = json.loads(grants_path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {a: sorted({c for c in v if isinstance(c, str) and _CODE_RE.match(c)})
            for a, v in data.items() if isinstance(a, str) and _ALIAS_RE.match(a) and isinstance(v, list)}


def granted(p: config.Paths, alias: str, code: str) -> bool:
    return isinstance(alias, str) and isinstance(code, str) and code in load_grants(p).get(alias, [])


def _save_grants(p: config.Paths, data: dict[str, list[str]]) -> None:
    if config.is_work_user():
        raise ReadonlyError("a grant of a read-only tenant is the owner's: he runs it in his own terminal")
    p.vault.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".readonly-grants.", dir=p.vault)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({a: c for a, c in sorted(data.items()) if c}, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, grants_path(p))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def grant(p: config.Paths, alias: str, code: str) -> bool:
    """Grant `code` the read-only tenant `alias`; False when it was granted already."""
    if not _ALIAS_RE.match(alias or "") or not _CODE_RE.match(code or ""):
        raise ReadonlyError("a grant names a tenant alias and a project code (tcp-q7m4)")
    data = load_grants(p)
    if code in data.get(alias, []):
        return False
    data[alias] = sorted(set(data.get(alias, [])) | {code})
    _save_grants(p, data)
    return True


def revoke(p: config.Paths, alias: str, code: str | None = None) -> int:
    """Take the grant of `alias` from `code`, or from every project for None; the count of grants taken."""
    data = load_grants(p)
    had = data.get(alias, [])
    left = [c for c in had if code is not None and c != code]
    if len(left) == len(had):
        return 0
    data[alias] = left
    _save_grants(p, data)
    return len(had) - len(left)


# --------------------------------------------------------------------------- the token of a login


def iam_base(region: str, endpoint: str | None = None) -> str:
    return endpoint.rstrip("/") if endpoint else "https://iam.%s.otc.t-systems.com" % region


def _expiry(text) -> float:
    """Seconds from now to an `expires_at` of IAM (2026-10-23T09:00:00.000000Z); FALLBACK_LIFE when unreadable."""
    if isinstance(text, str):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is not None:
                return when.timestamp() - time.time()
        except ValueError:
            pass
    return FALLBACK_LIFE


def refusal(status: int, data: dict, raw: bytes) -> str:
    """The one line of a refused login: MFA, console access only or the status and the IAM code, never a body."""
    err = data.get("error") if isinstance(data, dict) else None
    code = str((err.get("code") if isinstance(err, dict) else None) or (data or {}).get("error_code") or "")
    if code in ("IAM.1061",) or _MFA_RE.search(raw.decode("utf-8", "replace")[:4096]):
        return MFA_LINE
    if code == "IAM.0081":
        return CONSOLE_ONLY_LINE
    shown = code if _IAM_CODE_RE.match(code) else ""
    return "the login was refused by IAM (HTTP %d%s)" % (status, " %s" % shown if shown else "")


def fetch_token(login: dict, domain_id: str, region: str, *, endpoint: str | None = None,
                timeout: float = 30.0, project_id: str | None = None) -> tuple[str, str, float]:
    """(token, project id, seconds it lives) of a password login scoped to the project named like the region, or to
    the project `project_id` (a sub-project) at the IAM of `region`. The password goes into the body of this one
    request only; an error names no value."""
    if set(login) < set(LOGIN_PARTS):
        raise ReadonlyError("the login lacks its domain, user or password")
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": login["user"], "password": login["password"], "domain": {"name": login["domain"]}}}},
        "scope": {"project": {"id": project_id} if project_id else {"name": region, "domain": {"id": domain_id}}}}}
    url = iam_base(region, endpoint) + "/v3/auth/tokens?nocatalog=true"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json;charset=utf8")
    body = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw, headers = resp.status, resp.read(1 << 20), resp.headers
    except urllib.error.HTTPError as err:
        status, raw, headers = err.code, err.read(1 << 20), err.headers
    except (urllib.error.URLError, OSError) as err:
        raise ReadonlyError("no answer from IAM (%s)" % type(err).__name__) from None
    finally:
        req = None
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    token = (headers.get("X-Subject-Token") if headers is not None else None) or ""
    tok = data.get("token") if isinstance(data.get("token"), dict) else None
    if status != 201 or not token or tok is None:
        raise ReadonlyError(refusal(status, data, raw or b""))
    project = tok.get("project") if isinstance(tok.get("project"), dict) else {}
    if (project.get("domain") or {}).get("id") not in (None, domain_id):
        raise ReadonlyError("the login belongs to another domain than --domain-id")
    got = project.get("id")
    if not isinstance(got, str) or not re.match(r"^[0-9a-f]{32}$", got) or (project_id and got != project_id):
        raise ReadonlyError("the token of the login names no project of %s" % region)
    return token, got, _expiry(tok.get("expires_at"))


class Tokens:
    """The tokens of the logins, in memory only, by alias and region (the region project) or by alias and project
    id (a project the call names); taken again RENEW_MARGIN before expiry. Beside them the project list of each
    tenant, kept LIST_LIFE seconds; a clear forgets both."""

    def __init__(self, fetch=fetch_token, clock=time.monotonic):
        import threading

        self.fetch = fetch
        self.clock = clock
        self._held: dict[tuple[str, str], tuple[str, str, float]] = {}
        self._lists: dict[str, tuple[list[dict], float]] = {}
        self._lock = threading.Lock()

    def get(self, alias: str, region: str, login: dict, domain_id: str, endpoint: str | None = None,
            project_id: str | None = None) -> tuple[str, str]:
        key = (alias, project_id or region)
        with self._lock:
            held = self._held.get(key)
            if held and held[2] - self.clock() > RENEW_MARGIN:
                return held[0], held[1]
            extra = {"project_id": project_id} if project_id else {}
            token, got, life = self.fetch(login, domain_id, region, endpoint=endpoint, **extra)
            self._held[key] = (token, got, self.clock() + max(0.0, life))
            return token, got

    def drop(self, alias: str, scope: str | None = None) -> None:
        """Forget the tokens of `alias`: the one of a region or of a project id, or all of them for None."""
        with self._lock:
            for key in [k for k in self._held if k[0] == alias and (scope is None or k[1] == scope)]:
                del self._held[key]

    def listing(self, alias: str) -> list[dict] | None:
        with self._lock:
            held = self._lists.get(alias)
            return list(held[0]) if held and held[1] > self.clock() else None

    def keep_listing(self, alias: str, projects: list[dict]) -> None:
        with self._lock:
            self._lists[alias] = (list(projects), self.clock() + LIST_LIFE)

    def clear(self) -> None:
        with self._lock:
            self._held = {}
            self._lists = {}

    def values(self) -> list[str]:
        with self._lock:
            return [t for t, _, _ in self._held.values()]


# --------------------------------------------------------------------------- the projects of a tenant


def region_of(name) -> str | None:
    """The region of a project by its name: eu-de of eu-de and of eu-de_<suffix>; None for any other name."""
    if not isinstance(name, str) or not _SCOPE_NAME_RE.match(name):
        return None
    return name.partition("_")[0]


def projects_of(data) -> list[dict]:
    """[{id, name, region, domain_id}] of an answer of GET /v3/auth/projects; a row without a readable id is
    left out."""
    rows = data.get("projects") if isinstance(data, dict) else None
    out = []
    for p in rows if isinstance(rows, list) else []:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and _DOMAIN_ID_RE.match(p["id"]):
            out.append({"id": p["id"], "name": str(p.get("name") or ""), "region": region_of(p.get("name")),
                        "domain_id": str(p.get("domain_id") or "")})
    return out


def readable(projects: list[dict], entry: dict) -> list[dict]:
    """The projects of the tenant's domain in one of its regions, by name."""
    return sorted((p for p in projects if p["domain_id"] == entry["domain_id"] and p["region"] in entry["regions"]),
                  key=lambda p: p["name"])


def check_scope(scope) -> str:
    if not isinstance(scope, str) or not (_DOMAIN_ID_RE.match(scope) or _SCOPE_NAME_RE.match(scope)):
        raise ReadonlyError(SCOPE_LINE)
    return scope


def choose(projects: list[dict], scope: str, alias: str, entry: dict) -> dict:
    """The project `scope` (a name or an id) of the list, after its domain and its region are checked."""
    found = ([p for p in projects if p["id"] == scope]
             or [p for p in projects if p["name"] == scope and p["domain_id"] == entry["domain_id"]]
             or [p for p in projects if p["name"] == scope])
    if not found:
        raise ReadonlyError(UNKNOWN_PROJECT_LINE % (scope, alias, alias))
    target = found[0]
    if target["domain_id"] != entry["domain_id"]:
        raise ReadonlyError(OTHER_DOMAIN_LINE % (scope, alias))
    if target["region"] is None:
        raise ReadonlyError(NO_REGION_LINE % scope)
    if target["region"] not in entry["regions"]:
        raise ReadonlyError(REGION_LINE % (scope, target["region"], alias, "+".join(entry["regions"]), alias))
    return target


# --------------------------------------------------------------------------- the owner's commands


def add(alias: str, domain_id: str, auth: str | None, regions: list[str], *, store=None, registered=(),
        settings: dict | None = None, settings_path: Path | None = None) -> list[str]:
    """Register a read-only tenant in keys.conf after checking its pass entries by name. Returns the lines to
    print; the caller reloads the key service."""
    from awb.tcp import tenants as _tenants, xchg

    if config.is_work_user():
        raise ReadonlyError("read-only tenants are added on the owner side")
    if not _ALIAS_RE.match(alias or ""):
        raise ReadonlyError("an alias reads like cust-1: small letters, digits and dashes")
    if not _DOMAIN_ID_RE.match(domain_id or ""):
        raise ReadonlyError("--domain-id is the 32 hex characters of the account's domain")
    regions = regions or ["eu-de"]
    if not all(_REGION_RE.match(r) for r in regions):
        raise ReadonlyError("a region reads like eu-de")
    store = store or _tenants.Store()
    entry = "%s/%s" % (_tenants.TENANT_PREFIX, alias)
    has_key = all(store.has("%s/%s" % (entry, k)) for k in ("ak", "sk"))
    has_login = all(store.has("%s/login/%s" % (entry, k)) for k in LOGIN_PARTS)
    if auth is None:
        if has_key and has_login:
            raise ReadonlyError("%s holds a key and a login in pass: choose --key or --login" % alias)
        auth = "key" if has_key else "login"
    if auth == "key" and not has_key:
        raise ReadonlyError("%s/ak and %s/sk are missing: pass insert them first" % (entry, entry))
    if auth == "login" and not has_login:
        missing = [k for k in LOGIN_PARTS if not store.has("%s/login/%s" % (entry, k))]
        raise ReadonlyError("%s is missing: pass insert it first" % ", ".join("%s/login/%s" % (entry, k)
                                                                             for k in missing))
    if any(store.has("%s/lab/%s" % (entry, k)) for k in ("ak", "sk")) or _has_secret(store, entry):
        raise ReadonlyError("a read-only tenant holds no lab key and no secret: pass rm the lab/ and secret/ "
                            "entries of %s first" % alias)
    if alias in registered:
        raise ReadonlyError("%s is a test tenant of awb tenant add: a read-only tenant takes an alias of its own"
                            % alias)
    settings = xchg.read_settings(settings_path) if settings is None else settings
    if settings.get("bucket_tenant") == alias:
        raise ReadonlyError("%s holds the exchange buckets: it cannot be read-only" % alias)
    entries = of_settings(settings)
    before = entries.get(alias)
    entries[alias] = {"auth": auth, "domain_id": domain_id, "regions": tuple(regions)}
    write_setting(entries, settings_path)
    return ["tenant %s: read-only, %s, %s: %s in keys.conf" % (alias, auth, "+".join(regions),
                                                                 "changed" if before else "registered")]


def set_regions(alias: str, regions: list[str], *, settings_path: Path | None = None) -> list[str]:
    """Change the regions of a registered read-only tenant; its way in and its domain stay, no login is asked.
    The caller reloads the key service."""
    from awb.tcp import xchg

    if config.is_work_user():
        raise ReadonlyError("read-only tenants are changed on the owner side")
    if not regions or not all(_REGION_RE.match(r or "") for r in regions):
        raise ReadonlyError("name the regions with --region, each like eu-de")
    regions = list(dict.fromkeys(regions))
    entries = of_settings(xchg.read_settings(settings_path))
    if alias not in entries:
        raise ReadonlyError("%s is not a read-only tenant (awb tenant add-readonly)" % alias)
    before = entries[alias]["regions"]
    if tuple(regions) == tuple(before):
        return ["tenant %s: regions %s already" % (alias, "+".join(regions))]
    entries[alias] = dict(entries[alias], regions=tuple(regions))
    write_setting(entries, settings_path)
    return ["tenant %s: regions %s, were %s: changed in keys.conf" % (alias, "+".join(regions), "+".join(before))]


def _has_secret(store, entry: str) -> bool:
    root = getattr(store, "root", None)
    if root is None:
        return False
    folder = Path(root) / entry / "secret"
    return folder.is_dir() and any(folder.glob("*.gpg"))


def remove(p: config.Paths, alias: str, *, settings_path: Path | None = None) -> list[str]:
    """Forget a read-only tenant: its line in keys.conf and its grants. The pass entries stay (his to remove)."""
    from awb.tcp import xchg

    if config.is_work_user():
        raise ReadonlyError("read-only tenants are removed on the owner side")
    entries = of_settings(xchg.read_settings(settings_path))
    if alias not in entries:
        raise ReadonlyError("%s is not a read-only tenant: awb tenant remove takes read-only tenants only" % alias)
    del entries[alias]
    write_setting(entries, settings_path)
    taken = revoke(p, alias) if load_grants(p).get(alias) else 0
    return ["tenant %s: removed from keys.conf, %d grant(s) taken" % (alias, taken),
            "the pass entries stay: pass rm -r awb/tenant/%s removes them" % alias]


# --------------------------------------------------------------------------- the interactive login (UX change 5)

LOGIN_PROMPTS = (("domain", "Account name of the customer (the login's domain name, not the domain id): ", False),
                 ("user", "User name: ", False),
                 ("password", "Password (hidden, not shown, not logged): ", True))
TEST_PATH = "/v3/projects"
"""The one GET of the test: the projects of the login's domain, by the name of the region."""
NO_ACCESS_LINE = ("test GET failed (403): the user has no programmatic access: the customer's admin ticks "
                  "\"Programmatic access\" for the user")
WRONG_LINE = ("test GET failed (401): the password is wrong or the user must still change it at the first console "
              "login: the customer's admin or the user fixes it, then enter the password again")
MFA_NEEDED_LINE = ("test GET failed: login protection by virtual MFA is on: the customer's admin sets login "
                   "protection off for this user")
TEST_OK_LINE = "test GET through the key service: ok (the login gets a token and reads IAM)"


def terminal_ready() -> bool:
    """True when the owner sits at a terminal (the questions are asked only then)."""
    import sys

    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def read_line(prompt: str) -> str:
    return input(prompt)


def read_secret(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


def ask_login(alias: str, store, *, tty=None, line=None, secret=None) -> list[str]:
    """Ask on the terminal for the login entries that `pass` lacks and write them to awb/tenant/ALIAS/login/ through
    the store (standard input of `pass insert`). The password is read hidden and goes nowhere else: no echo, no log,
    no message. Returns the names of the parts it asked for; nothing when all three are there. Raises ReadonlyError
    when entries are missing and there is no terminal, or a value is empty."""
    if config.is_work_user():
        raise ReadonlyError("read-only tenants are added on the owner side")
    if not _ALIAS_RE.match(alias or ""):
        raise ReadonlyError("an alias reads like cust-1: small letters, digits and dashes")
    entry = "awb/tenant/%s/login" % alias
    missing = [(k, q, hidden) for k, q, hidden in LOGIN_PROMPTS if not store.has("%s/%s" % (entry, k))]
    if not missing:
        return []
    if not (tty or terminal_ready)():
        raise ReadonlyError("%s is missing: pass insert it first (or run this in a terminal and it asks)" %
                            ", ".join("%s/%s" % (entry, k) for k, _, _ in missing))
    line, secret = line or read_line, secret or read_secret
    asked = []
    for key, prompt, hidden in missing:
        value = (secret if hidden else line)(prompt)
        value = value.strip("\r\n") if hidden else value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ReadonlyError("the %s is empty or carries a control character: nothing was written for it" % key)
        try:
            store.insert("%s/%s" % (entry, key), value)
        finally:
            value = ""
        asked.append(key)
    return asked


def explain_test(answer: dict) -> tuple[bool, str]:
    """(passed, one line) of the answer of the key service to the test GET: 403 no programmatic access, 401 password
    change pending or wrong, MFA needed. The line names a status and a cause, never a value."""
    if not isinstance(answer, dict):
        return False, "test GET failed: no readable answer from the key service"
    status = answer.get("status")
    if answer.get("ok") and status == 200:
        return True, TEST_OK_LINE
    error = str(answer.get("error") or "")
    if error == MFA_LINE or (status in (401, 403) and _MFA_RE.search(str(answer.get("text") or ""))):
        return False, MFA_NEEDED_LINE
    if error == CONSOLE_ONLY_LINE or "IAM.0081" in error or status == 403 or "HTTP 403" in error:
        return False, NO_ACCESS_LINE
    if "HTTP 401" in error or status == 401:
        return False, WRONG_LINE
    if answer.get("ok") is False:
        return False, "test GET failed: %s" % (error[:160] or "the key service refused")
    return False, "test GET failed (HTTP %s)" % (status if isinstance(status, int) else "?")


def check_login(alias: str, region: str, request=None) -> tuple[bool, str]:
    """One GET of the projects of the region through the key service, as the owner, before any grant."""
    from awb.tcp import keys

    request = request or keys.request
    try:
        answer = request(keys.call_socket(), {"op": "call", "tenant": alias, "role": "read", "method": "GET",
                                              "service": "iam", "path": TEST_PATH, "region": region,
                                              "query": {"name": region}}, timeout=90)
    except keys.KeysError as err:
        return False, "test GET not run: %s" % err
    return explain_test(answer)


CUSTOMER_TEXT = (
    "Text for the customer's admin (copy it into a mail):\n\n"
    "Deutsch:\n"
    "Bitte legen Sie in Ihrem Konto einen Benutzer an, der nur lesen darf (eine Read-only-Rolle, zum Beispiel "
    "Tenant Guest). Setzen Sie bei dem Benutzer \"Programmatischer Zugriff\" (Programmatic access), aber erzeugen "
    "Sie keinen Access Key. Schalten Sie den Anmeldeschutz per virtueller MFA für diesen Benutzer aus. Beim "
    "ersten Login darf keine Passwortänderung mehr offen sein. Danke.\n\n"
    "English:\n"
    "Please create a user in your account that may only read (a read-only role, for example Tenant Guest). "
    "Tick \"Programmatic access\" for the user, but do not create an access key. Turn login protection by "
    "virtual MFA off for this user. No password change may be pending for the user. Thank you.")
