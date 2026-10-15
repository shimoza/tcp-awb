"""The watchdog of the fact-check service: the reaper of expired resources of the service project and the owner's mail
(design F5 of 2026-10-14, sections 5 and 7).

    awb kb recheck-watch [--project CODE] [--tenant ALIAS]...

The ExecStart of awb-kb-recheck-watch.service, which its root owned timer starts every 15 minutes as the owner
(seal/setup.sh). The owner's alone: the work user has no mail path.

1. Run ends: every line of `<shared>/recheck/state.tsv` (one per run of `awb kb recheck-run`) that no mail has carried
   yet goes into one mail of its own through the key service's notify: the tier, the date, the counts, the exit, the
   inventory and where the evidence is. A line whose date, tier or counts do not match is passed over; a cell outside
   the vocabulary of the board is left out. A mail that was not sent is tried again at the next call.
2. Sweep: per tenant (`--tenant`, repeatable; without it every tenant of the key service that holds a read and a lab
   key), `sweep.sweep` with the read key over the sources the key service can list (every source of the sweep and
   the named sources of FK3, but the ELB and NAT v2.0 listings, which need a header the key service does not send).
   It takes every resource whose project tag (or the code in its name) is the service project (default tcp-n7e6) and
   whose `awb-expiry` has passed. A resource of another project, one not expired yet and one without an expiry are
   never touched. A listing that cannot be read is named and passed over.
3. Delete, servers first and networks last, one at a time, with the lab key: a resource whose row in the project's
   RESOURCES.md names the lab run that made it (`awb lab run RECIPE DATE`) is deleted by that recipe's teardown call
   for its type and proven gone by the recipe's read; one no row names (or whose teardown needs a value only the run
   had) by the delete call of its type (DELETES, to confirm live) and proven gone by the listing of its type. A
   delete answered 2xx or 404 is followed by the read until it proves the resource gone or the time is up.
4. Record: each resource in RESOURCES.md of the project, its row by its id (a new row when none names it), state
   deleted with the proof or live with LEFT LIVE, the proof and the call that deletes it. Where this user cannot write
   the project's folder (on a sealed host it is the work user's), the rows go to RESOURCES-watch.md in the owner's
   state folder in the same form; the output says so.
5. Leftovers: the resources it could not delete go into one mail, each once: its handle, its kind and type, the
   tenant alias and why, never an id or a name. The mark holds the sha256 of the id (never the id) and is written only
   when the mail was sent.

The marks (recheck-watch.tsv), the fallback rows and this user's handles live in the owner's state folder
(`$XDG_STATE_HOME/awb`, default `~/.local/state/awb`, as `sweep.owner_handles_path`); the unit points XDG_STATE_HOME
at its own state folder `/var/lib/awb-kb-recheck-watch`, the one place it may write.

Exit codes: 0 done (nothing left live); 2 refused or the key service gives no tenant; 4 a resource is left live.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from awb import config, jobs

EXIT_OK, EXIT_STOPPED, EXIT_LEFT = 0, 2, 4
WATCH_TSV = "recheck-watch.tsv"
RECORD_MD = "RESOURCES-watch.md"
STATE_MAX = 64 << 10
EVERY = 10
GONE_WITHIN = 180
MAIL_MAX = 4000             # keys.NOTIFY_MAX: the longest message the admin socket takes
ORDER = ("ecs", "rds", "evs", "eip", "smns", "smn", "waf", "sg", "sub", "vpc")
"""The order of the deletes: what holds an address or a port before the network it sits in."""
GONE_STATES = {"ecs": ("DELETED",)}
"""A listed state that counts as gone: a deleted server may list as DELETED for a while."""
# the delete call of each kind the sweep lists, filled from the listed item (${id} its id, ${vpc_id} a field of it).
# The ecs call is the one of recipes/ecs-boot-ssh.json; the others follow the list paths of awb/tcp/sweep.py and are
# to confirm live
DELETES = {
    "ecs": {"method": "POST", "service": "ecs", "path": "/v1/{project_id}/cloudservers/delete",
            "body": {"servers": [{"id": "${id}"}], "delete_publicip": True, "delete_volume": True}},
    "evs": {"method": "DELETE", "service": "evs", "path": "/v2/{project_id}/cloudvolumes/${id}"},
    "eip": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/publicips/${id}"},
    "vpc": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/vpcs/${id}"},
    "sub": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/vpcs/${vpc_id}/subnets/${id}"},
    "sg": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/security-groups/${id}"},
    "waf": {"method": "DELETE", "service": "waf", "path": "/v1/{project_id}/waf/policy/${id}"},
    "smn": {"method": "DELETE", "service": "smn", "path": "/v2/{project_id}/notifications/topics/${id}"},
    "smns": {"method": "DELETE", "service": "smn", "path": "/v2/{project_id}/notifications/subscriptions/${id}"},
    "rds": {"method": "DELETE", "service": "rds", "path": "/v3/{project_id}/instances/${id}"},
}
EXIT_WORDS = {"0": "went through", "2": "stopped before its first call", "4": "a resource left live or the inventory "
              "differs"}
_LAB_LABEL_RE = re.compile(r"^awb lab run (?P<recipe>[a-z][a-z0-9-]{2,40}) \d{4}-\d{2}-\d{2}\b")
_HANDLE_RE = re.compile(r"^(?P<kind>[a-z]{2,5})-(?P<n>[1-9][0-9]{0,5})$")


class WatchError(Exception):
    """A refusal before the first call, with one line for the owner."""


# --------------------------------------------------------------------------- files of the shared side


def _read(folder: Path, name: str, cap: int = STATE_MAX) -> str | None:
    """A regular file, no link followed at its last step, under a cap, UTF-8; None when it is not there. Raises
    OSError when it is there and cannot be read."""
    try:
        fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise OSError("too large")
    return data.decode("utf-8", errors="replace")


def _replace(folder: Path, name: str, text: str) -> None:
    """Write a file atomically through a handle of its folder opened without following a link (the folder may be
    the work user's). Raises OSError."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp = ".%s.%d.tmp" % (name, os.getpid())
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=fd)
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=fd)
        with os.fdopen(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def id_mark(rid: str) -> str:
    """What the mark of a leftover keeps of its id: the first 16 hex digits of its sha256, never the id."""
    return hashlib.sha256(rid.encode("utf-8")).hexdigest()[:16]


@dataclass
class Marks:
    """`<shared>/recheck/watch.tsv`: `mailed\\tN` (the lines of state.tsv a mail carried) and one
    `left\\tTENANT\\tKIND\\tMARK\\tDATE` per leftover that was mailed."""
    mailed: int | None
    left: dict[tuple[str, str, str], str]

    @classmethod
    def read(cls, folder: Path) -> "Marks":
        try:
            text = _read(folder, WATCH_TSV) or ""
        except OSError:
            text = ""
        mailed, left = None, {}
        for line in text.splitlines():
            cells = line.split("\t")
            if cells[0] == "mailed" and len(cells) == 2 and cells[1].isdigit():
                mailed = int(cells[1])
            elif cells[0] == "left" and len(cells) == 5:
                left[(cells[1], cells[2], cells[3])] = cells[4]
        return cls(mailed, left)

    def text(self) -> str:
        lines = [] if self.mailed is None else ["mailed\t%d" % self.mailed]
        lines += ["left\t%s\t%s\t%s\t%s" % (k + (v,)) for k, v in sorted(self.left.items())]
        return "".join(x + "\n" for x in lines)


# --------------------------------------------------------------------------- the run ends of state.tsv


def run_end(line: str) -> dict | None:
    """The cells of one line of state.tsv (awb/tcp/recheck.py state_line) or None when its date, tier or counts do not
    match; an exit or an inventory outside the vocabulary of the board is left empty."""
    from awb.tcp import board

    cells = line.split("\t")
    if len(cells) != 8:
        return None
    day, tier, conf, ref, und, owner, code, inv = cells
    if not board._DATE.fullmatch(day) or tier not in board.RECHECK_TIERS or not all(
            board._COUNT.fullmatch(c) for c in (conf, ref, und, owner)):
        return None
    return {"date": day, "tier": tier, "confirmed": conf, "refuted": ref, "undecided": und, "owner": owner,
            "exit": code if code in board.RECHECK_EXITS else "",
            "inventory": inv if inv in board.RECHECK_INVENTORY else ""}


def run_end_mail(r: dict, project: str) -> tuple[str, str]:
    """(subject, message) of the mail of one run end: fixed words, counts and codes."""
    subject = "Workbench recheck: %s run of %s%s" % (r["tier"], r["date"],
                                                    (", exit %s" % r["exit"]) if r["exit"] else "")
    lines = ["The %s run of awb kb recheck-run of %s in %s ended." % (r["tier"], r["date"], project),
             "verdicts: %s confirmed, %s refuted, %s undecided" % (r["confirmed"], r["refuted"], r["undecided"]),
             "for the owner: %s" % r["owner"]]
    if r["exit"]:
        lines.append("exit: %s (%s)" % (r["exit"], EXIT_WORDS[r["exit"]]))
    if r["inventory"]:
        lines.append("inventory: %s" % r["inventory"])
    lines.append("evidence: evidence/recheck-%s-<n>/RESULT.md of %s" % (r["date"], project))
    if r["owner"] != "0":
        lines.append("next: the list for the owner in RESULT.md waits on your word, entry by entry")
    if r["exit"] == "4":
        lines.append("next: awb kb recheck-watch deletes what is left once its expiry has passed; RESOURCES.md of %s "
                     "names each" % project)
    return subject, "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- RESOURCES.md


def table_rows(text: str) -> list[dict]:
    """The rows of a RESOURCES.md table as dicts by the header's columns, the header and the rule left out."""
    head, out = None, []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if head is None:
            head = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        out.append(dict(zip(head, cells)))
    return out


def _row_line(cells: list[str]) -> str:
    return "| %s |\n" % " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells)


def with_row(text: str, rid: str, cells: list[str]) -> str:
    """The table with the row of `rid` (the id column, backticks aside) replaced by `cells` (or `cells` added)."""
    lines = text.splitlines(keepends=True)
    head = None
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        row = [c.strip() for c in line.strip().strip("|").split("|")]
        if head is None:
            head = [c.lower() for c in row]
            continue
        if dict(zip(head, row)).get("id", "").strip("`") == rid:
            lines[i] = _row_line(cells)
            return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines) + _row_line(cells)


