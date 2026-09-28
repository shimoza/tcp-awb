"""Jobs against a cloud tenant that survive their limits (T-60).

What this answers, all from one teardown in September: output was buffered, so a job killed at the time limit lost
everything it had done; a retry loop was sized past the limit; a watcher waited for "no such port anywhere in the
tenant" while two unrelated ones exist for good; a verification ran a minute after an asynchronous delete and
contradicted it; a direct index into an answer killed a job halfway.

- A list call answers one of three states: `list` (items), `empty` (the API answered and holds none) or `unknown`
  (no usable answer, with a reason). Unknown is never read as empty: a Listing refuses to be used as a truth value.
- `Job.say` writes one line and flushes it at once, so a killed job keeps everything it said.
- A job has a time budget. `Job.step` records every finished step in a state file (one JSON line, flushed and
  synced), refuses to start a step when the budget is spent (JobStopped) and skips a recorded step on the next
  run: the same command, run again, resumes where it stopped.
- `retry` never sleeps past the budget of its job.
- `wait_gone` asks about one resource, by its own probe, until it is gone. A probe that looks at the whole tenant is
  the wrong probe.
- `dig` reads a nested answer without raising: a missing part is MISSING, never an exception halfway through.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LIST, EMPTY, UNKNOWN = "list", "empty", "unknown"
STATES = (LIST, EMPTY, UNKNOWN)
PRESENT, GONE = "present", "gone"

EXIT_STOPPED = 3
"""Exit code of a command whose job stopped at its time limit: run the same command again to resume."""

DEFAULT_BUDGET = 100.0
"""Seconds. A command run by a session is killed after 120 seconds; the budget stops a job cleanly before that."""

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class Listing:
    """The answer of a list call: state list (with items), empty or unknown (with a reason)."""

    state: str
    items: tuple = ()
    reason: str = ""

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError("a listing state is list, empty or unknown")
        if (self.state == LIST) != bool(self.items):
            raise ValueError("a listing has items exactly when its state is list")

    @classmethod
    def of(cls, items) -> "Listing":
        items = tuple(items)
        return cls(LIST, items) if items else cls(EMPTY)

    @classmethod
    def unknown(cls, reason: str) -> "Listing":
        return cls(UNKNOWN, (), reason)

    @property
    def known(self) -> bool:
        return self.state != UNKNOWN

    def describe(self) -> str:
        if self.state == LIST:
            return "list (%d)" % len(self.items)
        return self.state if self.state == EMPTY else "unknown: %s" % self.reason

    def __bool__(self):
        raise TypeError("a Listing has three states; ask .state instead of its truth value")


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def dig(obj: Any, *path, default: Any = MISSING) -> Any:
    """`obj[p0][p1]...` without raising: `default` (MISSING) when any part is absent or of another type."""
    for part in path:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(part, int) and not isinstance(part, bool) and isinstance(obj, (list, tuple)) \
                and -len(obj) <= part < len(obj):
            obj = obj[part]
        else:
            return default
    return obj


class JobStopped(Exception):
    """The time budget is spent before the next step. Nothing is lost: run the same command again to resume."""

    def __init__(self, done: int):
        super().__init__("stopped at the time limit after %d finished step(s); run the same command again to resume"
                         % done)
        self.done = done


class Job:
    """A named job with a time budget and a state file `<state_dir>/<name>.jsonl` (no file when state_dir is None).

    `step(key, fn)` runs `fn` once per key across runs: a finished step is recorded with the value `fn` returned
    (JSON), and a later run returns the recorded value without calling `fn` again. `finish()` removes the state
    file when the whole job is done, so that the next run starts fresh."""

    def __init__(self, name: str, state_dir: Path | None, *, budget: float = DEFAULT_BUDGET, out=None,
                 clock: Callable[[], float] = time.monotonic):
        if not _NAME_RE.match(name):
            raise ValueError("a job name is letters, digits, dot, dash and underscore")
        self.name = name
        self.budget = float(budget)
        self.out = out
        self.clock = clock
        self.started = clock()
        self.state_path = Path(state_dir) / (name + ".jsonl") if state_dir is not None else None
        self._done: dict[str, Any] = {}
        self.skipped = 0
        self.ran = 0
        self._load()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        for line in self.state_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue                        # a line torn by a kill: that step runs again
            if isinstance(rec, dict) and isinstance(rec.get("key"), str):
                self._done[rec["key"]] = rec.get("value")

    def say(self, text: str) -> None:
        """One line, flushed at once."""
        out = self.out if self.out is not None else sys.stdout
        out.write(text + "\n")
        out.flush()

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return self.budget - self.elapsed()

    def is_done(self, key: str) -> bool:
        return key in self._done

    @property
    def done_count(self) -> int:
        return len(self._done)

    def mark(self, key: str, value: Any = None) -> None:
        """Record `key` as finished, with a JSON value; the line is on disk before this returns."""
        self._done[key] = value
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"key": key, "value": value,
                           "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                          ensure_ascii=False)
        fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def step(self, key: str, fn: Callable[[], Any], *, cost: float = 0.0) -> Any:
        """Run `fn` unless `key` is recorded; refuse (JobStopped) when less than `cost` seconds of the budget are left
        or the budget is spent."""
        if key in self._done:
            self.skipped += 1
            return self._done[key]
        if self.remaining() <= cost:
            raise JobStopped(len(self._done))
        value = fn()
        self.mark(key, value)
        self.ran += 1
        return value

    def finish(self) -> None:
        """The whole job is done: the state file goes, the next run starts fresh."""
        if self.state_path is not None:
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass


def retry(fn: Callable[[], Any], *, attempts: int = 3, delay: float = 2.0, job: Job | None = None,
          retry_on: tuple = (Exception,), sleep: Callable[[float], None] | None = None) -> Any:
    """`fn()`, again after `delay`, 2 x `delay`, ... on the exceptions of `retry_on`, at most `attempts` times. With a
    job, a wait that would pass the end of its budget is not taken: the last error is raised instead."""
    if attempts < 1:
        raise ValueError("attempts is at least 1")
    for n in range(attempts):
        try:
            return fn()
        except retry_on:
            if n == attempts - 1:
                raise
            wait = delay * (2 ** n)
            if job is not None and job.remaining() <= wait:
                raise
            (sleep or time.sleep)(wait)
    raise AssertionError("unreachable")


def wait_gone(probe: Callable[[], str], *, timeout: float, interval: float = 5.0,
              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] | None = None) -> str:
    """Ask `probe` until it answers gone or `timeout` seconds have passed. The probe looks at ONE resource by its own
    id and answers present, gone or unknown. Returns gone, or the last answer (present or unknown) at the timeout."""
    deadline = clock() + timeout
    while True:
        state = probe()
        if state not in (PRESENT, GONE, UNKNOWN):
            raise ValueError("a probe answers present, gone or unknown")
        if state == GONE:
            return GONE
        if clock() + interval > deadline:
            return state
        (sleep or time.sleep)(interval)
