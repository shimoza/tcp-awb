"""`awb fetch CODE`: what a project put for the owner, in one folder, with the names back (owner side, P12 of F3).

    awb fetch CODE [--again] [--into DIR]

A session puts a result with `awb xchg put` into `<code>/from-session/<date>/<id>` of the lab bucket and the owner
gets one mail per file. This command lists what the project put since his last fetch, downloads each file through
the key service (the op `obs` LIST and GET a session's `awb xchg list` uses, no new call), puts the real names back
into every text with the code of `awb reveal` and prints the folder. A file that is not a text is kept as it came.

- The folder is `~/awb-fetch/<code>/` (or DIR), mode 700, files mode 600. It is refused inside a git tree and in a
  place a working session reads (the shared side, the knowledge base, a registered project), as `awb reveal` is.
- A file counts as fetched once it was downloaded (its key, kept in `bucket-pulled.tsv` of the vault beside the
  files of `awb bucket pull`). `--again` takes every file again; a file of the same name gets a number.
- The objects stay in the bucket: `awb xchg clean` in the session deletes them, as before.

Owner side only: refused for the work user and inside an assistant session. Output carries the project code, the
object ids, counts and the folder, never a name.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from awb import codes, config

FETCH_DIR = "awb-fetch"
"""The folder under the owner's home that holds one folder per project code."""
MARK = "fetched"
"""The second column of a fetch row in bucket-pulled.tsv (an etag column there): the lab bucket gives none."""

_REL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/put-[0-9a-f]{12}(?:\.[a-z0-9]{1,5})?$")


class FetchError(Exception):
    """The fetch cannot run. The message carries codes and counts, never a name."""


def default_folder(code: str) -> Path:
    return Path.home() / FETCH_DIR / code


def _in_git(folder: Path) -> bool:
    return any((d / ".git").exists() for d in (folder, *folder.parents))


def _owner_side(code: str) -> None:
    from awb import reveal, vault

    if config.is_work_user() or vault.in_assistant_session():
        raise FetchError("awb fetch runs on the owner side, in your own terminal")
    try:
        reveal.owner_side()
    except reveal.RevealError as err:
        raise FetchError(str(err)) from None
    if not codes.is_project_code(code or ""):
        raise FetchError("a project code reads tcp-xxxx")


def prepare(p: config.Paths, folder: Path) -> Path:
    """The target folder, made with mode 700. Refused inside a git tree and where a working session reads."""
    from awb import reveal

    folder = Path(os.path.realpath(folder))
    if _in_git(folder):
        raise FetchError("the folder lies inside a git tree: give --into a folder outside every repository")
    for place in reveal.session_places(p):
        if folder == place or reveal._inside(folder, place):
            raise FetchError("the named texts must not go where a working session reads them")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def listing(code: str) -> list[dict]:
    """What the project put: {"name": "<date>/<id>", "size": n} for each object under <code>/from-session/."""
    from awb.tcp import xchg

    a = xchg._ok(xchg._call({"op": "obs", "method": "LIST", "prefix": "%s/%s" % (code, xchg.FROM), "project": code}))
    return [o for o in a.get("objects", []) if isinstance(o.get("name"), str) and _REL_RE.match(o["name"])]


def fetch(p: config.Paths, code: str, folder: Path | None = None, *, again: bool = False) -> dict:
    """Download the new puts of `code` into `folder` and reveal the texts. Returns the folder, the count fetched
    before and one (id, what) per file: what is the count of codes replaced or None for a file kept as it came."""
    from awb import bucket, projects, reveal
    from awb.tcp import xchg

    _owner_side(code)
    project = next((r for r in projects.load(p) if r.code == code), None)
    if project is None or project.state != "active":
        raise FetchError("%s is not an active project" % code)
    objects = listing(code)
    done = set() if again else {k for k, mark in bucket.pulled(p) if mark == MARK}
    keys = ["%s/%s%s" % (code, xchg.FROM, o["name"]) for o in objects]
    fresh = [(o, k) for o, k in zip(objects, keys) if k not in done]
    result = {"folder": None, "known": len(objects) - len(fresh), "files": []}
    if not fresh:
        return result
    target = prepare(p, folder or default_folder(code))
    result["folder"] = target
    tmp_root = config.make_dir(p.vault / "tmp", 0o700)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        for o, key in fresh:
            ident = o["name"].rsplit("/", 1)[-1]
            got = Path(tmp) / ident
            a = xchg._ok(xchg._call({"op": "obs", "method": "GET", "key": key, "project": code}, download=got))
            if not a.get("exists") or not got.is_file():
                continue                    # deleted between the list and the get (awb xchg clean)
            out = target / bucket._local_name(ident, target)
            try:
                text = got.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                text = None
            if text is None:
                shutil.copyfile(got, out)
                os.chmod(out, 0o600)
                result["files"].append((out.name, None))
            else:
                stats = reveal.write_named(text, out, p)
                result["files"].append((out.name, stats["replaced"]))
            bucket._remember(p, key, MARK)
    return result


def main(argv: list[str] | None = None) -> int:
    """`awb fetch CODE [--again] [--into DIR]`. Exit 0 done (also when nothing is new), 2 refused or an error."""
    from awb import obs, projects, register, reveal
    from awb.cli import SafeParser
    from awb.tcp import xchg

    ap = SafeParser(prog="awb fetch", description="What a project put for you, in one folder with the names back "
                                                  "(owner side).")
    ap.add_argument("code", metavar="CODE", help="the project code, tcp-xxxx")
    ap.add_argument("--again", action="store_true", help="take every file again, also those fetched before")
    ap.add_argument("--into", type=Path, default=None, metavar="DIR",
                    help="the folder (default ~/%s/CODE/), outside every git tree" % FETCH_DIR)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        r = fetch(config.paths(), args.code, args.into, again=args.again)
    except (FetchError, xchg.XchgError, reveal.RevealError, register.RegisterError, projects.ProjectError,
            obs.OBSError) as err:
        print("awb fetch: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb fetch: %s (the key service or the folder)" % type(err).__name__, file=sys.stderr)
        return 2
    print("%s: %d new file(s) from from-session/, %d fetched before" % (args.code, len(r["files"]), r["known"]))
    for name, replaced in r["files"]:
        print("  %s  %s" % (name, "kept as it came (not a text)" if replaced is None
                            else "names put back (%d codes replaced)" % replaced))
    if r["folder"] is not None:
        print("folder: %s" % r["folder"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
