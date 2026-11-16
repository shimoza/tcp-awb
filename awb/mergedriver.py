"""The git merge drivers of `build/tests.json` and `CHANGELOG.md` (`.gitattributes`: merge=awb-tests and
merge=awb-changelog).

    python3 -I mergedriver.py tests BASE OURS THEIRS
    python3 -I mergedriver.py changelog BASE OURS THEIRS

git gives the three versions as files (%O %A %B) and keeps OURS as the result when the driver exits 0. A refusal
leaves OURS as it is, prints one line `awb merge: ...` to standard error and exits 1, so git stops with the file in
conflict.

The invocation directory bounds all version paths. Versions must be single regular files with no links in the
path. Reads and results are limited to 8 MiB; JSON nesting is limited to 100 levels. An output inode lock refuses
overlapping runs on the same snapshot, and a complete staged result replaces OURS only after the inputs are
rechecked. Diagnostics withhold input values except short test codes of one capital letter and up to six digits.

tests: the rows are merged by id across the three versions. A row one side changed or added is taken from that side,
a row one side dropped (and the other kept as it was) is dropped, `total_tests` is the count of the result and
`checked` the later date. Two different rows with one id are refused and so is a version that holds one id twice.

changelog: each side keeps its lines in their week and their section (`### Added`, `### Changed` and so on). A line
item is the line that starts it and the indented lines below it; one side's new items come after the item they follow
on that side, a week only one side has comes in its place by order.

`awb pr take` copies this file out of the working copy before a rebase and runs it with `python3 -I`, so a branch
from outside never runs its own driver. Standard library only; it imports nothing of awb for the same reason.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import sys
import uuid
from contextlib import ExitStack
from datetime import date
from pathlib import Path


class Refused(Exception):
    """The versions cannot be merged. Only short test codes may appear in diagnostics."""


MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 100
_MISSING = object()
_ROW_DIAGNOSTIC = re.compile(r"two different rows with the id [A-Z][0-9]{1,6}")


# --------------------------------------------------------------------------- the keyed three-way merge

def merge_keyed(base: dict, ours: dict, theirs: dict, merge_value) -> dict:
    """Three ordered maps into one: the order of OURS, a key only THEIRS added comes after the key it follows in
    THEIRS and after the keys OURS added there (first when it follows none). A key one side dropped while the other left it as it was in BASE is
    dropped; a key both kept goes through merge_value(base or None, ours, theirs)."""
    out: dict = {}
    for k, a in ours.items():
        if k in theirs:
            out[k] = merge_value(base.get(k), a, theirs[k])
        elif k in base and base[k] == a:
            continue
        elif k in base:
            raise Refused("one side deleted an entry the other changed")
        else:
            out[k] = a
    # A linked order avoids rebuilding the whole map for each insertion. Each side's added block is visited
    # once, even when many thousands of new entries follow the same old entry.
    head = object()
    following = {}
    previous = head
    for k in out:
        following[previous] = k
        previous = k
    following[previous] = head
    after = head
    for k in theirs:
        if k not in ours and k in base and theirs[k] != base[k]:
            raise Refused("one side deleted an entry the other changed")
        if k in out:
            after = k
            continue
        if k in base:
            continue
        next_key = following[after]
        while next_key is not head and next_key not in base and next_key not in theirs:
            after = next_key
            next_key = following[after]
        following[after], following[k] = k, next_key
        out[k] = theirs[k]
        after = k
    ordered = {}
    k = following[head]
    while k is not head:
        ordered[k] = out[k]
        k = following[k]
    return ordered


def pick(base, ours, theirs, what: str):
    """The value one side changed; both alike is that value; both changed differently is refused."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    raise Refused(what)


# --------------------------------------------------------------------------- build/tests.json

