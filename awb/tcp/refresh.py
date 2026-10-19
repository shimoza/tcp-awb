"""The refresh of what the Workbench knows about T Cloud Public (TCP): scripts per part and a report each run,
instead of one long prompt (phase 7, R-12, R-80, R-82, section 6 of the design).

    awb refresh [--part P]... [--no-update] [--max-state-age DAYS]
    awb refresh apply RESULTS.jsonl [--plants PLANTS.json]

Parts, in this order:
- mirrors: the service description and the documentation mirror brought up to date (owner side, unless
  --no-update); a new revision of the service description marks every contract entry for a re-check.
- release-notes: the release notes of T Cloud Public brought up to date (awb mirror rn, owner side, unless
  --no-update) and every note first seen since the last refresh read against the knowledge base and the services:
  "recheck: KB-XXXX (release note DATE SERVICE)" for each active entry that names the note's service and shares at
  least two topic words (title or first paragraph of the note against statement and tags; stop and generic words and
  the service names do not count), most shared words first, 15 per note and then "and N more" (--all lists every
  match); a note dated after today is listed in its own block "announced" with its date; "new service?" for a
  service that rules/services.tsv does not know. Nothing is changed, the lines are only listed (also in release-notes.txt next to the report). The first run, without a day to compare with, reads the
  notes dated in the last 30 days. The day of a run that updated goes to <shared>/refresh/release-notes.json.
- prices: a snapshot of every region, unless there is one of today, and every value changed since the one before.
- knowledge: the expired entries of the knowledge base, each with the way it can be checked again, which follows
  from its grade and scope, with the tier of its check block ("no block" without one) and the block itself. An entry
  whose block a recheck run takes (tier read or write, needs none) names `awb kb recheck-run` and is that run's
  work; every other expired entry, first of all one without a block, counts as overdue. They go to worklist.jsonl;
  the expiry classes decide what is re-checked, nothing is re-read that has not expired. Every question file in
  <kb>/bench/ is run (awb kb bench), so each report carries the retrieval numbers.
- wipe: the red-team pack of wipe mode (calibration/redteam/wipe/) through the installed intake, rules and lists,
  the same intake.run as awb import, on the pack's invented texts only and in throw-away Workbenches. The pack's
  selfcheck runs first: when its planted leak comes out clean, nothing is recorded and the part ends in an error.
  The counts (cases, leaks, losses, by case module) go into the report and as one line into
  <shared>/refresh/wipe.tsv with the release; more leaks or more losses than the last line is overdue.
- projects: active projects whose STATE.md is older than --max-state-age days (14), with their open items.
- defaults: the pointers the Workbench carries still point somewhere: every command the work rules and
  COMMANDS.md name exists, every knowledge id the code and the rules cite exists and is not retired, and every
  contract entry that cites revisions of the service description cites the current one among them.

The report goes to <shared>/refresh/<date>/REPORT.md with worklist.jsonl next to it; a run of some parts writes
REPORT-<parts>.md instead, so it never overwrites the full report of the day. Exit 0 when nothing is overdue, 1
when something is, 2 on an error.

`apply` records the results of a re-check, one JSON line per entry: {"id", "verdict", "grade", "source",
"tried", "statement", "why", "checked", "batch"}. confirmed: `kb recheck` (grade, source, date and, for a
negative, the tried texts of the new check). changed: `kb amend` with the corrected statement, then `kb recheck`.
refuted: `kb retire` with the reason, and the corrected fact as a new entry when a statement is given.
unchecked: nothing changes; the reason goes to the output. Every write goes through the checks of the knowledge
base. The agents that produce the results read workflows/refresh-brief.md (first check) and
workflows/refresh-verify.md (second check); COMMANDS.md says how a re-check is run. With --plants, a file of planted false facts ({"PLANT-1": "batch-01", ...}): a batch that confirmed one of
its planted facts is not applied at all, because its checker has shown that it cannot fail.
"""
from __future__ import annotations

import datetime
import io
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import awb
from awb import codes, config, jobs, kb, offered, projects
from awb.tcp import board, dataset, mirror, price

