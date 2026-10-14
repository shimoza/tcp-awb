"""`awb pr take N`: take a pull request of a cloud session into main in one command (owner side).

    awb pr take N              fetch, rebase onto main, `awb build check`, merge, push, close
    awb pr take N --dry-run    the steps it would run, nothing changed

Run by the owner in his working copy, on main with a clean tree:

1. `git fetch origin pull/N/head` into the branch `take/pr-N`. Refused when the tree has a change, when the working
   copy is not on main or when origin's main or the build repository's main is not in the local main.
2. The branch is rebased onto the local main in a temporary worktree outside the working copy, so his tree never
   holds the branch's files and no git hook runs. A conflict in `build/tests.json` is merged by id and one in
   `CHANGELOG.md` keeps both sides' lines in their week (awb/mergedriver.py, copied out first and run with
   `python3 -I`, so the branch never runs its own driver). Any other conflict stops the take with the file names.
3. `awb build check take/pr-N` on the rebased branch (the gate and the whole suite as the build user) and its lines.
4. Only when the check passed: `git merge --no-ff` into main with "Merge pull request #N: TITLE" (the title from
   `gh pr view N --json title` when gh is there, else the branch name on origin; the gate checks the message), then
   `git push origin main`, `git push <build root>/main.git main` and, when gh is there, `gh pr close N` with a
   comment that names the merge commit (a rebased pull request does not show as merged).
5. On any failure main is as it was and the take prints what happened. It never deploys.

Exit codes: 0 taken (or the dry run), 1 a step failed, 2 refused before anything changed.
"""
from __future__ import annotations

import argparse
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from awb import mergedriver

MAIN = "main"
ORIGIN = "origin"
BRANCH = "take/pr-%d"
TITLE_MAX = 200
_SAFE_TITLE_RE = re.compile(r"[\x00-\x1f\x7f]")
_ATTRIBUTES = "build/tests.json merge=awb-tests\nCHANGELOG.md merge=awb-changelog\n"


class Stop(Exception):
    """A step failed; the message says what happened. `code` is the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _check(ref: str) -> int:
    """`awb build check REF`, the runner's public function; it prints the state line and the check lines."""
    from awb import buildrun

    return buildrun.check_main(ref)


def _build_repo() -> str:
    """main.git of the self-build: `build_root` of the host file (AWB_BUILD_ROOT in tests), /srv/awb-build else."""
    from awb import buildrun

    return str(buildrun.build_root() / "main.git")


def _gate_message(path: Path) -> int:
    """`awb gate --message FILE` with the vault register: 0 clean, 1 findings, 2 it cannot run."""
    from awb import gate

    return gate.main(["--message", str(path)])