def _rows(data, label: str) -> dict[str, dict]:
    if not isinstance(data, dict) or not isinstance(data.get("tests"), list):
        raise Refused("%s is not a tests file" % label)
    rows: dict[str, dict] = {}
    for row in data["tests"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise Refused("%s has a row without an id" % label)
        if row["id"] in rows:
            raise Refused("%s holds an id twice" % label)
        rows[row["id"]] = row
    return rows


def _json_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Refused("a JSON object holds a key twice")
        out[key] = value
    return out


def _finite_number(text):
    number = float(text)
    if not math.isfinite(number):
        raise Refused("a JSON number is not finite")
    return number


def _checked_text(text: str) -> bytes:
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        raise Refused("a version contains invalid Unicode") from None
    if len(data) > MAX_BYTES:
        raise Refused("a version is too large")
    return data


def _load_tests(text: str):
    _checked_text(text)
    try:
        data = json.loads(text, object_pairs_hook=_json_object,
                          parse_float=_finite_number, parse_constant=_finite_number)
    except (ValueError, RecursionError):
        raise Refused("a version of build/tests.json is not JSON") from None
    # Validate before comparisons and encoding, which also recurse through nested data.
    pending = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise Refused("a JSON value is nested too deeply")
        if isinstance(value, str):
            _checked_text(value)
        elif isinstance(value, dict):
            pending.extend((v, depth + 1) for pair in value.items() for v in pair)
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)
    if isinstance(data, dict) and "checked" in data:
        checked = data["checked"]
        try:
            if not isinstance(checked, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", checked):
                raise ValueError
            date.fromisoformat(checked)
        except ValueError:
            raise Refused("a checked date is invalid") from None
    return data


def merge_tests(base_text: str, ours_text: str, theirs_text: str) -> str:
    base = _load_tests(base_text) if base_text.strip() else {"tests": []}
    ours, theirs = _load_tests(ours_text), _load_tests(theirs_text)
    rb, ra, rt = _rows(base, "the base"), _rows(ours, "one side"), _rows(theirs, "the other side")

    def row(b, a, t):
        # Preserve the legacy diagnostic for short test codes (for example D1). Other IDs may contain names,
        # credentials or terminal controls, and cannot be screened against a registry by this copied driver.
        message = "two different rows with the id " + a["id"]
        if not _ROW_DIAGNOSTIC.fullmatch(message):
            message = "two different rows with one id"
        if b is None and a != t:
            raise Refused(message)
        return pick(b, a, t, message)

    rows = merge_keyed(rb, ra, rt, row)
    out: dict = {}
    for key in list(ours) + [k for k in theirs if k not in ours]:
        if key == "tests":
            out[key] = list(rows.values())
        elif key == "total_tests":
            out[key] = len(rows)
        elif key == "checked":
            out[key] = max(x for x in (ours.get(key), theirs.get(key)) if x is not None)
        else:
            value = pick(base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING),
                         "both sides changed a metadata field")
            if value is not _MISSING:
                out[key] = value
    if "total_tests" not in out:
        out["total_tests"] = len(rows)
    return json.dumps(out, indent=1, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- CHANGELOG.md

_WEEK_RE = re.compile(r"## Week ([0-9]+), ([0-9]{4})")


def _items(lines: list[str]) -> tuple[dict, list[str]]:
    """The items of a section body: {key: raw lines with the blank lines before it} and the blank lines at its end.
    A key is the item's text without the blank lines, with a count when the same text comes twice."""
    items: list[list[str]] = []
    blank: list[str] = []
    for ln in lines:
        if not ln.strip():
            blank.append(ln)
        elif items and ln[:1] in (" ", "\t"):
            items[-1].extend(blank + [ln])
            blank = []
        else:
            items.append(blank + [ln])
            blank = []
    out: dict = {}
    counts: dict[str, int] = {}
    for it in items:
        text = "\n".join(x.strip() for x in it if x.strip())
        n = counts.get(text, 0)
        counts[text] = n + 1
        out[(text, n)] = it
    return out, blank


def _split(lines: list[str], prefix: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Lines before the first heading with PREFIX, then each heading with the lines below it."""
    head: list[str] = []
    parts: list[tuple[str, list[str]]] = []
    for ln in lines:
        if ln.startswith(prefix):
            parts.append((ln, []))
        elif parts:
            parts[-1][1].append(ln)
        else:
            head.append(ln)
    return head, parts


def _week_key(heading: str):
    m = _WEEK_RE.match(heading)
    if heading.startswith("## Week ") and (not m or len(m.group(1)) > 2 or not 1 <= int(m.group(1)) <= 53):
        raise Refused("CHANGELOG.md has an invalid week number")
    return ("week", int(m.group(2)), int(m.group(1))) if m else ("heading", heading)


def parse_changelog(text: str) -> tuple[list[str], dict]:
    """(the lines before the first week, {week key: (heading, {section heading or None: (items, tail)})})."""
    _checked_text(text)
    preamble, weeks = _split(text.split("\n"), "## ")
    out: dict = {}
    for heading, body in weeks:
        key = _week_key(heading)
        if key in out:
            raise Refused("CHANGELOG.md holds one week twice")
        intro, sections = _split(body, "### ")
        secs: dict = {None: _items(intro)}
        for sh, sb in sections:
            if sh in secs:
                raise Refused("CHANGELOG.md holds one section twice in a week")
            secs[sh] = _items(sb)
        out[key] = (heading, secs)
    return preamble, out


def render_changelog(preamble: list[str], weeks: dict) -> str:
    lines = list(preamble)
    for heading, secs in weeks.values():
        lines.append(heading)
        for sh, (items, tail) in secs.items():
            if sh is not None:
                lines.append(sh)
            for it in items.values():
                lines.extend(it)
            lines.extend(tail)
    return "\n".join(lines)


def merge_changelog(base_text: str, ours_text: str, theirs_text: str) -> str:
    parsed = []
    for label, text in (("the base", base_text), ("one side", ours_text), ("the other side", theirs_text)):
        pre, weeks = parse_changelog(text)
        if render_changelog(pre, weeks) != text:
            raise Refused("the form of CHANGELOG.md on %s is not known" % label)
        parsed.append((pre, weeks))
    (pb, wb), (pa, wa), (pt, wt) = parsed

    def section(b, a, t):
        items = merge_keyed(b[0] if b else {}, a[0], t[0],
                            lambda old, x, y: pick(old, x, y, "both sides changed an item's formatting"))
        return items, pick(b[1] if b else None, a[1], t[1], "both sides changed a section's spacing")

    def week(b, a, t):
        heading = pick(b[0] if b else None, a[0], t[0], "both sides changed a week heading")
        return heading, merge_keyed(b[1] if b else {}, a[1], t[1], section)

    weeks = merge_keyed(wb, wa, wt, week)
    fresh = [k for k in weeks if k not in wa]
    if fresh and all(k[0] == "week" for k in weeks):
        weeks = dict(sorted(weeks.items(), key=lambda kv: kv[0][1:], reverse=True))
    preamble = pick(pb, pa, pt, "both sides changed the head of CHANGELOG.md")
    return render_changelog(preamble, weeks)


# --------------------------------------------------------------------------- the driver

ATTRIBUTES = "build/tests.json merge=awb-tests\nCHANGELOG.md merge=awb-changelog\n"
"""The two lines of `.gitattributes`, written to a file outside the branch and named by core.attributesFile."""


def git_config(driver: str, attributes: str, python: str) -> list[str]:
    """The `-c` arguments that make git merge the two files with the copy of this file at DRIVER, run by PYTHON with
    -I, and read the attributes from the file ATTRIBUTES instead of the branch's own `.gitattributes`."""
    run = "%s -I %s" % (_quote(python), _quote(driver))
    return ["-c", "core.attributesFile=%s" % attributes,
            "-c", "merge.awb-tests.name=build/tests.json by id",
            "-c", "merge.awb-tests.driver=%s tests %%O %%A %%B" % run,
            "-c", "merge.awb-changelog.name=CHANGELOG.md by week",
            "-c", "merge.awb-changelog.driver=%s changelog %%O %%A %%B" % run]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"

MERGES = {"tests": merge_tests, "changelog": merge_changelog}


def _signature(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _open_version(path: str, root: Path, root_fd: int, stack: ExitStack):
    """Resolve below the invocation directory using pinned, unlinked directory descriptors.

    Git supplies temporary files in its working directory (also for merge-tree in a bare repository).
    Absolute paths below that directory are allowed; links, traversal and multiply linked files are not.
    """
    relative = Path(path)
    if relative.is_absolute():
        try:
            relative = relative.relative_to(root)
        except ValueError:
            raise Refused("a version is outside the working directory") from None
    if not relative.parts or ".." in relative.parts:
        raise Refused("a version path is invalid")
    parent = root_fd
    for part in relative.parts[:-1]:
        parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, parent)
    name = relative.name
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    stack.callback(os.close, fd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise Refused("a version is not a single regular file")
    return parent, name, fd, _signature(st)


def _unchanged(version):
    parent, name, fd, signature = version
    if (_signature(os.fstat(fd)) != signature
            or _signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) != signature):
        raise Refused("a version changed during the merge")


def _read_version(version):
    fd = version[2]
    if os.fstat(fd).st_size > MAX_BYTES:
        raise Refused("a version is too large")
    data = bytearray()
    while True:
        block = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
        if not block:
            break
        data.extend(block)
        if len(data) > MAX_BYTES:
            raise Refused("a version is too large")
    _unchanged(version)
    # Preserve read_text's universal newline handling.
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _publish(versions, data: bytes):
    """Write completely before replacing OURS. Every operation stays relative to its pinned parent.

    The caller holds the output inode lock until publication. Cooperating drivers cannot use a stale snapshot;
    a fresh opener after replacement sees the completed result. Recheck all inputs for observed external edits.
    """
    parent, name, _, signature = versions[1]
    stage = ".awb-merge-" + uuid.uuid4().hex
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    published = False
    try:
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("incomplete write")
                remaining = remaining[written:]
            os.fchmod(fd, stat.S_IMODE(signature[2]) & 0o777)
            os.fsync(fd)
        finally:
            os.close(fd)
        for version in versions:
            _unchanged(version)
        os.replace(stage, name, src_dir_fd=parent, dst_dir_fd=parent)
        published = True
    finally:
        if not published:
            try:
                os.unlink(stage, dir_fd=parent)
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4 or argv[0] not in MERGES:
        print("awb merge: usage: mergedriver.py tests|changelog BASE OURS THEIRS", file=sys.stderr)
        return 2
    kind, base, ours, theirs = argv[:4]
    try:
        with ExitStack() as stack:
            root = Path.cwd()
            root_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, root_fd)
            versions = [_open_version(p, root, root_fd, stack) for p in (base, ours, theirs)]
            fcntl.flock(versions[1][2], fcntl.LOCK_EX | fcntl.LOCK_NB)
            texts = [_read_version(v) for v in versions]
            result = _checked_text(MERGES[kind](*texts))
            _publish(versions, result)
    except Refused as err:
        # Do not trust an exception's string conversion, including Refused instances from a dependency.
        message = err.args[0] if len(err.args) == 1 and type(err.args[0]) is str else ""
        if not _ROW_DIAGNOSTIC.fullmatch(message):
            message = "versions cannot be safely merged or written"
        print("awb merge: " + message, file=sys.stderr)
        return 1
    except Exception:
        # Parser, filesystem and dependency errors may all embed paths or document values. This standalone
        # process has no trusted name registry; emit a fixed diagnostic even for unexpected exceptions.
        print("awb merge: versions cannot be safely merged or written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
