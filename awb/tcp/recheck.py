"""The recheck run of the fact-check service: expired entries checked again by their check block (design F4 of
2026-10-14, section 3).

    awb kb recheck-run [--tier read|write] [--id KB-XXXX]... [--project CODE] [--tenant ALIAS] [--dry-run]

Without --tenant a run takes the tenant from `recheck_tenant` of the host file (awb/config.py: AWB_CONF or the root
host file, the root file alone for the work user), so that no tenant alias of a host is written into the
repository; with neither it stops.

The steps, each a line of the run's RESULT.md:

1. Pick: the expired entries (`kb.expired`) with a check block of the tier (default read), oldest expiry first;
   `--id` narrows them to the ids given. An entry without a block, with another tier or whose block is a decision
   tier or needs what the host cannot give goes to the lists "no block", "other tier" and "for the owner" and is not
   touched; so does an entry whose grade is not live: a run checks live facts again and never raises a grade, so a
   statement of the grade said, docs or contract with a check block waits for the owner (review of 2026-10-16,
   finding R3). An id given that has not expired goes to "not expired". `--dry-run` stops here and prints the calls
   or the recipe and its parameters of every entry it would check.
2. Inventory before, for a write run: `sweep.inventory` over the sources the key service can list (every source of
   the sweep and the named sources, but the ELB and NAT v2.0 listings, which need a header the key service does not
   send), with the read key, to inventory-before.txt. A listing that cannot be read stops the run before any create.
   A read run creates nothing and takes no inventory.
3. Check, one entry at a time. A read tier sends its calls (`GET service path`, `;`-separated) through the key
   service with the read key; its lines confirms, refutes and control are tests the run reads (below). A write tier
   runs its recipe as a lab run (the class Run of awb/tcp/lab.py) in the project, with the rows in RESOURCES.md, the
   tags and the teardown the lab does, its lifetime cut to 30 minutes (`lab.capped`). Its verdict follows the
   recipe's checks: every check passed is confirmed, a failed check whose id is `control` is undecided, another
   failed check is refuted, a run that stopped is undecided.
4. Delete: the recipe's own teardown, proven gone with a read. A resource left live sets the run to exit 4 and every
   further entry is skipped.
5. Inventory after: the same listing. Digests that differ end the run with "inventory differs: TYPE +n/-n" (counts,
   never ids), exit 4 and no write-back of a write-tier entry.
6. Write back through the checks of the knowledge base: confirmed without an event is `kb.recheck` (the entry's own
   grade, never a higher one, the evidence path as the source, for a negative the calls and the control as the tried
   texts of the new check);
   confirmed or refuted with an event is `kb.amend` with one more line "On DATE the state is: ..." (the confirms or
   the refutes line) and the run's date as the check date, the oldest such line dropped when there are three already;
   refuted or undecided without an event writes nothing: the entry stays expired and the result lists it (D2).

A test of a read tier reads `[N:]PATH OP VALUE`: N the number of the call (1 when left out), PATH `status` or a
dotted path into the call's answer in which `list[key=value]` picks the first item whose key has that value (a dot
inside the brackets is part of the value), OP one of `is`, `is not`, `has`, `lacks` (has: the value holds VALUE as a
part of its text or as an item of its list), VALUE the rest of the line. Example:
`flavors[name=s9.medium.1].os-extra_specs.cond:operation:az has eu-de-01(normal)`. The verdict: a call that was
refused, did not answer or answered other than 2xx is undecided; a line that is not a test is undecided; a control
that does not hold is undecided; confirms true and refutes false is confirmed, the reverse refuted, anything else
undecided.

The evidence goes to `evidence/recheck-<date>-<n>/` of the project: RESULT.md, the two inventories, calls.log
(answers cut at 1500 characters, the query of every web address cut off, ids as handles, a line with a secret class
withheld, an answer in which the name check finds a name withheld) and one file per checked entry (a write tier's
file is its lab evidence). Every run that got past the pick writes one line to `<shared>/recheck/state.tsv`:
date, tier, confirmed, refuted, undecided, for the owner, exit, inventory (equal, differs, unknown or none). The
board reads it (awb/tcp/board.py).

Exit codes: 0 the run went through (whatever the verdicts); 2 a usage error or a run refused or stopped before its
first call; 4 a resource is left live or the inventory differs.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from awb import codes, jobs, kb

SERVICE_PROJECT = "tcp-n7e6"
"""The standing service project of the design (kind project, no customer, granted on the lab test tenant)."""
TIERS = ("read", "write")
EXIT_OK, EXIT_STOPPED, EXIT_LEFT = 0, 2, 4
ANSWER_CHARS = 1500
ERROR_CHARS = 200
MAX_STATE_SENTENCES = 3
STATE_TSV = ("recheck", "state.tsv")
VERDICTS = ("confirmed", "refuted", "undecided")
_TEST_RE = re.compile(r"^(?:(?P<n>[1-9]):)?(?P<path>\S+) (?P<op>is not|is|has|lacks) (?P<value>.+)$")
_STATE_RE = re.compile(r"^On \d{4}-\d{2}-\d{2} the state is: ")
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")


class RecheckError(Exception):
    """A refusal before the first call, with one line for the owner."""


def _sources():
    """The sources of the inventory: every source of the sweep and the named ones the key service can list. The ELB
    and NAT v2.0 listings send an X-Project-Id header, which a call of the key service does not carry."""
    from awb.tcp import sweep

    return tuple(s for s in sweep.ALL_SOURCES + sweep.NAMED_SOURCES if not s[6])


# --------------------------------------------------------------------------- pick


@dataclass
class Pick:
    run: list[kb.Entry] = field(default_factory=list)
    no_block: list[kb.Entry] = field(default_factory=list)
    other_tier: list[kb.Entry] = field(default_factory=list)
    owner: list[kb.Entry] = field(default_factory=list)
    not_expired: list[str] = field(default_factory=list)


def pick(tier: str, ids=(), *, where=None, today: datetime.date | None = None) -> Pick:
    """Step 1: the expired entries sorted into the run and the lists that are not touched. An entry whose grade is
    not live goes to the owner's list whatever its block says: a run never raises a grade. An id that is not in the
    knowledge base raises KBError."""
    today = today or datetime.date.today()
    expired = kb.expired(where, today)
    if ids:
        for i in ids:
            kb.get(i, where)
        known = {e.id for e in expired}
        out = Pick(not_expired=[i for i in dict.fromkeys(ids) if i not in known])
        expired = [e for e in expired if e.id in set(ids)]
    else:
        out = Pick()
    for e in expired:
        b = e.block
        if not b:
            out.no_block.append(e)
        elif b.get("tier") == "decision" or b.get("needs", "none") != "none" or e.grade != "live":
            out.owner.append(e)
        elif b.get("tier") != tier:
            out.other_tier.append(e)
        else:
            out.run.append(e)
    return out


def recipe_call(calls: str) -> tuple[str, list[str]]:
    """(recipe name, its k=v parameters) of a write tier's calls line."""
    parts = calls.split()
    if len(parts) < 2 or parts[0] != "recipe":
        raise RecheckError("the calls line names no recipe")
    return parts[1], parts[2:]