PARTS = ("mirrors", "release-notes", "prices", "knowledge", "wipe", "projects", "defaults", "dataset")
VERDICTS = ("confirmed", "changed", "refuted", "unchecked")
MAX_STATE_AGE = 14
RN_FIRST_DAYS = 30
# tags of the release-notes page that name no service
RN_NOT_SERVICES = {"news for users"}
RN_LIMIT = 15                 # entries listed per note unless --all
RN_SHARED = 2                 # topic words an entry shares with a note to be listed
# words that make no topic: stop words and words every note has
RN_STOP = set("""a an and are as at be by can for from has have in into is it its of on or that the their this to
with will you your all also any more most new now not only other out over than then there these those up use used
using via was were when which who service services cloud support supports supported available availability
announce announced announcement release released general generally offer offered offers add added adds introduce
introduced introduces provide provides provided enable enables enabled allow allows allowed update updates updated
feature features""".split())
_KB_ID_RE = codes.KB_RE
_AWB_CMD_RE = re.compile(r"`awb ([a-z][a-z-]*)")
_REVISION_RE = re.compile(r"\b(?:revision|revised|Rev\.?)\s+(\d{2})\.(\d{2})\.(\d{4})\b", re.I)


@dataclass
class Part:
    name: str
    lines: list[str] = field(default_factory=list)
    overdue: int = 0
    error: str = ""


def refresh_dir(p: config.Paths, day: datetime.date) -> Path:
    return p.shared / "refresh" / day.isoformat()


# --------------------------------------------------------------------------- mirrors


def part_mirrors(base: Path, *, update: bool, job: jobs.Job) -> Part:
    part = Part("mirrors")
    before = mirror.current_sd(base)
    if update and not config.is_work_user():
        try:
            r = mirror.sync_sd(base)
            part.lines.append("service description: revision %s %s" % (r["revision"], r["state"]))
            if before is not None and r["current"] != before.parent.name:
                part.lines.append("NEW REVISION %s (was %s): every contract entry needs a re-check"
                                  % (r["current"], before.parent.name))
                part.overdue += 1
        except mirror.MirrorError as err:
            part.lines.append("service description: not updated (%s)" % err)
            part.overdue += 1
        listing = mirror.list_repos()
        if listing.state != jobs.LIST:
            part.lines.append("documentation: not updated, the repository list is %s" % listing.describe())
            part.overdue += 1
        else:
            try:
                counts = mirror.sync_docs(base, list(listing.items), job)
                part.lines.append("documentation: %s" % ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())))
                if counts.get("failed"):
                    part.overdue += 1
            except jobs.JobStopped as stop:
                part.lines.append("documentation: %s" % stop)
                part.overdue += 1
    part.lines += mirror.status(base)
    if mirror.current_sd(base) is None:
        part.lines.append("no service description in the mirror: contract entries cannot be checked here")
        part.overdue += 1
    return part


# --------------------------------------------------------------------------- release notes


def rn_state(p: config.Paths) -> Path:
    return p.shared / "refresh" / "release-notes.json"


def _forms_match(e: kb.Entry, s: offered.Service) -> bool:
    """True when the tags or the statement of the entry name the service: a tag equals a name or short name, the
    statement holds the name, or a short name of three letters or more written in capitals as a word."""
    tags = {offered.key(t) for t in e.tags}
    statement = " %s " % offered.norm(e.statement)
    for form in s.forms():
        if offered.key(form) in tags:
            return True
        if form.isupper() and len(form) >= 3:
            if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(form), e.statement):
                return True
        elif len(form) >= 3 and " %s " % offered.norm(form) in statement:
            return True
    return False


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> set[str]:
    """The significant words of a text: lower case, a plural made singular, no stop or generic words."""
    return {_stem(w) for w in offered.norm(text).split() if len(w) >= 3 and w not in RN_STOP and _stem(w) not in RN_STOP}


def note_topic(n: dict, names: set[str]) -> set[str]:
    """The topic words of a note: its title and first paragraph without the words of the service names."""
    first = (n.get("text") or "").strip().split("\n\n")[0]
    return _words("%s %s" % (n.get("title", ""), first)) - names


