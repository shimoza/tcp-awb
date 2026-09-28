"""The two intake reports.

The public report goes to the shared side next to the sanitised outputs. It carries the date, the customer
code and, per file id, the kind, the state, the counts of replaced items per class and the notes of the
readers. It never carries an original file name, a matched value, a token list or a candidate. It only says
how many candidates were reviewed.

The private report goes to the vault, file mode 600. It carries everything: original file names, member and
attachment names, reader metadata, every matched value with its token and positions, the candidates and every
dropped item with its reason. A working session never reads it.

This module only renders and writes. `awb.intake` decides what goes in.
"""
from __future__ import annotations

import os
import secrets
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_NAME = "intake-report.md"
"""File name of the public report inside `<shared>/outbox/<CUST>/`."""

MAX_POSITIONS = 5
"""How many positions of one value per part the private report lists."""


@dataclass
class Part:
    """One extraction: the file itself (part id = file id) or a member or attachment (F-ABCD.1, F-ABCD.1.2)."""

    part_id: str
    label: str                      # original file name, member name or attachment name: private only
    kind: str
    state: str
    notes: list[str] = field(default_factory=list)          # as the reader wrote them: private
    public_notes: list[str] = field(default_factory=list)   # the same notes after sanitising: public
    meta: dict = field(default_factory=dict)                # reader metadata: private
    counts: Counter = field(default_factory=Counter)        # class -> items replaced in the output


@dataclass
class FileRecord:
    """One file given to the intake."""

    file_id: str
    original_name: str              # private only
    kind: str
    state: str
    parts: list[Part] = field(default_factory=list)          # parts[0] is the file itself
    output: Path | None = None
    original: Path | None = None    # where the original went in the vault, None when it stayed
    reasons: list[str] = field(default_factory=list)         # dropped items and why: private

    def counts(self) -> Counter:
        total: Counter = Counter()
        for part in self.parts:
            total.update(part.counts)
        return total


@dataclass
class Hit:
    """One distinct value found by detection, with the token that stands for it. Private only."""

    token: str
    cls: str
    value: str
    where: dict = field(default_factory=dict)   # "F-ABCD.1 text" -> list of positions
    count: int = 0


@dataclass
class Run:
    """Everything one intake run knows, for both reports."""

    date: str
    time: str
    customer: str
    new_customer: bool
    force: bool
    blocked: bool
    files: list[FileRecord] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    candidates: list[tuple[str, str]] = field(default_factory=list)   # (part id, candidate): private
    candidate_count: int = 0
    register_forms: int = 0


# --------------------------------------------------------------------------- helpers


def _cell(value) -> str:
    """A value made safe for one Markdown table cell."""
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def counts_text(counts: Counter) -> str:
    items = [(cls, n) for cls, n in sorted(counts.items()) if n]
    return ", ".join("%s %d" % item for item in items) if items else "none"


def _notes_text(notes: list[str]) -> str:
    return "; ".join(n for n in notes if n) or "none"


# --------------------------------------------------------------------------- public report


