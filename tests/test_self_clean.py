"""The repository scans clean with its own gate. The tests use invented names only.

The file list is what git would commit: tracked files plus untracked files that are not ignored. Every file
is scanned with `gate.scan_files` against the fixture register. The only findings allowed are of class
'name' in tests/fixtures.py and tests/test_*.py, where the invented names are planted on purpose.

A second check reads every capitalised two-word run in tests/*.py. Each of its words must come from the
fixture names, from rules/stop-words.txt or from the short list of ordinary words kept below.
Failure messages carry file, line and class, never the text that was found.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awb import gate
from tests import fixtures as fx

REPO = Path(__file__).resolve().parent.parent
NAME_ALLOWED = re.compile(r"tests/(?:fixtures|test_\w+)\.py")

# Documentation that shows no fixture name. It is scanned like any other file and must be clean in every class
# (test_documentation_scans_clean).
DOCS = ("CLAUDE.md", "INTERFACES.md", "README.md", "COMMANDS.md")
DOCS_PENDING: dict[str, set[str]] = {}

# ordinary German words and technical words that stand next to each other in test texts, plus two nonsense words
ORDINARY_WORDS = frozenset((
    "Abcde", "Fghij", "Auftrag", "Dieser", "Vertrag", "Guten", "Tag", "Linke", "Rechte", "Spalte", "Zeile",
    "Region", "Nord", "Standort", "Technisches", "Konzept", "Macintosh", "Word",
))

CAP_RUN = re.compile(r"(?<![\w-])[A-ZÄÖÜ][a-zäöüß]+(?:[ \t]+[A-ZÄÖÜ][a-zäöüß]+)+(?![\w-])")


def repo_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return sorted({f for f in out.split("\0") if f and (REPO / f).is_file()})


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    reg = tmp_path_factory.mktemp("self-clean") / "register.tsv"
    reg.write_text("\n".join(fx.register_lines()) + "\n", encoding="utf-8")
    files = repo_files()
    found = gate.scan_files([REPO / f for f in files], reg)
    return files, [(Path(f.file).relative_to(REPO).as_posix(), f.line, f.cls) for f in found]


def test_the_scan_covers_the_repository_and_can_fail(scan):
    files, findings = scan
    for must in ("awb/gate.py", "awb/intake.py", "awb/cli.py", "tests/fixtures.py", "tests/test_self_clean.py",
                 "hooks/pre-commit", "CLAUDE.md"):
        assert must in files, "%s is not in the scanned file list" % must
    # the planted fixture names are found: the scan is not a stub that always passes
    fixture_lines = {line for f, line, cls in findings if f == "tests/fixtures.py" and cls == "name"}
    assert len(fixture_lines) >= 8


def test_repository_scans_clean(scan):
    _, findings = scan
    bad = []
    for f, line, cls in findings:
        if cls == "name" and NAME_ALLOWED.fullmatch(f):
            continue
        if cls in DOCS_PENDING.get(f, ()):
            continue
        bad.append("%s  %s:%d" % (cls, f, line))
    assert not bad, "gate findings:\n" + "\n".join(bad)


def test_documentation_scans_clean(scan):
    files, findings = scan
    for d in DOCS:
        assert d in files, "%s is not in the scanned file list" % d
    bad = ["%s  %s:%d" % (cls, f, line) for f, line, cls in findings if f in DOCS or f.startswith("rules/")]
    assert not bad, "gate findings:\n" + "\n".join(bad)


def test_no_design_documents_in_the_repository(scan):
    files, _ = scan
    assert not [f for f in files if f.startswith("design/")], "design documents belong outside the repository"


def _stop_words() -> set[str]:
    words: set[str] = set()
    for line in (REPO / "rules" / "stop-words.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            words.update(line.split())
    return words


def _fixture_words() -> set[str]:
    strings = (fx.ALL_REGISTERED + fx.KEEP_WORDS + fx.register_lines()
               + [fx.CONTROL_UNREGISTERED, fx.PLANTED_CANDIDATE, fx.PLANTED_PERSON, fx.SELFTEST_FORM])
    return {w for s in strings for w in re.findall(r"[^\W\d_]+", s)}


def test_capitalised_runs_in_tests_come_from_fixtures():
    allowed = _fixture_words() | _stop_words() | ORDINARY_WORDS
    bad = []
    seen = 0
    for path in sorted((REPO / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in CAP_RUN.finditer(text):
            seen += 1
            if any(w not in allowed for w in m.group(0).split()):
                line = text.count("\n", 0, m.start()) + 1
                bad.append("tests/%s:%d" % (path.name, line))
    assert seen > 0, "no capitalised run found at all: the check reads nothing"
    assert not bad, "capitalised runs with words that are not from the fixtures:\n" + "\n".join(bad)


def test_the_run_check_would_catch_an_unlisted_name():
    allowed = _fixture_words() | _stop_words() | ORDINARY_WORDS
    probe = "the report of %s was sent" % " ".join(("Bl" + "orvx", "Tamq" + "ueth"))
    runs = [m.group(0) for m in CAP_RUN.finditer(probe)]
    assert runs and any(w not in allowed for w in runs[0].split())
    assert all(w in allowed for w in fx.PLANTED_CANDIDATE.split())
