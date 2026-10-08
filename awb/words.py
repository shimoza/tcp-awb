"""The vocabulary of the public corpus for the intake (`awb words update`, owner side).

The run rule of the intake takes two capitalised words in a row for a name unless it knows every word. What it knows
comes from rules files; the two largest are written here from the public mirrors, the way `awb service update`
writes the service names:

    rules/known-words.txt     every capitalised word (Title case with three letters or more, or ALL CAPS with
                              four or more) of the documentation mirror (*.rst) and the service description text,
                              with the number of files it stands in and its count
    rules/known-phrases.txt   every two such words in a row, the same way

A word or a phrase is written from MIN_FILES files on; the intake reads them from LOAD_MIN_FILES files on (the
red team of 2026-10-07 measured two). The mirrors are AWB_MIRRORS or ~/tcp-mirrors on the owner side; the work user
reads them and never writes the lists. An empty corpus writes nothing: a missing or empty mirror must never leave
the intake with fewer known words than before.
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

from awb import config, patterns

WORDS_FILE = patterns.RULES_DIR / "known-words.txt"
PHRASES_FILE = patterns.RULES_DIR / "known-phrases.txt"
MIN_FILES = 2
LOAD_MIN_FILES = 2
MIN_WORDS = 100
"""A corpus that gives fewer words than this is not the mirror; nothing is written."""

_WORD = re.compile(r"(?<![\w-])(?:[A-Z][a-z]{2,}|[A-Z]{4,})(?![\w-])")
_PHRASE = re.compile(r"(?<![\w-])([A-Z][a-z]{2,}|[A-Z]{4,})[ \t]+([A-Z][a-z]{2,}|[A-Z]{4,})(?![\w-])")
_HEAD = "# %s\tfiles\tcount: awb words update, service description %s, %d files of the public mirrors\n"


class WordsError(Exception):
    """The lists cannot be written. The message says why."""


def mirror_root() -> Path:
    """The mirrors of the owner side (the same rule as awb mirror: AWB_MIRRORS or ~/tcp-mirrors)."""
    return Path(os.environ.get("AWB_MIRRORS") or "~/tcp-mirrors").expanduser()


def corpus(root: Path) -> tuple[list[Path], str]:
    """The files of the corpus and the revision of the service description that CURRENT names."""
    files = sorted((root / "docs").rglob("*.rst")) if (root / "docs").is_dir() else []
    revision = ""
    sd = root / "service-description"
    try:
        revision = (sd / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if revision and (sd / revision / "service-description.txt").is_file():
        files.append(sd / revision / "service-description.txt")
    return files, revision


def count(files: list[Path]) -> tuple[Counter, Counter, Counter, Counter, int]:
    words_files, words_count, phrases_files, phrases_count = Counter(), Counter(), Counter(), Counter()
    read = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        read += 1
        ws = _WORD.findall(text)
        words_count.update(ws)
        words_files.update(set(ws))
        ps = [" ".join(m) for m in _PHRASE.findall(text)]
        phrases_count.update(ps)
        phrases_files.update(set(ps))
    return words_files, words_count, phrases_files, phrases_count, read


def _write(path: Path, label: str, revision: str, read: int, files: Counter, counts: Counter) -> int:
    rows = [(w, n) for w, n in files.most_common() if n >= MIN_FILES]
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_HEAD % (label, revision or "none", read))
        for w, n in rows:
            fh.write("%s\t%d\t%d\n" % (w, n, counts[w]))
    os.replace(tmp, path)
    return len(rows)


def update(root: Path | None = None, out: Path | None = None) -> tuple[int, int, str]:
    """Write both lists from the mirrors under `root`. Returns (words, phrases, revision). Refused for the work
    user and for a corpus that is empty or too small, before anything is written."""
    if config.is_work_user():
        raise WordsError("the work user reads the word lists and never writes them; the owner runs awb words update")
    root = root or mirror_root()
    files, revision = corpus(root)
    if not files:
        raise WordsError("no documentation and no service description under the mirror root, nothing was written")
    wf, wc, pf, pc, read = count(files)
    if sum(1 for n in wf.values() if n >= MIN_FILES) < MIN_WORDS:
        raise WordsError("the corpus gave too few words (%d files read), nothing was written" % read)
    words_path = (out / WORDS_FILE.name) if out else WORDS_FILE
    phrases_path = (out / PHRASES_FILE.name) if out else PHRASES_FILE
    n_words = _write(words_path, "word", revision, read, wf, wc)
    n_phrases = _write(phrases_path, "phrase", revision, read, pf, pc)
    return n_words, n_phrases, revision


def load(path: Path, min_files: int = LOAD_MIN_FILES) -> set[str]:
    """The entries of one list (case folded) that stand in `min_files` files or more. Missing -> empty."""
    out: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        try:
            if len(parts) >= 2 and int(parts[1]) >= min_files:
                out.add(parts[0].casefold())
        except ValueError:
            continue
    return out


def revision(path: Path = WORDS_FILE) -> str:
    """The service description revision the list was written from, or empty."""
    try:
        head = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return ""
    m = re.search(r"service description (\S+),", head)
    return m.group(1) if m and m.group(1) != "none" else ""


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb words", description="The vocabulary of the public corpus for the intake (owner side).")
    sub = ap.add_subparsers(dest="command")
    u = sub.add_parser("update", help="rebuild rules/known-words.txt and rules/known-phrases.txt from the mirrors")
    u.add_argument("--root", type=Path, default=None, help="the mirror root (AWB_MIRRORS or ~/tcp-mirrors)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command == "update":
        try:
            words, phrases, rev = update(args.root)
        except WordsError as err:
            print("awb words: %s" % err, file=sys.stderr)
            return 1
        print("awb words: %d words and %d phrases written (service description %s)" % (words, phrases, rev or "none"))
        return 0
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
