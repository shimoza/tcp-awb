"""Codex for bounded non-UI work (CX): the quota readout and a task run in a worktree.

    awb codex usage [--json]
    awb codex run TASKFILE [--base REF]
    awb codex status

`usage` starts `codex app-server` (JSON-RPC over stdio), sends `initialize`, the `initialized` notification and
`account/rateLimits/read`, reads the answer with a deadline of 30 s and kills the process. It spends no tokens. It
prints one line per window (5h and week), the plan type and the reset credits, and never the account id, the e-mail
or a token. Exit 2 with one line when codex is missing or does not answer.

`run` checks the task file as `awb ui submit` checks its file (no registered name, secret, token, private key, home
path or blocklisted name) and wants a "Paths:" and an "Acceptance:" line. It makes a git worktree of REF (default
HEAD) in <state>/<ID>/wt on the branch codex/<ID>, runs `codex exec --sandbox workspace-write --cd <wt>` with the
task text on stdin and a deadline of 30 minutes, then writes <state>/<ID>/result.md: the exit, the diff stat, the
files changed outside "Paths:" (listed as a refusal) and the run log. It never commits, pushes, merges or deletes
the worktree: the steering session reviews the diff, runs the tests and the gate and commits as the owner. One run
at a time (the lock file <state>/lock); a second run is refused with the open ID.

`status` prints one line per run: id, state and finish time.

The state folder is ~/.local/state/awb-codex (AWB_CODEX_STATE gives another). AWB_CODEX_DEADLINE and
AWB_CODEX_TIMEOUT give other deadlines in seconds for `usage` and `run`.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import check
from awb.tcp import ui

STATE = Path(".local/state/awb-codex")          # under the home of the user who runs codex
USAGE_DEADLINE = 30.0
RUN_TIMEOUT = 30 * 60.0
PATHS_RE = re.compile(r"(?m)^[ \t]*Paths:[ \t]*(\S.*)$")
ACCEPT_RE = re.compile(r"(?m)^[ \t]*Acceptance:[ \t]*\S")
WINDOWS = (("primary", "5h"), ("secondary", "week"))


class CodexError(Exception):
    """A refusal; the text names a rule or a state, never a value of the task or the account."""


class Unavailable(CodexError):
    """codex is missing or does not answer."""


def state_dir() -> Path:
    env = os.environ.get("AWB_CODEX_STATE")
    return Path(env) if env else Path.home() / STATE


def _shown(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _utc(value) -> str:
    """An epoch (seconds) or an ISO time as 'YYYY-MM-DD HH:MM UTC'; '?' when it is neither."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            when = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return "?"


def _epoch(value) -> float | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


# --------------------------------------------------------------------------- usage


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()
        except OSError:
            pass


def read_limits(deadline: float) -> dict:
    """The result of account/rateLimits/read; Unavailable when codex is missing, fails or is too slow."""
    end = time.monotonic() + deadline
    try:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise Unavailable("codex is missing") from None
    buf = b""

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def answer(want: int) -> dict:
        nonlocal buf
        while True:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        raise Unavailable("codex refused the request")
                    return msg["result"] if isinstance(msg["result"], dict) else {}
            left = end - time.monotonic()
            if left <= 0:
                raise Unavailable("codex did not answer in %d s" % deadline)
            if not select.select([proc.stdout], [], [], left)[0]:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise Unavailable("codex ended without an answer")
            buf += chunk

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "awb", "title": "Architect Workbench", "version": "1"}}})
        answer(1)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        return answer(2)
    except (BrokenPipeError, OSError):
        raise Unavailable("codex ended without an answer") from None
    finally:
        _kill(proc)


def summary(result: dict) -> dict:
    """Only the fields `usage` shows: windows, plan and credits. Nothing else of the answer is kept."""
    limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
    out: dict = {"windows": {}}
    for key, label in WINDOWS:
        w = limits.get(key)
        if isinstance(w, dict):
            out["windows"][label] = {"used_percent": w.get("usedPercent"), "window_minutes": w.get("windowDurationMins"),
                                     "resets_at": _utc(w.get("resetsAt"))}
    plan = limits.get("planType", result.get("planType"))
    out["plan"] = plan if isinstance(plan, str) else None
    block = limits.get("rateLimitResetCredits", result.get("rateLimitResetCredits"))
    credits = block.get("credits") if isinstance(block, dict) else None
    open_ = [c for c in credits if isinstance(c, dict) and str(c.get("status", "")).lower() == "available"] \
        if isinstance(credits, list) else []
    stamps = sorted((e, c.get("expiresAt")) for c in open_ if (e := _epoch(c.get("expiresAt"))) is not None)
    out["credits_available"] = len(open_)
    out["credits_earliest_expiry"] = _utc(stamps[0][1]) if stamps else None
    return out