def render_public(run: Run) -> str:
    """The public report: codes, kinds, states, counts and notes. No name, no value, no candidate."""
    states = Counter(f.state for f in run.files)
    total: Counter = Counter()
    for f in run.files:
        total.update(f.counts())
    outputs = sum(1 for f in run.files if f.output is not None)
    lines = [
        "# Intake report %s" % run.customer,
        "",
        "- date: %s" % run.date,
        "- customer: %s" % run.customer,
        "- files: %d" % len(run.files),
        "- outputs: %d" % outputs,
        "- states: %s" % (", ".join("%s %d" % kv for kv in sorted(states.items())) or "none"),
        "",
        "| file id | kind | state | replaced | notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for f in run.files:
        for part in f.parts:
            state = f.state if part.part_id == f.file_id else part.state
            lines.append("| %s | %s | %s | %s | %s |" % (
                part.part_id, _cell(part.kind), _cell(state), _cell(counts_text(part.counts)),
                _cell(_notes_text(part.public_notes)),
            ))
    lines += [
        "",
        "Totals: %s replaced over %d files." % (counts_text(total), len(run.files)),
        "",
    ]
    failed = [f for f in run.files if f.state == "failed"]
    if failed:
        lines += ["Failed, no output: %s." % ", ".join(f.file_id for f in failed), ""]
        stayed = [f.file_id for f in failed if f.original is None]
        moved = [f.file_id for f in failed if f.original is not None]
        if stayed:
            lines += ["Originals still in the inbox: %s." % ", ".join(stayed), ""]
        if moved:
            lines += ["Originals moved to the vault although no output was written: %s." % ", ".join(moved), ""]
    unread = [f.file_id for f in run.files if f.state in ("unreadable", "unsupported")]
    if unread:
        lines += ["Not read as text, review the original in the vault: %s." % ", ".join(unread), ""]
    lines += ["%d candidates were reviewed." % run.candidate_count, ""]
    return "\n".join(lines)


def render_minimal(run: Run) -> str:
    """The public report without the table and without notes: date, customer code and counts only. Used when
    the full report does not pass the final check."""
    states = Counter(f.state for f in run.files)
    return "\n".join([
        "# Intake report %s" % run.customer,
        "",
        "- date: %s" % run.date,
        "- customer: %s" % run.customer,
        "- files: %d" % len(run.files),
        "- outputs: %d" % sum(1 for f in run.files if f.output is not None),
        "- states: %s" % (", ".join("%s %d" % kv for kv in sorted(states.items())) or "none"),
        "",
        "The detailed report was withheld because it did not pass the final check, see the private report.",
        "",
        "%d candidates were reviewed." % run.candidate_count,
        "",
    ])


# --------------------------------------------------------------------------- private report


def render_private(run: Run) -> str:
    """The private report: everything, including original names, values, tokens, positions, candidates."""
    lines = [
        "# Private intake report %s" % run.customer,
        "",
        "Vault side only. This file holds original names and matched values.",
        "",
        "- date: %s %s" % (run.date, run.time),
        "- customer: %s%s" % (run.customer, " (new code issued by this run)" if run.new_customer else ""),
        "- force: %s" % ("yes" if run.force else "no"),
        "- blocked: %s" % ("yes" if run.blocked else "no"),
        "- register forms used: %d" % run.register_forms,
        "- candidates: %d" % run.candidate_count,
        "",
    ]
    if run.blocked:
        lines += [
            "Blocked: the candidates below look like names that are not in the register. Nothing was written to "
            "the outbox and no original was moved. Register each real name with "
            "`awb register add CODE KIND FORM`, then run again with `--customer %s`. When every candidate "
            "is harmless, run again with `--force`." % run.customer,
            "",
        ]

    lines += ["## Files", "", "| file id | original name | kind | state | output | original in the vault |",
              "| --- | --- | --- | --- | --- | --- |"]
    for f in run.files:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            f.file_id, _cell(f.original_name), _cell(f.kind), _cell(f.state),
            _cell(f.output.name if f.output else "none"), _cell(f.original if f.original else "not moved"),
        ))
    lines.append("")

    lines += ["## Parts", "", "| part id | name | kind | state | replaced | notes |",
              "| --- | --- | --- | --- | --- | --- |"]
    for f in run.files:
        for part in f.parts:
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                part.part_id, _cell(part.label), _cell(part.kind), _cell(part.state),
                _cell(counts_text(part.counts)), _cell(_notes_text(part.notes)),
            ))
    lines.append("")

    meta_rows = []
    for f in run.files:
        for part in f.parts:
            for key, value in sorted(part.meta.items(), key=lambda kv: str(kv[0])):
                meta_rows.append("| %s | %s | %s |" % (part.part_id, _cell(key), _cell(value)))
    lines += ["## Metadata", ""]
    if meta_rows:
        lines += ["| part id | key | value |", "| --- | --- | --- |"] + meta_rows
    else:
        lines.append("none")
    lines.append("")

    lines += ["## Replacements", "",
              "Positions are character offsets. For \"detect\" and \"file name\" they point into the text the reader "
              "gave for detection (detect text, output text and metadata joined) and for \"raw parts\" into the raw "
              "part text; for \"output\" and \"note\" into the text of that sanitising pass. They are not offsets "
              "into the original file.", "",
              "| token | class | value | count | where and positions |",
              "| --- | --- | --- | --- | --- |"]
    for h in sorted(run.hits, key=lambda h: (h.token, h.value.casefold())):
        where = "; ".join(
            "%s at %s" % (k, ", ".join(str(p) for p in v[:MAX_POSITIONS]) + (" ..." if len(v) > MAX_POSITIONS else ""))
            for k, v in h.where.items()
        )
        lines.append("| %s | %s | %s | %d | %s |" % (h.token, h.cls, _cell(h.value), h.count, _cell(where)))
    lines.append("")

    lines += ["## Candidates", ""]
    if run.candidates:
        lines += ["| part id | candidate |", "| --- | --- |"]
        lines += ["| %s | %s |" % (pid, _cell(c)) for pid, c in run.candidates]
    else:
        lines.append("none")
    lines.append("")

    lines += ["## Dropped items and reasons", ""]
    reasons = [(f.file_id, r) for f in run.files for r in f.reasons]
    if reasons:
        lines += ["- %s: %s" % (fid, r) for fid, r in reasons]
    else:
        lines.append("none")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- writing


def _mkdir(path: Path, mode: int) -> None:
    """A folder open to the group (750) is one of the shared side, where the group-shared tree of a sealed host
    keeps its modes (config.make_dir); a private folder (700) of the vault side gets exactly its mode."""
    from awb import config

    config.make_dir(path, mode, shared=bool(mode & 0o070))


def write_new(path: Path, text: str, mode: int, dir_mode: int) -> Path:
    """Write `text` to a new file with `mode`. When the name is taken, -2, -3 ... is added before the suffix."""
    path = Path(path)
    _mkdir(path.parent, dir_mode)
    candidate = path
    n = 1
    while True:
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            break
        except FileExistsError:
            n += 1
            candidate = path.with_name("%s-%d%s" % (path.stem, n, path.suffix))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(candidate, mode)
    return candidate


def write_replace(path: Path, text: str, mode: int, dir_mode: int) -> Path:
    """Write `text` to `path`, replacing an older file atomically."""
    path = Path(path)
    _mkdir(path.parent, dir_mode)
    # a new name every time, opened exclusively and never through a link: on the shared side the work user
    # could plant a link under the old fixed name (the review of 2026-09-27)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(6)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        os.fchmod(fh.fileno(), mode)
    os.replace(tmp, path)
    return path


def write_private(path: Path, run: Run) -> Path:
    """Write the private report, file mode 600 and folder mode 700. Returns the path actually written."""
    return write_new(path, render_private(run), 0o600, 0o700)


def write_public(path: Path, text: str) -> Path:
    """Write the public report text, file mode 640 and folder mode 750."""
    return write_replace(path, text, 0o640, 0o750)