def release_note_lines(notes: list[dict], entries: list[kb.Entry], catalog: offered.Catalog, *,
                       today: datetime.date | None = None, limit: int | None = RN_LIMIT) -> list[str]:
    """For each note, oldest first: the active entries that name its services and share at least RN_SHARED topic
    words with it, most shared words first and at most `limit` of them (None: all, then no "and N more" line), and the
    services the catalog does not know. A note dated after `today` goes to the block "announced" at the end.
    Public names and codes only."""
    today = today or datetime.date.today()
    out: list[str] = []
    announced: list[str] = []
    for n in sorted(notes, key=lambda n: (n["date"], n["id"])):
        lines = announced if n["date"] > today.isoformat() else out
        topic: set[str] | None = None
        hits: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for name in n.get("services", []):
            if offered.norm(name) in RN_NOT_SERVICES:
                continue
            found = catalog.find(name)
            if not found:
                lines.append("new service?: %s (release note %s %s)" % (name, n["date"], n["title"]))
                continue
            if topic is None:
                topic = note_topic(n, set().union(*(_words(" ".join(s.forms())) for s in found), _words(name)))
            for e in entries:
                if e.is_retired or e.id in seen or not any(_forms_match(e, s) for s in found):
                    continue
                shared = len(topic & _words("%s %s" % (e.statement, " ".join(e.tags))))
                if shared >= RN_SHARED:
                    seen.add(e.id)
                    hits.append((shared, e.id, "recheck: %s (release note %s %s)" % (e.id, n["date"], found[0].name)))
        hits.sort(key=lambda h: (-h[0], h[1]))
        shown = hits if limit is None else hits[:limit]
        lines += [h[2] for h in shown]
        if len(shown) < len(hits):
            lines.append("and %d more (awb refresh --part release-notes --all)" % (len(hits) - len(shown)))
    if announced:
        out.append("announced:")
        out += ["  " + line for line in announced]
    return out


def part_release_notes(p: config.Paths, base: Path, out_dir: Path, *, update: bool, today: datetime.date,
                       show_all: bool = False) -> Part:
    part = Part("release-notes")
    if update and not config.is_work_user():
        try:
            r = mirror.sync_release_notes(base)
            part.lines.append("release notes: %d new, %d known, %d page(s) read" % (r["new"], r["known"], r["pages"]))
        except mirror.MirrorError as err:
            part.lines.append("release notes: not updated (%s)" % err)
            part.overdue += 1
    state = rn_state(p)
    try:
        last = json.loads(state.read_text(encoding="utf-8")).get("last", "")
    except (OSError, ValueError, AttributeError):
        last = ""
    notes = mirror.release_notes(base)
    if last:
        fresh = [n for n in notes if n.get("seen", "") > last]
        since = "first seen since %s" % last
    else:
        floor = (today - datetime.timedelta(days=RN_FIRST_DAYS)).isoformat()
        fresh = [n for n in notes if n.get("date", "") >= floor]
        since = "dated since %s, the first run" % floor
    if not notes:
        part.lines.append("no release notes in the mirror: run awb mirror rn")
        part.overdue += 1
        return part
    lines = release_note_lines(fresh, kb.load(p), offered.load(), today=today, limit=None if show_all else RN_LIMIT)
    part.lines.append("%d release note(s) %s; %d line(s) for a look" % (len(fresh), since, len(lines)))
    part.lines += ["  " + line for line in lines]
    part.overdue += len(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "release-notes.txt").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    if update and not config.is_work_user():
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"last": mirror._now()}) + "\n", encoding="utf-8")
    return part


# --------------------------------------------------------------------------- prices


def part_prices(p: config.Paths, *, today: datetime.date, job: jobs.Job) -> Part:
    part = Part("prices")
    for region in price.SNAPSHOT_REGIONS:
        folder = price.snapshot_dir(p) / region
        earlier = sorted(x for x in folder.glob("????-??-??.json") if x.stem < today.isoformat()) \
            if folder.is_dir() else []
        todays = folder / (today.isoformat() + ".json")
        if not todays.exists():
            f = price.fetch(None, region, job=job)
            try:
                price.write_snapshot(p, f, region, today=today)
            except price.PriceError as err:
                part.lines.append("%s: no snapshot (%s)" % (region, err))
                part.overdue += 1
                continue
        if not earlier:
            part.lines.append("%s: snapshot of %s, the first one: nothing to compare" % (region, today.isoformat()))
            continue
        _, old = price.load_snapshot(earlier[-1])
        _, new = price.load_snapshot(todays)
        d = price.diff(old, new)
        part.lines.append("%s: against %s: %s" % (region, earlier[-1].stem, d.lines()[0]))
        part.lines += ["  " + line for line in d.lines()[1:21]]
        if len(d.lines()) > 21:
            part.lines.append("  ... %d more, see awb price diff" % (len(d.lines()) - 21))
    return part


# --------------------------------------------------------------------------- knowledge


