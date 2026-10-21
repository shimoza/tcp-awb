"""Step-by-step how-tos for the TCP console, drafted from the console mirror and never from memory.

    awb howto draft "TOPIC" [--service SVC] [--step WORDS]... [--limit N] [--walk DATE] [--name NAME] [--force]
    awb howto render FILE [--pdf]

draft finds the screens of the topic in the console mirror (awb/tcp/console_mirror.py; the published copy for the
work user, ~/tcp-mirrors for the owner), orders them by service, menu path and step number and writes a Markdown
draft into the deliverables folder of the project: a title, a short "before you start", then one numbered step per
screen. A step names the menu path, the field and button labels as the INDEX note spells them, cites the screen as
`SVC/NN-slug, console mirror DATE` and embeds its screenshot from an `images/` folder next to the draft, where only
the cited images are copied. With --step WORDS (repeated) each step is searched on its own and the best screen is
the step; a step the mirror does not hold or whose image was dropped, reads "not in the mirror yet: check on
screen". The draft says the date of the walk. A draft that exists is never overwritten without --force: the
session edits its text and never invents a label.

render turns the draft into one self-contained HTML file next to it, the images inline as data URIs. It reads a
small Markdown subset (headings, paragraphs, lists, bold, italic, code, images) and nothing else. A PDF has no
standard-library route (reportlab is test-only and poppler cannot read HTML): --pdf writes the HTML, says so and
exits 1; print the HTML from a browser until that is built. The screenshots are private: the result goes out with
`awb xchg put --image --reason TEXT` to a customer or partner by mail, never into a public repository.
"""
from __future__ import annotations

import base64
import html
import os
import re
import shutil
import sys
from pathlib import Path

from awb.tcp import console_mirror
from awb.tcp.console_mirror import ConsoleError, Screen

MISSING = "not in the mirror yet: check on screen"
IMAGES = "images"
DELIVERABLES = "deliverables"
DEFAULT_LIMIT = 8
MAX_IMAGE = 8 * 1024 * 1024
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_NUM_RE = re.compile(r"^(\d{1,4})-")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


class HowtoError(Exception):
    pass


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return out or "console"


def _order(s: Screen) -> tuple:
    m = _NUM_RE.match(s.stem)
    return (s.service.lower(), [p.strip().lower() for p in s.menu.split(">")], int(m.group(1)) if m else 0, s.file)


def _pick(base: Path, walk: str, topic: str, service: str | None, steps: list[str],
          limit: int) -> list[tuple[str, Screen | None]]:
    """[(what the step is about, screen or None)] in reading order. Without steps the screens whose score is at
    least half of the best, at most `limit`, ordered by menu path and number; with steps one screen each, in the
    order the steps were given."""
    if steps:
        out: list[tuple[str, Screen | None]] = []
        for words in steps:
            _, hits = console_mirror.find(base, words, service=service, limit=1, walk=walk)
            out.append((words, hits[0][1] if hits else None))
        return out
    _, hits = console_mirror.find(base, topic, service=service, limit=max(limit, 1), walk=walk)
    if not hits:
        return [(topic, None)]
    top = hits[0][0]
    chosen = sorted((h[1] for h in hits if h[0] * 2 >= top), key=_order)
    return [(s.title or s.menu, s) for s in chosen]


def _labels(note: str) -> list[str]:
    """The INDEX note as lines: `fields: a, b; buttons: c` becomes one line per group, anything else stays as it is."""
    parts = [p.strip() for p in note.split(";") if p.strip()]
    lines = []
    for p in parts:
        head, sep, rest = p.partition(":")
        items = [i.strip() for i in rest.split(",") if i.strip()]
        if sep and items and len(head.split()) <= 2:
            lines.append("%s: %s" % (head.strip().capitalize(), ", ".join("`%s`" % i for i in items)))
        else:
            lines.append("On the screen: %s" % p)
    return lines


def _step(n: int, about: str, s: Screen | None, walk: str, base: Path, images: Path) -> list[str]:
    if s is None:
        return ["%d. **%s**" % (n, about), "   - Screen: %s" % MISSING, ""]
    lines = ["%d. **%s**" % (n, s.title or about),
             "   - Menu path: %s" % (s.menu or "not in the INDEX"),
             "   - Screen: `%s, console mirror %s`" % (s.ref, walk)]
    lines += ["   - %s" % x for x in _labels(s.note)]
    src = console_mirror.image_path(base, walk, s)
    if src is None:
        lines.append("   - Screenshot: %s" % MISSING)
    else:
        name = "%s-%s%s" % (s.service, s.stem, src.suffix)
        images.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, images / name)
        lines += ["", "   ![%s](%s/%s)" % (s.title or s.stem, IMAGES, name)]
    return lines + [""]