def record_template(project: str) -> str:
    from awb import projects

    return ("# Resources of %s the watchdog handled\n\n"
            "Rows of `awb kb recheck-watch` for RESOURCES.md of %s, which the owner could not write: the same "
            "columns, one row per resource by its id.\n\n| %s |\n|%s\n"
            % (project, project, " | ".join(projects.RESOURCE_COLUMNS), "---|" * len(projects.RESOURCE_COLUMNS)))


# --------------------------------------------------------------------------- one resource


@dataclass
class Target:
    tenant: str
    kind: str
    handle: str
    rid: str
    expiry: str
    raw: dict
    source: tuple
    row: dict | None = None
    state: str = "live"
    proof: str = ""
    how: str = ""
    owner_line: str = ""


def default_mail(subject: str, message: str) -> bool:
    """A mail through the key service's notify (the admin socket admits the owner's uid), as `awb build nudge` and
    `awb ui watch --mail` send theirs. True when it was sent."""
    from awb.tcp import keys

    try:
        answer = keys.request(keys.admin_socket(), {"op": "notify", "subject": subject, "message": message},
                              timeout=60)
    except (OSError, ValueError):
        return False
    return bool(answer.get("ok") and answer.get("sent"))


class Watch:
    """One call of the watchdog. `send(req) -> answer` is the key service's call socket and `mail(subject, message)`
    its notify (stand-ins in the tests); `clock` and `sleep` are the time of the gone reads."""

    def __init__(self, project: str, tenants: list[str], *, send=None, mail=None, clock=time.monotonic,
                 sleep=time.sleep, now: datetime.datetime | None = None, out=print, err=None):
        from awb.tcp import lab, sweep

        self.project = project
        self.tenants = list(tenants)
        self.send = send or lab._send_default
        self.mail = mail or default_mail
        self.clock, self.sleep = clock, sleep
        self.now = now or datetime.datetime.now(datetime.timezone.utc)
        self.today = self.now.date()
        self.out = out
        self.err = err or (lambda line: print(line, file=sys.stderr))
        self.p = config.paths()
        self.dir = sweep.owner_handles_path().parent        # the owner's state folder: the marks and the rows
        self.folder = self.project_folder()
        self.handles, self.handles_kept = self.open_handles()
        self.marks = Marks.read(self.dir)
        self.marks_changed = False

    def say(self, line: str) -> None:
        self.out("recheck-watch: %s" % line)

    def project_folder(self) -> Path | None:
        """The folder of the service project from the register, whatever its state; None when it has none."""
        from awb import projects

        try:
            row = next((r for r in projects.load(self.p) if r.code == self.project), None)
        except Exception:
            return None
        return Path(row.path) if row is not None and Path(row.path).is_dir() else None

    def open_handles(self):
        """(handles, kept): the shared handles where this user may write them, else this user's own; in memory only
        (kept False) when neither can be written (the timer's home is read-only)."""
        from awb.tcp import sweep

        try:
            return sweep.open_handles(self.p)[0], True
        except sweep.SweepError:
            pass
        try:
            return sweep.Handles(sweep.owner_handles_path()), False
        except sweep.SweepError:
            return sweep.Handles(self.dir / ".none"), False

    # ----------------------------------------------------------------- step 1, the run ends

    def mail_run_ends(self) -> None:
        from awb.tcp import recheck

        try:
            text = _read(self.p.shared.joinpath(*recheck.STATE_TSV[:-1]), recheck.STATE_TSV[-1])
        except OSError:
            self.say("state.tsv cannot be read; no run end mailed")
            return
        lines = (text or "").splitlines()
        done = self.marks.mailed
        if done is None or done > len(lines):       # the first call or a file begun anew: from its first line
            done = 0
        for i in range(done, len(lines)):
            r = run_end(lines[i])
            if r is not None:
                subject, message = run_end_mail(r, self.project)
                if not self._mail(subject, message):
                    self.say("run end of %s (%s): mail not sent, tried again next time" % (r["date"], r["tier"]))
                    break
                self.say("run end of %s (%s): mail sent" % (r["date"], r["tier"]))
            done = i + 1
        if done != self.marks.mailed:
            self.marks.mailed = done
            self.marks_changed = True

    def _mail(self, subject: str, message: str) -> bool:
        try:
            return bool(self.mail(subject, message[:MAIL_MAX]))
        except Exception:
            return False

    # ----------------------------------------------------------------- step 2, the sweep

    def tenant_aliases(self) -> list[str]:
        if self.tenants:
            return self.tenants
        try:
            a = self.send({"op": "tenants"})
        except Exception:
            raise WatchError("the key service does not answer") from None
        if not a.get("ok") or not isinstance(a.get("tenants"), dict):
            raise WatchError("the key service gives no tenant (%s)" % str(a.get("error") or "no answer")[:80])
        out = sorted(alias for alias, t in a["tenants"].items()
                     if isinstance(t, dict) and {"read", "lab"} <= set(t.get("roles") or ()))
        if not out:
            raise WatchError("the key service holds no tenant with a read and a lab key (locked?)")
        return out

    def expired(self, tenant: str) -> list[Target]:
        from awb.tcp import recheck, sweep

        sources = recheck._sources()
        base = recheck._lister(self.send, tenant, self.project)
        seen: dict[str, jobs.Listing] = {}

        def lister(service, path, key, paging, query=None, project_header=False):
            seen[path] = base(service, path, key, paging, query=query, project_header=project_header)
            return seen[path]

        rep = sweep.sweep(lister, self.today, self.handles, None, sources)
        if rep.unknown:
            self.say("%s: could not read %s; those listings are passed over" % (tenant, ", ".join(rep.unknown)))
        out = []
        for item in rep.items:
            if item.project != self.project or "expired" not in item.reasons:
                continue
            if item.state in GONE_STATES.get(item.kind, ()):
                continue
            source = next(s for s in sources if s[0] == item.kind)
            rid = self.handles.resolve(item.handle)
            raw = next((r for r in seen[source[2]].items if sweep._id_and_name(source, r)[0] == rid), {})
            out.append(Target(tenant, item.kind, item.handle, rid, item.expiry, raw, source))
        return out

    # ----------------------------------------------------------------- step 3, the delete

    def rows(self) -> list[dict]:
        if self.folder is None:
            return []
        try:
            return table_rows(_read(self.folder, "RESOURCES.md") or "")
        except OSError:
            return []

    def teardown_of(self, t: Target) -> tuple[dict, dict | None, int]:
        """(delete call, gone proof of the recipe or None for the listing, seconds) of a resource. Raises WatchError
        when there is no call for it."""
        from awb.tcp import lab

        row = t.row or {}
        m = _LAB_LABEL_RE.match(row.get("note", ""))
        if m:
            try:
                r = lab.load(lab.find(m.group("recipe")))
                step = next(s for s in r["steps"] if s["do"] == "create" and s["resource"]["kind"] == t.kind
                            and s["resource"]["type"] == row.get("type"))
                td = r["teardown"][step["id"]]
                env = {"id": t.rid, "project": self.project, "tenant": t.tenant,
                       "region": row.get("region") or "eu-de"}
                call, gone = lab.expand(td["call"], env), dict(td["gone"], call=lab.expand(td["gone"]["call"], env))
                t.how = "the teardown of recipe %s" % m.group("recipe")
                return call, gone, int(td["gone"].get("within", GONE_WITHIN))
            except (lab.LabError, lab.Stop, StopIteration, KeyError, TypeError):
                pass        # the recipe is gone, changed or needs a value only its run had: the type's call
        if t.kind not in DELETES:
            raise WatchError("no delete call is known for the kind %s" % t.kind)
        env = {k: v for k, v in t.raw.items() if isinstance(v, str)}
        env["id"] = t.rid
        try:
            call = lab.expand(DELETES[t.kind], env)
        except lab.Stop as exc:
            raise WatchError("the delete call of the kind %s has %s" % (t.kind, exc)) from None
        t.how = "the delete call of the kind %s" % t.kind
        return call, None, 300 if t.kind == "ecs" else GONE_WITHIN

    def _call(self, t: Target, call: dict) -> dict:
        req = {"op": "call", "tenant": t.tenant, "role": "lab", "method": call["method"], "service": call["service"],
               "path": call["path"], "region": (t.row or {}).get("region") or None, "query": call.get("query") or {},
               "body": call.get("body"), "project": self.project}
        if call["method"] in ("GET", "HEAD"):
            req["role"] = "read"
        try:
            return self.send(req)
        except Exception as exc:
            return {"ok": True, "status": 0, "data": None, "text": "no answer (%s)" % type(exc).__name__}

    @staticmethod
    def status(answer: dict) -> int:
        return int(answer.get("status") or 0) if answer.get("ok") else 0

    def gone(self, t: Target, proof: dict | None) -> tuple[bool, str]:
        from awb.tcp import lab, recheck, sweep

        if proof is not None:
            a = self._call(t, proof["call"])
            st = self.status(a)
            if a.get("ok") and st in (proof.get("status") or []):
                return True, "the read answered %d" % st
            if a.get("ok") and "field" in proof and 200 <= st < 300:
                value = lab.dig(a.get("data"), proof["field"])
                if value in proof.get("in", ()):
                    return True, "the read answered %s %s" % (proof["field"], value)
            return False, "the read answered %s" % (st or "nothing")
        source = t.source
        listing = sweep._list(recheck._lister(self.send, t.tenant, self.project), source)
        if listing.state == jobs.UNKNOWN:
            return False, "the listing of %s cannot be read" % t.kind
        for raw in listing.items:
            if sweep._id_and_name(source, raw)[0] == t.rid:
                if sweep._state(t.kind, raw) in GONE_STATES.get(t.kind, ()):
                    return True, "the listing of %s holds it as %s" % (t.kind, sweep._state(t.kind, raw))
                return False, "the listing of %s still holds it" % t.kind
        return True, "the listing of %s no longer holds it" % t.kind

    def delete(self, t: Target) -> None:
        try:
            call, proof, within = self.teardown_of(t)
        except WatchError as exc:
            t.state, t.proof = "live", str(exc)
            return
        t.owner_line = "delete it with awb cloud call %s %s %s --tenant %s --role lab%s --project %s" % (
            call["method"], call["service"], call["path"], t.tenant,
            " --body FILE" if call.get("body") is not None else "", self.project)
        stop_at = self.clock() + within
        deleted, said = False, ""
        while True:
            if not deleted:
                a = self._call(t, call)
                if not a.get("ok"):
                    t.state = "live"
                    t.proof = "the key service refused the delete (%s)" % self.handles.mask(str(a.get("error")))[:120]
                    return
                st = self.status(a)
                deleted = 200 <= st < 300 or st == 404
                said = "the delete answered %s" % (st or "nothing")
            if deleted:
                gone, said = self.gone(t, proof)
                if gone:
                    t.state, t.proof = "deleted", said
                    return
            if self.clock() + EVERY > stop_at:
                t.state = "live"
                t.proof = said if not deleted else "still there after %d s (%s)" % (within, said)
                return
            self.sleep(EVERY)

    # ----------------------------------------------------------------- step 4, the record

    def record(self, t: Target) -> None:
        row = t.row or {}
        when = "awb kb recheck-watch %s" % self.today.isoformat()
        if t.state == "deleted":
            note = "%s: deleted after its expiry by %s, %s" % (when, t.how, t.proof)
        else:
            note = "%s: LEFT LIVE after its expiry: %s%s, then set this row to deleted" % (
                when, t.proof, ("; " + t.owner_line) if t.owner_line else "; delete it by hand")
        cells = [row.get("handle") or t.handle, t.rid, row.get("type") or t.kind, row.get("region") or "-",
                 row.get("cost class") or "-", row.get("expiry") or t.expiry, t.state, note]
        if self.folder is not None:
            try:
                text = _read(self.folder, "RESOURCES.md")
                if text is None:
                    from awb import projects

                    text = projects._resources(self.project)
                cells[0] = t.handle = self._free_handle(cells[0], t, text)
                _replace(self.folder, "RESOURCES.md", with_row(text, t.rid, cells))
                return
            except OSError:
                pass
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            text = _read(self.dir, RECORD_MD) or record_template(self.project)
            cells[0] = t.handle = self._free_handle(cells[0], t, text)
            _replace(self.dir, RECORD_MD, with_row(text, t.rid, cells))
            self.say("RESOURCES.md of %s cannot be written here; the row of %s went to %s in the owner's state "
                     "folder" % (self.project, cells[0], RECORD_MD))
        except OSError:
            self.err("awb kb recheck-watch: the row of %s could not be written anywhere" % cells[0])

    def _free_handle(self, handle: str, t: Target, text: str) -> str:
        """The handle of a new row: the sweep's, unless another id's row of the file already carries it."""
        rows = table_rows(text)
        if t.row is not None or not any(r.get("handle") == handle and r.get("id", "").strip("`") != t.rid
                                        for r in rows):
            return handle
        used = [int(m.group("n")) for m in (_HANDLE_RE.match(r.get("handle", "")) for r in rows)
                if m and m.group("kind") == t.kind]
        return "%s-%d" % (t.kind, max(used + [0]) + 1)

    # ----------------------------------------------------------------- step 5 and the whole call

    def mail_leftovers(self, left: list[Target]) -> None:
        new = [t for t in left if (t.tenant, t.kind, id_mark(t.rid)) not in self.marks.left]
        if not new:
            return
        lines = ["awb kb recheck-watch could not delete %d expired resource(s) of %s:" % (len(new), self.project)]
        lines += ["- %s (%s, %s) on %s: %s" % ((t.row or {}).get("handle") or t.handle, t.kind,
                                             (t.row or {}).get("type") or t.kind, t.tenant, t.proof) for t in new]
        lines.append("Each has its row in RESOURCES.md of %s (or in %s of the owner's state folder) with the call that "
                     "deletes it; set the row to deleted after that. This mail comes once per resource."
                     % (self.project, RECORD_MD))
        if not self._mail("Workbench recheck-watch %s: %d resource(s) left live" % (self.project, len(new)),
                          self.handles.mask("\n".join(lines) + "\n")):
            self.say("%d resource(s) left live: mail not sent, tried again next time" % len(new))
            return
        for t in new:
            self.marks.left[(t.tenant, t.kind, id_mark(t.rid))] = self.today.isoformat()
        self.marks_changed = True
        self.say("%d resource(s) left live: mail sent" % len(new))

    def execute(self) -> int:
        self.mail_run_ends()
        code = EXIT_OK
        try:
            tenants = self.tenant_aliases()
        except WatchError as exc:
            self.err("awb kb recheck-watch: %s" % exc)
            self.save()
            return EXIT_STOPPED
        targets: list[Target] = []
        for tenant in tenants:
            targets += self.expired(tenant)
        rows = {r.get("id", "").strip("`"): r for r in self.rows()}
        for t in targets:
            t.row = rows.get(t.rid)
        targets.sort(key=lambda t: (ORDER.index(t.kind) if t.kind in ORDER else len(ORDER), t.tenant, t.handle))
        if not targets:
            self.say("no expired resource of %s on %s" % (self.project, ", ".join(tenants)))
        left: list[Target] = []
        for t in targets:
            self.delete(t)
            self.record(t)
            name = (t.row or {}).get("handle") or t.handle
            if t.state == "deleted":
                self.say("%s (%s) on %s: deleted and proven gone (%s)" % (name, t.kind, t.tenant, t.proof))
                if self.marks.left.pop((t.tenant, t.kind, id_mark(t.rid)), None) is not None:
                    self.marks_changed = True
            else:
                self.say("%s (%s) on %s: LEFT LIVE (%s)" % (name, t.kind, t.tenant, self.handles.mask(t.proof)))
                left.append(t)
                code = EXIT_LEFT
        self.mail_leftovers(left)
        self.save()
        return code

    def save(self) -> None:
        if self.marks_changed:
            try:
                self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                _replace(self.dir, WATCH_TSV, self.marks.text())
            except OSError:
                self.err("awb kb recheck-watch: the marks could not be written; a mail may come again")
        if self.handles_kept:
            with contextlib.suppress(Exception):
                self.handles.save()