@dataclass
class Take:
    number: int
    repo: Path
    check: Callable[[str], int] = _check
    build_repo: Callable[[], str] = _build_repo
    gate_message: Callable[[Path], int] = _gate_message
    gh: str | None = field(default_factory=lambda: shutil.which("gh"))
    out: Callable[[str], None] = print
    fetched: str | None = None

    @property
    def branch(self) -> str:
        return BRANCH % self.number

    # ----------------------------------------------------------------------- git

    def git(self, *args: str, cwd: Path | None = None, check: bool = True,
            env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", str(cwd or self.repo), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LANG="C", **(env or {})))
        if check and r.returncode != 0:
            raise Stop("git %s failed: %s" % (args[0], _last(r.stderr) or "exit %d" % r.returncode))
        return r

    def rev(self, ref: str) -> str | None:
        r = self.git("rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def contains(self, older: str, newer: str) -> bool:
        return self.git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    # ----------------------------------------------------------------------- the steps

    def steps(self) -> list[str]:
        n, b = self.number, self.branch
        return [
            "1. refuse a tree with a change or a working copy not on %s; git fetch %s %s and the main of the build "
            "repository: both must be in the local %s" % (MAIN, ORIGIN, MAIN, MAIN),
            "   git fetch %s +pull/%d/head:refs/heads/%s" % (ORIGIN, n, b),
            "2. git rebase %s in a temporary worktree of %s (build/tests.json by id, CHANGELOG.md by week, any other "
            "conflict stops)" % (MAIN, b),
            "3. awb build check %s" % b,
            "4. only when the check passed: git merge --no-ff %s -m \"Merge pull request #%d: TITLE\" (the title %s)"
            % (b, n, "from gh pr view %d --json title" % n if self.gh else "is the branch name, gh is not there"),
            "   git push %s %s" % (ORIGIN, MAIN),
            "   git push %s %s" % (self.build_repo(), MAIN),
            ("   gh pr close %d --comment \"Merged into main as COMMIT ...\"" % n) if self.gh
            else "   the pull request stays open on GitHub (gh is not there): close it there",
            "5. on a failure main stays as it is; nothing is deployed",
        ]

    def preflight(self) -> tuple[str, str]:
        """Step 1 before the fetch of the pull request: the tree, the branch, origin and the build repository.
        Returns (the local main, the build repository)."""
        if self.git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise Stop("the working tree has a change: commit or stash it first", 2)
        head = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        if head != MAIN:
            raise Stop("the working copy is not on %s: git switch %s first" % (MAIN, MAIN), 2)
        main = self.rev(MAIN)
        if main is None:
            raise Stop("there is no %s" % MAIN, 2)
        self.git("fetch", "--quiet", "--no-tags", ORIGIN, "+refs/heads/%s:refs/remotes/%s/%s" % (MAIN, ORIGIN, MAIN))
        if not self.contains("refs/remotes/%s/%s" % (ORIGIN, MAIN), main):
            raise Stop("%s of %s is not in your %s: git pull %s %s first" % (MAIN, ORIGIN, MAIN, ORIGIN, MAIN), 2)
        build = self.build_repo()
        r = self.git("ls-remote", "--heads", build, MAIN, check=False)
        if r.returncode != 0:
            raise Stop("the build repository %s cannot be read" % build, 2)
        theirs = r.stdout.split()[0] if r.stdout.split() else None
        if theirs and not (self.rev(theirs) and self.contains(theirs, main)):
            raise Stop("%s of the build repository is not in your %s: git pull %s %s first"
                       % (MAIN, MAIN, build, MAIN), 2)
        return main, build

    def fetch(self) -> str:
        self.git("fetch", "--quiet", "--no-tags", ORIGIN, "+pull/%d/head:refs/heads/%s" % (self.number, self.branch))
        head = self.rev(self.branch)
        if head is None:
            raise Stop("pull request #%d has no head on %s" % (self.number, ORIGIN))
        self.fetched = head
        self.out("fetched pull request #%d at %s into %s" % (self.number, head[:7], self.branch))
        return head

    def rebase(self, scratch: Path) -> str:
        """The branch onto main in a worktree under SCRATCH, with the drivers copied there. The new head."""
        driver = scratch / "mergedriver.py"
        driver.write_text(inspect.getsource(mergedriver), encoding="utf-8")
        attributes = scratch / "attributes"
        attributes.write_text(_ATTRIBUTES, encoding="utf-8")
        wt = scratch / "wt"
        self.git("worktree", "add", "--quiet", str(wt), self.branch)
        run = "%s -I %s" % (_quote(sys.executable), _quote(str(driver)))
        r = self.git("-c", "core.attributesFile=%s" % attributes,
                     "-c", "merge.awb-tests.name=build/tests.json by id",
                     "-c", "merge.awb-tests.driver=%s tests %%O %%A %%B" % run,
                     "-c", "merge.awb-changelog.name=CHANGELOG.md by week",
                     "-c", "merge.awb-changelog.driver=%s changelog %%O %%A %%B" % run,
                     "-c", "rebase.autoStash=false", "-c", "commit.gpgSign=false",
                     "rebase", "--quiet", MAIN, cwd=wt, check=False)
        if r.returncode != 0:
            files = self.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).stdout.split()
            why = [ln for ln in r.stderr.splitlines() if ln.startswith("awb merge:")]
            self.git("rebase", "--abort", cwd=wt, check=False)
            for ln in why:
                self.out(ln)
            if files:
                raise Stop("the rebase onto %s stopped on a conflict in %s" % (MAIN, ", ".join(files)))
            raise Stop("the rebase onto %s failed: %s" % (MAIN, _last(r.stderr) or "exit %d" % r.returncode))
        head = self.rev(self.branch)
        self.out("rebased %s onto %s: %s" % (self.branch, MAIN, head[:7]))
        return head

    def title(self) -> str:
        """The pull request's title from gh, else the branch name on origin, else pull/N/head."""
        if self.gh:
            r = subprocess.run([self.gh, "pr", "view", str(self.number), "--json", "title", "--jq", ".title"],
                               cwd=self.repo, capture_output=True, text=True, timeout=60)
            t = _SAFE_TITLE_RE.sub(" ", r.stdout).strip()[:TITLE_MAX]
            if r.returncode == 0 and t:
                return t
            self.out("gh pr view failed; the branch name stands for the title")
        head = self.fetched or ""
        r = self.git("ls-remote", "--heads", ORIGIN, check=False)
        for ln in r.stdout.splitlines():
            sha, _, ref = ln.partition("\t")
            if sha == head and ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
        return "pull/%d/head" % self.number

    def message(self, scratch: Path) -> str:
        msg = "Merge pull request #%d: %s" % (self.number, self.title())
        path = scratch / "message"
        path.write_text(msg + "\n", encoding="utf-8")
        code = self.gate_message(path)
        if code != 0:
            raise Stop("the gate %s the merge message" % ("refused" if code == 1 else "could not check"))
        return msg

    def merge(self, main: str, msg: str) -> str:
        if self.rev(MAIN) != main:
            raise Stop("%s moved during the take" % MAIN)
        r = self.git("-c", "commit.gpgSign=false", "merge", "--no-ff", "--quiet", "-m", msg, self.branch,
                     check=False)
        if r.returncode != 0:
            self.git("merge", "--abort", check=False)
            self.git("reset", "--quiet", "--hard", main, check=False)
            raise Stop("git merge failed: %s" % (_last(r.stderr) or "exit %d" % r.returncode))
        merged = self.rev(MAIN)
        self.out("merged into %s: %s %s" % (MAIN, merged[:7], msg))
        return merged

    def push(self, main: str, merged: str, build: str) -> None:
        r = self.git("push", "--quiet", ORIGIN, MAIN, check=False)
        if r.returncode != 0:
            self.git("reset", "--quiet", "--hard", main, check=False)
            raise Stop("git push %s %s failed (%s); %s is back at %s, nothing was pushed"
                       % (ORIGIN, MAIN, _last(r.stderr) or "exit %d" % r.returncode, MAIN, main[:7]))
        self.out("pushed %s to %s" % (MAIN, ORIGIN))
        r = self.git("push", "--quiet", build, MAIN, check=False)
        if r.returncode != 0:
            raise Stop("git push %s %s failed (%s); %s is on %s at %s: push it there by hand"
                       % (build, MAIN, _last(r.stderr) or "exit %d" % r.returncode, MAIN, ORIGIN, merged[:7]))
        self.out("pushed %s to %s" % (MAIN, build))

    def close(self, merged: str) -> None:
        if not self.gh:
            self.out("gh is not there: close pull request #%d on GitHub, merged as %s" % (self.number, merged[:7]))
            return
        comment = ("Merged into main as %s: rebased onto main, checked with awb build check and merged with --no-ff. "
                   "A rebased pull request does not show as merged, so it is closed here." % merged)
        r = subprocess.run([self.gh, "pr", "close", str(self.number), "--comment", comment], cwd=self.repo,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise Stop("gh pr close %d failed; main is merged and pushed as %s: close it on GitHub"
                       % (self.number, merged[:7]))
        self.out("closed pull request #%d with a comment naming %s" % (self.number, merged[:7]))

    def run(self) -> int:
        try:
            main, build = self.preflight()
            self.fetch()
            with tempfile.TemporaryDirectory(prefix="awb-pr-take-") as tmp:
                scratch = Path(tmp)
                try:
                    self.rebase(scratch)
                finally:
                    self.git("worktree", "remove", "--force", str(scratch / "wt"), check=False)
                    self.git("worktree", "prune", check=False)
                self.out("awb build check %s" % self.branch)
                code = self.check(self.branch)
                if code != 0:
                    raise Stop("the check did not pass (exit %d): %s is as it was, %s is kept"
                               % (code, MAIN, self.branch))
                msg = self.message(scratch)
            merged = self.merge(main, msg)
            self.push(main, merged, build)
            self.close(merged)
            self.git("branch", "--quiet", "-D", self.branch, check=False)
            self.out("pull request #%d taken as %s; nothing was deployed" % (self.number, merged[:7]))
            return 0
        except Stop as err:
            self.out("awb pr take: %s" % err)
            return err.code
        except (OSError, subprocess.SubprocessError) as err:
            self.out("awb pr take: stopped (%s); %s is as it was unless a line above says it moved"
                     % (type(err).__name__, MAIN))
            return 1


def _last(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _toplevel() -> Path | None:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def main(argv: list[str] | None = None) -> int:
    from awb import config
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb pr", description="Take a pull request of a cloud session into main (owner side).")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("take", help="fetch, rebase onto main, awb build check, merge --no-ff, push and close")
    s.add_argument("number", type=int)
    s.add_argument("--dry-run", action="store_true", help="print the steps, change nothing")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    if args.number < 1:
        print("awb pr: N is the number of a pull request", file=sys.stderr)
        return 2
    if config.is_work_user() or config.is_build_user():
        print("awb pr: taking a pull request is the owner's", file=sys.stderr)
        return 2
    top = _toplevel()
    if top is None:
        print("awb pr: run it in your working copy", file=sys.stderr)
        return 2
    take = Take(args.number, top)
    if args.dry_run:
        for ln in take.steps():
            print(ln)
        return 0
    return take.run()
