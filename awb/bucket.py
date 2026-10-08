"""The owner's bucket: one working folder per project, `<YYYY-MM>/<project-code>/` with `in/` for his originals
and `out/` for results (his decisions of 2026-09-24).

    awb bucket sync                         the folders of every active project; folders that match no project
    awb bucket folder CODE                  the folder of one project, created when missing
    awb bucket pull CODE [--again]          new files of in/ into the vault inbox, then the intake of its customer
    awb bucket put CODE FILE [--replace] [--reveal]
                                            FILE into out/; --reveal puts the real names back first
    awb bucket move <YYYY-MM>/<NAME>/ CODE  a folder whose name drifted to <YYYY-MM>/<CODE>/: copy, check, delete

A folder is looked for under every month and used where it is. A new one goes under the month the project was
created. The project code is the fixed point: a drifted folder is renamed in the bucket, never the project.

Owner side only. The bucket takes the owner's IAM user alone. A working session must never see an original before
the intake: as the work user of a sealed host every command refuses. `pull` and `put --reveal` read the
vault, so he runs them in his own shell.

Settings, from the environment or the host file: AWB_BUCKET / bucket (default awb), AWB_BUCKET_REGION /
bucket_region (default eu-de), AWB_BUCKET_KEYS / bucket_keys (pass:<entry of the password store>, required).
AWB_BUCKET_ENDPOINT points at a stand-in (tests). Output carries codes, months, counts and sizes, never the name
of a file of in/ and never the name of a folder that matches no project.
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import re
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from awb import config, obs

IN = "in/"
OUT = "out/"
PULLED = "bucket-pulled.tsv"
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])/")
_UNSAFE_RE = re.compile(r"[\x00-\x1f/\\\x7f]")


class BucketError(Exception):
    """A bucket step cannot be done. The message carries codes, months and counts only."""


# --------------------------------------------------------------------------- settings and client


def settings() -> dict[str, str | None]:
    if config.is_work_user():
        raise BucketError("the bucket is the owner's: run awb bucket in your own shell")
    conf = config.host_conf()

    def get(env: str, key: str, default: str | None) -> str | None:
        return os.environ.get(env) or conf.get(key) or default

    return {"bucket": get("AWB_BUCKET", "bucket", "awb"), "region": get("AWB_BUCKET_REGION", "bucket_region", "eu-de"),
            "keys": get("AWB_BUCKET_KEYS", "bucket_keys", None), "endpoint": os.environ.get("AWB_BUCKET_ENDPOINT")}


def client() -> obs.Client:
    s = settings()
    if not s["keys"]:
        raise BucketError("no key setting: set AWB_BUCKET_KEYS=pass:<entry of your password store>")
    return obs.Client(s["bucket"], obs.keys_from_reference(s["keys"]), s["region"], endpoint=s["endpoint"])


# --------------------------------------------------------------------------- the layout


def months(c: obs.Client) -> list[str]:
    return [m for m in c.list("", "/").prefixes if _MONTH_RE.fullmatch(m)]


def project_folders(c: obs.Client) -> list[tuple[str, str]]:
    """(month folder, name) of every folder one level below a month."""
    out = []
    for month in months(c):
        for pref in c.list(month, "/").prefixes:
            out.append((month, pref[len(month):].rstrip("/")))
    return out


def find_folder(c: obs.Client, code: str) -> str | None:
    """The folder of a project under any month, None when there is none. Refused when two months hold one."""
    found = [month + code + "/" for month, name in project_folders(c) if name == code]
    if len(found) > 1:
        raise BucketError("%s has a folder under more than one month (%s): bring them together first"
                          % (code, ", ".join(f.split("/")[0] for f in found)))
    return found[0] if found else None


def _project(p: config.Paths, code: str):
    from awb import projects
    for row in projects.load(p):
        if row.code == code:
            return row
    raise BucketError("%s is not a registered project" % code)


def ensure_folder(c: obs.Client, project) -> tuple[str, bool]:
    """The folder of `project` with its in/ and out/, created under the month of the project when missing.
    Returns (folder, created)."""
    folder = find_folder(c, project.code)
    created = folder is None
    if created:
        folder = "%s/%s/" % (project.created[:7], project.code)
    for key in (folder, folder + IN, folder + OUT):
        if c.head(key) is None:
            c.put_bytes(key, b"")
    return folder, created


def sync(p: config.Paths, c: obs.Client) -> list[str]:
    from awb import projects
    rows = projects.load(p)
    known = {r.code for r in rows}
    lines = []
    for r in rows:
        if r.state != "active":
            continue
        folder, created = ensure_folder(c, r)
        lines.append("%s  %s%s" % (r.code, folder, "  created" if created else ""))
    drift = Counter(month for month, name in project_folders(c) if name not in known)
    for month, n in sorted(drift.items()):
        lines.append("%s  %d folder(s) match no project; name them after their project with awb bucket move"
                     % (month.rstrip("/"), n))
    return lines


# --------------------------------------------------------------------------- pull


def _pulled_file(p: config.Paths) -> Path:
    return p.vault / PULLED


def pulled(p: config.Paths) -> set[tuple[str, str]]:
    try:
        text = _pulled_file(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {tuple(line.split("\t")[:2]) for line in text.splitlines() if line.count("\t") >= 2}


def _remember(p: config.Paths, key: str, etag: str) -> None:
    f = _pulled_file(p)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write("%s\t%s\t%s\n" % (key, etag, date.today().isoformat()))


def _local_name(rel: str, folder: Path) -> str:
    base = _UNSAFE_RE.sub(" ", rel.rsplit("/", 1)[-1]).strip() or "file"
    if base.startswith("."):
        base = "_" + base
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    name, n = base, 1
    while (folder / name).exists():
        n += 1
        name = "%s (%d)%s" % (stem, n, "." + suffix if suffix else "")
    return name


def pull(p: config.Paths, c: obs.Client, code: str, *, again: bool = False) -> dict:
    """The new files of in/ into the vault inbox, then the intake for the project's customer. A file counts as
    new until it was downloaded once (object name and etag, kept on the vault side)."""
    from awb import intake

    result = fetch(p, c, code, again=again)
    if result["files"]:
        result["intake"] = intake.run(result["files"], _project(p, code).customer, p)
    return result


def fetch(p: config.Paths, c: obs.Client, code: str, *, again: bool = False) -> dict:
    """The new files of in/ into the vault inbox, without the intake (`awb import` runs it): the counts and the
    downloaded files under "files"."""
    from awb import projects
    project = _project(p, code)
    if project.customer == projects.NO_CUSTOMER:
        raise BucketError("%s has no customer: pull hands its files to the intake of a customer" % code)
    folder = find_folder(c, code)
    if folder is None:
        raise BucketError("%s has no folder in the bucket; awb bucket folder %s creates it" % (code, code))
    objects = [o for o in c.list(folder).objects if not o[0].endswith("/")]
    incoming = [o for o in objects if o[0].startswith(folder + IN)]
    outside = [o for o in objects if not o[0].startswith((folder + IN, folder + OUT))]
    done = set() if again else pulled(p)
    fresh = [o for o in incoming if (o[0], o[2]) not in done and "\t" not in o[0] and "\n" not in o[0]]
    result = {"new": len(fresh), "known": len(incoming) - len(fresh), "outside": len(outside), "intake": None,
              "files": []}
    if not fresh:
        return result
    config.ensure_layout(p)
    files = []
    for key, _, etag in fresh:
        dst = p.inbox / _local_name(key[len(folder) + len(IN):], p.inbox)
        c.get(key, dst)
        os.chmod(dst, 0o600)
        files.append(dst)
        _remember(p, key, etag)
    result["files"] = files
    return result


# --------------------------------------------------------------------------- put and move


def put(p: config.Paths, c: obs.Client, code: str, file: Path, *, replace: bool = False,
        reveal: bool = False) -> tuple[str, dict | None]:
    """`file` into out/ of the project's folder (created when missing). With `reveal` the real names are put back
    first, in a private temporary file on the vault side that is removed after the upload. The send gate
    (`review.send_check`, decision 15) runs first: it refuses a deliverable of a customer or partner project that
    has no valid review at tier 3 and warns for an internal file."""
    project = _project(p, code)
    file = Path(os.path.realpath(file))
    if not file.is_file():
        raise BucketError("the file to put is not there")
    from awb import projects, review
    for_customer = bool(project.customer) and project.customer != projects.NO_CUSTOMER
    judged: list[str] = []
    verdict, message = review.send_check(Path(project.path), file, for_customer, workbench=p, digest_out=judged)
    if verdict == review.SEND_REFUSE:
        raise BucketError("the send gate refused the file: %s" % message)
    if verdict == review.SEND_WARN:
        print("awb bucket: warning: %s" % message, file=sys.stderr)
    folder, _ = ensure_folder(c, project)
    key = folder + OUT + file.name
    if not replace and c.head(key) is not None:
        raise BucketError("out/ of %s already holds a file of that name; give --replace" % code)
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    # what leaves is a private copy of the bytes the gate judged, never the path again
    tmp_root = config.make_dir(p.vault / "tmp", 0o700)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        copy = Path(tmp) / file.name
        shutil.copyfile(file, copy)
        os.chmod(copy, 0o600)
        if not judged or review.sha256_file(copy) != judged[0]:
            raise BucketError("the file changed after the send gate judged it, put it again")
        if not reveal:
            c.put_file(key, copy, ctype)
            return key, None
        from awb import reveal as _reveal
        try:
            text = copy.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise BucketError("--reveal takes a text file") from None
        named = Path(tmp) / ("named-" + file.name)
        stats = _reveal.write_named(text, named, p)
        c.put_file(key, named, ctype)
    return key, stats


def move(p: config.Paths, c: obs.Client, old: str, code: str) -> dict:
    """Move a folder whose name drifted to `<its month>/<code>/`: copy every object inside the object storage,
    compare names and sizes, then delete the old objects. Nothing is deleted when the comparison fails."""
    _project(p, code)
    if not (_MONTH_RE.match(old or "") and old.endswith("/") and old.count("/") == 2):
        raise BucketError("give the folder as <YYYY-MM>/<name>/")
    target = old[:8] + code + "/"
    if target == old:
        raise BucketError("the folder already carries the name of %s" % code)
    if find_folder(c, code) is not None:
        raise BucketError("%s already has a folder: bring the files together by hand" % code)
    objects = c.list(old).objects
    if not objects:
        raise BucketError("that folder is empty or not there")
    for key, _, _ in objects:
        c.copy(key, target + key[len(old):])
    want = {k[len(old):]: s for k, s, _ in objects}
    got = {k[len(target):]: s for k, s, _ in c.list(target).objects}
    if got != want:
        raise BucketError("the copy does not match the folder (names or sizes differ); nothing was deleted")
    for key, _, _ in objects:
        c.delete(key)
    return {"moved": len(objects), "target": target}


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """`awb bucket sync|folder|pull|put|move`. Exit 0 done, 1 the final check withheld a file, 2 refused or an
    error."""
    from awb import intake
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb bucket", description="The owner's bucket: one folder per project, in/ and out/.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    sub.add_parser("sync", help="the folders of every active project; folders that match no project")
    s = sub.add_parser("folder", help="the folder of one project, created when missing")
    s.add_argument("code")
    s = sub.add_parser("pull", help="new files of in/ into the vault inbox, then the intake (your own shell)")
    s.add_argument("code")
    s.add_argument("--again", action="store_true", help="take files again that were downloaded before")
    s = sub.add_parser("put", help="a file into out/")
    s.add_argument("code")
    s.add_argument("file", type=Path)
    s.add_argument("--replace", action="store_true", help="overwrite a file of the same name in out/")
    s.add_argument("--reveal", action="store_true", help="put the real names back first (your own shell)")
    s = sub.add_parser("move", help="rename a drifted folder to <month>/<code>/")
    s.add_argument("folder")
    s.add_argument("code")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    from awb import projects, register
    from awb import reveal as _reveal
    try:
        p = config.paths()
        c = client()
        if args.command == "sync":
            for line in sync(p, c):
                print(line)
            return 0
        if args.command == "folder":
            folder, created = ensure_folder(c, _project(p, args.code))
            print("%s  %s%s" % (args.code, folder, "  created" if created else ""))
            return 0
        if args.command == "pull":
            r = pull(p, c, args.code, again=args.again)
            line = "%s: %d new files from in/, %d taken before" % (args.code, r["new"], r["known"])
            if r["outside"]:
                line += ", %d outside in/ and out/ left alone" % r["outside"]
            print(line)
            res = r["intake"]
            if res is None:
                return 0
            print("intake for %s: %d outputs in the outbox, wiped %s" % (res.customer, len(res.outputs),
                                                                       intake.wiped_text(res.wiped)))
            if res.held:
                print("withheld by the final check: %d file(s), the originals stay in the inbox" % len(res.held))
                return 1
            return 0
        if args.command == "put":
            key, stats = put(p, c, args.code, args.file, replace=args.replace, reveal=args.reveal)
            line = "%s: %s" % (args.code, key)
            if stats is not None:
                line += "  (%d codes replaced by names)" % stats["replaced"]
            print(line)
            return 0
        if args.command == "move":
            r = move(p, c, args.folder, args.code)
            print("%s: %d objects moved to %s" % (args.code, r["moved"], r["target"]))
            return 0
    except (BucketError, obs.OBSError, projects.ProjectError, intake.IntakeError, register.RegisterError,
            _reveal.RevealError) as err:
        print("awb bucket: %s" % err, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
