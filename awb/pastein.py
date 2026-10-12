"""`awb import CODE --paste [--kind mail|transcript|table]`: pasted text through the intake (owner side).

Material that arrives as text he copies from a mail client, a meeting tool or a spreadsheet has the same one door as
a file. The text is read from standard input, never echoed and run through the engine of the intake in wipe mode
(`awb.intake`): the project's customer forms become its code and every other person, company, place, mail address
and phone becomes a token. The sanitised copy goes where an imported file goes, to the outbox under the customer
code (the project code for a project without a customer), where the next prompt of a session tells of it.

What is added to the file path:

- A secret (a key, a token, a password line) refuses the whole paste before anything else happens. The message
  names the class and the line, never the text. Nothing is written (`PasteRefused`).
- `mail` (and plain text, which has no kind): a signature block and a quoted chain header are wiped as a whole after
  the sanitising. A signature runs from a closing line (Best regards, Mit freundlichen Grüßen, a line `--`) to the
  next chain header or the end, 25 lines at most. It becomes the one line `[signature]`, followed by the person token
  of its first line. A chain header (an `On ... wrote:` line, a `-----Original Message-----` line, a block of From,
  Sent, To and Subject lines) becomes the one line `[mail header] from [person 2] to [person 1]` with the tokens of
  the people in it and no date, subject or address.
- `transcript`: every speaker label becomes a person token numbered by the first turn (`[person 1]`, `[person 2]`
  within the paste), also where the name stands in the speech. Timestamps stay. The filler lines of the meeting tool
  (who joined or left, recording started, a generated-by line, a cue number) are dropped.
- `table`: tab-separated text as copied from a spreadsheet, written as CSV with its columns. A column whose header
  names people or mail addresses (also phones, companies and places) is wiped as a whole column.

The copy is named `mail-<date>-<n>.md`, `transcript-<date>-<n>.md`, `table-<date>-<n>.csv` (`paste-<date>-<n>.md`
without a kind). The number counts up over the outbox and the input/ folder of the project, so two pastes of one
day never meet under one name. The public report next to the copy and the private report of the vault are written
as for a file; the paste itself is not kept (it is no file: the mail client still has it), so `--redo` does not take
it again. Nothing here prints, logs or raises with a value of the text.
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from awb import config, intake, register, wipe
from awb import report as _report
from awb.matcher import Span

KINDS = ("mail", "transcript", "table")
NAMES = {"mail": ("mail", ".md"), "transcript": ("transcript", ".md"), "table": ("table", ".csv"),
         None: ("paste", ".md")}
"""The word and the suffix of the copy of each kind (no kind: plain text)."""
NAME_RE = re.compile(r"(?:mail|transcript|table|paste)-\d{4}-\d{2}-\d{2}-\d+\.(?:md|csv)")
"""The shape of a copy this module writes (the notice of the sessions knows it)."""
MAX_BYTES = 1024 * 1024
"""The largest paste taken, as for `awb paste`."""
MAX_SIGNATURE_LINES = 25
"""The most lines of one signature block."""


class PasteRefused(Exception):
    """The paste cannot be taken. The message carries classes, lines and counts, never a part of the text."""


class PasteHeld(PasteRefused):
    """The final check withheld the paste: something of a name is left in the sanitised text. Nothing was written."""


# --------------------------------------------------------------------------- reading and the secret check


def read_text(data: bytes) -> str:
    """The text of the bytes read from standard input: UTF-8, no byte order mark, newlines as `\\n`."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PasteRefused("the paste is not UTF-8 text") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise PasteRefused("the paste is empty: pipe the text in")
    if len(data) > MAX_BYTES:
        raise PasteRefused("the paste is over %d MB: split it" % (MAX_BYTES // (1024 * 1024)))
    return text


def secret_findings(text: str) -> list[tuple[str, int]]:
    """(class, line) of every secret of the gate's classes in `text`, by line. Never a value."""
    from awb import gate, hooks

    found = {(f.cls, f.line) for f in gate._scan_text("paste", text, None) if f.cls in hooks.SECRET_CLASSES}
    return sorted(found, key=lambda f: (f[1], f[0]))


def secret_message(findings: list[tuple[str, int]]) -> str:
    shown = ", ".join("%s at line %d" % (cls, line) for cls, line in findings[:8])
    more = " and %d more" % (len(findings) - 8) if len(findings) > 8 else ""
    return ("the paste carries a secret (%s%s): nothing was written. Take the secret out of the text and paste "
            "again." % (shown, more))


# --------------------------------------------------------------------------- the people of a text


_WHO_RE = re.compile(r"\[(?:person|company|name) \d+\]|(?<![\w-])(?:CUST|PART|ORG)-[A-Z2-7]{4}(?:-PERS-\d+)?(?![\w-])")
"""A token or a code that stands for a person or a company in a sanitised text."""
_PERSON_RE = re.compile(r"\[(?:person|name) \d+\]|(?<![\w-])(?:CUST|PART|ORG)-[A-Z2-7]{4}-PERS-\d+(?![\w-])")
_STRUCTURED_RE = re.compile(r"(?<![\w-])(?:MAIL|PHONE|URL|IP|MAC|IBAN|BANK|TAX|REG|REF|DOM|ID)-[A-Z2-7]{4}(?![\w-])")


def _who(text: str, persons_only: bool = False) -> list[str]:
    seen: list[str] = []
    for m in (_PERSON_RE if persons_only else _WHO_RE).finditer(text):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


# --------------------------------------------------------------------------- mail: signature and chain header


_QUOTE_RE = re.compile(r"^[ \t]*(?:>[ \t]*)*")
_SIGNER = (r"(?:\[(?:person|name) \d+\]|(?:CUST|PART|ORG)-[A-Z2-7]{4}-PERS-\d+|[A-ZÄÖÜ][\w'’.-]*)"
           r"(?:[ \t]+(?:\[(?:person|name) \d+\]|[A-ZÄÖÜ][\w'’.-]*)){0,3}")
_CLOSING_LINE_RE = re.compile(
    r"(?i)^(?:(?:vielen[ \t]+dank|danke|thanks|thank[ \t]+you|спасибо)[ \t]*(?:und|and|,|и)?[ \t]*)?"
    + wipe._CLOSING + r"(?![^\W\d_])(?:[ \t]*[/|,-][ \t]*" + wipe._CLOSING + r"(?![^\W\d_]))?"
    r"(?P<sep>[ \t]*[,.!:-]*[ \t]*)(?P<signer>" + _SIGNER + r")?[ \t]*$")
_DELIMITER_RE = re.compile(r"^(?:--|\u2014)[ \t]?$")
_FIELD_RE = re.compile(
    r"(?i)^[*_ \t]*(?P<key>from|von|to|an|cc|bcc|subject|betreff|sent|gesendet|date|datum|reply-to|antwort[ \t]+an|"
    r"от|кому|копия|тема|отправлено|дата|importance|priorität|wichtigkeit)[*_ \t]*:")
_FROM_KEYS = ("from", "von", "от", "reply-to")
_TO_KEYS = ("to", "an", "cc", "bcc", "кому", "копия")
_REPLY_RE = re.compile(r"(?i)^(?:on|am)[ \t].{4,200}?(?:wrote|schrieb)[ \t]*:[ \t]*$|^.{0,200}?(?:написал(?:а)?|пишет)"
                       r"(?:\(а\))?[ \t]*:[ \t]*$|^.{0,200}?a[ \t]+écrit[ \t]*:[ \t]*$")
_REPLY_START_RE = re.compile(r"(?i)^(?:on|am)[ \t].{4,200}$")
_REPLY_TAIL_RE = re.compile(r"(?i)^(?:<[^>\n]*>[ \t]*)?(?:wrote|schrieb)[ \t]*:[ \t]*$")
_SEPARATOR_RE = re.compile(
    r"(?i)^(?:-{2,}[ \t]*(?:original[ \t]+message|ursprüngliche[ \t]+nachricht|forwarded[ \t]+message|"
    r"weitergeleitete[ \t]+nachricht|исходное[ \t]+сообщение|пересылаемое[ \t]+сообщение)[ \t]*-{2,}|"
    r"begin[ \t]+forwarded[ \t]+message:?)[ \t]*$")
_RULE_RE = re.compile(r"^_{10,}[ \t]*$")
"""A line of underscores: the start of a chain header only when header fields follow it."""
_CONTINUATION_RE = re.compile(r"^(?:%s|[\s;,<>\"'()|]|und|and)*$" % (_WHO_RE.pattern + "|" + _STRUCTURED_RE.pattern))


def _split_quote(line: str) -> tuple[str, str]:
    m = _QUOTE_RE.match(line)
    return line[:m.end()], line[m.end():]


def _depth(prefix: str) -> int:
    return prefix.count(">")


def _field_key(body: str) -> str | None:
    m = _FIELD_RE.match(body)
    return re.sub(r"\s+", " ", m.group("key").casefold()) if m else None


def _header_end(lines: list[str], i: int) -> int:
    """The end (exclusive) of the chain header that starts at line `i` (`i` itself when none does)."""
    prefix, body = _split_quote(lines[i])
    if _REPLY_RE.match(body):
        return i + 1
    if _REPLY_START_RE.match(body) and i + 1 < len(lines):
        next_prefix, next_body = _split_quote(lines[i + 1])
        if _depth(next_prefix) == _depth(prefix) and _REPLY_TAIL_RE.match(next_body):
            return i + 2
    start = i
    strong = bool(_SEPARATOR_RE.match(body))
    if strong or _RULE_RE.match(body):
        i += 1
    keys: list[str] = []
    values: list[str] = []
    j = i
    while j < len(lines):
        p, b = _split_quote(lines[j])
        key = _field_key(b) if _depth(p) == _depth(prefix) else None
        if key is not None:
            keys.append(key)
            values.append(b)
        elif keys and _depth(p) == _depth(prefix) and b.strip() and _CONTINUATION_RE.match(b):
            values.append(b)
        else:
            break
        j += 1
    if len(keys) >= 3 or (len(keys) >= 2 and any(_WHO_RE.search(v) or _STRUCTURED_RE.search(v) for v in values)):
        return j
    return start + 1 if strong else start


def _header_line(lines: list[str], i: int, end: int) -> str:
    """The one line that stands for the chain header of lines i..end: the quote prefix of the first line, then
    `[mail header]` with the tokens of who wrote and who received it."""
    prefix, _ = _split_quote(lines[i])
    block = [_split_quote(x)[1] for x in lines[i:end]]
    sender: list[str] = []
    receivers: list[str] = []
    for b in block:
        key = _field_key(b)
        if key in _TO_KEYS:
            receivers += [w for w in _who(b) if w not in receivers]
        elif key in _FROM_KEYS or key is None:
            sender += [w for w in _who(b) if w not in sender]
    out = "[mail header]"
    if sender:
        out += " from " + ", ".join(sender)
    if receivers:
        out += " to " + ", ".join(receivers)
    return prefix + out


def _closing(body: str) -> bool:
    """A closing line: a farewell word with nothing after it or with the signer, who is a token or follows a comma.
    "Best Practices" is no closing."""
    m = _CLOSING_LINE_RE.match(body.strip())
    if not m:
        return False
    signer = m.group("signer")
    if not signer:
        return True
    punctuated = bool(re.search(r"[,:-]", m.group("sep")))
    if re.match(r"(?i)best\b", body.strip()) and not punctuated:
        return False    # "Best Practices" is a heading, "Best, Ulvrad" a closing
    return bool(_PERSON_RE.match(signer)) or punctuated


def _opens_signature(body: str, signer_on_closing: bool) -> bool:
    """The first line after a closing opens a signature block when the signer stood on the closing already. It does
    too when the line has a token in it. It does when the line starts with a capital and is short with no full stop
    at its end. ("the plan is ready" after a thanks is text.)"""
    if signer_on_closing or _WHO_RE.search(body):
        return True
    words = body.split()
    return bool(words) and words[0][:1].isupper() and len(words) <= 6 and not body.rstrip().endswith(".")


def _signature_end(lines: list[str], i: int, depth: int) -> int:
    """The end (exclusive) of the signature block that starts at line `i`: up to a chain header, a line outside the
    quote level of the closing, two blank lines in a row after the first line of the block or the limit."""
    j = i
    taken = 0
    blanks = 0
    while j < len(lines) and taken < MAX_SIGNATURE_LINES:
        p, b = _split_quote(lines[j])
        if not b.strip():
            blanks += 1
            if p.strip() and _depth(p) != depth:
                break
            if blanks >= 2 and taken:
                break
            j += 1
            continue
        if _depth(p) != depth or _header_end(lines, j) > j:
            break
        blanks = 0
        taken += 1
        j += 1
    while j > i and not _split_quote(lines[j - 1])[1].strip():
        j -= 1    # the blank lines after the block belong to the text again
    return j


def collapse_mail(text: str) -> tuple[str, Counter]:
    """`text` (already sanitised) with every chain header and every signature block replaced by its one line.
    Counts per kind of block: "signature", "header"."""
    lines = text.split("\n")
    out: list[str] = []
    counts: Counter = Counter()
    i = 0
    while i < len(lines):
        prefix, body = _split_quote(lines[i])
        end = _header_end(lines, i)
        if end > i:
            out.append(_header_line(lines, i, end))
            counts["header"] += 1
            i = end
            continue
        delimiter = bool(_DELIMITER_RE.match(body))
        said = _CLOSING_LINE_RE.match(body.strip())
        signed = bool(said and said.group("signer"))
        if delimiter or _closing(body):
            first = i + 1 if not delimiter else i
            # one blank line may stand between the closing and the signer
            k = first
            if k < len(lines) and not _split_quote(lines[k])[1].strip():
                k += 1
            if k < len(lines) and _split_quote(lines[k])[1].strip() and (
                    delimiter or _opens_signature(_split_quote(lines[k])[1], signed)):
                end = _signature_end(lines, first, _depth(prefix))
                block = [x for x in lines[first:end] if _split_quote(x)[1].strip()]
                if block:
                    if not delimiter:
                        out.append(lines[i])
                    head = _split_quote(block[0])[1]
                    persons = _who(head, persons_only=True)
                    out.append(prefix + "[signature]" + (" " + " ".join(persons) if persons else ""))
                    counts["signature"] += 1
                    i = end
                    continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), counts


# --------------------------------------------------------------------------- transcript


_STAMP = r"(?:[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?(?:[ \t]*(?:AM|PM|am|pm))?[\])]?)"
_LEAD = r"^[ \t]*(?:%s[ \t]*[-–]?[ \t]*)?" % _STAMP
_FILLER_RES = tuple(re.compile(r, re.IGNORECASE) for r in (
    r"^[ \t]*(?:recording|transcription|transcript|live[ \t]+transcription)[ \t]+(?:has[ \t]+|is[ \t]+)?"
    r"(?:started|stopped|ended|paused|resumed|in[ \t]+progress)[ \t.!]*$",
    _LEAD + r"[^:\n]{1,80}?[ \t]+(?:has[ \t]+)?(?:joined|left|entered|exited)[ \t]+(?:the[ \t]+)?(?:meeting|call|conversation|"
    r"room|session)[ \t.!]*$",
    r"^[ \t]*(?:meeting|call|session)[ \t]+(?:started|ended|has[ \t]+ended|was[ \t]+recorded)\b.*$",
    _LEAD + r"[^:\n]{1,80}?[ \t]+(?:is[ \t]+sharing|started[ \t]+sharing|stopped[ \t]+sharing|has[ \t]+stopped[ \t]+sharing)"
    r"\b.*$",
    r"^[ \t]*this[ \t]+(?:transcript|recording)[ \t]+(?:was|has[ \t]+been)[ \t]+(?:automatically[ \t]+)?"
    r"(?:generated|created|transcribed)\b.*$",
    r"^[ \t]*(?:transcribed|generated|captioned|powered)[ \t]+(?:by|with)\b.*$",
    r"^[ \t]*[\[(]?(?:music|applause|laughter|laughs|silence|inaudible|crosstalk|pause|noise|background[ \t]+noise|"
    r"unintelligible)[\])]?[ \t.]*$",
    r"^[ \t]*(?:start[ \t]+time|end[ \t]+time|duration)[ \t]*:.*$",
    _LEAD + r"[^:\n]{1,80}?[ \t]+(?:reacted[ \t]+to|raised[ \t]+(?:a|their)[ \t]+hand|lowered[ \t]+(?:a|their)[ \t]+hand)\b.*$",
))
_FILLER_CASED_RES = (re.compile(r"^[ \t]*(?:WEBVTT|NOTE)\b.*$"),)
"""The marks of a caption file are in capitals: a spoken line that begins with note is no filler."""
MAX_FILLER_LENGTH = 100
"""A filler line is short: a longer line is speech."""
_CUE_NUMBER_RE = re.compile(r"^[ \t]*\d{1,6}[ \t]*$")
_CUE_TIMING_RE = re.compile(r"^[ \t]*\d{1,2}:\d{2}:\d{2}[.,]\d{3}[ \t]*-->")
_SPEAKER_FORMS = (
    wipe._SPEAKER_RE, wipe._SPEAKER_PIPE_RE, wipe._STAMP_ONLY_RE, wipe._TEAMS_RE, wipe._ZOOM_RE, wipe._VTT_V_RE,
    wipe._QA_PAREN_RE,
)


def _speaker_label(label: str) -> str | None:
    """The label as a speaker's name. None when it is a field or a role or a word of the language. A speaker is
    one to four words of letters, each capitalised, with no digit, no neutral label (Note, Agenda, Action items)
    and no role word."""
    label = " ".join(label.split())
    words = label.split()
    if not 1 <= len(words) <= 4 or len(label) > 40 or any(c.isdigit() for c in label):
        return None
    if not all(wipe._name_shaped(w) for w in words):
        return None
    if not all(w[:1].isupper() or w.casefold() in wipe._PARTICLE_WORDS for w in words):
        return None
    if label.casefold() in wipe._NEUTRAL_LABELS or label.casefold() in wipe._COMPANY_LABELS:
        return None
    if all(wipe._role_word(w) or w.casefold().strip(".") in wipe._TITLE_WORDS for w in words):
        return None
    if len(words) == 1 and wipe._person_known(words[0]) and words[0].casefold() not in wipe._FIRST_NAMES:
        return None
    return label


def _speakers(text: str) -> list[tuple[int, int, str]]:
    """(start, end, label) of every speaker label of the turns of a transcript, by position, no two overlapping."""
    found: dict[int, tuple[int, int, str]] = {}
    for rx in _SPEAKER_FORMS:
        for m in rx.finditer(text):
            a, b = m.start("label"), m.end("label")
            von = wipe._LABEL_AFTER_VON_RE.search(text[a:b])
            if von:
                b = a + von.start()
            raw = text[a:b]
            lead = len(raw) - len(raw.lstrip())
            a, b = a + lead, a + len(raw.rstrip())
            label = _speaker_label(text[a:b])
            if label is not None:
                found.setdefault(a, (a, b, label))
    out: list[tuple[int, int, str]] = []
    for a, b, label in sorted(found.values()):
        if not out or a >= out[-1][1]:
            out.append((a, b, label))
    return out


def tokenize_speakers(engine: intake._Engine, text: str) -> tuple[str, int]:
    """`text` with every speaker label replaced by its person token, numbered by the first turn. The words of
    every label are taught to the run so that the name in the speech goes the same way. A label the register knows
    is left to the sanitising, which writes its code. Returns the text and the number of labels replaced."""
    wipe.bind(engine.wipe)
    spans = []
    for a, b, label in _speakers(text):
        if engine.matcher.find(label):
            continue
        spans.append((a, b, label))
    out: list[str] = []
    last = 0
    for a, b, label in spans:
        key = ("person", wipe._canon(label))
        token = wipe.token_for_key(engine.wipe, key)
        span = Span(a, b, "person", None, "speaker label")
        engine.record("paste speaker", span, label, token)
        wipe._learn(engine.wipe, "person", label, key)
        out.append(text[last:a])
        out.append(token)
        last = b
    out.append(text[last:])
    return "".join(out), len(spans)


def drop_filler(text: str) -> tuple[str, int]:
    """`text` without the filler lines of a meeting tool and without a run of blank lines. Returns the text and
    the number of lines dropped. A line with a colon after the stamp is a turn and never filler."""
    lines = text.split("\n")
    keep: list[str] = []
    dropped = 0
    for i, line in enumerate(lines):
        if _CUE_NUMBER_RE.match(line) and i + 1 < len(lines) and _CUE_TIMING_RE.match(lines[i + 1]):
            dropped += 1
            continue
        if line.strip() and len(line) <= MAX_FILLER_LENGTH and any(rx.match(line) for rx in _FILLER_RES + _FILLER_CASED_RES):
            dropped += 1
            continue
        if not line.strip() and keep and not keep[-1].strip():
            continue
        keep.append(line)
    while keep and not keep[0].strip():
        keep.pop(0)
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep), dropped