def run(project: str, tenants: list[str], *, send=None, mail=None, clock=time.monotonic, sleep=time.sleep, now=None,
        out=print, err=None) -> int:
    from awb import codes

    err = err or (lambda line: print(line, file=sys.stderr))
    if config.is_work_user() or config.is_build_user():
        err("awb kb recheck-watch: the watchdog is the owner's (the work user has no mail path)")
        return EXIT_STOPPED
    if not codes.is_project_code(project or ""):
        err("awb kb recheck-watch: a project code reads like tcp-q7m4")
        return EXIT_STOPPED
    if not all(re.match(r"^[a-z][a-z0-9-]{1,30}$", t) for t in tenants):
        err("awb kb recheck-watch: a tenant alias reads like test-1")
        return EXIT_STOPPED
    return Watch(project, tenants, send=send, mail=mail, clock=clock, sleep=sleep, now=now, out=out,
                 err=err).execute()


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser
    from awb.tcp import recheck

    ap = SafeParser(prog="awb kb recheck-watch",
                    description="Delete the expired resources of the service project and mail the owner.")
    ap.add_argument("--project", default=recheck.SERVICE_PROJECT)
    ap.add_argument("--tenant", action="append", default=[], dest="tenants", metavar="ALIAS")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_STOPPED
    return run(args.project, args.tenants)


if __name__ == "__main__":
    sys.exit(main())