def draft(topic: str, *, base: Path, project: Path, service: str | None = None, steps: list[str] | None = None,
          limit: int = DEFAULT_LIMIT, walk: str | None = None, name: str | None = None,
          force: bool = False) -> tuple[Path, int, int]:
    """Write the draft; (path, steps, lines that say the mirror lacks something)."""
    topic = " ".join(topic.split())
    if not topic:
        raise HowtoError("a how-to needs a topic")
    if not (project / "SCOPE.md").is_file():
        raise HowtoError("not inside a project (no SCOPE.md)")
    day = console_mirror.pick_walk(base, walk)
    chosen = _pick(base, day, topic, service, [" ".join(s.split()) for s in steps or [] if s.strip()], limit)
    stem = _slug(name or topic)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", stem):
        raise HowtoError("a draft name is letters, digits and dashes")
    out_dir = project / DELIVERABLES
    target = out_dir / ("howto-%s.md" % stem)
    if target.exists() and not force:
        raise HowtoError("%s exists; edit it or use --force to draft again" % target.relative_to(project))
    if os.path.islink(out_dir) or os.path.islink(target):
        raise HowtoError("a link stands where the deliverables folder or the draft should be")
    out_dir.mkdir(parents=True, exist_ok=True)
    services = sorted({s.service for _, s in chosen if s})
    text = ["# How to: %s" % topic, "", "## Before you start", "",
            "- You are signed in to the TCP console.",
            "- Your account may use %s." % (("the service %s" % services[0]) if len(services) == 1 else
                                            ("the services %s" % ", ".join(services)) if services else
                                            "the service of this topic"),
            "- The screens are from the console mirror walk of %s. A later console may look different." % day,
            "", "## Steps", ""]
    for n, (about, s) in enumerate(chosen, start=1):
        text += _step(n, about, s, day, base, out_dir / IMAGES)
    target.write_text("\n".join(text).rstrip("\n") + "\n", encoding="utf-8")
    return target, len(chosen), sum(1 for line in text if MISSING in line)


# --------------------------------------------------------------------------- render

_CSS = ("body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b}"
        "img{max-width:100%;border:1px solid #bbb;margin:.5rem 0}code{background:#f0f0f0;padding:0 .2em}"
        "li{margin:.8rem 0}")


def _inline(text: str, root: Path, problems: list[str]) -> str:
    parts = _IMG_RE.split(text)
    out = []
    # split() yields text, alt, path, text, ...: escape the text parts, build the images
    for i in range(0, len(parts), 3):
        chunk = html.escape(parts[i], quote=False)
        chunk = re.sub(r"`([^`]+)`", r"<code>\1</code>", chunk)
        chunk = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", chunk)
        chunk = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\w)", r"<em>\1</em>", chunk)
        out.append(chunk)
        if i + 2 < len(parts):
            out.append(_data_image(parts[i + 1], parts[i + 2], root, problems))
    return "".join(out)


def _data_image(alt: str, rel: str, root: Path, problems: list[str]) -> str:
    p = Path(rel)
    ext = p.suffix.lower()
    if p.is_absolute() or ".." in p.parts or "://" in rel or ext not in _MIME:
        problems.append("image %s: only a png or jpg below the draft's folder" % rel)
        return html.escape("[image refused: %s]" % rel)
    full = root / p
    try:
        if os.path.islink(full) or not full.resolve().is_relative_to(root.resolve()):
            raise OSError
        if full.stat().st_size > MAX_IMAGE:
            problems.append("image %s: larger than %d bytes" % (rel, MAX_IMAGE))
            return html.escape("[image too large: %s]" % rel)
        data = full.read_bytes()
    except OSError:
        problems.append("image %s: not there" % rel)
        return html.escape("[image missing: %s]" % rel)
    return '<img alt="%s" src="data:%s;base64,%s">' % (html.escape(alt, quote=True), _MIME[ext],
                                                       base64.b64encode(data).decode("ascii"))