# --------------------------------------------------------------------------- table


_MAIL_WORDS = frozenset(("mail", "email", "emails", "mails", "mailadresse", "emailadresse", "mailadressen", "почта",
                         "почты", "эл", "e-post"))
_PHONE_WORDS = frozenset(("telefon", "telefone", "phone", "phones", "tel", "mobil", "mobile", "handy", "fax", "rufnummer",
                          "durchwahl", "телефон", "моб", "телефоны"))


def column_classes(header: list[str]) -> list[str | None]:
    """What each column holds by its header: "person", "company", "place", "mail" or "phone"; None for any other.
    A Name column of a technical table (it has Typ, IP, Version ... next to it) holds resources and stays, unless
    the table has a mail or a phone column: then it is a list of contacts."""
    labels = [" ".join(h.split()).casefold().strip("*_ :") for h in header]
    out: list[str | None] = []
    for h, label in zip(header, labels):
        words = set(re.findall(r"[^\W\d_]+", label.replace("-", "")))
        cls = wipe._header_class(h)
        if words & _MAIL_WORDS:
            out.append("mail")
        elif words & _PHONE_WORDS:
            out.append("phone")
        elif cls in ("person", "company", "place"):
            out.append(cls)
        else:
            out.append(None)
    contacts = any(c in ("mail", "phone") for c in out)
    technical = any(label in wipe._TECH_HEADERS for label in labels)
    return [None if cls == "person" and technical and not contacts and label in wipe._WEAK_PERSON_LABELS else cls
            for cls, label in zip(out, labels)]


