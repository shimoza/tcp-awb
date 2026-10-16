"""The runner of the build queue: `awb build serve` (presentations/self-build, DESIGN.md sections 3 to 7, step SB3).

The runner is installed code with the owner's uid and never a model. It reads the queue (awb/build.py), takes the
next task whose dependencies are done and whose paths meet no reserved path. It runs it in four kinds of units as
the build user, each through one fixed `sudo -n systemd-run` form with a clock, a CPU share and the isolation of
DESIGN.md section 4:

    awb-task-<ID>-prep     the task home from the root template, the login or the model key, the clone of main
    awb-task-<ID>          the headless session (the pinned client) on the snapshot of the prompt
    awb-task-<ID>-hand     the hand-over: task.bundle and status.txt of the clone (later the rebase and the cleaning)
    awb-task-<ID>-suite    the suite, the baseline and the task's own check on a `git archive` export of a commit

The owner never runs git or Python in a folder the build user can write. `main` leaves `main.git` as a bundle on
the stdin of the prepare unit; the branch comes back as a bundle that the owner copies (no link followed, the build
user's file, capped) into the run folder and fetches into `refs/quarantine/<ID>/<n>/` of `main.git` with hooks and
fsmonitor off. Checks 1 to 8 read data there; 9 to 11 run code as the build user on the export. Every value from the
agent's side is matched against a vocabulary or treated as bytes; the agent's last message goes to message.txt
(control characters dropped, capped), never into result.md, the board or a mail.

The checks (DESIGN.md section 5), in order, the first failure sets the state:

    1  the unit ended by itself             hung
    2  the client ended well                stopped: <subtype>
    3  the last message is an answer        asked
    4  a commit and nothing else moved      red: no commit, red: dirty tree, red: second branch, ...
    5  the commits are his                  red: identity
    6  the commits pass the gate            red: gate <class>; a name check that cannot run is held: checks pending
    7  the commits stay inside the task     red: outside paths, red: link or submodule
    8  the bookkeeping is there             red: changelog, red: tests.json, red: rules list lost lines
    9  the whole suite on a fresh export    red: suite, hung: suite (A7: the failed nodes run once more in the same
                                            suite unit; when they pass, a line `flaky: NODE...`)
    10 no test was hollowed                 red: tests lost
    11 the task's own check                 red: check

Before check 4 runs on the branch to judge (A2): when `main` moved since the start, check 4 runs on the branch as
the session left it, then the build unit rebases it onto a fresh bundle of main (a conflict is `red: rebase
conflict`) and the checks judge it against the main of now.

Green: when `main` moved since the start the build unit rebases the branch onto a fresh bundle of main (a conflict
is `stale`) and the checks run again; then `update-ref` of main with the expected old value after `merge-base
--is-ancestor`, the state `green` before the push, the push (D-S2) after the gate over the whole range, `done`.

The units are behind `Units`: `SystemdUnits` builds the one fixed argument list; `LocalUnits` runs the same steps as
this uid in this process (the tests and `awb build selftest`), with the same clock and kill of a process group.

SB5: every task unit names the oneshot awb-task-failed@<unit>.service in OnFailure= (`awb build failed`, one board
line when no runner waits on the unit); `awb build probe` proves the network of a task in units of the same form (the
model endpoint and the token host of his login connect, a TCP API host and GitHub do not) and leaves the record that
`awb seal check` reads; the selftest fails when one of its planted cases is missing.

F1 step 6 split the runner into parts: awb/build_units.py (the host, the run's context and the units),
awb/build_unit_side.py (what runs in the units as the build user), awb/build_checks.py (the checks),
awb/build_probe.py (the probe) and awb/build_selftest.py (the selftest). This module keeps the runner and its
commands; every name of the parts is still reached through it (awb/parts.py).
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from awb import build, build_probe, config, parts  # noqa: F401 (build_probe: reached through parts.join below)
from awb.build_checks import (BUNDLE_MAX, CHECK_WAIT, TAIL_LINES, GitFailed, Stop, _fail, _json, _matcher,
                              _plain_path, check_answer, check_books, check_branch_commits, check_client,
                              check_commits, check_gate, check_hollow, check_identity, check_onto_main, check_outdir,
                              check_own, check_paths, check_protected, check_suite, check_task, check_unit, clean,
                              commit_fields, ensure_main, fetch_bundle, fetch_ref, git, handover, is_ancestor,
                              oversized, owner_identity, parse_result, resolve_ref, rev, roll_week, scan_commits,
                              unit_run)
from awb.build_unit_side import MODELS, SHA_RE, UUID_RE, handin
from awb.build_units import (CLIENT, PROBE_UNIT, RESULTS, SMALL_MAX, UNIT_RE, Ctx, Ended, Host, Spec, _hidden, _read,
                             _take, _text, _write, board_read, board_write, client_argv, model_allow, name_binds,
                             resolvers, wait)


# --------------------------------------------------------------------------- the fixed values

LOGIN_COMMAND = "sudo -u awb-build -H %s login" % CLIENT
"""The one command he runs, as the owner in his own terminal, when the build user has no login yet."""
LOGIN_HELD = "held: login missing"
PREP_SECONDS = 600
FLOOR_BYTES = 5 << 30       # free space on the build root before a session is spent
PUSH_RETRY = 600            # a held push is tried again after ten minutes
HELD_RESTART = ("held: prompt", "held: prepare", "held: model endpoint", "held: no main", "held: changelog week")
"""Held before a session was spent: the same run starts again at a later poll, once the cause cleared."""
MAX_ROUNDS = 3              # rebase rounds when main moves under a green task
RESULT_MAX = 4 << 20
FAILED_ROWS = 5             # the failed rows state.tsv keeps, the newest last
NUDGE_MINUTES = 15          # A6: a task that waits on him this long gets one mail from awb build nudge
NUDGE_FILE = "nudged"
"""The mark of `awb build nudge` in a run folder: `<state>\\t<since>\\t<sent>`, the state line it mailed for and the
time of the mail. A new state line (an answer or a widen that ends in the same state again) is mailed again."""
NUDGE_WORDS = ("asked", "red", "held")
SHARED_FILES = ("CHANGELOG.md", "README.md", "COMMANDS.md", "INTERFACES.md")


def _log(text: str) -> None:
    """One line for the journal of the runner's unit: fixed words, ids, states and hashes only."""
    print("awb build: %s" % text, file=sys.stderr, flush=True)


def clean_message(data: bytes) -> str:
    """The agent's text for message.txt: control characters but newline and tab dropped, capped."""
    return build._clean_text(data)


# --------------------------------------------------------------------------- the runner

