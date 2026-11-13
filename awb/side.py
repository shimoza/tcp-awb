"""Side sessions of a project (SS, 2026-11-13): side work (a report, research, an analysis) runs in a session of
its own next to the main session, so it does not fill the main session's context.

    awb side start NAME --brief FILE [--model MODEL] [--effort LEVEL]
    awb side status
    awb side take NAME [--merge]
    awb side stop NAME
    awb side drop NAME

It runs in the root of a project folder (a project code). `start` makes a git worktree .side/NAME on the
branch side/NAME (`.side/` goes into .git/info/exclude, never into a commit), and starts a headless client session
there in the background with the brief as its task: the brief is a file of the project the main session writes,
so the side session gets that text and the files it names, never the main session's context. The side session is
a session of the same user in a folder of the same project: the managed settings, the deny rules and the hooks of
the work user apply to it as to the main session. It runs without approval prompts (no one is there to answer
one) and without MCP servers. It writes its result to SIDE-RESULT.md in its worktree and commits there; the main
session never writes in .side/NAME and the side session never writes outside it, so two writers never touch the
same files.

`status` prints one line per side session: name, state (running, done, failed, lost), model, effort, commits and
whether a result is there. `take` copies SIDE-RESULT.md to notes/side-NAME.md of the main folder and lists the
commits and files of the branch; with `--merge` it merges side/NAME into the main branch (no fast-forward; a
conflict is aborted and named) and removes the worktree and the branch. `stop` ends a running side session,
`drop` removes its worktree and branch without taking anything.

The model and the effort are the main session's choice per task (for example fable at effort high for an
analysis, sonnet for a report). AWB_SIDE_CLIENT names another client binary (tests).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SIDE_DIR = ".side"
RESULT = "SIDE-RESULT.md"
NOTES = "notes"
MAX_BRIEF = 64 * 1024
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"
CLIENT_ENV = "AWB_SIDE_CLIENT"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.\-\[\]]{0,59}")
_DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

PREAMBLE = """You are a side session of project {code}, started by its main session for one piece of side work.
Your folder is a git worktree of the project on the branch side/{name}. Work only inside this folder. The project
rules in CLAUDE.md apply to you as to the main session. Do not change STATE.md or OPEN.md: they belong to the main
session. Write your result to {result} in this folder (plain Markdown, codes only, never a name), put any other
file you make under deliverables/ or notes/, commit your own paths with `git commit -- PATH...`, and end with one
last line that starts with DONE. If something blocks you, write it into {result}, commit and end with BLOCKED.

The task from the main session:

"""


class SideError(Exception):
    """A fixed message; never a file's content."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_root(cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()).resolve()
    from awb import codes

    if not codes.is_project_code(root.name) or not (root / ".git").exists() or not (root / "CLAUDE.md").is_file():
        raise SideError("run awb side in the root of a project folder (tcp-xxxx)")
    if (root / ".git").is_file():
        raise SideError("run awb side in the main folder of the project, not in a side session")
    return root


def _name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise SideError("NAME is 1-31 lowercase letters, digits or hyphens")
    return name


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SideError("git %s failed" % args[0])
    return r


def _paths(root: Path, name: str) -> dict[str, Path]:
    base = root / SIDE_DIR
    return {"base": base, "wt": base / name, "state": base / (name + ".state"), "exit": base / (name + ".exit"),
            "out": base / (name + ".json"), "err": base / (name + ".err")}


def _exclude(root: Path) -> None:
    info = root / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if not any(line.strip() in (SIDE_DIR + "/", "/" + SIDE_DIR + "/") for line in text.splitlines()):
        f.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "/" + SIDE_DIR + "/\n",
                     encoding="utf-8")


def _client() -> str:
    c = os.environ.get(CLIENT_ENV) or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not os.path.isfile(c) or not os.access(c, os.X_OK):
        raise SideError("the client binary is missing")
    return c


def client_argv(client: str, model: str, effort: str) -> list[str]:
    """The headless side session: the model and effort the main session chose, no approval prompt, no MCP server,
    one JSON object at the end. The managed settings and hooks of the user apply as always."""
    return [client, "-p", "--model", model, "--effort", effort, "--permission-mode", "bypassPermissions",
            "--strict-mcp-config", "--output-format", "json"]