def read_calls(calls: str) -> list[tuple[str, str, str, dict]]:
    """(method, service, path, query) of each call of a read tier's calls line."""
    out = []
    for part in calls.split(";"):
        bits = part.strip().split(" ", 2)
        if len(bits) != 3 or bits[0] not in ("GET", "HEAD"):
            raise RecheckError("a read tier sends GET or HEAD calls only")
        path, _, query = bits[2].partition("?")
        out.append((bits[0], bits[1], path, {k: v for k, v in urllib.parse.parse_qs(query).items()}))
    return out


def dry_run_lines(p: Pick, tier: str) -> list[str]:
    out = []
    for e in p.run:
        calls = e.block["calls"]
        if tier == "write":
            try:
                name, params = recipe_call(calls)
                out.append("%s: recipe %s%s" % (e.id, name, (" with " + " ".join(params)) if params else ""))
            except RecheckError as err:
                out.append("%s: %s" % (e.id, err))
        else:
            out.append("%s: %s" % (e.id, calls))
    return out + list_lines(p)


def owner_reason(e: kb.Entry) -> str:
    """Why an entry is the owner's: what its block needs, else its grade (a run checks live facts only)."""
    b = e.block
    if b.get("tier") == "decision" or b.get("needs", "none") != "none":
        return "needs %s" % b.get("needs", "none")
    return "grade %s, not live: a run never raises a grade" % e.grade


