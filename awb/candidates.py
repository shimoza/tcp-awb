"""The candidates of a blocked intake, sorted in one pass (`awb register review CUST-XXXX`, owner side).

An intake that finds words looking like names it does not know stops, and its private report lists them as
candidates: real names of people and companies next to brands, products, terms and headings. This command opens
the candidates still open in an editor on the owner's own terminal, one per line. He marks the real names and
leaves every other line as it is:

    p  a person: a new PERS code under the customer     o  another company or organisation: a new ORG code
    c  one more written form of this customer           s  a place: a new SITE code under the customer
    +  one more form of the name on the marked line above (the same person, company or place)
    -  decide later: the candidate stays open
    no mark: not a name; it goes to the keep list and is never a candidate again

Before anything is written he sees the counts and confirms them. Then the marked names go into the register in one
save (codes issued in order) and the unmarked phrases into the keep list in one write. Nothing is printed but
counts. The review file lives in the vault's tmp folder, mode 600, and is removed afterwards. Refused for the work
user, inside an assistant session and without a terminal: what the editor shows carries names.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from awb import config, intake, register

MARKS = {"p": "PERS", "o": "ORG", "c": "CUST", "s": "SITE"}
HEADER = (
    "# The candidates of the intake for {customer}: {count} words that look like names.\n"
    "# Put a letter in front of each real name, leave every other line as it is.\n"
    "#   p  a person                o  another company or organisation\n"
    "#   c  this customer           s  a place\n"
    "#   +  one more form of the marked line above (the same person, company or place)\n"
    "#   -  decide later\n"
    "#   no letter: not a name (a brand, a product, a term, a heading); it passes and is not asked again\n"
    "# Save and close the editor. You see the counts and confirm them before anything is written.\n"
    "\n"
)


class ReviewError(Exception):
    """A review that cannot be applied. The message names a line number at most, never a phrase."""


@dataclass
class Decisions:
    keep: list[str] = field(default_factory=list)
    names: list[tuple[str, list[str]]] = field(default_factory=list)   # (mark, its forms), one per new name
    later: int = 0

    def summary(self) -> Counter:
        out = Counter(MARKS[mark] for mark, _ in self.names if mark != "c")
        out["CUST forms"] = sum(len(forms) for mark, forms in self.names if mark == "c")
        return out


def key(phrase: str) -> str:
    return intake.keep_key(phrase)


def latest_report(p: config.Paths, customer: str) -> Path:
    folder = p.private_reports / customer
    reports = sorted(f for f in folder.glob("*.md*") if f.is_file()) if folder.is_dir() else []
    if not reports:
        raise ReviewError("no private report of %s: run awb intake --customer %s first" % (customer, customer))
    return reports[-1]


def read_report(path: Path) -> str:
    """The text of a private report; a sealed one through the vault daemon."""
    if path.suffix != ".gpg":
        return path.read_text(encoding="utf-8")
    import base64

    from awb import vault

    answer = vault.admin_call("open_file", path=str(path.resolve()))
    try:
        return base64.b64decode(answer.get("data") or "", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ReviewError("the vault daemon answered in an unexpected form") from None


def _cells(row: str) -> list[str]:
    """The cells of one Markdown table row as awb.report writes them (a pipe inside a cell is escaped)."""
    cells, cur, i, body = [], [], 0, row.strip()[1:-1]
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            cur.append(body[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def parse_candidates(report: str) -> list[str]:
    """The candidates of a private report, each once, in the order of the report."""
    out, seen, inside = [], set(), False
    for line in report.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Candidates"
            continue
        if not inside or not line.startswith("|") or line.startswith("| part id") or line.startswith("| ---"):
            continue
        cells = _cells(line)
        if len(cells) == 2 and cells[1] and key(cells[1]) not in seen:
            seen.add(key(cells[1]))
            out.append(cells[1])
    return out


def still_open(p: config.Paths, candidates: list[str]) -> list[str]:
    """The candidates that are neither an active form of the register nor on the keep list."""
    done = {key(e.form) for e in register.load(p.register) if e.status == "active"}
    done |= {key(k) for k in intake.load_keep(p)}
    return [c for c in candidates if key(c) not in done]


def review_text(customer: str, candidates: list[str]) -> str:
    return HEADER.format(customer=customer, count=len(candidates)) + "".join("   %s\n" % c for c in candidates)


def parse_review(text: str, candidates: list[str]) -> Decisions:
    """The decisions of an edited review file. Refuses an unknown mark, a + without a marked line above and an
    unmarked line that is not one of the candidates. A deleted line leaves its candidate open."""
    known = {key(c) for c in candidates}
    d = Decisions()
    seen: set[str] = set()
    current: tuple[str, list[str]] | None = None
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        first, _, rest = line.lstrip().partition(" ")
        if line[:1].isspace() or len(first) != 1 or not rest.strip():
            mark, phrase = "", line.strip()
        else:
            mark, phrase = first.lower(), rest.strip()
            if mark not in MARKS and mark not in "+-":
                raise ReviewError("line %d starts with a letter that is not p, o, c, s, + or -" % n)
        phrase = " ".join(phrase.split())
        if key(phrase) in seen:
            continue
        seen.add(key(phrase))
        if mark == "":
            if key(phrase) not in known:
                raise ReviewError("line %d is not one of the candidates: mark it or delete the line" % n)
            d.keep.append(phrase)
        elif mark == "-":
            d.later += 1
        elif mark == "+":
            if current is None:
                raise ReviewError("line %d: + needs a marked name on a line above" % n)
            current[1].append(phrase)
        else:
            current = (mark, [phrase])
            d.names.append(current)
    d.later += sum(1 for c in candidates if key(c) not in seen)
    return d


def apply(p: config.Paths, customer: str, d: Decisions) -> tuple[Counter, int]:
    """Register the names (one save) and keep the rest (one write). Returns the counts and the phrases kept."""
    from awb import vault

    with vault.vault_lock(p, wait=10):
        entries = register.load(p.register)
        if customer not in register.codes(entries):
            raise ReviewError("%s is not in the register" % customer)
        today = date.today().isoformat()
        new: list[register.Entry] = []
        for mark, forms in d.names:
            kind = MARKS[mark]
            if mark == "c":
                code = customer
            elif kind in ("PERS", "SITE"):
                code = register.next_sub_code(entries + new, customer, kind)
            else:
                code = intake.safe_new_code(entries + new, kind, intake.issued_codes(p) | {e.code for e in new})
            for form in forms:
                entry = register.Entry(code, kind, form, today, "active")
                try:
                    register.check_entry(entry, "review")
                except register.RegisterError:
                    raise ReviewError("a marked name cannot go into the register: brackets, pipes, hashes and "
                                      "notes are not allowed in a name; nothing was changed") from None
                if not any(e.code == code and key(e.form) == key(form) for e in entries + new):
                    new.append(entry)
        if new:
            register.save(p.register, entries + new)
    kept = intake.keep_phrases(p, d.keep)[0] if d.keep else 0
    return d.summary(), kept


def _editor() -> list[str]:
    chosen = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if chosen:
        return shlex.split(chosen)
    for name in ("nano", "vi"):
        if shutil.which(name):
            return [name]
    raise ReviewError("no editor found: set EDITOR")


def _ask(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "j", "ja", "д", "да")
    except EOFError:
        return False


def review(p: config.Paths, customer: str, report: Path | None = None, *, edit=None, confirm=None,
           out=print) -> int:
    """One review: the open candidates into a private file, the editor, the counts to confirm, then the register
    and the keep list. `edit` and `confirm` stand in for the editor and the question in the tests."""
    path = report or latest_report(p, customer)
    candidates = still_open(p, parse_candidates(read_report(path)))
    if not candidates:
        out("every candidate of %s is sorted: run awb intake --customer %s" % (customer, customer))
        return 0
    tmp_dir = p.vault / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tmp_dir, 0o700)
    fd, name = tempfile.mkstemp(prefix="review-", suffix=".txt", dir=tmp_dir)
    review_file = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(review_text(customer, candidates))
        os.chmod(review_file, 0o600)
        while True:
            if edit is None:
                if subprocess.run(_editor() + [str(review_file)]).returncode != 0:
                    raise ReviewError("the editor ended with an error: nothing was changed")
            else:
                edit(review_file)
            text = review_file.read_text(encoding="utf-8")
            try:
                decisions = parse_review(text, candidates)
                break
            except ReviewError as err:
                if edit is not None:
                    raise
                # his marks stay: the file opens again with the reason on top
                body = "".join(line + "\n" for line in text.splitlines() if not line.startswith("# FIX: "))
                review_file.write_text("# FIX: %s\n" % err + body, encoding="utf-8")
                out("%s; the editor opens again with your marks" % err)
    finally:
        review_file.unlink(missing_ok=True)
    s = decisions.summary()
    question = ("register %d people, %d companies, %d places and %d more forms of %s; keep %d as not a name; "
                "leave %d for later. Apply? [y/N] " % (s["PERS"], s["ORG"], s["SITE"], s["CUST forms"], customer,
                                                       len(decisions.keep), decisions.later))
    if not (confirm or _ask)(question):
        out("nothing was changed")
        return 1
    counts, kept = apply(p, customer, decisions)
    out("registered %d people, %d companies, %d places, %d more forms of %s; kept %d as not a name; %d left for "
        "later" % (counts["PERS"], counts["ORG"], counts["SITE"], counts["CUST forms"], customer, kept,
                   decisions.later))
    out("now run: awb intake --customer %s" % customer)
    return 0


def command(args, p: config.Paths) -> int:
    """`awb register review CUST-XXXX [--report FILE]`: the owner's own terminal only."""
    from awb import vault

    if config.is_work_user():
        print("awb register review: the review is for the owner", file=sys.stderr)
        return 2
    if vault.in_assistant_session():
        print("awb register review: never inside an assistant session; run it in your own terminal",
              file=sys.stderr)
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("awb register review: it needs a terminal (an editor opens)", file=sys.stderr)
        return 2
    try:
        return review(p, args.customer, Path(args.report).expanduser() if args.report else None)
    except ReviewError as err:
        print("awb register review: %s" % err, file=sys.stderr)
        return 2