_WRAPPER = r"""
import os, subprocess, sys
argv, wt, out, err, exitf, prompt = sys.argv[1:-5], *sys.argv[-5:]
with open(prompt, "rb") as p, open(out, "wb") as o, open(err, "wb") as e:
    rc = subprocess.run(argv, cwd=wt, stdin=p, stdout=o, stderr=e).returncode
os.unlink(prompt)
with open(exitf + ".tmp", "w") as f:
    f.write("%d\n" % rc)
os.replace(exitf + ".tmp", exitf)
"""


def start(name: str, brief: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
          cwd: Path | None = None) -> str:
    root = project_root(cwd)
    name = _name(name)
    if not _MODEL_RE.fullmatch(model or ""):
        raise SideError("MODEL is a model alias or id such as sonnet, opus or fable")
    if effort not in EFFORTS:
        raise SideError("LEVEL is one of: %s" % ", ".join(EFFORTS))
    bpath = (root / brief).resolve()
    if not bpath.is_relative_to(root) or bpath.is_relative_to(root / SIDE_DIR) or not bpath.is_file():
        raise SideError("the brief is a file of this project, outside .side/")
    if bpath.stat().st_size > MAX_BRIEF:
        raise SideError("the brief is longer than 64 KiB; point to files instead of pasting them")
    text = bpath.read_text(encoding="utf-8")
    p = _paths(root, name)
    if p["wt"].exists() or p["state"].exists():
        raise SideError("a side session of that name exists: awb side status, awb side drop NAME")
    if _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/side/" + name, check=False).returncode == 0:
        raise SideError("the branch side/NAME exists: awb side drop NAME")
    client = _client()
    p["base"].mkdir(mode=0o750, exist_ok=True)
    _exclude(root)
    _git(root, "worktree", "add", "--quiet", "-b", "side/" + name, str(p["wt"]), "HEAD")
    # the template lines of the project as `awb projects sync` keeps them: the main folder may carry them
    # uncommitted, and the start guard of the side session compares them with the installed rules
    from awb import rulesync

    try:
        rulesync.sync_project(p["wt"])
    except Exception:  # the start guard names the rules then; never a failed start for this
        pass
    prompt = p["base"] / (name + ".prompt")
    prompt.write_text(PREAMBLE.format(code=root.name, name=name, result=RESULT) + text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    proc = subprocess.Popen([sys.executable, "-I", "-c", _WRAPPER, *client_argv(client, model, effort),
                             str(p["wt"]), str(p["out"]), str(p["err"]), str(p["exit"]), str(prompt)],
                            cwd=str(p["wt"]), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    p["state"].write_text(json.dumps({"name": name, "model": model, "effort": effort, "started": now(),
                                      "pid": proc.pid, "brief": bpath.relative_to(root).as_posix()}) + "\n",
                          encoding="utf-8")
    return ("side session %s started (%s, effort %s) in %s/%s; awb side status, then awb side take %s"
            % (name, model, effort, SIDE_DIR, name, name))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _state(p: dict[str, Path]) -> tuple[str, dict]:
    try:
        meta = json.loads(p["state"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "lost", {}
    if p["exit"].exists():
        try:
            rc = int(p["exit"].read_text().strip())
        except ValueError:
            rc = 1
        if rc != 0:
            return "failed", meta
        try:
            out = json.loads(p["out"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "failed", meta
        # a refused prompt (a guard of the start hook) ends with exit 0 and no turn: that is no result
        return ("done" if isinstance(out, dict) and not out.get("is_error") and out.get("num_turns") else "failed"), meta
    return ("running" if _alive(int(meta.get("pid", 0) or 0)) else "lost"), meta


def status_lines(cwd: Path | None = None) -> list[str]:
    root = project_root(cwd)
    base = root / SIDE_DIR
    lines = []
    for f in sorted(base.glob("*.state")) if base.is_dir() else []:
        name = f.name[:-len(".state")]
        p = _paths(root, name)
        st, meta = _state(p)
        n = _git(root, "rev-list", "--count", "HEAD..side/" + name, check=False).stdout.strip() or "0"
        lines.append("%s  %s  %s/%s  %s commit(s)  result: %s"
                     % (name, st, meta.get("model", "?"), meta.get("effort", "?"), n,
                        "yes" if (p["wt"] / RESULT).is_file() else "no"))
    return lines or ["no side sessions"]


def take(name: str, merge: bool = False, cwd: Path | None = None) -> list[str]:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    if not p["state"].exists():
        raise SideError("no side session of that name: awb side status")
    st, _ = _state(p)
    if st == "running":
        raise SideError("the side session still runs: awb side status, or awb side stop NAME")
    out = []
    result = p["wt"] / RESULT
    if result.is_file():
        notes = root / NOTES
        notes.mkdir(exist_ok=True)
        shutil.copyfile(result, notes / ("side-%s.md" % name))
        out.append("result: %s/side-%s.md" % (NOTES, name))
    else:
        out.append("result: none (%s missing)" % RESULT)
    log = _git(root, "log", "--format=%h %s", "HEAD..side/" + name, check=False).stdout.strip()
    files = _git(root, "diff", "--name-only", "HEAD...side/" + name, check=False).stdout.split()
    out.append("commits: %d" % (len(log.splitlines()) if log else 0))
    out += ["  " + ln for ln in log.splitlines()[:20]]
    out.append("files: %s" % (", ".join(files[:30]) if files else "none"))
    if merge and files:
        r = _git(root, "merge", "--no-ff", "--no-edit", "side/" + name, check=False)
        if r.returncode != 0:
            conflict = _git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.strip()
            _git(root, "merge", "--abort", check=False)
            raise SideError("the merge of side/%s %s; nothing merged, the side stays"
                            % (name, "conflicts" if conflict else "failed (git merge)"))
        out.append("merged side/%s" % name)
    if merge:
        out.append(_drop(root, name))
    return out


def _drop(root: Path, name: str) -> str:
    p = _paths(root, name)
    _git(root, "worktree", "remove", "--force", str(p["wt"]), check=False)
    _git(root, "worktree", "prune", check=False)
    _git(root, "branch", "-D", "side/" + name, check=False)
    for k in ("state", "exit", "out", "err"):
        p[k].unlink(missing_ok=True)
    (p["base"] / (name + ".prompt")).unlink(missing_ok=True)
    return "side session %s removed" % name


def stop(name: str, cwd: Path | None = None) -> str:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    st, meta = _state(p)
    if st != "running":
        return "side session %s is %s" % (name, st)
    try:
        os.killpg(int(meta["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, KeyError, ValueError):
        pass
    if not p["exit"].exists():
        p["exit"].write_text("143\n")
    return "side session %s stopped" % name


def drop(name: str, cwd: Path | None = None) -> str:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    if _state(p)[0] == "running":
        raise SideError("the side session still runs: awb side stop NAME first")
    return _drop(root, name)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb side", description="Side sessions of a project: side work in a session of its own.")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    s = sub.add_parser("start", help="start a side session in a worktree with a brief file of the project")
    s.add_argument("name", metavar="NAME")
    s.add_argument("--brief", required=True, metavar="FILE")
    s.add_argument("--model", default=DEFAULT_MODEL, metavar="MODEL")
    s.add_argument("--effort", default=DEFAULT_EFFORT, metavar="LEVEL")
    sub.add_parser("status", help="one line per side session: state, model, commits, result")
    t = sub.add_parser("take", help="copy the result to notes/side-NAME.md; --merge merges the branch and removes it")
    t.add_argument("name", metavar="NAME")
    t.add_argument("--merge", action="store_true")
    for cmd, helptext in (("stop", "end a running side session"), ("drop", "remove a side session unused")):
        sub.add_parser(cmd, help=helptext).add_argument("name", metavar="NAME")
    args = ap.parse_args(argv)
    try:
        if args.command == "start":
            print(start(args.name, args.brief, args.model, args.effort))
        elif args.command == "status":
            print("\n".join(status_lines()))
        elif args.command == "take":
            print("\n".join(take(args.name, args.merge)))
        elif args.command == "stop":
            print(stop(args.name))
        else:
            print(drop(args.name))
    except SideError as err:
        print("awb side: %s" % err, file=sys.stderr)
        return 2
    return 0
