"""What a put does to a file before it leaves (task GT, his decision of 2026-10-27; B-089, 2026-11-25).

Customer material is checked once, at the entry (awb start, intake, import, awb clean, a pasted text). After the
entry nothing stops his work: a put (`awb bucket put`, `awb xchg put` and the key service that receives it) never
refuses a file for its review state, a registered name, structured data or a secret, and it changes no byte: the
copy that is uploaded is the file (B-089: the put goes to the owner and to the sessions, so it is inside and runs no
content check; the replacing of a secret with [secret] is gone). The put prints one line: `review: none` or
`review: passed DATE`.

    scrub(src, dst)          the copy at dst, byte for byte; the number replaced, always 0
    scrub_text(text)         the text and 0
    review_line(project, f)  "review: passed DATE" for a deliverable (or a byte copy of one) with a valid
                             record, "review: none" for anything else; it never raises
    put_line(review, n)      the one line the put prints

Standard library only. Nothing here prints, logs or raises with a value.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def scrub_text(text: str, lines: bool = True) -> tuple[str, int]:
    """`text` as it is and 0: no secret is replaced any more (B-089). `lines` is kept for the callers."""
    return text, 0


def is_binary(name: str, data: bytes) -> bool:
    """A file that is not text, by the gate's rule: a NUL byte early, the magic of a PDF, a ZIP or a gzip, or the
    suffix of a document, an archive, an image or a key store (task T7: a PDF of ASCII streams decodes as UTF-8,
    and its stream lines were taken for tokens)."""
    from awb import gate

    return gate.is_opaque(name, data)


def scrub(src: Path, dst: Path) -> int:
    """Copy `src` to `dst` byte for byte, whatever the file is (B-089: no content check, no secret replaced). The
    number of secrets replaced, always 0."""
    shutil.copyfile(Path(src), Path(dst))
    return 0


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
