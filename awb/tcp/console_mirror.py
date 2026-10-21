"""The private console reference: the owner's own searchable source for console how-tos, read only here.

    awb mirror console find WORDS [--service SVC] [--limit N] [--walk DATE]
    awb mirror console show SVC/NN-slug [--walk DATE]
    awb mirror console publish [--to DIR] [--walk DATE]            (owner side)

<root>/console/<date>/ is one walk. It holds INDEX.tsv, one line per screen with seven tab-separated columns (service,
file name, menu path, URL path with ids as {id}, page title, date, a note listing the visible field labels, tabs and
buttons), and per service a folder with NN-slug.jpg (or .png) and NN-slug.txt, the masked page text. The extension
"none" in the file column means the image was dropped for a leak. The mirror is private: never in the public dataset,
never in git. It is read only here, so the work user reads it too; the owner side fills it.

find ranks the screens by word overlap (the words of `awb kb find`) over the note, the menu path, the title and the
text, and prints service, file, menu path, URL path and the first matching line. A walk is the newest unless --walk
names one. show prints the INDEX line, the text and the image path. `awb howto draft` keeps the sources of a how-to
in the internal note reviews/<name>/sources.md and never in the text it writes.

publish copies the newest walk from the owner's ~/tcp-mirrors to <place>/console/<date>/ (folders 0750, files 0640,
group of the work user), where the work user reads it: the place is --to, AWB_MIRRORS_PUBLISH or the `mirrors` of
the host file (/srv/tcp-mirrors), the same root `find` and `show` read for the work user. seal/setup.sh makes
<place>/console owned by the owner with the group of the work user and the setgid bit, so the work user can read
and cannot write there.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import NamedTuple

CONSOLE = "console"
INDEX_TSV = "INDEX.tsv"
COLUMNS = 7
DEFAULT_LIMIT = 10
MAX_TEXT = 1024 * 1024                      # one screen text, read at most this far
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SVC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})/(\d{1,4}-[A-Za-z0-9._-]{1,150})$")
_IMAGE_EXT = ("jpg", "png")
WEIGHTS = {"title": 3.0, "menu": 2.0, "note": 2.0, "text": 1.0, "url": 1.0}


class ConsoleError(Exception):
    pass


class Screen(NamedTuple):
    service: str
    file: str
    menu: str
    url: str
    title: str
    date: str
    note: str

    @property
    def stem(self) -> str:
        return self.file.rsplit(".", 1)[0] if "." in self.file else self.file

    @property
    def ref(self) -> str:
        return "%s/%s" % (self.service, self.stem)


def walks(base: Path) -> list[str]:
    """The walks that have an INDEX.tsv, oldest first."""
    folder = base / CONSOLE
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if _DATE_RE.match(p.name) and (p / INDEX_TSV).is_file())


def pick_walk(base: Path, walk: str | None = None) -> str:
    have = walks(base)
    if walk:
        if walk not in have:
            raise ConsoleError("no console walk %s in %s" % (walk, base / CONSOLE))
        return walk
    if not have:
        raise ConsoleError("no console mirror in %s" % (base / CONSOLE))
    return have[-1]


def load_index(base: Path, walk: str) -> list[Screen]:
    """The screens of a walk. A line with fewer than seven columns and the header line are skipped."""
    out: list[Screen] = []
    path = base / CONSOLE / walk / INDEX_TSV
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        cols = line.split("\t")
        if len(cols) < COLUMNS or cols[0].strip().lower() == "service" or not _SVC_RE.match(cols[0].strip()):
            continue
        out.append(Screen(*[c.strip() for c in cols[:COLUMNS]]))
    return out


def screen_text(base: Path, walk: str, s: Screen) -> str:
    p = base / CONSOLE / walk / s.service / (s.stem + ".txt")
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_TEXT)
    except OSError:
        return ""


def image_path(base: Path, walk: str, s: Screen) -> Path | None:
    for ext in _IMAGE_EXT:
        p = base / CONSOLE / walk / s.service / ("%s.%s" % (s.stem, ext))
        if p.is_file():
            return p
    return None


def find(base: Path, query: str, *, service: str | None = None, limit: int = DEFAULT_LIMIT,
         walk: str | None = None) -> tuple[str, list[tuple[float, Screen, str]]]:
    """(walk, [(score, screen, first matching line)]) best first. The score is the weighted count of query words
    found in each field; a screen with no query word is left out."""
    from awb import kb

    q = kb.words(query)
    if not q:
        raise ConsoleError("no search word in %r" % query)
    day = pick_walk(base, walk)
    hits: list[tuple[float, Screen, str]] = []
    for s in load_index(base, day):
        if service and s.service.lower() != service.lower():
            continue
        text = screen_text(base, day, s)
        fields = {"title": s.title, "menu": s.menu, "note": s.note, "text": text, "url": s.url}
        score = sum(WEIGHTS[k] * len(q & kb.words(v)) for k, v in fields.items())
        if score <= 0:
            continue
        line = next((ln.strip() for ln in text.splitlines() if ln.strip() and q & kb.words(ln)), "")
        hits.append((score, s, line or s.note))
    hits.sort(key=lambda h: (-h[0], h[1].service, h[1].file))
    return day, hits[:max(limit, 0)]


def show(base: Path, ref: str, *, walk: str | None = None) -> list[str]:
    m = _REF_RE.match(ref)
    if not m:
        raise ConsoleError("a screen is named SERVICE/NN-slug, as `find` prints it")
    day = pick_walk(base, walk)
    s = next((x for x in load_index(base, day) if x.ref == ref), None)
    if s is None:
        raise ConsoleError("no screen %s in the walk %s" % (ref, day))
    img = image_path(base, day, s)
    lines = ["walk: %s" % day, "service: %s" % s.service, "file: %s" % s.file, "menu path: %s" % s.menu,
             "url path: %s" % s.url, "title: %s" % s.title, "date: %s" % s.date, "note: %s" % s.note,
             "image: %s" % (img if img else "none (dropped for a leak, or not in the mirror)"), "", "text:"]
    return lines + (screen_text(base, day, s).splitlines() or ["(no text file)"])


def status(base: Path) -> str:
    have = walks(base)
    if not have:
        return "console screens: none"
    screens = load_index(base, have[-1])
    return "console screens: %d walk(s), newest %s, %d screens of %d services, in %s" % (
        len(have), have[-1], len(screens), len({s.service for s in screens}), base / CONSOLE)


# --------------------------------------------------------------------------- publish (owner side)

PUBLISH_ENV = "AWB_MIRRORS_PUBLISH"
_COPY_EXT = (".jpg", ".png", ".txt")


def publish_root() -> Path:
    """Where the work user reads the console mirror: AWB_MIRRORS_PUBLISH, else the `mirrors` of the host file,
    else /srv/tcp-mirrors. seal/setup.sh makes <that>/console owned by the owner, group of the work user, so the
    work user reads it and cannot write into it."""
    from awb import config
    from awb.tcp import mirror

    env = os.environ.get(PUBLISH_ENV)
    return Path(env).expanduser() if env else Path(config.host_conf().get("mirrors") or mirror.SEALED_ROOT)


def publish(src: Path, dest: Path, *, walk: str | None = None) -> tuple[str, int]:
    """Copy the newest walk (or `walk`) of `src` to <dest>/console/<date>/: folders 0750, files 0640, only INDEX.tsv
    and the jpg, png and txt files below a service folder, no link followed. The copy is built beside the target
    and renamed into place, so a reader never sees half a walk and a second run replaces the walk. Returns (date,
    files copied)."""
    from awb import config

    day = pick_walk(src, walk)
    if src.resolve() == dest.resolve():
        raise ConsoleError("the published place is the mirror itself")
    top = dest / CONSOLE
    if not top.is_dir():
        raise ConsoleError("no %s: run seal/setup.sh (it makes the folder) or name another place with --to" % top)
    walk_dir = src / CONSOLE / day
    stage = top / (".%s.new" % day)
    old = top / (".%s.old" % day)
    for leftover in (stage, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    config.make_dir(stage, 0o750, shared=True)
    count = 0
    try:
        for entry in sorted(os.scandir(walk_dir), key=lambda e: e.name):
            if entry.is_symlink():
                continue
            if entry.is_file() and entry.name == INDEX_TSV:
                _copy_file(Path(entry.path), stage / entry.name)
                count += 1
            elif entry.is_dir() and _SVC_RE.match(entry.name):
                sdir = config.make_dir(stage / entry.name, 0o750, shared=True)
                for f in sorted(os.scandir(entry.path), key=lambda e: e.name):
                    if f.is_file(follow_symlinks=False) and f.name.lower().endswith(_COPY_EXT):
                        _copy_file(Path(f.path), sdir / f.name)
                        count += 1
        final = top / day
        if final.exists():
            os.rename(final, old)
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return day, count


def _copy_file(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o640)


def main(args, base: Path) -> int:
    """`awb mirror console find|show|publish`; `args` is the parsed namespace of awb.tcp.mirror."""
    try:
        if args.console_command == "publish":
            from awb.tcp import mirror

            try:
                mirror.owner_side()
            except mirror.MirrorError as err:
                raise ConsoleError(str(err)) from None
            dest = Path(args.to).expanduser() if args.to else publish_root()
            day, n = publish(base, dest, walk=args.walk)
            print("published the walk %s: %d files to %s" % (day, n, dest / CONSOLE / day))
            return 0
        if args.console_command == "find":
            day, hits = find(base, " ".join(args.words), service=args.service, limit=args.limit, walk=args.walk)
            if not hits:
                print("no console screen matches (walk %s)" % day)
                return 0
            print("walk %s" % day)
            for score, s, line in hits:
                print("%s\t%s\t%s\t%s\t%s" % (s.service, s.file, s.menu, s.url, line))
            return 0
        for line in show(base, args.ref, walk=args.walk):
            print(line)
        return 0
    except ConsoleError as err:
        print("awb mirror console: %s" % err, file=sys.stderr)
        return 2
