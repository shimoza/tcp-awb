"""`awb build draft FINDING`: a build task file from a row of a findings table, so the steering session does not
write tasks by hand (automation review A4).

    awb build draft FINDING [--from FILE] [--id B-NNN] [--out FILE]

FINDING names a row of a findings table in a markdown file (default docs/review-2026-10-13.md, --from another one
such as docs/strategy/F2-threat-model-2026-10-13.md). A findings table has a header line with an id column (`ID` or
`#`) and a summary column (`Finding`, `Summary` or `gap`); `Severity` (or `impact`) and `Place` are read when there.
The section of the finding is the heading below the table that starts with its id, up to the next heading of the
same or a higher level.

The draft is the block, a line `---` and the prompt, the form `awb build add` takes, from a fixed skeleton:

- the title from the summary and the id of the finding;
- `paths` the files of the place column that exist, the test file of each module (tests/test_<module>.py when it
  exists) and the document that marks the finding fixed, never a file every task may change anyway;
- `check` the test files of the modules, `none` when there is none;
- `tests_replaced: none` and above it comment lines (`# ...`, which `awb build add` skips) with the rows of
  build/tests.json whose `run` names a test file of `paths`: the steering session names the ones the fix replaces
  before it adds the task;
- the prompt: the finding's row and section, the defaults and the rules of a fix.

It runs in the root of the working copy, writes to standard output or --out FILE (never over a file) and never adds
to the queue itself. The id defaults to the number after the highest of the queue and the runner's records.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from awb import build

DEFAULT_FROM = "docs/review-2026-10-13.md"
TESTS_JSON = "build/tests.json"
ID_COLUMNS = ("id", "#")
SUMMARY_COLUMNS = ("finding", "summary", "gap")
SEVERITY_COLUMNS = ("severity", "impact")
PLACE_COLUMNS = ("place",)

_SEP_RE = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*")
_HEADING_RE = re.compile(r"(#{1,6})\s+(.*?)\s*")
_PLACE_TOKEN_RE = re.compile(r"`([^`]+)`|([A-Za-z0-9_][A-Za-z0-9_./-]*)")
_LINE_SUFFIX_RE = re.compile(r":[0-9][0-9,]*$")


class DraftError(Exception):
    """The draft cannot be made: the message names the file or the finding, never other content."""


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    place: str
    summary: str
    row: str            # the row as the table has it
    section: str        # the section below the table, empty when there is none
    document: str       # the file, relative to the root


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    low = [h.lower() for h in header]
    for n in names:
        if n in low:
            return low.index(n)
    return None


def _plain(text: str) -> str:
    return text.replace("`", "").strip()


def find(text: str, fid: str, document: str) -> Finding:
    """The finding `fid` of a findings table in `text` and its section. Raises DraftError."""
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if not lines[i].lstrip().startswith("|") or not _SEP_RE.fullmatch(lines[i + 1]):
            continue
        header = _cells(lines[i])
        cid, csum = _column(header, ID_COLUMNS), _column(header, SUMMARY_COLUMNS)
        if cid is None or csum is None:
            continue
        csev, cplace = _column(header, SEVERITY_COLUMNS), _column(header, PLACE_COLUMNS)
        j = i + 2
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            cells = _cells(lines[j])
            if len(cells) == len(header) and _plain(cells[cid]) == fid:
                get = lambda c: cells[c] if c is not None else ""
                return Finding(fid, _plain(get(csev)), get(cplace), _plain(cells[csum]), lines[j].strip(),
                               section(lines[j + 1:], fid), document)
            j += 1
    raise DraftError("%s names no row %s in a findings table" % (document, fid))


def section(lines: list[str], fid: str) -> str:
    """The section whose heading starts with `fid` (`### E1, high: ...`) up to the next heading of the same or a
    higher level, without trailing empty lines; empty when there is none."""
    head = re.compile(r"(#{1,6})\s+`?%s`?(?![A-Za-z0-9_-])" % re.escape(fid))
    for i, ln in enumerate(lines):
        m = head.match(ln)
        if not m:
            continue
        level = len(m.group(1))
        out = [ln.rstrip()]
        for nxt in lines[i + 1:]:
            hm = _HEADING_RE.fullmatch(nxt)
            if hm and len(hm.group(1)) <= level:
                break
            out.append(nxt.rstrip())
        return "\n".join(out).strip("\n")
    return ""


def place_paths(place: str, root: Path) -> list[str]:
    """The files and folders of the tree that the place column names (`awb/obs.py:104`, `tests/conftest.py:298,314`),
    in their order, each once; a word that is no path of the tree is left out."""
    out: list[str] = []
    for m in _PLACE_TOKEN_RE.finditer(place):
        token = _LINE_SUFFIX_RE.sub("", (m.group(1) or m.group(2)).strip())
        if "/" not in token or not build._plain_path(token) or token in out:
            continue
        if (root / token).exists():
            out.append(token.rstrip("/") + ("/" if (root / token).is_dir() else ""))
    return out


def module_tests(paths: list[str], root: Path) -> list[str]:
    """The test file beside each module of `paths`: tests/test_<module>.py when it exists; a test file itself."""
    out: list[str] = []
    for p in paths:
        name = Path(p).name
        if p.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
            cand = p
        elif name.endswith(".py"):
            cand = "tests/test_%s.py" % name[:-3]
        else:
            continue
        if cand not in out and (root / cand).is_file():
            out.append(cand)
    return out


def rows_of(tests: list[str], root: Path) -> list[tuple[str, str]]:
    """(id, run) of each row of build/tests.json whose `run` names one of `tests`."""
    try:
        data = json.loads((root / TESTS_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DraftError("%s cannot be read; run awb build draft in the root of the working copy" % TESTS_JSON) \
            from None
    out = []
    for row in data.get("tests", []):
        run = str(row.get("run", ""))
        names = {w.split("::")[0] for w in run.split()}
        if names & set(tests):
            out.append((str(row.get("id", "")), run))
    return out


def next_id(qdir: Path | None = None, runs: Path | None = None) -> str:
    """The number after the highest task id of QUEUE.md and of the runner's records, read where they can be."""
    nums = [0]
    try:
        text = (Path(qdir or build.queue_dir()) / build.QUEUE_FILE).read_text(encoding="utf-8", errors="replace")
        nums += [int(n) for n in re.findall(r"(?m)^## B-([0-9]{3,4}):", text)]
    except OSError:
        pass
    try:
        nums += [int(t[2:]) for t in build.ids_with_runs(runs)]
    except OSError:
        pass
    return "B-%03d" % (max(nums) + 1)