def _number(value) -> str:
    return ("%g" % value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def usage_lines(data: dict) -> list[str]:
    lines = ["%s: %s%% used, resets %s" % (label, _number(w["used_percent"]), w["resets_at"])
             for label, w in data["windows"].items()]
    lines.append("plan: %s" % (data["plan"] or "?"))
    line = "reset credits: %d available" % data["credits_available"]
    if data["credits_earliest_expiry"]:
        line += ", earliest expiry %s" % data["credits_earliest_expiry"]
    lines.append(line)
    return lines


def _usage(a) -> int:
    data = summary(read_limits(_seconds("AWB_CODEX_DEADLINE", USAGE_DEADLINE)))
    if a.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print("\n".join(usage_lines(data)))
    return 0


# --------------------------------------------------------------------------- run


def check_task(path: Path) -> tuple[str, str, list[str]]:
    """(id, text, paths) of a task file that may go to Codex; CodexError names what is wrong."""
    job_id = path.stem
    if path.suffix != ".txt" or not ui.ID_RE.match(job_id):
        raise CodexError("the task file must be named <ID>.txt with an id of 3-80 lowercase letters, digits or hyphens")
    if not path.is_file():
        raise CodexError("the task file is missing")
    data = path.read_bytes()
    if not data.strip() or len(data) > ui.MAX_TASK:
        raise CodexError("the task file must be nonempty and at most %d bytes" % ui.MAX_TASK)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CodexError("the task file is not UTF-8") from None
    found = PATHS_RE.search(text)
    if not found:
        raise CodexError('the task must have a "Paths:" line')
    if not ACCEPT_RE.search(text):
        raise CodexError('the task must have an "Acceptance:" line')
    matcher, why = ui._matcher()
    if matcher is None:
        raise CodexError(why)
    try:
        problems = ui.findings(text, matcher, ui.TASK_CLASSES)
    except check.CheckUnavailable:
        raise CodexError("the name check cannot run: is the vault unlocked?") from None
    if problems:
        raise CodexError("the task file is refused: " + "; ".join(problems))
    paths = [p.strip().strip("`'\"").rstrip("/") for p in re.split(r"[,\s]+", found.group(1)) if p.strip("`'\",/ ")]
    if not paths:
        raise CodexError('the "Paths:" line names no path')
    return job_id, text, paths


def outside(changed: list[str], paths: list[str]) -> list[str]:
    """The changed files that no entry of Paths: covers (a file, a folder or a pattern)."""
    def covered(f: str) -> bool:
        return any(f == p or f.startswith(p + "/") or fnmatch.fnmatch(f, p) for p in paths)
    return [f for f in changed if not covered(f)]


def _git(wt: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=timeout)


def _set_state(run: Path, state: str, finished: bool = False) -> None:
    f = run / "state.json"
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    old["state"] = state
    if finished:
        old["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    f.write_text(json.dumps(old), encoding="utf-8")


def _lock(root: Path, job_id: str):
    """The open lock file with the lock held; CodexError names the open ID when another run holds it."""
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / "lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        open_id = fh.read().strip() or "unknown"
        fh.close()
        raise CodexError("another run is open: %s" % open_id) from None
    fh.seek(0)
    fh.truncate()
    fh.write(job_id + "\n")
    fh.flush()
    return fh


def _repo() -> Path:
    done = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if done.returncode != 0:
        raise CodexError("this is not a git repository")
    return Path(done.stdout.strip())


def _result_text(job_id: str, base: str, state: str, code, stat: str, bad: list[str], log: Path) -> str:
    lines = ["# Codex run %s" % job_id, "", "- state: %s" % state, "- exit: %s" % code,
             "- branch: codex/%s (no commit, base %s)" % (job_id, base), "- worktree: %s" % _shown(log.parent / "wt"),
             "- run log: %s" % _shown(log), "", "## Diff stat", "", stat.strip() or "(no change)", "",
             "## Files changed outside Paths:", ""]
    if bad:
        lines.append("REFUSED: the run changed %d file%s outside Paths:" % (len(bad), "" if len(bad) == 1 else "s"))
        lines += ["- %s" % f for f in bad]
    else:
        lines.append("none")
    lines += ["", "Review the diff, run the tests and the gate, and commit as the owner. Nothing is committed, "
              "pushed or merged, and the worktree stays.", ""]
    return "\n".join(lines)


def _run(a) -> int:
    job_id, text, paths = check_task(Path(a.taskfile).absolute())
    repo = _repo()
    root = state_dir()
    lock = _lock(root, job_id)
    try:
        run = root / job_id
        if run.exists():
            raise CodexError("a run %s exists already" % job_id)
        run.mkdir(parents=True)
        wt, log = run / "wt", run / "run.log"
        added = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "codex/%s" % job_id, str(wt), a.base],
                               capture_output=True, text=True)
        if added.returncode != 0:
            run.rmdir()
            raise CodexError("the worktree of %s cannot be made (branch exists or unknown ref)" % a.base)
        (run / "state.json").write_text(json.dumps({"state": "running", "started": datetime.now(timezone.utc)
                                                    .isoformat(timespec="seconds")}), encoding="utf-8")
        state, code = "done", None
        with open(log, "wb") as out:
            try:
                proc = subprocess.Popen(["codex", "exec", "--sandbox", "workspace-write", "--cd", str(wt)],
                                        stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, cwd=str(wt),
                                        start_new_session=True)
            except OSError:
                state, proc = "failed", None
            if proc is not None:
                try:
                    proc.communicate(text.encode("utf-8"), timeout=_seconds("AWB_CODEX_TIMEOUT", RUN_TIMEOUT))
                except subprocess.TimeoutExpired:
                    state = "timeout"
                    _kill(proc)
                code = proc.returncode
                if state == "done" and code != 0:
                    state = "failed"
        _git(wt, "add", "-N", ".")
        stat = _git(wt, "diff", "--stat", "HEAD").stdout
        names = _git(wt, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
        bad = outside([n for n in names if n], paths)
        base = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip() or a.base
        (run / "result.md").write_text(_result_text(job_id, base, state, code, stat, bad, log), encoding="utf-8")
        _set_state(run, state, finished=True)
        print("Codex run %s: %s (exit %s); result %s" % (job_id, state, code, _shown(run / "result.md")))
        if bad:
            print("REFUSED: %d file%s changed outside Paths:" % (len(bad), "" if len(bad) == 1 else "s"))
        return 0 if state == "done" and not bad else 1
    finally:
        lock.close()


# --------------------------------------------------------------------------- status


def _status(a) -> int:
    root = state_dir()
    dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    for d in dirs:
        try:
            info = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        finished = info.get("finished")
        if info.get("state") == "running":
            probe = _probe(root)
            if probe != d.name:
                info["state"] = "lost"
        print("%-24s %-8s finished %s" % (d.name, info.get("state", "unknown"), _utc(finished) if finished else "-"))
    if not dirs:
        print("No runs.")
    return 0


def _probe(root: Path) -> str | None:
    """The ID of the open run (the lock is held), None when no run is open."""
    try:
        fh = open(root / "lock", "a+", encoding="utf-8")
    except OSError:
        return None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            return fh.read().strip() or None
        return None


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awb codex", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage", help="the Codex quota: 5-hour and weekly window, plan, reset credits")
    u.add_argument("--json", action="store_true", help="the same as JSON")
    r = sub.add_parser("run", help="a bounded code task for Codex in a worktree; never commits")
    r.add_argument("taskfile")
    r.add_argument("--base", default="HEAD", help="the ref the worktree starts from (default HEAD)")
    sub.add_parser("status", help="one line per run: id, state, finish time")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        return {"usage": _usage, "run": _run, "status": _status}[a.cmd](a)
    except Unavailable as err:
        print("awb codex: %s" % err, file=sys.stderr)
        return 2
    except CodexError as err:
        print("awb codex: %s" % err, file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("awb codex: git did not answer", file=sys.stderr)
        return 2
    except OSError as err:
        print("awb codex: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