def list_lines(p: Pick) -> list[str]:
    out = ["checked: %d" % len(p.run)]
    for label, entries in (("no block", p.no_block), ("other tier", p.other_tier)):
        out.append("%s: %d%s" % (label, len(entries), (" (%s)" % " ".join(e.id for e in entries)) if entries else ""))
    out.append("for the owner: %d" % len(p.owner))
    out += ["  %s (%s): %s" % (e.id, e.block.get("tier"), owner_reason(e)) for e in p.owner]
    if p.not_expired:
        out.append("not expired, not checked: %s" % " ".join(p.not_expired))
    return out


# --------------------------------------------------------------------------- the tests of a read tier


def _split_path(path: str) -> list[str]:
    parts, cur, depth = [], "", 0
    for ch in path:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "." and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def dig(obj, path: str):
    """The value at PATH in an answer, None when a part is absent."""
    for part in _split_path(path):
        key, _, sel = part.partition("[")
        if key:
            if isinstance(obj, dict) and key in obj:
                obj = obj[key]
            elif isinstance(obj, list) and key.isdigit() and int(key) < len(obj):
                obj = obj[int(key)]
            else:
                return None
        if sel:
            name, eq, want = sel.rstrip("]").partition("=")
            if not eq or not isinstance(obj, list):
                return None
            obj = next((x for x in obj if isinstance(x, dict) and str(x.get(name)) == want), None)
            if obj is None:
                return None
    return obj


def parse_test(line: str):
    """(call number, path, op, value) of a test line, None when the line is not a test."""
    m = _TEST_RE.match(line.strip())
    if not m:
        return None
    return int(m.group("n") or 1), m.group("path"), m.group("op"), m.group("value").strip()


def holds(test, answers: list[dict]) -> bool | None:
    """True or False for a test over the answers; None when it names a call that was not made."""
    n, path, op, want = test
    if n > len(answers):
        return None
    answer = answers[n - 1]
    value = int(answer.get("status") or 0) if path == "status" else dig(answer.get("data"), path)
    if op in ("is", "is not"):
        same = value is not None and (str(value).lower() if isinstance(value, bool) else str(value)) == want
        return same if op == "is" else not same
    if isinstance(value, list):
        inside = any(str(x) == want for x in value) or any(want in str(x) for x in value)
    else:
        inside = value is not None and want in (value if isinstance(value, str) else json.dumps(value))
    return inside if op == "has" else not inside


def read_verdict(block: dict, answers: list[dict]) -> tuple[str, str]:
    """(verdict, why) of a read tier from its answers."""
    for i, a in enumerate(answers, start=1):
        if not a.get("ok"):
            return "undecided", "call %d was refused by the key service" % i
        st = int(a.get("status") or 0)
        if not 200 <= st < 300:
            return "undecided", "call %d answered %s" % (i, st or "nothing")
    results = {}
    for key in ("confirms", "refutes", "control"):
        test = parse_test(block.get(key, ""))
        if test is None:
            return "undecided", "the %s line is not a test the run reads" % key
        results[key] = holds(test, answers)
        if results[key] is None:
            return "undecided", "the %s line names a call the block does not make" % key
    if not results["control"]:
        return "undecided", "the control does not hold: the answer cannot tell a confirm from a refute"
    if results["confirms"] and not results["refutes"]:
        return "confirmed", "confirms holds, refutes does not"
    if results["refutes"] and not results["confirms"]:
        return "refuted", "refutes holds, confirms does not"
    return "undecided", "confirms and refutes both %s" % ("hold" if results["confirms"] else "fail")


# --------------------------------------------------------------------------- the run


@dataclass
class Outcome:
    entry: kb.Entry
    verdict: str = "undecided"
    why: str = ""
    file: str = ""
    written: str = ""          # what the write-back did, empty before step 6


def _clean_text(text: str, handles) -> str:
    """A text for the evidence: the query of every address cut off, ids as handles, a line with a secret class
    withheld; withheld whole when the name check finds a name or cannot run."""
    from awb import check, config
    from awb.tcp import lab

    text = _URL_QUERY_RE.sub(r"\1", text)
    text = handles.mask(text)
    try:
        if check.check_text(text, config.paths().register):
            return "(withheld: the name check found a name)"
    except Exception:
        return "(withheld: the name check could not run)"
    return lab.Run._scrub(text).rstrip("\n")