def to_html(markdown: str, root: Path) -> tuple[str, list[str]]:
    """(one HTML document, problems). `root` is the folder the image paths are relative to."""
    problems: list[str] = []
    body: list[str] = []
    title = "How-to"
    stack: list[str] = []            # open list tags

    def close() -> None:
        while stack:
            body.append("</li></%s>" % stack.pop())

    para: list[str] = []

    def flush() -> None:
        if para:
            body.append("<p>%s</p>" % _inline(" ".join(para), root, problems))
            para.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        ol = re.match(r"^(\d+)\.\s+(.*)$", line)
        ul = re.match(r"^\s+[-*]\s+(.*)$|^[-*]\s+(.*)$", line)
        cont = re.match(r"^\s{2,}(\S.*)$", line)
        if h:
            flush()
            close()
            n = len(h.group(1))
            if n == 1:
                title = re.sub(r"[*`]", "", h.group(2))
            body.append("<h%d>%s</h%d>" % (n, _inline(h.group(2), root, problems), n))
        elif ol:
            flush()
            if stack[:1] != ["ol"]:
                close()
                body.append("<ol>")
                stack.append("ol")
            elif len(stack) > 1:
                body.append("</li></ul>")
                stack.pop()
                body.append("</li>")
            else:
                body.append("</li>")
            body.append("<li>%s" % _inline(ol.group(2), root, problems))
        elif ul:
            flush()
            item = ul.group(1) or ul.group(2)
            nested = line[:1] in " \t" and stack[:1] == ["ol"]
            if nested and len(stack) == 1:
                body.append("<ul>")
                stack.append("ul")
                body.append("<li>%s" % _inline(item, root, problems))
            elif nested:
                body.append("</li><li>%s" % _inline(item, root, problems))
            else:
                if stack[:1] != ["ul"] or len(stack) > 1:
                    close()
                    body.append("<ul>")
                    stack.append("ul")
                    body.append("<li>%s" % _inline(item, root, problems))
                else:
                    body.append("</li><li>%s" % _inline(item, root, problems))
        elif cont and stack:
            body.append("<div>%s</div>" % _inline(cont.group(1), root, problems))
        elif not line.strip():
            flush()
        else:
            close()
            para.append(line.strip())
    flush()
    close()
    doc = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>%s</title>'
           '<meta name="viewport" content="width=device-width,initial-scale=1"><style>%s</style></head>'
           '<body>\n%s\n</body></html>\n') % (html.escape(title), _CSS, "\n".join(body))
    return doc, problems


def render(path: Path) -> tuple[Path, list[str], int]:
    """Write FILE.html beside the draft; (path, problems, steps still marked as missing). Nothing is written when
    an image is missing, refused or too large."""
    if path.suffix.lower() != ".md" or not path.is_file() or os.path.islink(path):
        raise HowtoError("render takes a Markdown draft, as `awb howto draft` wrote it")
    text = path.read_text(encoding="utf-8")
    doc, problems = to_html(text, path.parent)
    if problems:
        raise HowtoError("; ".join(problems))
    out = path.with_suffix(".html")
    out.write_text(doc, encoding="utf-8")
    return out, problems, text.count(MISSING)


def main(argv: list[str] | None = None) -> int:
    from awb import review
    from awb.cli import SafeParser
    from awb.tcp import mirror

    ap = SafeParser(prog="awb howto", description="Step-by-step console how-tos from the console mirror.")
    sub = ap.add_subparsers(dest="command")
    d = sub.add_parser("draft", help="a Markdown draft with the screens of the topic, into the project's deliverables")
    d.add_argument("topic", help="what the how-to is about")
    d.add_argument("--service", default=None, help="only this service")
    d.add_argument("--step", action="append", default=[], metavar="WORDS",
                   help="one step by its words, the best screen is taken; repeat for each step in order")
    d.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="at most this many screens")
    d.add_argument("--walk", default=None, help="a walk date (default: the newest)")
    d.add_argument("--name", default=None, help="the file is deliverables/howto-NAME.md (default: from the topic)")
    d.add_argument("--force", action="store_true", help="draft again over an existing draft")
    r = sub.add_parser("render", help="one self-contained HTML file of a draft, images inline")
    r.add_argument("file", help="the Markdown draft")
    r.add_argument("--pdf", action="store_true", help="also a PDF (waits: HTML only for now)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    try:
        if args.command == "draft":
            path, n, lacking = draft(args.topic, base=mirror.root(), project=review.find_project(),
                                     service=args.service, steps=args.step, limit=args.limit, walk=args.walk,
                                     name=args.name, force=args.force)
            print("howto: %d step(s), %d not in the mirror yet, written to %s" % (n, lacking, path))
            print("next: edit the text, never invent a label; `awb howto render %s`" % path)
            return 0
        out, _, lacking = render(Path(args.file))
        print("howto: %s" % out)
        if lacking:
            print("note: %d step(s) still say %r" % (lacking, MISSING), file=sys.stderr)
        if args.pdf:
            print("howto: no PDF yet, there is no standard-library route; print the HTML from a browser",
                  file=sys.stderr)
            return 1
        print("next: awb xchg put %s --image --reason \"console screenshots of the mirror, masked text only\"" % out)
        return 0
    except (HowtoError, ConsoleError) as err:
        print("awb howto: %s" % err, file=sys.stderr)
        return 2
