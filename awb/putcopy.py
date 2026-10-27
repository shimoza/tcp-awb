"""What a put does to a file before it leaves (task GT, his decision of 2026-10-27).

Customer material is checked once, at the entry (awb start, intake, import, awb clean, a pasted text). After the
entry nothing stops his work: a put (`awb bucket put`, `awb xchg put` and the key service that receives it) never
refuses a file for its review state, a registered name or structured data. One check stays: a real secret (the
gate classes secret, private-key and token) is replaced with [secret] in the copy that is uploaded, never in the
file itself. The put prints one line: `review: none` or `review: passed DATE` and the secrets it replaced.

    scrub(src, dst)          the copy at dst with every secret replaced; the number replaced
    review_line(project, f)  "review: passed DATE" for a deliverable (or a byte copy of one) with a valid
                             record, "review: none" for anything else; it never raises
    put_line(review, n)      the one line the put prints

Standard library only. Nothing here prints, logs or raises with a value.
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections import Counter
from pathlib import Path

SECRET = "[secret]"
SECRET_CLASSES = ("secret", "private-key", "token")
OFFICE_SUFFIXES = (".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".odt", ".ods", ".odp")
"""Zip containers whose XML parts are scrubbed one by one."""


def scrub_text(text: str, lines: bool = True) -> tuple[str, int]:
    """`text` with every secret replaced and the count. The wipe of awb clean runs first; with `lines`, a line the
    commit gate still finds a secret, a private key or a token in becomes [secret] as a whole."""
    from awb import cleanlogs, gate

    counts: Counter = Counter()
    out = cleanlogs.wipe_secrets(text, counts)
    n = sum(counts.values())
    if lines:
        found = {f.line for f in gate._scan_text("put", out, None) if f.cls in SECRET_CLASSES and f.line > 0}
        if found:
            rows = out.split("\n")
            for line in sorted(found):
                if 0 < line <= len(rows):
                    rows[line - 1] = SECRET
                    n += 1
            out = "\n".join(rows)
    return out, n


def _scrub_office(src: Path, dst: Path) -> int:
    n = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename.endswith((".xml", ".rels")):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    new, k = scrub_text(text, lines=False)
                    if k:
                        data, n = new.encode("utf-8"), n + k
            zout.writestr(info, data)
    return n


def scrub(src: Path, dst: Path) -> int:
    """Copy `src` to `dst` with every secret replaced: a text file (UTF-8) as text, an office file part by part,
    anything else as it is. The number of secrets replaced."""
    src, dst = Path(src), Path(dst)
    if src.suffix.lower() in OFFICE_SUFFIXES and zipfile.is_zipfile(src):
        try:
            n = _scrub_office(src, dst)
        except (zipfile.BadZipFile, OSError, ValueError):
            n = 0
        if n:
            return n
        shutil.copyfile(src, dst)
        return 0
    data = src.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        shutil.copyfile(src, dst)
        return 0
    new, n = scrub_text(text)
    if n:
        dst.write_bytes(new.encode("utf-8"))
    else:
        shutil.copyfile(src, dst)
    return n


def review_line(project: Path, file: Path) -> str:
    """`review: passed DATE` when the file is a deliverable of the project, by path or by its bytes, whose record
    is valid; else `review: none`. Informs, never decides."""
    try:
        from awb import review

        f = Path(os.path.realpath(file))
        digest = review.sha256_file(f)
        base = Path(os.path.realpath(Path(project) / review.DELIVERABLES))
        rel = None
        if review._under(f, base):
            rel = f.relative_to(base).as_posix()
        else:
            for r, real, state in review._deliverables_of(Path(project)):
                if state == review.VALID and review.sha256_file(real) == digest:
                    rel = r
                    break
        if rel is None:
            return "review: none"
        record = review.review_dir(Path(project), rel) / review.RECORD
        state, _ = review._record_state(base / rel, record)
        if state != review.VALID:
            return "review: none"
        date = json.loads(record.read_text(encoding="utf-8")).get("date")
        return "review: passed %s" % date if isinstance(date, str) and date else "review: passed"
    except Exception:
        return "review: none"


def put_line(review: str, secrets: int) -> str:
    """The one line of a put: the review state and, when any, the secrets replaced."""
    if secrets:
        return "%s; %d secret%s replaced" % (review, secrets, "" if secrets == 1 else "s")
    return review