def parse_table(text: str) -> list[list[str]]:
    """The rows of tab-separated text, as a spreadsheet copies it: no empty row, every row as wide as the widest."""
    if "\t" not in text:
        raise PasteRefused("a table needs tab-separated columns: copy the cells in the spreadsheet and paste them")
    try:
        rows = [r for r in csv.reader(io.StringIO(text), dialect=csv.excel_tab) if any(c.strip() for c in r)]
    except csv.Error:
        raise PasteRefused("the table does not read as tab-separated text") from None
    width = max((len(r) for r in rows), default=0)
    if len(rows) < 1 or width < 2:
        raise PasteRefused("a table needs tab-separated columns: copy the cells in the spreadsheet and paste them")
    return [r + [""] * (width - len(r)) for r in rows]


def _cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def table_view(rows: list[list[str]]) -> str:
    """The rows as a Markdown table: the shape the table rules of wipe mode read."""
    lines = []
    for i, row in enumerate(rows):
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
        if i == 0:
            lines.append("|" + " --- |" * len(row))
    return "\n".join(lines)


def table_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


_CODE = r"(?:CUST|PART|ORG)-[A-Z2-7]{4}(?:-[A-Z]+-\d+)?"
_WHOLE = {
    "person": r"\[person \d+\]|" + _CODE,
    "company": r"\[company \d+\]|" + _CODE,
    "place": r"\[place \d+\]|" + _CODE,
    "mail": _STRUCTURED_RE.pattern + "|" + _CODE,
    "phone": _STRUCTURED_RE.pattern + "|" + _CODE,
}
"""What a cell of a column may hold when the sanitising made it whole already: tokens of the class of the column and
codes, with the marks between them."""
_WHOLE = {cls: re.compile(r"^(?:%s|[\s;,/&()|-]|und|and)*$" % alt) for cls, alt in _WHOLE.items()}