def _lister(send, tenant: str, project: str, max_pages: int = 200):
    """A lister of the sweep over the key service, with the read key."""
    def lister(service, path, key, paging, query=None, project_header=False):
        q = dict(query or {})
        limit = 100
        if paging != "none":
            q["limit"] = str(limit)
        items: list = []
        for _ in range(max_pages):
            try:
                a = send({"op": "call", "tenant": tenant, "role": "read", "method": "GET", "service": service,
                          "path": path, "region": None, "query": q, "body": None, "project": project})
            except Exception as err:
                return jobs.Listing.unknown("no answer (%s)" % type(err).__name__)
            if not a.get("ok") or not 200 <= int(a.get("status") or 0) < 300:
                return jobs.Listing.unknown("HTTP %s" % (a.get("status") or "refused"))
            page = jobs.dig(a.get("data"), key)
            if not isinstance(page, list):
                return jobs.Listing.unknown("the answer holds no list under %s" % key)
            items.extend(page)
            if paging == "none" or len(page) < limit:
                return jobs.Listing.of(items)
            if paging == "marker":
                last = jobs.dig(page, -1, "id")
                if not isinstance(last, str) or not last:
                    return jobs.Listing.unknown("the last item of a page has no id to go on from")
                q["marker"] = last
            else:
                q["offset"] = str(int(q.get("offset", "0")) + len(page))
        return jobs.Listing.unknown("more than %d pages" % max_pages)
    return lister


def evidence_dir(folder: Path, day: datetime.date) -> Path:
    base = folder / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    stem = "recheck-%s-" % day.isoformat()
    taken = [int(p.name[len(stem):]) for p in base.glob(stem + "*") if p.name[len(stem):].isdigit()]
    out = base / ("%s%d" % (stem, max(taken + [0]) + 1))
    out.mkdir()
    return out


def state_sentence(statement: str, day: datetime.date, line: str) -> str:
    """The statement with one more line "On DATE the state is: LINE.", the oldest such line dropped while there would
    be more than three."""
    lines = statement.split("\n")
    states = [i for i, x in enumerate(lines) if _STATE_RE.match(x)]
    while len(states) >= MAX_STATE_SENTENCES:
        del lines[states[0]]
        states = [i for i, x in enumerate(lines) if _STATE_RE.match(x)]
    lines.append("On %s the state is: %s." % (day.isoformat(), line.strip().rstrip(".")))
    return "\n".join(lines)


def state_line(day: datetime.date, tier: str, outcomes: list[Outcome], owner: int, code: int, inv: str) -> str:
    n = {v: sum(1 for o in outcomes if o.verdict == v) for v in VERDICTS}
    return "\t".join([day.isoformat(), tier, str(n["confirmed"]), str(n["refuted"]), str(n["undecided"]), str(owner),
                      str(code), inv]) + "\n"