class Runner:
    def __init__(self, host: Host):
        self.host = host
        self.held_queue: str | None = None

    def log(self, text: str) -> None:
        if not self.host.quiet:
            _log(text)

    # ------------------------------------------------------------------ records

    def state(self, ctx: Ctx, state: str) -> None:
        build.write_state(ctx.folder, state, self.host.now())
        ctx.run = dataclasses.replace(ctx.run, state=state, since=self.host.now())
        self.log("%s run %d %s" % (ctx.tid, ctx.n, state))

    def phase(self, ctx: Ctx, word: str) -> None:
        _write(ctx.folder / "phase", word + "\n")

    def _snapshot_task(self, ctx: Ctx) -> None:
        _write(ctx.folder / "task.md", ctx.task.block())

    # ------------------------------------------------------------------ the loop

    def serve(self) -> int:
        h = self.host
        fd = acquire_lock(h.runs)
        if fd is None:
            print("awb build: another runner holds runs/.runner.lock; this one exits", file=sys.stderr)
            return 1
        try:
            ensure_main(h)
            self.reconcile()
            while True:
                if os.path.lexists(h.runs / build.STOP_FILE):
                    os.unlink(h.runs / build.STOP_FILE)
                    self.log("stopped by awb build stop")
                    return 0
                if os.path.lexists(h.runs / "restart-wanted"):
                    os.unlink(h.runs / "restart-wanted")
                    self.log("exits between two tasks for the new code (restart-wanted)")
                    return 75
                if not self.poll_once():
                    h.sleep(h.poll)
        finally:
            os.close(fd)

    def preconditions(self) -> str | None:
        """Before a session is spent: a resolver for the task unit, the vault unlocked, the key service loaded, the
        gate's self-test, room on the build root, the identity, the model key and a no-op unit as the build user."""
        h = self.host
        why = self.model_hold()
        if why is not None:
            return why
        if h.resolve is None and not resolvers():
            return "no resolver for the task unit"
        if h.precheck is not None:
            return h.precheck()
        from awb import deploy, gate, vault

        keys = deploy.owner_keys()
        try:
            if vault.ping(config.paths().check_socket) not in ("unlocked", "plain"):
                return "vault locked"
        except Exception:
            return "no vault daemon"
        try:
            ans = keys.request(keys.admin_socket(), {"op": "status"}, timeout=10)
            if not ans.get("ok") or not ans.get("tenants"):
                return "key service not loaded"
        except Exception:
            return "no key service"
        if gate.selftest(None):
            return "gate selftest"
        try:
            st = os.statvfs(h.root)
            if st.f_bavail * st.f_frsize < FLOOR_BYTES:
                return "build root full"
        except OSError:
            return "build root missing"
        if owner_identity(h) is None:
            return "no identity"
        if h.model == "key" and h.model_key is not None and _key(h) is None:
            return "model key"
        if not h.units.probe():
            return "systemd-run as the build user"
        return None

    def model_hold(self) -> str | None:
        """The build model of the host file is login or key; with login, a task held for a missing login holds the
        whole queue until he logs in and retries it (the owner cannot look into the build user's home himself)."""
        h = self.host
        if h.model not in MODELS:
            return "build model"
        if h.model == "login":
            for tid in build.ids_with_runs(h.runs):
                r = build.latest(tid, h.runs)
                if r is not None and r.state == LOGIN_HELD:
                    return "login missing"
        return None

    def poll_once(self) -> bool:
        """One pass: the open business of the records first, then the next task. True when something ran."""
        h = self.host
        try:
            tasks, _notes = build.read_queue(h.queue)
        except OSError:
            tasks = []
        by_id = {t.id: t for t in tasks}
        for tid in build.ids_with_runs(h.runs):
            r = build.latest(tid, h.runs)
            if r is None:
                continue
            if r.state == "green" or (r.state in ("held: push refused", "held: push pending") and (
                    r.since is None or (h.now() - r.since).total_seconds() >= PUSH_RETRY)):
                return self._guarded(tid, lambda: self.publish(self._ctx(r, by_id.get(tid))))
            if r.state == "held: checks pending" and self._names_ok():
                return self._guarded(tid, lambda: self.rejudge(self._ctx(r, by_id.get(tid))))
            if r.state == "widened":
                return self._guarded(tid, lambda: self.rejudge(self._ctx(r, by_id.get(tid)), widened=True))
        for cid in build.check_ids(h.runs):
            r = build.latest(cid, h.runs)
            if r is not None and r.state == "queued":
                return self._guarded(cid, lambda: self.check_branch(r))
        pick = self.pick(tasks)
        if pick is None:
            self.write_board()
            return False
        task, run = pick
        why = self.preconditions()
        if why is not None:
            if why != self.held_queue:
                self.held_queue = why
                self.log("queue held: %s" % why)
                self.mail("Workbench build queue: held", ["The queue is held: %s." % why,
                                                          "No session is spent until it clears."])
            self.write_board()
            return False
        if self.held_queue is not None:
            self.log("queue no longer held")
        self.held_queue = None
        if run is not None and run.state == "answered":
            return self._guarded(task.id, lambda: self.run_session(self.resume(task, run)))
        return self._guarded(task.id, lambda: self.run_session(self.start(task, run)))

    def _guarded(self, tid: str, step: Callable[[], None]) -> bool:
        """One step of a task; a step that breaks (git, a file, a unit) leaves its run interrupted with one line, the
        runner goes on with the next task."""
        try:
            step()
        except Exception as err:
            r = build.latest(tid, self.host.runs)
            self.log("%s: the runner's step broke (%s)" % (tid, type(err).__name__))
            if r is not None and r.state in ("running", "green"):
                with contextlib.suppress(Exception):
                    self.finish(self._ctx(r, None), "interrupted" if r.state == "running" else "held: push pending")
        return True

    def _names_ok(self) -> bool:
        try:
            _matcher(self.host.register)
            return True
        except Exception:
            return False

    def pick(self, tasks: list[build.Task]) -> tuple[build.Task, build.Run | None] | None:
        """The first task with no run (or a queued or answered run) whose depends are done and whose paths meet no
        path a task that is not done keeps reserved. The always allowed files reserve nothing on either side (A3), so a
        task that waits on a question and lists README.md keeps no other task waiting. Serial: the caller runs it to
        its end."""
        h = self.host
        last = {t.id: build.latest(t.id, h.runs) for t in tasks}
        done = {tid for tid, r in last.items() if r is not None and r.state == "done"}
        for tid in build.ids_with_runs(h.runs):
            if tid not in last:
                r = build.latest(tid, h.runs)
                if r is not None and r.state == "done":
                    done.add(tid)
        reserved: list[tuple[str, str]] = []
        for t in tasks:
            r = last[t.id]
            if r is not None and r.state != "done":
                reserved += [(p, t.id) for p in build.reserving(t.paths)]
        for t in tasks:
            r = last[t.id]
            if r is not None and r.state not in ("queued", "answered") + HELD_RESTART:
                continue
            if r is not None and r.state in HELD_RESTART and r.since is not None and \
                    (h.now() - r.since).total_seconds() < h.poll:
                continue
            if r is not None and r.state == "queued":
                nb = _text(r.folder / "not-before")
                if nb and nb.strip().isdigit() and h.wall() < int(nb.strip()):
                    continue
            if any(d not in done for d in t.depends):
                continue
            if any(_meet(p, q) for p in build.reserving(t.paths) for q, owner in reserved if owner != t.id):
                continue
            return t, r
        return None

    # ------------------------------------------------------------------ a session

    def _ctx(self, run: build.Run, task: build.Task | None) -> Ctx:
        """The context of a run from its records: the task as snapshotted at its start, never the queue of now."""
        snap = _text(run.folder / "task.md", build.QUEUE_MAX)
        tasks = build.parse(snap)[0] if snap else []
        t = tasks[0] if tasks else task
        ctx = Ctx(t, run)
        ctx.start = (_text(run.folder / "start") or "").strip()
        ctx.tip = (_text(run.folder / "commit") or "").strip()
        wid = _text(run.folder / "widen.txt")
        ctx.widened = tuple(p for p in (wid or "").split() if build._plain_path(p))
        return ctx

    def start(self, task: build.Task, run: build.Run | None) -> None:
        h = self.host
        with build.task_lock(task.id, h.runs):
            if run is None:
                if build.runs_of(task.id, h.runs):
                    return
                run = build.new_run(task.id, h.runs, state="running", now=h.now())
            else:
                cur = build.latest(task.id, h.runs)
                if cur is None or cur.n != run.n or cur.state not in ("queued",) + HELD_RESTART:
                    return
                build.write_state(run.folder, "running", h.now())
                run = dataclasses.replace(run, state="running")
        ctx = Ctx(task, run)
        self.phase(ctx, "prep")
        self._snapshot_task(ctx)
        try:
            prompt = build.read_prompt(task, h.queue, h.register, names=True)
        except build.PromptHeld:
            return self.finish(ctx, "held: prompt")
        snapshot_prompt(h, ctx, prompt)
        try:
            rolled = roll_week(h)
        except GitFailed as err:
            self.log("build %s: the CHANGELOG week could not be opened on main (%s)" % (task.id, err))
            return self.finish(ctx, "held: changelog week")
        if rolled:
            self.log("build: CHANGELOG.md opens the week of %s on main (%s)" % (h.now().strftime("%Y-%m-%d"),
                                                                                rolled[:7]))
        start = rev(h, "refs/heads/main")
        if start is None:
            return self.finish(ctx, "held: no main")
        ctx.start = start
        _write(ctx.folder / "start", start + "\n")
        found = model_allow(h)
        if found is None:
            return self.finish(ctx, "held: model endpoint")
        bundle = ctx.folder / "main.bundle"
        git(h, "bundle", "create", "--quiet", str(bundle), "refs/heads/main")
        key = (_key(h) or b"") if h.model == "key" else b""
        _write(ctx.folder / "handin", handin(key, bundle))
        key = b""
        os.unlink(bundle)
        try:
            e = unit_run(h, ctx, "prep", ["prepare", str(h.root), task.id, str(run.n), h.hooks_path] +
                         (["login"] if h.model == "login" else []), stdin=ctx.folder / "handin", seconds=PREP_SECONDS)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(ctx.folder / "handin")
        if e.code == 7 and h.model == "login":
            return self.finish(ctx, LOGIN_HELD)
        got = (_take(h.out(task.id, run.n) / "start.txt", h.uid(), 128) or b"").decode("ascii", "replace").strip()
        if e.code != 0 or got != start:
            clean(h, ctx, "all")
            return self.finish(ctx, "held: prepare")
        return self.spawn(ctx, found, None)

    def resume(self, task: build.Task, run: build.Run) -> None:
        """An answered task: the same session, the same clock and budget again, his answer on its stdin."""
        h = self.host
        with build.task_lock(task.id, h.runs):
            cur = build.latest(task.id, h.runs)
            if cur is None or cur.n != run.n or cur.state != "answered":
                return
            build.write_state(run.folder, "running", h.now())
        ctx = self._ctx(dataclasses.replace(run, state="running"), task)
        self.phase(ctx, "resume")
        session = (_text(run.folder / "session") or "").strip()
        answer = _text(run.folder / "answer.txt", build.ANSWER_MAX)
        k = 1
        while os.path.lexists(run.folder / ("result.asked-%d.json" % k)):
            k += 1
        for name, new in (("result.json", "result.asked-%d.json" % k), ("message.txt", "message.asked-%d.txt" % k)):
            with contextlib.suppress(FileNotFoundError):
                os.replace(run.folder / name, run.folder / new)
        if not UUID_RE.fullmatch(session) or answer is None:
            clean(h, ctx, "home")
            return self.finish(ctx, "stopped: session_gone")
        _write(ctx.folder / "prompt.answer.txt", answer)
        found = model_allow(h)
        if found is None:
            # the answer waits for the next poll: the state goes back, the queue line says why
            build.write_state(ctx.folder, "answered", h.now())
            self.held_queue = "model endpoint"
            return None
        _write(ctx.folder / "handin", handin((_key(h) or b"") if h.model == "key" else b"", None))
        try:
            e = unit_run(h, ctx, "prep", ["rekey", str(h.root), task.id, str(run.n), session] +
                         (["login"] if h.model == "login" else []), stdin=ctx.folder / "handin", seconds=PREP_SECONDS)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(ctx.folder / "handin")
        if e.code == 7 and h.model == "login":
            clean(h, ctx, "home")
            return self.finish(ctx, LOGIN_HELD)
        if e.code != 0:
            clean(h, ctx, "home")
            return self.finish(ctx, "stopped: session_gone")
        return self.spawn(ctx, found, session)

    def spawn(self, ctx: Ctx, found: tuple[list[str], dict[str, list[str]]], session: str | None):
        """The session's unit with the allow list and the hosts file of `found` (model_allow): started, its deadline
        recorded. Returns (ctx, handle, deadline)."""
        h = self.host
        allow, names = found
        clock = ctx.task.clock * h.minute
        deadline = h.wall() + clock
        _write(ctx.folder / "deadline", "%d\n" % math.ceil(deadline))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.folder / "ended")
        self.phase(ctx, "agent")
        clone, home = h.clone(ctx.tid, ctx.n), h.home(ctx.tid, ctx.n)
        t = h.template
        spec = Spec("awb-task-%s" % ctx.tid, client_argv(h, ctx.task, session), clock, str(clone), str(home),
                    rw=[str(clone), str(home)], hidden=_hidden(h, ctx),
                    binds=[(str(t / ".claude" / "settings.json"), str(home / ".claude" / "settings.json")),
                           (str(t / ".claude" / "CLAUDE.md"), str(home / ".claude" / "CLAUDE.md")),
                           (str(t / ".gitconfig"), str(home / ".gitconfig"))] + name_binds(ctx.folder, names),
                    allow=allow, env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
                    stdin=ctx.folder / ("prompt.answer.txt" if session else "prompt.txt"),
                    stdout=ctx.folder / "result.json", stderr=ctx.folder / "agent.err")
        return ctx, h.units.start(spec), deadline

    def run_session(self, started) -> None:
        """Wait for the session's unit (stop-wanted stops it), record how it ended, then the rest."""
        if not started:
            return
        ctx, handle, deadline = started
        h = self.host
        ended = wait(h, handle, ctx, deadline, stop_name="awb-task-%s" % ctx.tid)
        at = h.wall()
        _write(ctx.folder / "ended", "%s %d %d\n" % (ended.result, ended.code, math.ceil(at)))
        self.after_agent(ctx, ended, deadline, at)

    def after_agent(self, ctx: Ctx, ended: Ended | None, deadline: float | None, ended_at: float | None = None) -> None:
        """Checks 1 to 3 on what the session left, the hand-over, the home closed, then checks 4 to 11."""
        h = self.host
        by_owner = os.path.lexists(ctx.folder / "stop-wanted")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.folder / "stop-wanted")
        raw = _read(ctx.folder / "result.json", RESULT_MAX)
        obj = parse_result(raw)
        if obj is not None and isinstance(obj.get("result"), str):
            _write(ctx.folder / "message.txt", clean_message(obj["result"].encode("utf-8")))
        if obj is not None and isinstance(obj.get("session_id"), str) and UUID_RE.fullmatch(obj["session_id"]):
            _write(ctx.folder / "session", obj["session_id"] + "\n")
        self.phase(ctx, "handover")
        handover(h, ctx)
        state = None
        try:
            if by_owner:
                raise _fail(ctx, 1, "stopped: by_owner")
            check_unit(ctx, ended, deadline, ended_at if ended_at is not None else h.wall())
            check_client(ctx, obj)
            check_answer(ctx, obj)
        except Stop as s:
            state = s.state
        if state == "asked":
            clean(h, ctx, "key")
            self._checks_log(ctx)
            return self.finish(ctx, "asked")
        clean(h, ctx, "home")
        if state is not None:
            self._checks_log(ctx)
            if state.startswith("stopped: ") and state != "stopped: by_owner" and self._api_retry(ctx, obj):
                return self.finish(ctx, state, note="one retry in %d min" % (h.retry_wait // 60))
            return self.finish(ctx, state)
        self.judge_and_merge(ctx)

    def _api_retry(self, ctx: Ctx, obj: dict | None) -> bool:
        """D-S4: an API error before the first tool call (num_turns 0 from a valid object), once, after ten
        minutes, as a new numbered run; a run that is itself that retry is not retried again."""
        h = self.host
        if obj is None or obj.get("num_turns") != 0 or os.path.lexists(ctx.folder / "auto-retry"):
            return False
        nr = build.new_run(ctx.tid, h.runs, now=h.now())
        _write(nr.folder / "not-before", "%d\n" % math.ceil(h.wall() + h.retry_wait))
        _write(nr.folder / "auto-retry", "one retry of an API error before the first tool call (D-S4)\n")
        return True

    def _checks_log(self, ctx: Ctx) -> None:
        _write(ctx.folder / "checks.log", "\n".join(ctx.lines) + "\n")

    # ------------------------------------------------------------------ checks 4 to 11 and the merge

    def judge(self, ctx: Ctx) -> str:
        """Checks 4 to 11 on the kept bundle. Returns the state: green or the first failure's."""
        h = self.host
        self.phase(ctx, "checks")
        ctx.lines = [x for x in ctx.lines if x.split(" ", 1)[0] in ("1", "2", "3", "-")]
        heads = fetch_bundle(h, ctx)
        status = _read(ctx.folder / "status.txt")
        identity = owner_identity(h)
        try:
            check_commits(h, ctx, heads, status)
            check_identity(h, ctx, identity)
            check_gate(h, ctx, identity)
            check_paths(h, ctx)
            check_books(h, ctx, h.now())
            after = check_suite(h, ctx)
            check_hollow(h, ctx, after)
            check_own(h, ctx)
        except Stop as s:
            self._checks_log(ctx)
            return s.state
        self._checks_log(ctx)
        return "green"

    def judge_and_merge(self, ctx: Ctx) -> None:
        h = self.host
        state = self.onto_main(ctx)
        if state is not None:
            return self.finish(ctx, state)
        for _round in range(MAX_ROUNDS):
            state = self.judge(ctx)
            if state != "green":
                return self.finish(ctx, state)
            cur = rev(h, "refs/heads/main")
            if cur == ctx.start:
                if is_ancestor(h, ctx.start, ctx.tip) and git(
                        h, "update-ref", "-m", "awb build %s" % ctx.tid, "refs/heads/main", ctx.tip, ctx.start,
                        check=False).returncode == 0:
                    _write(ctx.folder / "commit", ctx.tip + "\n")
                    self.state(ctx, "green")
                    clean(h, ctx, "all")
                    return self.publish(ctx)
                cur = rev(h, "refs/heads/main")
            if cur is None:
                return self.finish(ctx, "held: no main")
            if self.rebase(ctx, cur) != "rebased":
                return self.finish(ctx, "stale")
        return self.finish(ctx, "stale")

    def onto_main(self, ctx: Ctx) -> str | None:
        """A2: before the checks the branch moves onto the main of now, so check 9 and 10 judge it against today's
        baseline and never against the start commit of the session. Check 4 runs first on the branch as the session
        left it (a dirty tree or a merge commit is named as such, never as a conflict). A conflict is `red: rebase
        conflict`. Returns the state that ends the round, None to go on with the checks."""
        h = self.host
        cur = rev(h, "refs/heads/main")
        if cur is None:
            return "held: no main"
        if cur == ctx.start:
            return None
        self.phase(ctx, "checks")
        ctx.lines = [x for x in ctx.lines if x.split(" ", 1)[0] in ("1", "2", "3", "-")]
        try:
            check_commits(h, ctx, fetch_bundle(h, ctx), _read(ctx.folder / "status.txt"))
        except Stop as s:
            self._checks_log(ctx)
            return s.state
        word = self.rebase(ctx, cur, "before the checks")
        if word == "rebased":
            return None
        if word == "conflict":
            ctx.lines.append("- main moved to %s: the branch does not rebase onto it without a conflict" % cur[:7])
            self._checks_log(ctx)
            return "red: rebase conflict"
        self._checks_log(ctx)
        return "stale"

    def rebase(self, ctx: Ctx, main: str, when: str = "and the checks run again") -> str:
        """main moved: the build unit rebases the branch onto a fresh bundle of main, hands it over again. Returns
        `rebased`, `conflict` (the unit's exit 3, the rebase aborted) or `failed`."""
        h = self.host
        self.phase(ctx, "rebase")
        bundle = ctx.folder / "main.bundle"
        git(h, "bundle", "create", "--quiet", str(bundle), "refs/heads/main")
        try:
            e = unit_run(h, ctx, "hand", ["rebase", str(h.root), ctx.tid, str(ctx.n), ctx.start], stdin=bundle)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(bundle)
        if e.code != 0:
            return "conflict" if e.code == 3 else "failed"
        out = h.out(ctx.tid, ctx.n)
        status = _take(out / "status.txt", h.uid(), SMALL_MAX)
        if status is not None:
            _write(ctx.folder / "status.txt", status)
        if _take(out / "task.bundle", h.uid(), BUNDLE_MAX, dest=ctx.folder / "task.bundle") is None:
            return "failed"
        ctx.start = main
        _write(ctx.folder / "start", main + "\n")
        ctx.lines.append("- main moved: the branch was rebased onto %s %s" % (main[:7], when))
        return "rebased"

    def rejudge(self, ctx: Ctx, widened: bool = False) -> None:
        """`held: checks pending` after the vault opened (or `widened`): the checks again on the kept branch."""
        h = self.host
        with build.task_lock(ctx.tid, h.runs):
            build.write_state(ctx.folder, "running", h.now())
        if ctx.task is None:
            return self.finish(ctx, "interrupted")
        old = _text(ctx.folder / "checks.log") or ""
        ctx.lines = [x for x in old.split("\n") if x[:2] in ("1 ", "2 ", "3 ", "- ")]
        if not os.path.lexists(ctx.folder / "task.bundle"):
            return self.finish(ctx, "red: no commit")
        self.judge_and_merge(ctx)

    # ------------------------------------------------------------------ a branch from outside (F2 G1)

    def _check_ctx(self, run: build.Run) -> Ctx:
        ctx = Ctx(check_task(run.id), run)
        ctx.tip = (_text(run.folder / "head") or "").strip()
        ctx.start = (_text(run.folder / "start") or "").strip()
        return ctx

    def check_branch(self, run: build.Run) -> None:
        """A queued check of a branch (awb build check); the caller holds the runner's lock. Checks 4, 6, 9 and 10 of
        a task on the commit in quarantine, the suite as the build user in the task's suite unit; `passed` or the
        first failure's state. Nothing moves main, nothing is pushed. A check found running lost its runner."""
        h = self.host
        with build.task_lock(run.id, h.runs):
            cur = build.latest(run.id, h.runs)
            if cur is None or cur.n != run.n or cur.state not in ("queued", "running"):
                return
            gone = cur.state == "running"
            if not gone:
                build.write_state(run.folder, "running", h.now())
        ctx = self._check_ctx(dataclasses.replace(cur, state="running"))
        if gone:
            with contextlib.suppress(Exception):
                clean(h, ctx, "all")
            return self.finish(ctx, "interrupted")
        self.phase(ctx, "checks")
        try:
            state = self.judge_check(ctx)
        finally:
            with contextlib.suppress(Exception):
                clean(h, ctx, "all")
        self.finish(ctx, state)

    def judge_check(self, ctx: Ctx) -> str:
        h = self.host
        ctx.lines = []
        try:
            check_branch_commits(h, ctx)
            check_onto_main(h, ctx)
            check_gate(h, ctx, owner_identity(h))
            check_protected(h, ctx, (_text(ctx.folder / ALLOW_PROTECTED) or "").strip() == "yes")
            if not check_outdir(h, ctx):
                ctx.lines.append("- the out folder of the suite unit could not be made")
                raise Stop("held: prepare")
            after = check_suite(h, ctx)
            check_hollow(h, ctx, after)
        except Stop as s:
            self._checks_log(ctx)
            return s.state
        self._checks_log(ctx)
        return "passed"

    # ------------------------------------------------------------------ the push

    def publish(self, ctx: Ctx) -> None:
        """green -> the push (D-S2) -> done; without the push done is main.git. A refused push is held."""
        h = self.host
        if not h.push:
            return self.finish(ctx, "done")
        self.phase(ctx, "push")
        word = push_main(h, h.register)
        if word == "pushed":
            return self.finish(ctx, "done")
        if word == "pending":
            return self.finish(ctx, "held: push pending")
        return self.finish(ctx, "held: push refused")

    # ------------------------------------------------------------------ the end of a run

    def finish(self, ctx: Ctx, state: str, note: str = "") -> None:
        h = self.host
        prev = _text(ctx.folder / "state") or ""
        self.state(ctx, state)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.folder / "phase")
        if not ctx.lines and os.path.lexists(ctx.folder / "checks.log"):
            ctx.lines = (_text(ctx.folder / "checks.log") or "").strip().split("\n")
        info = deployed_info(h)
        write_result(h, ctx, state, info, note)
        self.write_board(info)
        if state.startswith("held: ") and prev.split("\t", 1)[0] == state:
            return
        self.mail_run(ctx, state, info, note)

    def mail_run(self, ctx: Ctx, state: str, info: dict, note: str) -> None:
        h = self.host
        word = state.split(":", 1)[0]
        lines = ["state: %s" % state, "commits: %d" % len(ctx.commits),
                 "suite: %s" % (_suite_line(ctx) if ctx.suite is not None else "not run"),
                 "note: runs/%s/%d/result.md" % (ctx.tid, ctx.n),
                 "live %s, main %s, %s not deployed" % (info["live"], info["main"], info["not_deployed"]),
                 "next: %s" % next_step(ctx, state)]
        if note:
            lines.append("note: %s" % note)
        sent = self.mail("Workbench build %s: %s" % (ctx.tid, word), lines)
        _write(ctx.folder / "mail", ("sent" if sent else "not sent") + "\n")
        self.write_board(info)

    def mail(self, subject: str, lines: list[str]) -> bool:
        if self.host.mail is None:
            return False
        try:
            return bool(self.host.mail(subject, "\n".join(lines) + "\n"))
        except Exception:
            return False

    def write_board(self, info: dict | None = None) -> None:
        h = self.host
        if h.board is None:
            return
        write_state_tsv(h, info or deployed_info(h), self.held_queue)

    # ------------------------------------------------------------------ the reconcile at start

    def reconcile(self) -> None:
        """Every run left `running` by a runner that ended: its unit still active means reattach and wait; gone
        means the checks on what it left or `interrupted`."""
        h = self.host
        try:
            tasks, _ = build.read_queue(h.queue)
        except OSError:
            tasks = []
        by_id = {t.id: t for t in tasks}
        for tid in build.ids_with_runs(h.runs):
            r = build.latest(tid, h.runs)
            if r is None or r.state != "running":
                continue
            ctx = self._ctx(r, by_id.get(tid))
            phase = (_text(r.folder / "phase") or "").strip()
            if ctx.task is None:
                self.finish(ctx, "interrupted")
                continue
            if phase in ("agent", "resume"):
                handle = h.units.attach("awb-task-%s" % tid)
                dl = _text(r.folder / "deadline")
                deadline = float(dl) if dl and dl.strip().isdigit() else None
                self.log("%s run %d: %s" % (tid, r.n, "reattached to its unit" if handle else "its unit is gone"))
                if handle is not None:
                    self.run_session((ctx, handle, deadline))
                    continue
                result = h.units.result("awb-task-%s" % tid)
                if result is not None:
                    self.after_agent(ctx, Ended(result, 1), deadline)
                elif parse_result(_read(r.folder / "result.json", RESULT_MAX)) is not None:
                    at = os.lstat(r.folder / "result.json").st_mtime
                    self.after_agent(ctx, Ended("success", 0), deadline, at)
                else:
                    clean(h, ctx, "home")
                    self.finish(ctx, "interrupted")
                continue
            if phase in ("handover", "checks"):
                rec = (_text(r.folder / "ended") or "").split()
                dl = _text(r.folder / "deadline")
                deadline = float(dl) if dl and dl.strip().isdigit() else None
                if len(rec) == 3 and rec[0] in RESULTS and rec[2].isdigit():
                    self.after_agent(ctx, Ended(rec[0], int(rec[1]) if rec[1].lstrip("-").isdigit() else 1),
                                     deadline, ended_at=float(rec[2]))
                    continue
            self.log("%s run %d: interrupted in the phase %s" % (tid, r.n, phase if phase.isalpha() else "unknown"))
            with contextlib.suppress(Exception):
                clean(h, ctx, "all" if phase == "prep" else "home")
            self.finish(ctx, "interrupted")
        for cid in build.check_ids(h.runs):
            r = build.latest(cid, h.runs)
            if r is not None and r.state == "running":
                self.check_branch(r)


def acquire_lock(runs: Path) -> int | None:
    """The runner's flock on runs/.runner.lock for its life; None when another runner holds it."""
    build._mkdir(Path(runs))
    fd = os.open(Path(runs) / ".runner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def snapshot_prompt(host: Host, ctx: Ctx, prompt: str) -> None:
    """prompt.txt: the fixed header and the prompt as read at the start; the session reads this copy, never the
    queue (an edit of the queue after the start changes nothing, THREAT-MODEL N6)."""
    _write(ctx.folder / "prompt.txt", build.compose(ctx.task, prompt))


def _meet(a: str, b: str) -> bool:
    if a == b:
        return True
    return (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def _key(host: Host) -> bytes | None:
    """The model key the runner copies into the task home (D-S7): a regular file of this uid, never printed."""
    if host.model_key is None:
        return b""
    try:
        data = build._read_regular(host.model_key, 4096)
    except OSError:
        return None
    data = data.strip()
    return data if data and b"\n" not in data else None


def push_main(host: Host, register: Path | None) -> str:
    """main of main.git to origin main (D-S2): fetch origin, refuse a main that does not descend from it, the gate
    with the register over origin/main..main, `git push origin main:main` without --force. pushed, pending (the name
    check cannot run) or refused."""
    from awb import register as reg

    if git(host, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main", check=False,
           net=True, timeout=300).returncode:
        return "refused"
    remote = rev(host, "refs/remotes/origin/main")
    main = rev(host, "refs/heads/main")
    if main is None or remote is None or not is_ancestor(host, remote, main):
        return "refused"
    commits = git(host, "rev-list", "--reverse", "%s..%s" % (remote, main)).stdout.decode("ascii").split()
    if not commits:
        return "pushed"
    if oversized(host, commits):
        return "refused"
    try:
        found = scan_commits(host, commits, _matcher(register), owner_identity(host))
    except reg.RegisterError:
        return "pending"
    if found:
        return "refused"
    r = git(host, "push", "--quiet", "origin", "refs/heads/main:refs/heads/main", check=False, net=True,
            timeout=300)
    return "pushed" if r.returncode == 0 else "refused"


# --------------------------------------------------------------------------- what he reads

def deployed_info(host: Host) -> dict:
    """live / main / N not deployed and the daemons behind, from /opt/tcp-awb/DEPLOYED and main.git."""
    data = _json(_read_any(host.deployed))
    target = data.get("target") if isinstance(data, dict) else None
    target = target if isinstance(target, str) and SHA_RE.fullmatch(target) else None
    main = None
    with contextlib.suppress(GitFailed, OSError):
        main = rev(host, "refs/heads/main")
    behind = []
    if isinstance(data, dict) and target:
        daemons = data.get("daemons") if isinstance(data.get("daemons"), dict) else {}
        code = data.get("daemon_code") if isinstance(data.get("daemon_code"), dict) else {}
        for kind, unit in (("vault", "awb-vaultd.service"), ("keys", "awb-keyd.service")):
            r = daemons.get(kind)
            if isinstance(r, str) and r != target:
                c = code.get(unit) if isinstance(code.get(unit), dict) else {}
                if not (c.get("release") == r and c.get("code") == "unchanged"):
                    behind.append(kind)
    n = 0
    known = target is not None and main is not None
    for tid in build.ids_with_runs(host.runs):
        r = build.latest(tid, host.runs)
        if r is None or r.state != "done":
            continue
        commit = (_text(r.folder / "commit") or "").strip()
        if not SHA_RE.fullmatch(commit):
            continue
        with contextlib.suppress(GitFailed, OSError):
            if not known or not is_ancestor(host, commit, target):
                n += 1
    return {"live": target[:7] if target else "unknown", "main": main[:7] if main else "unknown",
            "not_deployed": str(n), "behind": behind}


def _read_any(path: Path) -> bytes | None:
    try:
        return build._read_regular(Path(path), SMALL_MAX, own=False)
    except OSError:
        return None


def _suite_line(ctx: Ctx) -> str:
    c = ctx.suite or {}
    t = " (%.0f min)" % ctx.suite_minutes if ctx.suite_minutes is not None else ""
    return "%d passed, %d failed, %d skipped%s" % (c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0), t)


def login_note(tid: str) -> str:
    """What he does when the build user has no login: one command as the owner, then the retry."""
    return "as the owner in your terminal: %s, then awb build retry %s" % (LOGIN_COMMAND, tid)


def next_step(ctx: Ctx, state: str) -> str:
    w = state.split(":", 1)[0]
    if build.CHECK_ID_RE.fullmatch(ctx.tid):
        if state == "passed":
            return "read the diff, then merge it yourself; the check merged nothing"
        if state == "held: checks pending":
            return "awb vault unlock, then awb build check again"
        return "nothing was merged; awb build check again after a fix"
    if w == "done":
        return "deploy: sudo awb deploy --task %s" % ctx.tid
    if w == "asked":
        return "answer with awb build answer %s" % ctx.tid
    if w == "red":
        return "awb build retry %s or awb build widen %s PATH..." % (ctx.tid, ctx.tid)
    if state == LOGIN_HELD:
        return login_note(ctx.tid)
    if w == "held":
        return "the runner tries again when the cause clears"
    return "awb build retry %s" % ctx.tid


def _subjects(host: Host, ctx: Ctx) -> list[str]:
    """`<short> <subject>` of every commit after the gate's classes with the register; a subject with a finding, or
    one that cannot be checked, is withheld."""
    out = []
    fields = commit_fields(host, ctx.commits) if ctx.commits else {}
    try:
        matcher = _matcher(host.register)
    except Exception:
        matcher = None
    from awb import gate

    for c in ctx.commits:
        f = fields.get(c)
        subject = f[4].split("\n", 1)[0] if f else ""
        subject = "".join(ch for ch in subject if ch.isprintable())[:120]
        if matcher is None:
            out.append("%s (subject withheld: the name check cannot run)" % c[:7])
            continue
        found = gate._scan_text("subject", subject, matcher)
        out.append("%s (subject withheld: %s)" % (c[:7], found[0].cls) if found else "%s %s" % (c[:7], subject))
    return out


def write_result(host: Host, ctx: Ctx, state: str, info: dict, note: str = "") -> None:
    """result.md: fixed fields only (state, commits after the gate, the suite line, the check lines, the diff stat,
    the lines lost in shared files, live / main / not deployed). The agent's text is in message.txt, not here."""
    lines = ["# build %s run %d" % (ctx.tid, ctx.n), "",
             "state: %s" % state, "since: %s" % host.now().strftime("%Y-%m-%d %H:%M")]
    if note:
        lines.append("note: %s" % note)
    start = ctx.start[:7] if SHA_RE.fullmatch(ctx.start or "") else "-"
    tip = ctx.tip[:7] if SHA_RE.fullmatch(ctx.tip or "") else "-"
    lines += ["start: %s  commit: %s" % (start, tip), ""]
    with contextlib.suppress(GitFailed, OSError):
        subjects = _subjects(host, ctx)
        if subjects:
            lines += ["commits:"] + ["- %s" % s for s in subjects] + [""]
    lines.append("suite: %s" % (_suite_line(ctx) if ctx.suite is not None else "not run"))
    if ctx.lost or ctx.markers or ctx.removed_tests:
        lines.append("tests: %d lost, %d skip or xfail marker(s) added, %d test line(s) removed"
                     % (ctx.lost, ctx.markers, ctx.removed_tests))
    lines += ["", "checks:"] + (ctx.lines or ["(none ran)"]) + [""]
    if ctx.entries and SHA_RE.fullmatch(ctx.start or "") and SHA_RE.fullmatch(ctx.tip or ""):
        with contextlib.suppress(GitFailed, OSError):
            stat_ = git(host, "diff", "--numstat", "-z", "--no-renames", ctx.start, ctx.tip).stdout
            lines.append("diff stat:")
            lost = []
            for rec in stat_.split(b"\0"):
                parts = rec.decode("utf-8", "replace").split("\t")
                if len(parts) == 3 and _plain_path(parts[2]):
                    lines.append("  %s +%s -%s" % (parts[2], parts[0], parts[1]))
                    if parts[2] in SHARED_FILES:
                        lost.append("%s %s" % (parts[2], parts[1]))
            lines.append("lines lost in shared files: %s" % (", ".join(lost) or "none"))
            lines.append("")
    behind = info.get("behind") or []
    lines.append("live %s / main %s / %s tasks not deployed%s" % (
        info["live"], info["main"], info["not_deployed"],
        "; daemon code behind: %s" % ", ".join(behind) if behind else ""))
    lines.append("next: %s" % next_step(ctx, state))
    if ctx.tail:
        lines += ["", "suite.log, the last %d lines:" % TAIL_LINES, "```"] + ctx.tail + ["```"]
    lines.append("")
    _write(ctx.folder / "result.md", "\n".join(lines))


_TSV_STATE = build.STATE_RE


def write_state_tsv(host: Host, info: dict, held: str | None) -> None:
    """<shared>/build/state.tsv for the board: ids, states, hashes and times only, one row per task."""
    rows = ["# written by awb build serve: ids, states, hashes and times; the board and the portal read it"]
    held_word = held if held and re.fullmatch(r"[a-z0-9 -]{1,40}", held) else None
    rows.append("queue\t%s\t%s" % ("held" if held_word else "ok", held_word or "-"))
    for tid in build.ids_with_runs(host.runs):
        r = build.latest(tid, host.runs)
        if r is None or not _TSV_STATE.fullmatch(r.state):
            continue
        commit = (_text(r.folder / "commit") or "").strip()
        mail = (_text(r.folder / "mail") or "").strip()
        rows.append("task\t%s\t%d\t%s\t%s\t%s\t%s" % (
            tid, r.n, r.state, r.since.strftime(build._STAMP) if r.since else "-",
            commit[:7] if SHA_RE.fullmatch(commit) else "-", "not_sent" if mail == "not sent" else
            "sent" if mail == "sent" else "-"))
    rows += nudged_rows(host.runs)
    for cid in build.check_ids(host.runs)[-build.CHECKS_SHOWN:]:
        r = build.latest(cid, host.runs)
        if r is None or not _TSV_STATE.fullmatch(r.state):
            continue
        head = (_text(r.folder / "head") or "").strip()
        rows.append("check\t%s\t%s\t%s\t%s" % (cid, r.state, r.since.strftime(build._STAMP) if r.since else "-",
                                                 head[:7] if SHA_RE.fullmatch(head) else "-"))
    rows.append("live\t%s" % info["live"])
    rows.append("main\t%s" % info["main"])
    rows.append("not_deployed\t%s" % info["not_deployed"])
    rows.append("daemons\t%s" % (",".join(info.get("behind") or []) or "current"))
    board_write(host.board, "state.tsv", ("\n".join(rows) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------- a task unit that failed (SB5)

def runner_alive(runs: Path) -> bool:
    """True while a runner holds runs/.runner.lock (it waits on its units and reports them itself)."""
    try:
        fd = os.open(Path(runs) / ".runner.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def record_failed(host: Host, name: str, result: str) -> str:
    """The oneshot's work: one `failed` row in state.tsv (the board line) when no runner waits on the unit. Returns
    `watched`, `written` or `not written`. The runner's next write of state.tsv drops the row: its reconcile reports
    the task by then."""
    if runner_alive(host.runs):
        return "watched"
    rows = ["# written by awb build serve: ids, states, hashes and times; the board and the portal read it"]
    failed: list[str] = []
    for row in (board_read(host.board, "state.tsv", 256 << 10) or "").splitlines():
        kind = row.split("\t", 1)[0]
        if kind == "failed":
            failed.append(row)
        elif kind in ("queue", "task", "nudged", "check", "live", "main", "not_deployed", "daemons"):
            rows.append(row)
    failed.append("failed\t%s\t%s\t%s" % (name, result, host.now().strftime(build._STAMP)))
    rows += failed[-FAILED_ROWS:]
    ok = board_write(host.board, "state.tsv", ("\n".join(rows) + "\n").encode("utf-8"))
    return "written" if ok else "not written"


def failed_main(name: str) -> int:
    """`awb build failed UNIT`, the ExecStart of awb-task-failed@.service (OnFailure= of every task unit): the unit
    name of the instance and MONITOR_SERVICE_RESULT, each matched against its vocabulary."""
    if config.is_work_user() or config.is_build_user():
        print("awb build: the failure line is the owner's", file=sys.stderr)
        return 2
    if not UNIT_RE.fullmatch(name) or name == PROBE_UNIT:
        print("awb build: not a task unit of the runner", file=sys.stderr)
        return 2
    result = os.environ.get("MONITOR_SERVICE_RESULT", "")
    result = result if result in RESULTS else "unknown"
    try:
        host = Host.from_config()
    except Exception as err:
        print("awb build: the failure line cannot be written (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    word = record_failed(host, name, result)
    if word == "watched":
        print("awb build: %s failed (%s); the runner waits on it and reports it" % (name, result))
        return 0
    if word == "written":
        print("awb build: %s failed (%s) while no runner watched; one line on the board" % (name, result))
        return 0
    print("awb build: %s failed (%s); the board line could not be written" % (name, result), file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- the nudge of a waiting task (A6)

def nudge_commands(tid: str, state: str) -> list[str]:
    """His exact next commands for a run that waits on him: fixed words and the id, nothing of the agent's side. A
    hold without a command of its own is cleared by a new run, as `awb build list` says."""
    w = state.split(":", 1)[0]
    if w == "asked":
        return ["awb build show %s" % tid, 'awb build answer %s "..."' % tid]
    if w == "red":
        return ["awb build retry %s" % tid, "awb build widen %s PATH..." % tid]
    if state == LOGIN_HELD:
        return [LOGIN_COMMAND, "awb build retry %s" % tid]
    if state == "held: checks pending":
        return ["awb vault unlock"]
    return ["awb build retry %s" % tid]


def _nudge_mark(r: build.Run) -> list[str] | None:
    """The fields of a run's mark when it holds the run's state line of now and a time, else None."""
    mark = (_text(r.folder / NUDGE_FILE) or "").rstrip("\n").split("\t")
    if r.since is None or len(mark) != 3 or mark[:2] != [r.state, r.since.strftime(build._STAMP)]:
        return None
    try:
        datetime.strptime(mark[2], build._STAMP)
    except ValueError:
        return None
    return mark


def nudge_due(host: Host, r: build.Run) -> bool:
    """The run is asked, red or held for NUDGE_MINUTES and its state line of now has no mail yet. An answer, a widen
    or a retry writes a new state or a new run, so a due run had none of them since."""
    if r.word not in NUDGE_WORDS or r.since is None or not _TSV_STATE.fullmatch(r.state):
        return False
    if (host.now() - r.since).total_seconds() < NUDGE_MINUTES * 60:
        return False
    return _nudge_mark(r) is None


def nudged_rows(runs: Path) -> list[str]:
    """The `nudged` rows of state.tsv: id, state and the time of the mail of every newest run whose mark holds its
    state line of now. The board line names them until the state moves."""
    rows = []
    for tid in build.ids_with_runs(runs):
        r = build.latest(tid, runs)
        mark = _nudge_mark(r) if r is not None else None
        if mark is not None:
            rows.append("nudged\t%s\t%s\t%s" % (tid, r.state, mark[2]))
    return rows


def nudge(host: Host) -> list[str]:
    """`awb build nudge`: one mail through the runner's mail path for every task whose newest run is due
    (nudge_due), its body his next commands and never the agent's text. The mark is written only when the mail was
    sent, so one that failed is tried at the next call. Then the `nudged` rows of state.tsv, the other rows as they
    are. One line per run it tried."""
    out = []
    for tid in build.ids_with_runs(host.runs):
        r = build.latest(tid, host.runs)
        if r is None or not nudge_due(host, r):
            continue
        lines = ["%s is %s since %s and waits on you. As the owner in your terminal:" % (
            tid, r.state, build._since(r.since))] + nudge_commands(tid, r.state)
        try:
            sent = bool(host.mail and host.mail("Workbench build %s: %s, waits on you" % (tid, r.word),
                                                "\n".join(lines) + "\n"))
        except Exception:
            sent = False
        if sent:
            _write(r.folder / NUDGE_FILE, "%s\t%s\t%s\n" % (r.state, r.since.strftime(build._STAMP),
                                                         host.now().strftime(build._STAMP)))
        out.append("%s %s: %s" % (tid, r.state, "mail sent" if sent else "mail not sent, tried again next time"))
    text = board_read(host.board, "state.tsv", 256 << 10)
    if text is not None:
        rows = [x for x in text.splitlines() if x.split("\t", 1)[0] != "nudged"]
        board_write(host.board, "state.tsv", ("\n".join(rows + nudged_rows(host.runs)) + "\n").encode("utf-8"))
    return out


def nudge_main() -> int:
    """`awb build nudge`, the ExecStart of awb-build-nudge.service, which its timer starts every 15 minutes as the
    owner (seal/setup.sh)."""
    if config.is_work_user() or config.is_build_user():
        print("awb build: the nudge is the owner's", file=sys.stderr)
        return 2
    try:
        host = Host.from_config()
    except Exception as err:
        print("awb build: the nudge cannot run (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    for ln in nudge(host):
        print("awb build: nudge %s" % ln)
    return 0


# --------------------------------------------------------------------------- the commands

ALLOW_PROTECTED = "allow-protected"
"""The file of a check's run folder that `--allow-protected` writes (R2): check 7 then passes a change of a path in
`build_checks.PROTECTED` and names it; without the file such a change is `red: protected path`."""


def check_main(ref: str, host: Host | None = None, allow_protected: bool = False) -> int:
    """`awb build check REF` (F2 G1): REF of the owner's working copy into the quarantine of main.git, then the checks
    of a task on it (check_branch) through the runner's lock: at once when no runner holds it, else the runner takes
    it between two tasks and this waits. The state line and the check lines; exit 0 for `passed`, else 1. It never
    merges, pushes or deploys and starts no model session. `allow_protected` lets the branch change a protected path
    (R2), which the owner reads first."""
    if config.is_work_user() or config.is_build_user():
        print("awb build: the check of a branch is the owner's", file=sys.stderr)
        return 2
    try:
        h = host or Host.from_config()
    except Exception as err:
        print("awb build: the check cannot start (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    h.quiet = True
    commit = resolve_ref(h, ref)
    if commit is None:
        print("awb build: REF is not a branch or commit of your working copy", file=sys.stderr)
        return 2
    try:
        ensure_main(h)
        run = build.new_check(h.runs, now=h.now())
    except (GitFailed, OSError, ValueError) as err:
        print("awb build: the check cannot start (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    _write(run.folder / "head", commit + "\n")
    if allow_protected:
        _write(run.folder / ALLOW_PROTECTED, "yes\n")
    runner = Runner(h)
    if not fetch_ref(h, runner._check_ctx(run), commit):
        ctx = runner._check_ctx(run)
        _fail(ctx, 4, "red: no commit")
        runner._checks_log(ctx)
        runner.finish(ctx, "red: no commit")
    told = False
    while True:
        r = build.latest(run.id, h.runs)
        if r is None or r.state not in ("queued", "running"):
            break
        fd = acquire_lock(h.runs)
        if fd is not None:
            try:
                runner._guarded(run.id, lambda: runner.check_branch(r))
            finally:
                os.close(fd)
            break
        if not told:
            print("check %s of %s queued: the runner takes it after its current task" % (run.id, commit[:7]))
            told = True
        h.sleep(CHECK_WAIT)
    r = build.latest(run.id, h.runs)
    print(build.line(r))
    for ln in (_text(r.folder / "checks.log") or "").split("\n"):
        if ln.strip():
            print("  " + ln)
    return 0 if r.state == "passed" else 1


def serve_main() -> int:
    if config.is_work_user() or config.is_build_user():
        print("awb build: the runner runs as the owner", file=sys.stderr)
        return 2
    try:
        host = Host.from_config()
    except Exception as err:
        print("awb build: the runner cannot start (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    try:
        return Runner(host).serve()
    except GitFailed as err:
        print("awb build: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb build: the runner cannot start (%s)" % (err.strerror or type(err).__name__).lower(),
              file=sys.stderr)
        return 2


# --------------------------------------------------------------------------- the parts (F1 step 6)

parts.join(__name__, "awb.build_units", "awb.build_unit_side", "awb.build_checks", "awb.build_probe",
           "awb.build_selftest")


def __getattr__(name: str):
    """A name of the selftest, which imports this module and so is loaded on its first use here."""
    if not name.startswith("__"):
        from awb import build_selftest  # noqa: F401

        parts.join(__name__, "awb.build_selftest")
        if name in globals():
            return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