def _first_item(cell: str) -> str:
    """The first item of a person or company cell: what stands before a bracket, a comma, a semicolon or a slash."""
    return re.split(r"[(<,;|/]", cell, maxsplit=1)[0].strip()


def _whole_cell(engine: intake._Engine, cell: str, cls: str, counts: Counter, where: str) -> str:
    """The cell of a column that is wiped as a whole: what the sanitising made of it when that is only tokens of the
    class of the column and codes, else the token of the whole value (the first item of a person or company
    cell)."""
    clean, found = engine.sanitize(cell, where)
    counts.update(found)
    if not cell.strip() or _WHOLE[cls].match(clean.strip()):
        return clean
    rule = wipe.RULE_LABELS["_table_candidates"]
    if cls in ("mail", "phone"):
        parts = [x for x in re.split(r"[;,\s]+", cell) if x]
        tokens = []
        for part in parts:
            span = Span(0, len(part), cls, None, rule)
            token = engine.token_for(span, part)
            engine.record(where, span, part, token)
            counts[cls] += 1
            tokens.append(token)
        return "; ".join(tokens)
    item = _first_item(cell) or cell.strip()
    span = Span(0, len(item), cls, None, rule)
    token = engine.token_for(span, item)
    engine.record(where, span, item, token)
    counts[cls] += 1
    return token