def tier_of(e: kb.Entry) -> str:
    """The tier of the entry's check block (read, write, decision, none) or "no block"."""
    return e.block.get("tier", "no block") if e.check else "no block"


def by_run(e: kb.Entry) -> bool:
    """True when awb kb recheck-run takes the entry: a block of the tier read or write that needs nothing."""
    return tier_of(e) in ("read", "write") and e.block.get("needs", "none") == "none"


def method_for(e: kb.Entry) -> str:
    """How an expired entry can be checked again: by its check block when a recheck run takes it, else from its
    scope, grade and tags."""
    if by_run(e):
        return "awb kb recheck-run --tier %s (its check block)" % tier_of(e)
    if e.scope != "tcp":
        return "%s: not from this host" % e.scope
    if "pricing" in e.tags:
        return "price API (awb price), else the service description"
    if e.grade == "contract":
        return "service description (current revision in the mirror)"
    if e.grade == "docs":
        return "documentation mirror"
    if e.grade == "live" and e.cls == "availability":
        return "read-only live call (awb cloud get) or the documentation mirror"
    if e.grade == "live":
        return "read-only live call where a GET shows it, else the API reference in the mirror (grade docs)"
    return "find a source: mirror, service description or a read-only call; without one it stays a lead"


def worklist(entries: list[kb.Entry], today: datetime.date) -> list[dict]:
    out = []
    for e in entries:
        if e.is_retired or e.expires >= today.isoformat():
            continue
        out.append({"id": e.id, "scope": e.scope, "grade": e.grade, "class": e.cls, "tags": list(e.tags),
                    "checked": e.checked, "expires": e.expires, "source": e.source, "tried": list(e.tried),
                    "statement": e.statement, "method": method_for(e), "tier": tier_of(e), "block": e.block})
    return sorted(out, key=lambda w: (w["checked"], w["id"]))


def part_knowledge(p: config.Paths, out_dir: Path, *, today: datetime.date) -> Part:
    part = Part("knowledge")
    entries = kb.load(p)
    work = worklist(entries, today)
    active = [e for e in entries if not e.is_retired]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "worklist.jsonl").write_text("".join(json.dumps(w, ensure_ascii=False) + "\n" for w in work),
                                            encoding="utf-8")
    # an expired entry a recheck run takes (a read or write block) is that run's work; every other one, first of all
    # one without a block, is overdue here
    by_tier: dict[str, int] = {}
    for w in work:
        by_tier[w["tier"]] = by_tier.get(w["tier"], 0) + 1
    taken = sum(1 for w in work if w["tier"] in ("read", "write") and w["block"].get("needs", "none") == "none")
    part.overdue = len(work) - taken
    part.lines.append("%d of %d active entries expired; worklist: %s" % (len(work), len(active),
                                                                       out_dir / "worklist.jsonl"))
    part.lines.append("by check block: %s; %d without a block, %d overdue, %d for awb kb recheck-run"
                      % (", ".join("%s %d" % kv for kv in sorted(by_tier.items())) or "none expired",
                         by_tier.get("no block", 0), part.overdue, taken))
    by_method: dict[str, int] = {}
    for w in work:
        by_method[w["method"]] = by_method.get(w["method"], 0) + 1
    part.lines += ["  %4d  %s" % (n, m) for m, n in sorted(by_method.items(), key=lambda kv: -kv[1])]
    if work:
        part.lines.append("oldest check: %s (%s)" % (work[0]["checked"], work[0]["id"]))
    for bench_file in sorted((p.kb / "bench").glob("*.json")):
        try:
            r = kb.bench(kb.load_bench(bench_file), p)
        except kb.KBError as err:
            part.lines.append("retrieval, bench/%s: %s" % (bench_file.name, err))
            continue
        n = r["answerable"] or 1
        part.lines.append("retrieval, bench/%s: %d answerable, first %d (%d%%), in the first %d %d (%d%%)"
                          % (bench_file.name, r["answerable"], r["hit1"], round(100 * r["hit1"] / n), r["top"],
                             r["hit_top"], round(100 * r["hit_top"] / n)))
    return part


# --------------------------------------------------------------------------- wipe

def _wipe_harness(code_root: Path):
    """The harness of the pack in the tree the running package came from, so the installed rules and lists are the
    ones measured."""
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))
    from calibration.redteam.wipe import harness
    return harness


def _release() -> str:
    from awb import hooks
    return hooks._release()


