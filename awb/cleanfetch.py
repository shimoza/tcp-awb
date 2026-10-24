"""`awb clean fetch tcp-xxxx [--to DIR] [--save]` (task ux2-laptop-fetch, UX review of 2026-10-24, change 2): a
finished deliverable reaches the owner's laptop.

On the host `awb bucket put CODE FILE --reveal` puts the file with the real names back into `<YYYY-MM>/tcp-xxxx/out/`
of the bucket. On the laptop this command lists that `out/` with his own key (AWB_OBS_AK and AWB_OBS_SK), downloads the
files that are not fetched yet into a folder and prints one line per file. It never lists, reads or writes `in/`, and
it goes through HTTPS_PROXY like `--put`.

The folder is `--to DIR`, else the one saved before with `--to DIR --save` (the file `fetch-dir` of the laptop folder),
for example a Windows folder under /mnt/c/Users/<him>/... What was fetched is remembered in `vault/fetched.tsv` of the
laptop (key, etag, day, mode 600): a file counts as fetched while its etag is the same, a file put again with
`--replace` has a new etag and comes again, next to the first under a name with (2). Nothing here prints a customer
name: the lines carry file names and counts.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from awb import laptop

FETCHED = "fetched.tsv"
DIR_FILE = "fetch-dir"


class FetchError(Exception):
    pass


def _fetched_file() -> Path:
    return laptop.vault() / FETCHED


def fetched() -> set[tuple[str, str]]:
    try:
        text = _fetched_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {tuple(line.split("\t")[:2]) for line in text.splitlines() if line.count("\t") >= 2}


def _remember(key: str, etag: str) -> None:
    laptop.init()
    fd = os.open(_fetched_file(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write("%s\t%s\t%s\n" % (key, etag, date.today().isoformat()))


def saved_dir() -> Path | None:
    try:
        text = (laptop.home() / DIR_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text).expanduser() if text else None


def save_dir(folder: Path) -> None:
    laptop.init()
    fd = os.open(laptop.home() / DIR_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(folder) + "\n")


def fetch(project: str, to: Path | None, *, client=None, save: bool = False, say=print) -> int:
    """The new files of out/ of `project` into the folder. Returns the number of files that failed."""
    from awb import bucket, bucket_layout, clean, obs

    laptop.customer_of(project)                       # a project code reads tcp-xxxx
    folder = to if to is not None else saved_dir()
    if folder is None:
        raise FetchError("give the folder with --to DIR (add --save to keep it for the next time)")
    folder = Path(os.path.abspath(folder.expanduser()))
    c = client or clean._client("fetch")
    try:
        base = bucket.find_folder(c, project)
        if base is None:
            raise FetchError("%s has no folder in the bucket yet" % project)
        prefix = base + bucket_layout.OUT                       # in/ is never asked for
        objects = [(k, s, e) for k, s, e in c.list(prefix).objects
                   if k.startswith(prefix) and not k.endswith("/") and k.rsplit("/", 1)[-1] != ".keep"]
        folder.mkdir(parents=True, exist_ok=True)
        if save:
            save_dir(folder)
        if not objects:
            say("%s: out/ holds no file yet" % project)
            return 0
        known, failed = fetched(), 0
        for key, size, etag in sorted(objects):
            label = key[len(prefix):]
            if (key, etag) in known:
                say("%s: already fetched" % label)
                continue
            name = bucket._local_name(label, folder)
            part = folder / (".%s.part" % name)
            try:
                c.get(key, part, if_match=etag)
                try:
                    os.chmod(part, 0o600)
                except OSError:
                    pass                                         # a Windows folder keeps no mode
                os.replace(part, folder / name)
            except (obs.OBSError, OSError) as err:
                failed += 1
                part.unlink(missing_ok=True)
                say("%s: failed (%s)" % (label, err if isinstance(err, obs.OBSError) else type(err).__name__))
                continue
            _remember(key, etag)
            say("%s: fetched, %d bytes%s" % (label, size, "" if name == label else " as " + name))
        return failed
    except (obs.OBSError, bucket.BucketError) as err:
        raise FetchError("the bucket: %s" % err) from None