def sanitize_table(engine: intake._Engine, rows: list[list[str]]) -> tuple[list[list[str]], Counter, int]:
    """The rows with every cell sanitised and the columns of people, mail addresses, phones, companies and places
    wiped as a whole. Returns the rows, the counts per class and the number of columns wiped as a whole. The whole
    table is read first, so that the table rules of wipe mode and the names of its people column teach the run."""
    counts: Counter = Counter()
    classes = column_classes(rows[0])
    # the people and companies of the columns teach the run first, so that a name in another cell goes the same way
    wipe.bind(engine.wipe)
    for row in rows[1:]:
        for cls, cell in zip(classes, row):
            name = _first_item(cell)
            if cls in ("person", "company") and name and not engine.matcher.find(name):
                wipe._learn(engine.wipe, cls, name, (cls, wipe._canon(name)))
    engine.detect("paste detect", table_view(rows))
    out: list[list[str]] = []
    for r, row in enumerate(rows):
        cells = []
        for cls, cell in zip(classes, row):
            if r == 0 or cls is None:
                clean, found = engine.sanitize(cell, "paste output")
                counts.update(found)
                cells.append(clean)
            else:
                cells.append(_whole_cell(engine, cell, cls, counts, "paste output"))
        out.append(cells)
    return out, counts, sum(1 for c in classes if c)


