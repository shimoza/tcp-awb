"""`awb kb import FILE`: the facts of a TCP Facts release become entries of a knowledge base.

FILE is the facts.jsonl of a release or its zip. Every line is checked as `awb kb add` checks a fact (scope, grade,
class, tags, the text checks with the name check, a negative needs its tried texts) and written with the id,
the dates and the source the release carries; the source line starts with `imported: tcp-facts RELEASE`. An id that is
taken is skipped, with --update only an entry that came from the same dataset and has an older check date is replaced.
The run needs no vault daemon, no key service and no work user: the name check reads the register of the folder
(on a laptop after `awb init` it is empty). The near-duplicate check of `add` is left out, since a release holds
facts that read alike on purpose (one per region or flavor).

The counts name classes and numbers, never a statement.
"""
from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from pathlib import Path

from awb import kb

MARK = "imported: tcp-facts"
MAX_FILE = 64 * 1024 * 1024
FACTS = "facts.jsonl"
_RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_FIELDS = ("statement", "scope", "grade", "checked", "class", "tags", "source")


def read_source(path: Path) -> str:
    """The text of a facts.jsonl or of the one facts.jsonl inside a zip."""
    path = Path(path)
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [i for i in z.infolist() if not i.is_dir() and i.filename.rsplit("/", 1)[-1] == FACTS]
                if len(names) != 1:
                    raise kb.KBError("the zip must hold exactly one %s, it holds %d" % (FACTS, len(names)))
                if names[0].file_size > MAX_FILE:
                    raise kb.KBError("%s in the zip is larger than %d MB" % (FACTS, MAX_FILE >> 20))
                data = z.read(names[0])
        else:
            if path.stat().st_size > MAX_FILE:
                raise kb.KBError("the file is larger than %d MB" % (MAX_FILE >> 20))
            data = path.read_bytes()
        return data.decode("utf-8")
    except FileNotFoundError:
        raise kb.KBError("there is no such file") from None
    except UnicodeDecodeError:
        raise kb.KBError("the file is not UTF-8 text") from None
    except (OSError, zipfile.BadZipFile) as err:
        raise kb.KBError("the file cannot be read (%s)" % type(err).__name__) from None