def title(f: Finding) -> str:
    """The summary and the finding's id, cut at a word to the length a title may have."""
    tail = " (%s)" % f.id
    text = re.sub(r"[\x00-\x1f\x7f`]", "", " ".join(f.summary.split()))
    room = build.TITLE_MAX - len(tail)
    if len(text) > room:
        text = text[:room].rsplit(" ", 1)[0].rstrip(" ,;:")
    return text + tail


PROMPT = """\
Fix the finding {fid} of {document}.

The row of its table:

{row}

{section_text}

Defaults, do not ask: where the task leaves a choice take the default named here or else the smallest change and name the choice in your result.

- The fix goes where the place column of the row points; the fix the section names is the default.
- Where the section names an alternative, take the first one it names.
- Change no file outside `paths` and the files every task may change.

Rules:

- Write the tests first and run them: each must fail before the fix and pass after it. Name in your result the
  tests that failed before.
- Mark the finding fixed in its document with the commit: the state of its row and its heading read
  `fixed, <short hash of the fix commit>`, in a second commit that changes only the document. Move the section
  from the open part to the fixed part where the document has those parts. The document:
  {document}
- Add a line for the fix to this week's entry in CHANGELOG.md and fix the README and COMMANDS.md where they describe
  the part.
- Add a row to build/tests.json for each new test, status passing, with the ids {rowid}-1, {rowid}-2 and so on.
  Never renumber or drop another row and never change a test outside `tests_replaced`.
- Capitalised word runs in test files come from tests/fixtures.py; a new invented name goes there first.
- Commit as the owner, the identity of this clone, with no co-author line and no "generated with" line.
"""


def draft(f: Finding, root: Path, tid: str) -> str:
    """The add file of the finding: the block, comment lines with the rows a fix may replace, `---`, the prompt."""
    if not build.ID_RE.fullmatch(tid):
        raise DraftError("the id is not B-NNN")
    places = place_paths(f.place, root)
    tests = module_tests(places, root)
    paths = [p for p in [*places, *tests, f.document] if p not in build.ALWAYS_ALLOWED]
    paths = list(dict.fromkeys(paths))
    rows = rows_of(tests, root)
    head = ["## %s: %s" % (tid, title(f))]
    head += ["model: %s" % build.DEFAULTS["model"], "effort: %s" % build.DEFAULTS["effort"], "depends: none",
             "paths: %s" % " ".join(paths)]
    if rows:
        head += ["# tests_replaced: the rows of %s whose run names a test file of paths. Name the ones the fix"
                 % TESTS_JSON, "# replaces (their ids) before awb build add; these lines are skipped."]
        head += ["# %s  %s" % (rid, run) for rid, run in rows]
    else:
        head += ["# tests_replaced: no row of %s names a test file of paths." % TESTS_JSON]
    head += ["tests_replaced: none", "check: %s" % (" ".join(tests) or "none"),
             "budget: %s" % build.DEFAULTS["budget"], "clock: %s" % build.DEFAULTS["clock"]]
    section_text = f.section or "The document has no section for %s below the table; the row is all there is." % f.id
    prompt = PROMPT.format(fid=f.id, document=f.document, row=f.row, section_text=section_text,
                           rowid=tid.replace("-", ""))
    return "\n".join(head) + "\n---\n" + prompt


def make(fid: str, source: str | None = None, root: Path | None = None, tid: str | None = None,
         qdir: Path | None = None, runs: Path | None = None) -> str:
    """The draft of the finding `fid` of `source` (relative to `root`, default the current folder)."""
    root = Path(root or Path.cwd())
    source = source or DEFAULT_FROM
    path = Path(source) if Path(source).is_absolute() else root / source
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise DraftError("%s is not a file of the working copy" % source) from None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise DraftError("%s cannot be read" % rel) from None
    return draft(find(text, fid, rel), root, tid or next_id(qdir, runs))


def cmd_draft(fid: str, source: str | None, out: str | None, tid: str | None) -> int:
    try:
        text = make(fid, source, tid=tid)
        if out is None:
            sys.stdout.write(text)
            return 0
        with open(out, "x", encoding="utf-8") as fh:
            fh.write(text)
    except DraftError as err:
        print("awb build: draft refused: %s" % err, file=sys.stderr)
        return 2
    except FileExistsError:
        print("awb build: draft refused: %s exists; name another file" % out, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb build: the draft could not be written (%s)" % (err.strerror or "error").lower(), file=sys.stderr)
        return 2
    print("awb build: draft written to %s; read it, name tests_replaced, then awb build add %s" % (out, out))
    return 0
