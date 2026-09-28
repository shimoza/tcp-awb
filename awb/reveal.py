"""Put the real names back into a finished text (T-04), on the owner side only.

    awb reveal FILE --out PATH [--replace]

Every register code in the text becomes the display form of that code: its first active form in the register,
the full name he registers first. A retired code and a code the register does not know (a token of the intake
such as MAIL-XXXX) stay as they are and are counted. The named text goes to a file of his choice with mode 600,
never to standard output and never into a place a working session reads: the shared side, the knowledge base or
a registered project.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from awb import codes, config, register


class RevealError(Exception):
    """The names cannot be put back. The message carries counts and codes, never a form."""


def display_forms(entries) -> tuple[dict[str, str], set[str]]:
    """(code -> its first active form in register order, codes that have retired forms only)."""
    forms: dict[str, str] = {}
    retired: set[str] = set()
    for e in entries:
        if e.status == "active":
            forms.setdefault(e.code, e.form)
        else:
            retired.add(e.code)
    return forms, retired - set(forms)


def reveal_text(text: str, entries) -> tuple[str, dict[str, int]]:
    """`text` with every known code replaced by its display form; the counts replaced, retired, unknown."""
    forms, retired = display_forms(entries)
    stats = {"replaced": 0, "retired": 0, "unknown": 0}

    def one(m) -> str:
        code = m.group(0)
        if code in forms:
            stats["replaced"] += 1
            return forms[code]
        stats["retired" if code in retired else "unknown"] += 1
        return code

    return codes.PLACEHOLDER_RE.sub(one, text), stats


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def session_places(p: config.Paths) -> list[Path]:
    """The folders a working session reads: a named text must never land in one of them."""
    places = [p.shared, p.kb]
    try:
        from awb import projects
        places += [Path(r.path) for r in projects.load(p)]
    except Exception:
        pass
    return [Path(os.path.realpath(x)) for x in places]


def owner_side() -> None:
    if config.is_work_user():
        raise RevealError("the names are put back on the owner side only, in your own shell")


def write_named(text: str, out: Path, p: config.Paths, *, replace: bool = False) -> dict[str, int]:
    """Reveal `text` into `out` (mode 600). Refused for a place a session reads and for an existing file
    unless `replace`."""
    owner_side()
    out = Path(os.path.realpath(out))
    for place in session_places(p):
        if _inside(out, place):
            raise RevealError("the named text must not go where a working session reads it")
    named, stats = reveal_text(text, register.load(p.register))
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    try:
        fd = os.open(out, flags, 0o600)
    except FileExistsError:
        raise RevealError("the output file exists; give --replace to overwrite it") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(named)
    os.chmod(out, 0o600)
    return stats


def main(argv: list[str] | None = None) -> int:
    """`awb reveal FILE --out PATH [--replace]`. Exit 0 written, 2 refused or an error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb reveal", description="Put the real names back into a finished text (owner side).")
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="where the named text goes (mode 600)")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing output file")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        text = args.file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        print("awb reveal: the file cannot be read as text (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    try:
        stats = write_named(text, args.out, config.paths(), replace=args.replace)
    except (RevealError, register.RegisterError) as err:
        print("awb reveal: %s" % err, file=sys.stderr)
        return 2
    left = stats["retired"] + stats["unknown"]
    print("awb reveal: %d codes replaced, %d left as they are (%d retired, %d not in the register); written to %s"
          % (stats["replaced"], left, stats["retired"], stats["unknown"], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
