"""The git merge drivers of `build/tests.json` and `CHANGELOG.md` (`.gitattributes`: merge=awb-tests and
merge=awb-changelog).

    python3 -I mergedriver.py tests BASE OURS THEIRS
    python3 -I mergedriver.py changelog BASE OURS THEIRS

git gives the three versions as files (%O %A %B) and keeps OURS as the result when the driver exits 0. A refusal
leaves OURS as it is, prints one line `awb merge: ...` to standard error and exits 1, so git stops with the file in
conflict.

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

import json
import re
import sys
from pathlib import Path


class Refused(Exception):
    """The versions cannot be merged; the message names ids and keys only."""


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
        else:
            out[k] = a
    keys = list(theirs)
    for i, k in enumerate(keys):
        if k in ours or k in base:
            continue
        after = None
        for prev in reversed(keys[:i]):
            if prev in out:
                after = prev
                break
        items = list(out.items())
        at = 0 if after is None else [x for x, _ in items].index(after) + 1
        while at < len(items) and items[at][0] not in base and items[at][0] not in theirs:
            at += 1
        items.insert(at, (k, theirs[k]))
        out = dict(items)
    return out


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
            raise Refused("%s holds the id %s twice" % (label, row["id"]))
        rows[row["id"]] = row
    return rows


def merge_tests(base_text: str, ours_text: str, theirs_text: str) -> str:
    try:
        base = json.loads(base_text) if base_text.strip() else {"tests": []}
        ours = json.loads(ours_text)
        theirs = json.loads(theirs_text)
    except ValueError:
        raise Refused("a version of build/tests.json is not JSON") from None
    rb, ra, rt = _rows(base, "the base"), _rows(ours, "one side"), _rows(theirs, "the other side")

    def row(b, a, t):
        if b is None and a != t:
            raise Refused("two different rows with the id %s" % a["id"])
        return pick(b, a, t, "two different rows with the id %s" % a["id"])

    rows = merge_keyed(rb, ra, rt, row)
    out: dict = {}
    for key in list(ours) + [k for k in theirs if k not in ours]:
        if key == "tests":
            out[key] = list(rows.values())
        elif key == "total_tests":
            out[key] = len(rows)
        elif key == "checked":
            out[key] = max(str(x) for x in (ours.get(key), theirs.get(key)) if x is not None)
        elif key in ours and key in theirs:
            out[key] = pick(base.get(key), ours[key], theirs[key], "both sides changed the key %s" % key)
        elif key in ours:
            out[key] = ours[key]
        else:
            out[key] = theirs[key]
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
    for it in items:
        text = "\n".join(x.strip() for x in it if x.strip())
        n = 0
        while (text, n) in out:
            n += 1
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
    return ("week", int(m.group(2)), int(m.group(1))) if m else ("heading", heading)


def parse_changelog(text: str) -> tuple[list[str], dict]:
    """(the lines before the first week, {week key: (heading, {section heading or None: (items, tail)})})."""
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
        items = merge_keyed(b[0] if b else {}, a[0], t[0], lambda _b, x, _t: x)
        return items, a[1]

    def week(b, a, t):
        heading = t[0] if b is not None and a[0] == b[0] else a[0]
        return heading, merge_keyed(b[1] if b else {}, a[1], t[1], section)

    weeks = merge_keyed(wb, wa, wt, week)
    fresh = [k for k in weeks if k not in wa]
    if fresh and all(k[0] == "week" for k in weeks):
        weeks = dict(sorted(weeks.items(), key=lambda kv: kv[0][1:], reverse=True))
    preamble = pick(pb, pa, pt, "both sides changed the head of CHANGELOG.md")
    return render_changelog(preamble, weeks)


# --------------------------------------------------------------------------- the driver

MERGES = {"tests": merge_tests, "changelog": merge_changelog}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4 or argv[0] not in MERGES:
        print("awb merge: usage: mergedriver.py tests|changelog BASE OURS THEIRS", file=sys.stderr)
        return 2
    kind, base, ours, theirs = argv[:4]
    try:
        texts = [Path(p).read_text(encoding="utf-8") for p in (base, ours, theirs)]
        result = MERGES[kind](*texts)
    except Refused as err:
        print("awb merge: %s" % err, file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as err:
        print("awb merge: a version cannot be read (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    Path(ours).write_text(result, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