class Recheck:
    """One recheck run. `send(req) -> answer` is the key service (a stand-in in the tests); `clock`, `sleep` and
    `now` are the time of the lab runs."""

    def __init__(self, tier: str, project: str, tenant: str, folder: Path, handles, *, send=None,
                 clock=time.monotonic, sleep=time.sleep, now: datetime.datetime | None = None, where=None,
                 out=print, err=None):
        from awb.tcp import lab

        self.tier = tier
        self.project = project
        self.tenant = tenant
        self.folder = Path(folder)
        self.handles = handles
        self.send = send or lab._send_default
        self.clock, self.sleep = clock, sleep
        self.now = now or datetime.datetime.now(datetime.timezone.utc)
        self.today = self.now.date()
        self.where = where
        self.out = out
        self.err = err or (lambda line: print(line, file=sys.stderr))
        self.steps: list[str] = []
        self.calls_log: list[str] = []
        self.dir: Path | None = None

    def rel(self, path: Path) -> str:
        return path.relative_to(self.folder).as_posix()

    def step(self, line: str) -> None:
        self.steps.append(line)
        self.out("recheck: %s" % line)

    # ----------------------------------------------------------------- step 3, one entry

    def check_read(self, e: kb.Entry) -> Outcome:
        o = Outcome(e)
        lines = ["# Recheck %s" % e.id, "", "- tier read, run of %s" % self.today.isoformat(), ""]
        try:
            calls = read_calls(e.block["calls"])
        except RecheckError as exc:
            o.why = str(exc)
            calls = []
        answers: list[dict] = []
        from awb.tcp import lab

        for i, (method, service, path, query) in enumerate(calls, start=1):
            try:
                a = self.send({"op": "call", "tenant": self.tenant, "role": "read", "method": method,
                               "service": service, "path": path, "region": None, "query": query, "body": None,
                               "project": self.project})
            except Exception as exc:            # no answer: a timeout, a socket gone; undecided, never confirmed
                a = {"ok": True, "status": 0, "data": None, "text": "no answer (%s)" % type(exc).__name__}
            answers.append(a)
            st = int(a.get("status") or 0) if a.get("ok") else 0
            said = "status %s" % (st if a.get("ok") else "refused (%s)" % _clean_text(str(a.get("error", "")),
                                                                                       self.handles)[:ERROR_CHARS])
            code, msg = lab.error_of(a) if a.get("ok") and not 200 <= st < 300 else ("", "")
            if code:
                said += ", error_code %s" % _clean_text(code, self.handles)[:40]
            if msg:
                said += ", message %s" % _clean_text(msg[:ERROR_CHARS], self.handles)
            lines.append("- call %d: %s %s %s: %s" % (i, method, service, path, said))
            body = a.get("data") if a.get("data") is not None else a.get("text", "")
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, sort_keys=True)
            self.calls_log.append("%s call %d: %s %s %s -> %s" % (e.id, i, method, service, path, said))
            self.calls_log.append("  " + _clean_text(text[:ANSWER_CHARS], self.handles).replace("\n", " "))
        if calls:
            o.verdict, o.why = read_verdict(e.block, answers)
        lines += ["", "- verdict: %s (%s)" % (o.verdict, o.why)]
        path = self.dir / ("%s.md" % e.id)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        o.file = self.rel(path)
        return o

    def check_write(self, e: kb.Entry) -> tuple[Outcome, int]:
        """(outcome, exit of the lab run) of a write tier: the recipe as a lab run in the project."""
        from awb.tcp import lab

        o = Outcome(e)
        path = self.dir / ("%s.md" % e.id)
        try:
            name, given = recipe_call(e.block["calls"])
            r = lab.capped(lab.load(lab.find(name)))
            params = lab.params_of(r, given)
            job = lab.Run(r, params, self.project, self.tenant, self.folder, self.handles, send=self.send,
                          clock=self.clock, sleep=self.sleep, out=lambda line: None, err=lambda line: None)
        except (RecheckError, lab.LabError) as exc:
            o.why = "refused before the first call: %s" % exc
            path.write_text("# Recheck %s\n\n- verdict: undecided (%s)\n" % (e.id, o.why), encoding="utf-8")
            o.file = self.rel(path)
            return o, lab.EXIT_STOPPED
        refusal = job.probe() or job.missing_secret() or job.disk_check()
        if refusal is not None:
            o.why = "refused before the first call: %s" % _clean_text(refusal, self.handles)
            path.write_text("# Recheck %s\n\n- verdict: undecided (%s)\n" % (e.id, o.why), encoding="utf-8")
            o.file = self.rel(path)
            return o, lab.EXIT_STOPPED
        code = job.execute()
        path.write_text(job.evidence(path), encoding="utf-8")
        o.file = self.rel(path)
        self.calls_log.append("%s: recipe %s, %d call(s), every call in %s" % (e.id, name, len(job.calls), o.file))
        failed = [c for c, _, result in job.checks if result == "failed"]
        if code == lab.EXIT_LEFT:
            o.why = "a resource is left live"
        elif job.outcome == "passed":
            o.verdict, o.why = "confirmed", "every check of the recipe passed"
        elif job.outcome == "check failed" and any(c["id"] == "control" for c in failed):
            o.why = "the control check failed: the answer cannot tell a confirm from a refute"
        elif job.outcome == "check failed":
            o.verdict, o.why = "refuted", "the check %s failed" % ", ".join(c["id"] for c in failed)
        else:
            o.why = "the run stopped (%s)" % job.outcome
        return o, code

    # ----------------------------------------------------------------- step 6

    def write_back(self, o: Outcome, run_dir: str) -> None:
        """Step 6. A confirmed entry keeps its own grade: the run never raises one (a planted statement of the grade
        said must not come out of a Sunday run as live) and `pick` lists an entry whose grade is not live for the
        owner before any call."""
        e, b = o.entry, o.entry.block
        try:
            if b.get("event") and o.verdict in ("confirmed", "refuted"):
                line = b["confirms"] if o.verdict == "confirmed" else b["refutes"]
                kb.amend(e.id, state_sentence(e.statement, self.today, line), checked=self.today, where=self.where,
                         today=self.today)
                o.written = "amended with the state of %s" % self.today.isoformat()
            elif o.verdict == "confirmed":
                tried = ()
                if kb.is_negative(e.statement):
                    tried = ("check calls %s" % b["calls"].replace(";", ","),
                             "check control %s" % b["control"].replace(";", ","))
                kb.recheck(e.id, grade=e.grade, source="recheck run, %s/%s.md" % (run_dir, e.id), tried=tried,
                           checked=self.today, where=self.where, today=self.today)
                o.written = "rechecked, grade %s kept" % e.grade
            else:
                o.written = "stays expired"
        except kb.Refused as ref:
            o.written = "stays expired: the knowledge base refused the write-back (%s)" % "; ".join(ref.reasons)
        except kb.KBError as exc:
            o.written = "stays expired: the knowledge base refused the write-back (%s)" % exc

    # ----------------------------------------------------------------- the whole run

    def execute(self, p: Pick) -> int:
        from awb.tcp import lab, sweep

        self.dir = evidence_dir(self.folder, self.today)
        run_dir = self.rel(self.dir)
        self.step("1 pick: %d entry(ies) of the tier %s; %d without a block, %d of another tier, %d for the owner"
                  % (len(p.run), self.tier, len(p.no_block), len(p.other_tier), len(p.owner)))
        code, inv_state, before = EXIT_OK, "none", None
        outcomes: list[Outcome] = []
        skipped: list[tuple[kb.Entry, str]] = []
        sources = _sources()
        lister = _lister(self.send, self.tenant, self.project)
        todo = list(p.run)
        if self.tier == "write" and todo:
            before = sweep.inventory(lister, sources)
            (self.dir / "inventory-before.txt").write_text("\n".join(sweep.inventory_lines(before)) + "\n",
                                                           encoding="utf-8")
            unknown = sorted(k for k, v in before.items() if v is None)
            if unknown:
                self.step("2 inventory before: cannot read %s; the run stops before any create" % ", ".join(unknown))
                skipped = [(e, "the inventory before cannot be read") for e in todo]
                todo, code, inv_state = [], EXIT_STOPPED, "unknown"
            else:
                self.step("2 inventory before: %d type(s) read" % len(before))
        else:
            self.step("2 inventory before: not taken (a read run creates nothing)" if self.tier == "read"
                      else "2 inventory before: not taken (nothing to check)")
        for i, e in enumerate(todo):
            if self.tier == "read":
                o = self.check_read(e)
            else:
                o, lab_code = self.check_write(e)
                if lab_code == lab.EXIT_LEFT:
                    outcomes.append(o)
                    code = EXIT_LEFT
                    skipped += [(x, "a resource of an earlier entry is left live") for x in todo[i + 1:]]
                    self.step("3 check %s: %s (%s)" % (e.id, o.verdict, o.why))
                    self.step("4 delete: a resource is LEFT LIVE; its row in RESOURCES.md says what to do; the "
                              "further entries are skipped")
                    break
            outcomes.append(o)
            self.step("3 check %s: %s (%s)" % (e.id, o.verdict, o.why))
        if self.tier == "write" and outcomes and code != EXIT_LEFT:
            self.step("4 delete: every resource of the recipes deleted and proven gone")
        write_back = True
        if before is not None and inv_state != "unknown":
            after = sweep.inventory(lister, sources)
            (self.dir / "inventory-after.txt").write_text("\n".join(sweep.inventory_lines(after)) + "\n",
                                                          encoding="utf-8")
            diff = sweep.inventory_diff(before, after)
            if diff:
                inv_state, code, write_back = ("unknown" if all(d.endswith(" unknown") for d in diff) else
                                               "differs"), EXIT_LEFT, False
                self.step("5 inventory after: inventory differs: %s; no write-back" % ", ".join(diff))
            else:
                inv_state = "equal"
                self.step("5 inventory after: inventory equal")
        else:
            self.step("5 inventory after: not taken")
        for o in outcomes:
            if write_back:
                self.write_back(o, run_dir)
            else:
                o.written = "not written: the inventory differs"
            self.step("6 write back %s: %s" % (o.entry.id, o.written))
        self.result(p, outcomes, skipped, code, inv_state)
        try:
            self.handles.save()
        except Exception:
            self.err("awb kb recheck-run: the handles could not be saved")
        return code

    def result(self, p: Pick, outcomes: list[Outcome], skipped, code: int, inv_state: str) -> None:
        from awb import config

        lines = ["# Recheck run %s, tier %s" % (self.today.isoformat(), self.tier), "",
                 "- project %s, tenant %s, exit %d, inventory %s" % (self.project, self.tenant, code, inv_state), "",
                 "## Steps", ""] + ["- %s" % s for s in self.steps] + ["", "## Entries", ""]
        for o in outcomes:
            lines.append("- %s: %s (%s); %s; %s" % (o.entry.id, o.verdict, o.why, o.written or "-", o.file))
        for e, why in skipped:
            lines.append("- %s: skipped (%s)" % (e.id, why))
        lines += ["", "## Not checked", ""] + ["- %s" % x for x in list_lines(p)]
        (self.dir / "RESULT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.dir / "calls.log").write_text("".join(x + "\n" for x in self.calls_log), encoding="utf-8")
        tsv = config.paths().shared.joinpath(*STATE_TSV)
        tsv.parent.mkdir(parents=True, exist_ok=True)
        with open(tsv, "a", encoding="utf-8") as fh:
            fh.write(state_line(self.today, self.tier, outcomes, len(p.owner), code, inv_state))
        n = {v: sum(1 for o in outcomes if o.verdict == v) for v in VERDICTS}
        self.out("recheck: %d confirmed, %d refuted, %d undecided, %d for the owner; evidence %s"
                 % (n["confirmed"], n["refuted"], n["undecided"], len(p.owner), self.rel(self.dir)))


def run(tier: str, ids, project: str, tenant: str | None, *, dry_run: bool = False, send=None,
        clock=time.monotonic, sleep=time.sleep, now=None, where=None, out=print, err=None) -> int:
    from awb import config
    from awb.tcp import lab, sweep

    err = err or (lambda line: print(line, file=sys.stderr))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        if tier not in TIERS:
            raise RecheckError("the tier is read or write; a decision tier stays the owner's")
        for i in ids:
            if not re.fullmatch(codes.KB, i):
                raise RecheckError("an id reads like KB-Q7M4")
        p = pick(tier, ids, where=where, today=now.date())
        if dry_run:
            for line in dry_run_lines(p, tier):
                out(line)
            return EXIT_OK
        tenant = tenant or config.host_conf().get("recheck_tenant")
        if not re.match(r"^[a-z][a-z0-9-]{1,30}$", tenant or ""):
            raise RecheckError("a run needs --tenant or recheck_tenant in the host file: a tenant alias reads like "
                               "test-1")
        folder = lab.project_folder(project)
        handles = sweep.Handles(sweep.handles_path(config.paths()))
    except (RecheckError, kb.KBError, lab.LabError, sweep.SweepError) as e:
        err("awb kb recheck-run: %s" % e)
        return EXIT_STOPPED
    job = Recheck(tier, project, tenant, folder, handles, send=send, clock=clock, sleep=sleep, now=now, where=where,
                  out=out, err=err)
    return job.execute(p)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb kb recheck-run",
                    description="Check expired entries again by their check block and write the result back.")
    ap.add_argument("--tier", choices=TIERS, default="read")
    ap.add_argument("--id", action="append", default=[], dest="ids", metavar="KB-XXXX")
    ap.add_argument("--project", default=SERVICE_PROJECT)
    ap.add_argument("--tenant")
    ap.add_argument("--dry-run", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_STOPPED
    return run(args.tier, args.ids, args.project, args.tenant, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