def part_wipe(p: config.Paths, *, today: datetime.date, code_root: Path | None = None,
              only: dict[str, list[str]] | None = None) -> Part:
    """The pack through the installed intake. `only` (dimension -> case ids) runs a part of it, for the tests."""
    part = Part("wipe")
    code_root = code_root or Path(awb.__file__).resolve().parent.parent
    try:
        harness = _wipe_harness(code_root)
        harness.rt.code_under_test()
    except (ImportError, SystemExit) as err:
        part.error = "the pack cannot be loaded (%s)" % type(err).__name__
        return part
    work = Path(tempfile.mkdtemp(prefix="awb-wipe-"))
    try:
        failures = harness.selfcheck(work)
        if failures:
            part.error = "not recorded, the harness did not show that it can fail: %s" % "; ".join(failures)
            return part
        part.lines.append("selfcheck: the planted leak and the forced loss are reported, the caught case is clean")
        by_module: list[tuple[str, dict]] = []
        results: list[dict] = []
        for dim in harness.DIMENSIONS:
            cases = harness.load_cases(harness.module_path(dim))
            if only is not None:
                if dim not in only:
                    continue
                cases = [c for c in cases if c["id"] in set(only[dim])]
            entries = [harness.run_case(c, work) for c in cases]
            results += entries
            by_module.append((dim, harness.summary_of(entries)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    s = harness.summary_of(results)
    release = _release()
    part.lines.append("%d cases: %d with a leak (%d values), %d with a loss (%d terms), %d withheld, %d errors; "
                      "release %s" % (s["cases"], s["leak_cases"], s["leaked_values"], s["loss_cases"],
                                      s["lost_terms"], s["withheld"], s["errors"], release))
    part.lines += ["  %-12s %3d cases, %d leaks, %d losses" % (dim, m["cases"], m["leak_cases"], m["loss_cases"])
                   for dim, m in by_module]
    history = board.wipe_history(p)
    if history:
        last = history[-1]
        grown = [(what, last[key], s[mine]) for what, key, mine in (("leaks", "leaks", "leak_cases"),
                                                                     ("losses", "losses", "loss_cases"))
                 if s[mine] > last[key]]
        for what, before, now in grown:
            part.lines.append("OVERDUE: %s grew from %d to %d since %s" % (what, before, now, last["date"]))
        part.overdue += len(grown)
        if not grown:
            part.lines.append("against %s: %d leaks, %d losses, not more" % (last["date"], last["leaks"],
                                                                            last["losses"]))
    else:
        part.lines.append("the first line of wipe.tsv: nothing to compare")
    if s["errors"]:
        part.lines.append("OVERDUE: %d case(s) ended in an error, a leak there is not seen" % s["errors"])
        part.overdue += 1
    row = [today.isoformat(), s["cases"], s["leak_cases"], s["loss_cases"], s["leaked_values"], s["lost_terms"],
           s["withheld"], s["errors"], release,
           ",".join("%s=%d/%d" % (dim, m["leak_cases"], m["loss_cases"]) for dim, m in by_module)]
    tsv = board.wipe_tsv(p)
    tsv.parent.mkdir(parents=True, exist_ok=True)
    new = not tsv.exists()
    with tsv.open("a", encoding="utf-8") as fh:
        if new:
            fh.write("\t".join(board.WIPE_COLUMNS) + "\n")
        fh.write("\t".join(str(x) for x in row) + "\n")
    part.lines.append("recorded in %s" % tsv)
    return part


# --------------------------------------------------------------------------- projects


def part_projects(p: config.Paths, *, today: datetime.date, max_age: int) -> Part:
    part = Part("projects")
    try:
        registered = [r for r in projects.load(p) if r.state == "active"]
    except projects.ProjectError as err:
        part.error = str(err)
        return part
    for r in registered:
        state = Path(r.path) / "STATE.md"
        opened = Path(r.path) / "OPEN.md"
        try:
            age = (today - datetime.date.fromtimestamp(state.stat().st_mtime)).days
        except OSError:
            part.lines.append("%s: no STATE.md" % r.code)
            part.overdue += 1
            continue
        try:
            items = sum(1 for line in opened.read_text(encoding="utf-8").splitlines() if line.startswith("- "))
        except OSError:
            items = 0
        mark = ""
        if age > max_age:
            mark = "  OVERDUE"
            part.overdue += 1
        part.lines.append("%s: STATE.md %d day(s) old, %d open item(s)%s" % (r.code, age, items, mark))
    if not registered:
        part.lines.append("no active project")
    return part


# --------------------------------------------------------------------------- defaults


def _commands() -> set[str]:
    """Every first word `awb` takes: the delegated commands and the subcommands of its own parser."""
    import argparse

    from awb import cli
    names = set(cli.DELEGATED)
    for action in cli._build()._actions:
        if isinstance(action, argparse._SubParsersAction):
            names |= set(action.choices)
    return names


def part_defaults(p: config.Paths, base: Path, *, code_root: Path | None = None) -> Part:
    part = Part("defaults")
    code_root = code_root or Path(awb.__file__).resolve().parent.parent
    rules = code_root / "seal" / "work-claude" / "CLAUDE.md"
    guide = code_root / "COMMANDS.md"
    known = _commands()
    for doc in (rules, guide):
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            part.lines.append("%s cannot be read" % doc.name)
            part.overdue += 1
            continue
        missing = sorted({c for c in _AWB_CMD_RE.findall(text)} - known)
        if missing:
            part.lines.append("%s names commands that do not exist: %s" % (doc.name, ", ".join(missing)))
            part.overdue += len(missing)
    entries = {e.id: e for e in kb.load(p)}
    cited: dict[str, set[str]] = {}
    for path in sorted((code_root / "awb").rglob("*.py")) + [rules, guide]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for kid in _KB_ID_RE.findall(text):
            if kid == "KB-XXXX":                       # the placeholder of the help texts, not an id
                continue
            cited.setdefault(kid, set()).add(str(path.relative_to(code_root)))
    bad = []
    for kid in sorted(cited):
        e = entries.get(kid)
        if e is None:
            bad.append("%s is not in the knowledge base (%s)" % (kid, ", ".join(sorted(cited[kid]))))
        elif e.is_retired:
            bad.append("%s is retired since %s (%s)" % (kid, e.retired, ", ".join(sorted(cited[kid]))))
    part.lines.append("%d knowledge id(s) cited by the code and the rules, %d stale" % (len(cited), len(bad)))
    part.lines += ["  " + b for b in bad]
    part.overdue += len(bad)
    sd = mirror.current_sd(base)
    if sd is not None:
        current = sd.parent.name
        other = []
        for e in entries.values():
            if e.is_retired or e.grade != "contract":
                continue
            revs = {"%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
                    for m in _REVISION_RE.finditer(e.statement + " " + e.source)}
            if revs and current not in revs:     # an entry that also cites the current one was checked against it
                other.append("%s cites revision %s" % (e.id, ", ".join(sorted(revs))))
        part.lines.append("contract entries citing another revision than %s: %d" % (current, len(other)))
        part.lines += ["  " + o for o in sorted(set(other))]
        part.overdue += len(set(other))
        catalog = offered.load()
        if catalog.revision != current:
            part.lines.append("rules/services.tsv is of the revision %s, the mirror holds %s: run awb service update"
                              % (catalog.revision or "unknown", current))
            part.overdue += 1
        else:
            part.lines.append("rules/services.tsv: the current revision %s" % current)
    return part


# --------------------------------------------------------------------------- dataset


def part_dataset(p: config.Paths, *, today: datetime.date, after: list[Part]) -> Part:
    """The dataset TCP Facts of the day, built again from what the parts before brought up to date. Not built
    when a part before ended in an error, so a half refreshed state never becomes the dataset of the day. The wipe
    part brings nothing up to date, so its error does not stop the dataset."""
    part = Part("dataset")
    failed = [x.name for x in after if x.error and x.name != "wipe"]
    if failed:
        part.lines.append("not built: %s ended in an error" % ", ".join(failed))
        return part
    if not kb.export(p, today=today)[0]:
        part.lines.append("not built: no fact fit to leave")
        return part
    built = dataset.build(p, today=today, force=True)
    part.lines += built.lines
    return part


# --------------------------------------------------------------------------- the run and the report


def run(p: config.Paths, *, parts=PARTS, update: bool = True, today: datetime.date | None = None,
        max_state_age: int = MAX_STATE_AGE, job: jobs.Job | None = None,
        show_all: bool = False) -> tuple[list[Part], Path]:
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    job = job or jobs.Job("refresh", None, budget=jobs.DEFAULT_BUDGET, out=io.StringIO())
    out_dir = refresh_dir(p, today)
    base = mirror.root()
    done: list[Part] = []
    for name in PARTS:
        if name not in parts:
            continue
        try:
            if name == "mirrors":
                done.append(part_mirrors(base, update=update, job=job))
            elif name == "release-notes":
                done.append(part_release_notes(p, base, out_dir, update=update, today=today,
                                                 show_all=show_all))
            elif name == "prices":
                done.append(part_prices(p, today=today, job=job))
            elif name == "knowledge":
                done.append(part_knowledge(p, out_dir, today=today))
            elif name == "wipe":
                done.append(part_wipe(p, today=today))
            elif name == "projects":
                done.append(part_projects(p, today=today, max_age=max_state_age))
            elif name == "defaults":
                done.append(part_defaults(p, base))
            else:
                done.append(part_dataset(p, today=today, after=done))
        except (kb.KBError, price.PriceError, mirror.MirrorError, dataset.DatasetError, OSError) as err:
            done.append(Part(name, error="%s: %s" % (type(err).__name__, err if not isinstance(err, OSError)
                                                                               else "operating system error")))
    out_dir.mkdir(parents=True, exist_ok=True)
    # a run of some parts gets a report of its own, so that it never overwrites the full report of the day
    chosen = [n for n in PARTS if n in parts]
    report = out_dir / ("REPORT.md" if chosen == list(PARTS) else "REPORT-%s.md" % "-".join(chosen))
    lines = ["# Refresh of %s" % today.isoformat(), ""]
    for part in done:
        lines.append("## %s: %s" % (part.name, "ERROR " + part.error if part.error else
                                    ("%d overdue" % part.overdue if part.overdue else "nothing overdue")))
        lines.append("")
        lines += part.lines
        lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")
    return done, report


# --------------------------------------------------------------------------- apply


@dataclass
class Applied:
    id: str
    verdict: str
    outcome: str
    ok: bool


def read_results(path: Path) -> list[dict]:
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise kb.KBError("the results cannot be read (%s)" % type(err).__name__) from None
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise kb.KBError("line %d of the results is not JSON" % n) from None
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("verdict") not in VERDICTS:
            raise kb.KBError("line %d of the results needs an id and a verdict (%s)" % (n, ", ".join(VERDICTS)))
        row["line"] = n
        rows.append(row)
    return rows


def rejected_batches(rows: list[dict], plants: dict[str, str]) -> tuple[set[str], list[str]]:
    """The batches that confirmed a planted false fact, and a line for every planted fact."""
    lines, rejected = [], set()
    seen = {r["id"]: r for r in rows}
    for pid, batch in sorted(plants.items()):
        r = seen.get(pid)
        verdict = r["verdict"] if r else "missing"
        if verdict == "confirmed":
            rejected.add(batch)
            lines.append("%s (%s): CONFIRMED although false: nothing of %s is applied" % (pid, batch, batch))
        elif verdict in ("changed", "refuted"):
            lines.append("%s (%s): caught (%s)" % (pid, batch, verdict))
        else:
            lines.append("%s (%s): %s, so %s did not show that it can fail" % (pid, batch, verdict, batch))
    return rejected, lines


def apply(rows: list[dict], *, plants: dict[str, str] | None = None, where=None,
          today: datetime.date | None = None) -> tuple[list[Applied], list[str]]:
    today = today or datetime.date.today()
    plants = plants or {}
    rejected, calibration = rejected_batches(rows, plants)
    out: list[Applied] = []
    seen: set[str] = set()
    for r in rows:
        rid, verdict = r["id"], r["verdict"]
        if rid in plants:
            continue
        if rid in seen:
            out.append(Applied(rid, verdict, "skipped: a second result for the same id", False))
            continue
        seen.add(rid)
        if r.get("batch") in rejected:
            out.append(Applied(rid, verdict, "not applied: its batch confirmed a planted false fact", False))
            continue
        tried = [t for t in (r.get("tried") or []) if isinstance(t, str)]
        checked = r.get("checked") or None
        try:
            if verdict == "unchecked":
                out.append(Applied(rid, verdict, "left as it is: %s" % (r.get("why") or "no reason given"), True))
            elif verdict == "confirmed":
                e = kb.recheck(rid, grade=r.get("grade", ""), source=r.get("source", ""), tried=tried,
                               checked=checked, where=where, today=today)
                out.append(Applied(rid, verdict, "rechecked, grade %s, expires %s" % (e.grade, e.expires), True))
            elif verdict == "changed":
                kb.amend(rid, r.get("statement", ""), tried=tried or None, where=where, today=today)
                try:
                    e = kb.recheck(rid, grade=r.get("grade", ""), source=r.get("source", ""), tried=tried,
                                   checked=checked, where=where, today=today)
                except (kb.Refused, kb.KBError) as err:
                    out.append(Applied(rid, verdict, "amended, but the recheck was refused: %s"
                                       % _reasons(err), False))
                    continue
                out.append(Applied(rid, verdict, "amended and rechecked, grade %s, expires %s"
                                   % (e.grade, e.expires), True))
            else:
                old = kb.get(rid, where)
                kb.retire(rid, r.get("why", ""), where=where, today=today)
                outcome = "retired"
                if r.get("statement"):
                    try:
                        new = kb.add(r["statement"], scope=old.scope, tags=list(old.tags), grade=r.get("grade", ""),
                                     cls=old.cls, source=r.get("source", ""), tried=tried, checked=checked,
                                     where=where, today=today)
                        outcome += ", the corrected fact added as %s" % new.id
                    except (kb.Refused, kb.KBError) as err:
                        out.append(Applied(rid, verdict, "retired, but the corrected fact was refused: %s"
                                           % _reasons(err), False))
                        continue
                out.append(Applied(rid, verdict, outcome, True))
        except (kb.Refused, kb.KBError) as err:
            out.append(Applied(rid, verdict, "refused: %s" % _reasons(err), False))
    return out, calibration


def _reasons(err: Exception) -> str:
    reasons = getattr(err, "reasons", None)
    return "; ".join(reasons) if reasons else str(err)


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """`awb refresh [--part P]...` and `awb refresh apply RESULTS`."""
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["apply"]:
        ap = SafeParser(prog="awb refresh apply", description="Record the results of a re-check.")
        ap.add_argument("results", type=Path)
        ap.add_argument("--plants", type=Path, default=None, help="planted false facts and their batches (JSON)")
        try:
            args = ap.parse_args(argv[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        try:
            rows = read_results(args.results)
            plants = json.loads(args.plants.read_text(encoding="utf-8")) if args.plants else {}
            if not isinstance(plants, dict):
                raise kb.KBError("the plants file is a JSON object of id and batch")
        except (kb.KBError, OSError, ValueError) as err:
            print("awb refresh apply: %s" % (err if isinstance(err, kb.KBError) else type(err).__name__),
                  file=sys.stderr)
            return 2
        applied, calibration = apply(rows, plants=plants)
        for line in calibration:
            print("plant: " + line)
        for a in applied:
            print("%s  %-9s  %s" % (a.id, a.verdict, a.outcome))
        counts: dict[str, int] = {}
        for a in applied:
            key = a.verdict if a.ok else "not applied"
            counts[key] = counts.get(key, 0) + 1
        print("awb refresh apply: %s" % ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())))
        return 0 if all(a.ok for a in applied) else 1

    ap = SafeParser(prog="awb refresh", description="The refresh of TCP knowledge: mirrors, release notes, "
                                                    "prices, knowledge, wipe, projects, defaults, dataset; a report "
                                                    "each run.")
    ap.add_argument("--part", action="append", choices=PARTS, default=[])
    ap.add_argument("--no-update", action="store_true", help="read the mirrors, do not bring them up to date")
    ap.add_argument("--all", action="store_true", dest="show_all",
                    help="release-notes: list every entry that fits a note, not the first %d" % RN_LIMIT)
    ap.add_argument("--max-state-age", type=int, default=MAX_STATE_AGE)
    ap.add_argument("--budget", type=float, default=jobs.DEFAULT_BUDGET)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    job = jobs.Job("refresh", None, budget=args.budget)
    parts, report = run(config.paths(), parts=tuple(args.part) or PARTS, update=not args.no_update,
                        max_state_age=args.max_state_age, job=job, show_all=args.show_all)
    for part in parts:
        print("%-10s %s" % (part.name, "ERROR " + part.error if part.error else
                            ("%d overdue" % part.overdue if part.overdue else "nothing overdue")))
    print("awb refresh: report %s" % report)
    if any(p.error for p in parts):
        return 2
    return 1 if any(p.overdue for p in parts) else 0


if __name__ == "__main__":
    sys.exit(main())