def parse_lines(text: str) -> tuple[dict, list[tuple[int, dict | None]]]:
    """(metadata, rows). The metadata is the first line when it has no id (the release date is in it); a row is
    (line number, record), the record None for a line that is not a JSON object."""
    meta: dict = {}
    rows: list[tuple[int, dict | None]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            rows.append((n, None))
        elif "id" not in rec and not rows and not meta:
            meta = rec
        else:
            rows.append((n, rec))
    return meta, rows


def release_label(meta: dict, given: str | None, today: date) -> str:
    label = given or (meta.get("date") if isinstance(meta.get("date"), str) else None) or today.isoformat()
    if not _RELEASE_RE.fullmatch(label):
        raise kb.KBError("the release label must be a date or a short word of letters, digits, dot, dash")
    return label


def reason_class(reason: str) -> str:
    """The class of one refusal message of the checks."""
    for needle, cls in (("name check", "name"), ("not in English", "language"), ("a price", "price"),
                        ("identifier, a home path", "secret"), ("project code", "code"), ("tag", "tag"),
                        ("negative", "negative"), ("holds", "scope")):
        if needle in reason:
            return cls
    return "other"


def _plain_list(value) -> list[str] | None:
    if value in (None, ""):
        return []
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    return None


def _entry_of(rec: dict, label: str, today: date, reg, folder_holds: str | None) -> tuple[kb.Entry | None, list[str]]:
    """The entry a record would be or the refusal classes. Raises nothing for a bad record."""
    kb_id = rec.get("id")
    if not isinstance(kb_id, str) or not kb.ID_RE.fullmatch(kb_id):
        return None, ["id"]
    if not all(isinstance(rec.get(k), (str, list)) for k in _FIELDS):
        return None, ["shape"]
    tags = _plain_list(rec["tags"])
    tried = _plain_list(rec.get("tried"))
    if tags is None or tried is None or not tags or any(";" in t or "\n" in t for t in tried):
        return None, ["shape"]
    scope, grade, cls = rec["scope"], rec["grade"], rec["class"]
    if not all(isinstance(x, str) for x in (scope, grade, cls, rec["statement"], rec["source"], rec["checked"])):
        return None, ["shape"]
    if scope not in kb.SCOPES or grade not in kb.GRADES or cls not in kb.CLASSES:
        return None, ["shape"]
    if folder_holds and scope != folder_holds:
        return None, ["scope"]
    try:
        if not rec["checked"]:
            return None, ["date"]
        checked = kb._checked(rec["checked"], today)
        statement = kb._clean_statement(rec["statement"])
        source = kb._one_line_field("%s %s; %s" % (MARK, label, rec["source"].strip()), "the source")
        tried = [kb._one_line_field(t, "tried") for t in tried]
        tag_list, reasons = kb._clean_tags(tags)
    except kb.KBError:
        return None, ["shape"]
    expires = rec.get("expires")
    if not (isinstance(expires, str) and kb._valid_date(expires)):
        expires = kb.expires_for(checked, cls)
    reasons = list(reasons)
    reasons += kb.text_problems("the statement", statement, reg)
    reasons += kb.text_problems("the source", source, reg, language=False)
    for t in tried:
        reasons += kb.text_problems("tried", t, reg)
    reasons += kb._negative_problems(statement, tried, "tried texts")
    if reasons:
        return None, sorted({reason_class(r) for r in reasons})
    return kb.Entry(id=kb_id, scope=scope, tags=tuple(tag_list), grade=grade, checked=checked, cls=cls,
                    expires=expires, source=source, tried=tuple(tried), statement=statement), []


def import_facts(text: str, *, where=None, release: str | None = None, update: bool = False, dry_run: bool = False,
                 register_path=None, today: date | None = None) -> dict:
    """Import the records of a facts.jsonl text. Returns {"label", "imported", "updated", "skipped", "refused"}:
    counts. `refused` maps a class to its count. Nothing is written for a refused record. With
    `dry_run` nothing at all."""
    today = today or date.today()
    meta, rows = parse_lines(text)
    label = release_label(meta, release, today)
    reg = kb.register_of(register_path)
    kb._require_register(reg)
    folder = kb.root(where)
    holds = kb.folder_scope(folder)
    out = {"label": label, "imported": 0, "updated": 0, "skipped": 0, "refused": {}}

    def refuse(classes):
        for c in classes:
            out["refused"][c] = out["refused"].get(c, 0) + 1

    seen: set[str] = set()
    with kb._locked(folder):
        have = {e.id: e for e in kb.load(folder)}
        written: dict[str, kb.Entry] = {}
        for _, rec in rows:
            if rec is None:
                refuse(["format"])
                continue
            entry, classes = _entry_of(rec, label, today, reg, holds)
            if entry is None:
                refuse(classes)
                continue
            if entry.id in seen:
                refuse(["id"])      # the same id twice in one release
                continue
            seen.add(entry.id)
            old = have.get(entry.id)
            if old is not None:
                same = old.source.startswith(MARK + " ") and not old.is_retired
                if not (update and same and old.checked < entry.checked):
                    out["skipped"] += 1
                    continue
                out["updated"] += 1
            else:
                out["imported"] += 1
            written[entry.id] = entry
            if not dry_run:
                path = folder / kb.ENTRIES / (entry.id + ".md")
                entry = kb.Entry(**{**entry.__dict__, "path": path})
                kb._write_atomic(path, kb.render(entry))
                have[entry.id] = entry
        if written and not dry_run:
            kb.write_index(folder, sorted(have.values(), key=lambda e: e.id), today)
    return out


def summary(r: dict, dry_run: bool = False) -> list[str]:
    refused = sum(r["refused"].values())
    head = "awb kb import: tcp-facts %s%s: imported %d, updated %d, skipped %d (id taken), refused %d" % (
        r["label"], " (dry run, nothing written)" if dry_run else "", r["imported"], r["updated"], r["skipped"], refused)
    lines = [head]
    if r["refused"]:
        lines.append("refused by class: " + ", ".join("%s %d" % kv for kv in sorted(r["refused"].items())))
    return lines
