"""Write STATS.json at the root of the repository (B-114): the numbers of "What this proves" in the README, each one
with `counted_by`, the command that gives it again.

    python3 tools/stats.py                 write STATS.json of this repository, test functions counted by grep
    python3 tools/stats.py --pytest        count the test functions with pytest --collect-only instead
    python3 tools/stats.py --repo DIR      the repository at DIR
    python3 tools/stats.py --no-build      leave the build numbers out without asking `awb build list`

The build tasks done and the branch checks passed and refused come from `awb build list` and are left out when the
repository has no awb/build.py or the command fails. A number without `counted_by` is refused: nothing is written.
Standard library only; tests/test_stats_json.py runs it on a temporary git repository.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = "STATS.json"
SINCE = "2026-09-28"                  # the day the project's history is counted from
TEST_DEF = r"^[[:space:]]*(async[[:space:]]+)?def test_"
COMMANDS_HEAD = "## Reference of every command and option"


class StatsError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if done.returncode not in (0, 1) or done.stderr.strip():
        raise StatsError("git %s failed: %s" % (" ".join(args), done.stderr.strip() or done.returncode))
    return done.stdout


def _files(root: Path, pattern: str) -> list[Path]:
    """The files git tracks that match `pattern` (a git pathspec: `*` also matches a slash)."""
    return [root / x for x in _git(root, "ls-files", "-z", "--", pattern).split("\0") if x]


def _stat(value, counted_by: str) -> dict:
    return {"value": value, "counted_by": counted_by}


def validate(stats: dict) -> None:
    """Refuse a number without `counted_by`: every field that is not text is {"value", "counted_by"}."""
    for name, field in stats.items():
        if isinstance(field, str):
            continue
        if not isinstance(field, dict) or "value" not in field:
            raise StatsError("%s has no value" % name)
        by = field.get("counted_by")
        if not isinstance(by, str) or not by.strip():
            raise StatsError("%s has no counted_by: a number without the command that counts it is refused" % name)


def _tests(root: Path, pytest: bool) -> dict:
    if pytest:
        done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                              cwd=root, capture_output=True, text=True, check=False)
        n = sum(1 for line in done.stdout.splitlines() if "::" in line)
        if done.returncode not in (0, 5):
            raise StatsError("pytest --collect-only failed: %s" % (done.stderr.strip() or done.stdout.strip())[-200:])
        return _stat(n, "python3 -m pytest --collect-only -q | grep -c '::' (a parametrized test counts once per case)")
    out = _git(root, "grep", "-hE", TEST_DEF, "--", "tests/*.py")
    return _stat(len(out.splitlines()), "git grep -hE '%s' -- 'tests/*.py' | wc -l (a def, a parametrized test "
                                        "counts once)" % TEST_DEF)


def _commands(root: Path) -> dict:
    path = root / "COMMANDS.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    ref = text.partition(COMMANDS_HEAD)[2]
    return _stat(len(re.findall(r"^### `awb ", ref, re.M)),
                 "sed -n '/^%s/,$p' COMMANDS.md | grep -c '^### `awb '" % COMMANDS_HEAD)


def build_lines(root: Path) -> list[str] | None:
    """`awb build list` of this repository, None when it has no build queue or the command fails."""
    if not (root / "awb" / "build.py").is_file():
        return None
    done = subprocess.run([sys.executable, "-m", "awb", "build", "list"], cwd=root, capture_output=True, text=True,
                          check=False)
    return done.stdout.splitlines() if done.returncode == 0 else None


def _build(lines: list[str]) -> dict:
    """Tasks done and branch checks passed and refused from the lines of `awb build list`: `ID  STATE  since ...`;
    a check has an id C-NNN and ends `a check of a branch`; a check that is `red` was refused."""
    tasks = checks = passed = refused = 0
    for line in lines:
        fields = re.split(r"\s{2,}", line.strip())
        if len(fields) < 3 or not fields[2].startswith("since "):
            continue
        if fields[0].startswith("C-"):
            checks += 1
            passed += fields[1] == "passed"
            refused += fields[1].startswith("red")
        else:
            tasks += fields[1] == "done"
    by = "awb build list | awk '$1 !~ /^C-/ && $2 == \"done\"' | wc -l"
    check = "awb build list | awk '$1 ~ /^C-/ && %s' | wc -l"
    return {"build_tasks_done": _stat(tasks, by),
            "branch_checks_passed": _stat(passed, check % "$2 == \"passed\""),
            "branch_checks_refused": _stat(refused, check % "$2 ~ /^red/")}


def collect(root: Path = ROOT, *, pytest: bool = False, since: str = SINCE, today: datetime.date | None = None,
            lines: list[str] | None = None, ask_build: bool = True) -> dict:
    """The numbers of the repository at `root`. `lines` are the lines of `awb build list` (None: ask the command,
    unless `ask_build` is False)."""
    root = Path(root)
    py = _files(root, "awb/*.py")
    stats: dict = {"repository": "tcp-awb",
                   "date": _stat((today or datetime.date.today()).isoformat(), "date -I"),
                   "commits": _stat(int(_git(root, "rev-list", "--count", "--since=" + since, "HEAD").strip() or 0),
                                    "git rev-list --count --since=%s HEAD" % since),
                   "lines_awb": _stat(sum(p.read_bytes().count(b"\n") for p in py),
                                      "git ls-files -z -- 'awb/*.py' | xargs -0 cat | wc -l (lines of the tracked "
                                      "Python files of awb/, comments and blanks included)"),
                   "modules": _stat(sum(1 for p in py if p.name != "__init__.py"),
                                    "git ls-files -- 'awb/*.py' | grep -v '/__init__.py$' | wc -l"),
                   "test_functions": _tests(root, pytest),
                   "skills": _stat(len(_files(root, "plugin/skills/*/SKILL.md")),
                                   "git ls-files -- 'plugin/skills/*/SKILL.md' | wc -l"),
                   "agents": _stat(len(_files(root, "plugin/agents/*.md")),
                                   "git ls-files -- 'plugin/agents/*.md' | wc -l"),
                   "commands": _commands(root)}
    lines = build_lines(root) if lines is None and ask_build else lines
    if lines is not None:
        stats.update(_build(lines))
    validate(stats)
    return stats


def write(root: Path = ROOT, **kw) -> Path:
    stats = collect(root, **kw)
    path = Path(root) / OUT
    path.write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/stats.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, default=ROOT, help="the repository (default: this one)")
    ap.add_argument("--pytest", action="store_true", help="count the test functions with pytest --collect-only")
    ap.add_argument("--no-build", action="store_true", help="leave out the numbers of the build queue")
    ap.add_argument("--since", default=SINCE, help="count the commits since this day (default %s)" % SINCE)
    args = ap.parse_args(argv)
    try:
        print("wrote", write(args.repo, pytest=args.pytest, since=args.since, ask_build=not args.no_build))
    except StatsError as err:
        print("refused: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