# --------------------------------------------------------------------------- the run


def _next_number(word: str, ext: str, day: str, folders: list[Path]) -> int:
    rx = re.compile(r"%s-%s-(\d+)%s" % (word, day, re.escape(ext)))
    top = 0
    for folder in folders:
        try:
            names = [f.name for f in folder.iterdir()]
        except OSError:
            continue
        top = max([top] + [int(m.group(1)) for m in map(rx.fullmatch, names) if m])
    return top + 1


def _write_copy(outdir: Path, word: str, ext: str, day: str, first: int, text: str) -> Path:
    """The copy under the first free number, written exclusively with mode 640."""
    config.make_dir(outdir, 0o750, shared=True)
    n = first
    while True:
        path = outdir / ("%s-%s-%d%s" % (word, day, n, ext))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(path, 0o640)
        return path


def _wiped(counts: Counter) -> Counter:
    return Counter({wipe.TOKEN_TEXT[cls]: n for cls, n in counts.items() if cls in wipe.WIPE_CLASSES and n})


def run(p: config.Paths, customer: str, text: str, kind: str | None = None, *, input_dir: Path | None = None,
        today: date | None = None) -> intake.IntakeResult:
    """Take one paste in for `customer` (a CUST code or the code of a project without a customer). `input_dir` is
    the input/ folder of the project, so that the number of the copy is free there too. Raises PasteRefused for a
    secret or a text that is not a table, PasteHeld when the final check withholds the paste (nothing is written),
    intake.IntakeError when the intake cannot run."""
    from awb import vault

    if kind not in NAMES:
        raise PasteRefused("a kind is mail, transcript or table")
    failed = intake.selftest()
    if failed:
        raise intake.IntakeError("the intake failed its self-test and takes nothing in: %s" % "; ".join(failed))
    findings = secret_findings(text)
    if findings:
        raise PasteRefused(secret_message(findings))
    rows = parse_table(text) if kind == "table" else None

    config.ensure_layout(p)
    word, ext = NAMES[kind]
    day = (today or date.today()).isoformat()
    try:
        with vault.vault_lock(p):
            return _run(p, customer, text, kind, rows, word, ext, day, input_dir)
    except vault.VaultError as err:
        raise intake.IntakeError(str(err)) from None


