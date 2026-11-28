"""The owner's grant of one existing bucket to one project (ask 2026-11-27-tcp-7c7b-2).

    awb tenant bucket-grant TENANT BUCKET CODE --owner-word TEXT [--day YYYY-MM-DD]   owner side: CODE owns BUCKET
    awb tenant bucket-revoke TENANT BUCKET                                             owner side: take it back
    awb tenant bucket-grants                                                           owner side: every grant

A session reaches on a test tenant only the buckets named after its project (<code>-...). A bucket the owner made
before, under another name, and gave to one project alone is granted here: one entry TENANT BUCKET PROJECT with his
words and the day, in `<vault>/bucket-grants.json` (the owner's, mode 600, like readonly-grants.json). The tenant is
written by its name tcp-NNNNN; its old name test-NNNNN is taken through awb/tenantname.py and matches as well.

What a grant does in the key service: for the granted project the bucket counts as its own for an OBS read of
`awb cloud call` and for `awb lab obs` (get, put, and their listing); for every other project the bucket is refused,
the tags read of the sweep included. An OBS write of `awb cloud call` stays on the buckets named after the project,
and a leased key (lease_policy) keeps its policy of `<code>-*` buckets: that is an IAM policy, another code path,
which this grant does not widen. An ungranted bucket keeps the old rule. A missing file, a file that does not parse
or one entry that is not well formed grants nothing at all.

The granted bucket is one the owner made before, and nothing checks what it holds: `awb lab obs get` brings its
objects into the project with no entry check, an entry beside `awb import`. The grant says so in one line
(ENTRY_LINE) and his word is given with that in mind: a bucket that ever held customer files is not granted. A
grant writes nothing to OBS itself; the granted project does, with `awb lab obs put` and a lab call of its open run
(G11 of the attacker review of 2026-11-28: the two were said apart).

A grant has an end: `close_lines` names the grants a project still holds with the revoke command of each, for
`awb close` to print and refuse on, so a closed code keeps no line here.

A grant is refused for the exchange buckets, for a bucket named after a project (that bucket is already the
project's own, or another project's) and for a bucket granted to another project already (revoke it first).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from awb import codes, config, tenantname

NAME = "bucket-grants.json"
MAX_GRANTS = 200
_TENANT_RE = re.compile(r"^tcp-[0-9]{5,6}\Z")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PROJECT_NAMED_RE = re.compile(r"^%s-" % codes.TCP_PROJECT_WIDE)
_CODE_RE = re.compile(r"^%s\Z" % codes.TCP_PROJECT_WIDE)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
FIELDS = ("tenant", "bucket", "project", "word", "day")

REFUSED_LINE = "refused: the bucket %s is granted to another project"
ENTRY_LINE = ("note: nothing checks what %s holds: awb lab obs get brings its objects into %s with no entry check, "
              "beside awb import, and awb lab obs put writes into it; grant only a bucket that never held customer "
              "files")
CLOSE_LINE = "%s still holds the owner's grant of the bucket %s on %s: awb tenant bucket-revoke %s %s"


class BucketGrantError(Exception):
    """The grant cannot be written; the text names the reason, never a value of the vault."""


def path(p: config.Paths) -> Path:
    return p.vault / NAME


def _well_formed(e) -> bool:
    if not isinstance(e, dict) or set(e) != set(FIELDS) or not all(isinstance(e[k], str) for k in FIELDS):
        return False
    if not (_TENANT_RE.match(e["tenant"]) and _BUCKET_RE.match(e["bucket"]) and _CODE_RE.match(e["project"])
            and not _PROJECT_NAMED_RE.match(e["bucket"]) and e["word"].strip() and _DAY_RE.match(e["day"])):
        return False
    try:
        date.fromisoformat(e["day"])
    except ValueError:
        return False
    return True


def load(p: config.Paths) -> list[dict]:
    """The owner's grants; nothing when the file is missing, does not parse, holds one entry that is not well formed
    or names one bucket of a tenant twice (fail closed)."""
    try:
        data = json.loads(path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list) or len(data) > MAX_GRANTS or not all(_well_formed(e) for e in data):
        return []
    seen = {(e["tenant"], e["bucket"]) for e in data}
    if len(seen) != len(data):
        return []
    return [dict(e) for e in data]


def _names(alias) -> set[str]:
    if not isinstance(alias, str):
        return set()
    twin = tenantname.twin(alias)
    return {alias} | ({twin} if twin else set())


def holder(p: config.Paths, alias, bucket) -> str | None:
    """The project the owner granted `bucket` of the tenant `alias` (tcp-NNNNN or test-NNNNN), or None."""
    if not isinstance(bucket, str):
        return None
    names = _names(alias)
    return next((e["project"] for e in load(p) if e["tenant"] in names and e["bucket"] == bucket), None)


def _load_for_write(p: config.Paths) -> list[dict]:
    """The grants before a change; a file that is there but grants nothing for being broken is never overwritten."""
    data = load(p)
    if not data and path(p).exists():
        try:
            empty = json.loads(path(p).read_text(encoding="utf-8")) == []
        except (OSError, ValueError):
            empty = False
        if not empty:
            raise BucketGrantError("%s is broken and grants nothing: fix or remove it first" % NAME)
    return data


def _save(p: config.Paths, data: list[dict]) -> None:
    if config.is_work_user():
        raise BucketGrantError("a bucket grant is the owner's: he runs awb tenant bucket-grant in his own terminal")
    p.vault.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bucket-grants.", dir=p.vault)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(sorted(data, key=lambda e: (e["tenant"], e["bucket"])), fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path(p))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _tenant(alias, say=None) -> str:
    try:
        name = tenantname.canonical(alias, say=say)
    except tenantname.OldNameRefused as err:
        raise BucketGrantError(str(err)) from None
    if not isinstance(name, str) or not _TENANT_RE.match(name):
        raise BucketGrantError("a bucket grant names a tenant tcp-NNNNN (or its old name test-NNNNN)")
    return name


def _exchange_buckets() -> set[str]:
    from awb.tcp import xchg

    try:
        settings = xchg.read_settings()
    except Exception:
        settings = {}
    return {str(settings.get(k) or xchg.DEFAULTS[k]) for k in ("lab_bucket", "owner_bucket")}


def grant(p: config.Paths, alias, bucket, code, word, day: str | None = None, say=None,
          exchange: set[str] | None = None) -> bool:
    """Grant `bucket` of the tenant to the project `code` with his words; False when it was granted already."""
    from awb import ownerword

    tenant = _tenant(alias, say)
    if not isinstance(bucket, str) or not _BUCKET_RE.match(bucket):
        raise BucketGrantError("a bucket name is 3 to 63 lowercase letters, digits, dots and dashes")
    if not isinstance(code, str) or not _CODE_RE.match(code):
        raise BucketGrantError("a bucket grant names a project code (tcp-q7m4)")
    if _PROJECT_NAMED_RE.match(bucket):
        raise BucketGrantError("a bucket named after a project needs no grant: it is that project's own")
    if bucket in (_exchange_buckets() if exchange is None else exchange):
        raise BucketGrantError("the exchange buckets are reached through awb xchg only")
    try:
        words = ownerword.clean(word)
    except ownerword.WordError as err:
        raise BucketGrantError(str(err)) from None
    day = day or date.today().isoformat()
    if not _DAY_RE.match(day):
        raise BucketGrantError("--day takes a day, YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise BucketGrantError("--day takes a day, YYYY-MM-DD") from None
    data = _load_for_write(p)
    had = next((e for e in data if e["tenant"] == tenant and e["bucket"] == bucket), None)
    if had is not None and had["project"] != code:
        raise BucketGrantError("the bucket is granted to another project: awb tenant bucket-revoke %s %s first"
                               % (tenant, bucket))
    if had is not None:
        return False
    data.append({"tenant": tenant, "bucket": bucket, "project": code, "word": words, "day": day})
    _save(p, data)
    if say is not None:
        say(ENTRY_LINE % (bucket, code))
    return True


def of_project(p: config.Paths, code) -> list[dict]:
    """The grants the project `code` holds, by tenant and bucket."""
    return sorted((e for e in load(p) if e["project"] == code), key=lambda e: (e["tenant"], e["bucket"]))


def close_lines(p: config.Paths, code) -> list[str]:
    """One line for each grant the project still holds, with the revoke command of the owner; [] when it holds
    none. `awb close` prints them and refuses without --force: a closed code keeps no grant."""
    return [CLOSE_LINE % (code, e["bucket"], e["tenant"], e["tenant"], e["bucket"]) for e in of_project(p, code)]


def revoke(p: config.Paths, alias, bucket, say=None) -> bool:
    """Take the grant of `bucket` of the tenant back; False when there was none."""
    tenant = _tenant(alias, say)
    data = _load_for_write(p)
    kept = [e for e in data if not (e["tenant"] == tenant and e["bucket"] == bucket)]
    if len(kept) == len(data):
        return False
    _save(p, kept)
    return True


def list_lines(p: config.Paths) -> list[str]:
    data = load(p)
    if not data:
        return ["no bucket grant"]
    return ['%s %s %s: owner\'s word: "%s", %s' % (e["tenant"], e["bucket"], e["project"], e["word"], e["day"])
            for e in sorted(data, key=lambda e: (e["tenant"], e["bucket"]))]