def _run(p, customer, text, kind, rows, word, ext, day, input_dir) -> intake.IntakeResult:
    encrypted = intake.vault_encrypted(p)
    entries = register.load(p.register)
    cust, is_new = intake._resolve_customer(customer, entries, p)
    engine = intake._Engine(entries)
    engine.keep = tuple(intake.load_keep(p))
    now = datetime.now()
    notes: list[str] = []
    counts: Counter = Counter()

    if kind == "table":
        out_rows, found, whole = sanitize_table(engine, rows)
        counts.update(found)
        body = table_csv(out_rows)
        view = table_view(out_rows)
        if whole:
            notes.append("%d column(s) wiped as a whole" % whole)
        hits = engine.leaks(body, candidates=False) + engine.leaks(view, candidates=False) \
            + intake._body_hits(engine, view)
        shown = "%d rows, %d columns" % (len(out_rows), len(out_rows[0]))
    else:
        raw = text
        spoken = 0
        if kind == "transcript":
            raw, spoken = tokenize_speakers(engine, raw)
            raw, dropped = drop_filler(raw)
            if spoken:
                notes.append("%d speaker label(s) replaced" % spoken)
            if dropped:
                notes.append("%d filler line(s) dropped" % dropped)
        engine.detect("paste detect", raw)
        body, found = engine.sanitize(raw, "paste output")
        counts.update(found)
        if kind in (None, "mail"):
            body, blocks = collapse_mail(body)
            if blocks["signature"]:
                notes.append("%d signature block(s) wiped as a whole" % blocks["signature"])
            if blocks["header"]:
                notes.append("%d mail header(s) wiped as a whole" % blocks["header"])
        body = body.rstrip("\n") + "\n"
        hits = engine.leaks(body, candidates=False) + intake._body_hits(engine, body)
        shown = "%d lines" % body.count("\n")

    if hits:
        raise PasteHeld("the final check withheld the paste (%s left in the sanitised text): nothing was written"
                        % _report.counts_text(Counter(h.cls for h in hits)))

    outdir = p.outbox / cust
    folders = [outdir] + ([input_dir] if input_dir is not None else [])
    out = _write_copy(outdir, word, ext, day, _next_number(word, ext, day, folders), body)
    stem = out.name
    rec = _report.Run(date=now.date().isoformat(), time=now.strftime("%H:%M:%S"), customer=cust, new_customer=is_new,
                      register_forms=engine.forms)
    fr = _report.FileRecord(file_id=stem, original_name="paste (%s)" % (kind or "text"), kind="paste-%s" % (kind or "text"),
                            state="ok")
    part = _report.Part(part_id=stem, label=fr.original_name, kind=fr.kind, state="ok", notes=list(notes),
                        public_notes=list(notes), meta={"size": shown})
    part.counts.update(counts)
    fr.parts.append(part)
    fr.output = out
    rec.files.append(fr)
    rec.hits = list(engine.hits.values())
    rec.learned = engine.learned_forms()
    try:
        public_written = _report.write_public(p.outbox / cust / _report.PUBLIC_NAME, intake._public_text(rec, engine))
        private_path = p.private_reports / cust / ("%s.md" % now.strftime("%Y-%m-%d-%H%M%S"))
        private_written, unsealed = intake._write_private_report(p, private_path, rec, encrypted)
    except intake._Undo:
        out.unlink(missing_ok=True)
        raise intake.IntakeError(intake.UNDONE) from None
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return intake.IntakeResult(customer=cust, outputs=[out], public_report=public_written,
                               private_report=private_written, states={stem: "ok"}, unsealed=unsealed,
                               wiped=_wiped(counts))
