"""Pictures of the files the intake takes in, held on the vault side until he has looked at them.

    <vault>/quarantine/<CUST>/<file id>/img-001.png ...   (sealed as .gpg when the vault is encrypted)
    <vault>/quarantine/<CUST>/<file id>/manifest.json     numbers, kinds, sizes, hashes and states only
    awb images list [CUST]                                what is held (his own shell, vault side)
    awb images release CUST FILEID N [N...] | --all       copy pictures into input/<file id>-img/ of the project

The intake keeps only text. A picture can show a logo, a name, a signature or a network plan with addresses that
no text check sees, so the intake holds every picture of a file (the media of docx, pptx, xlsx, odt, odp and ods,
the images of a pdf through pdfimages, an image file itself) and nothing reaches the outbox before he releases it
by number after a look. Released pictures land in the project folder at `input/<file id>-img/img-001.png` (the
project of the code or the only project of the customer; `--project CODE` names one of several). The project commit
gate checks no content since B-087, so a released picture can be committed. A customer that has no project gets them
in `<outbox>/<CUST>/images/<file id>-img-001.png`. The manifest names no member and no file: a member name can carry
a name as well.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import stat
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from awb import codes, config, safefile

QUARANTINE = "quarantine"
MANIFEST = "manifest.json"
IMAGES = "images"
MAX_IMAGES = 200
"""At most this many pictures are held per file; the manifest says how many more there were."""
MAX_IMAGE_BYTES = 20_000_000
MAX_TOTAL_IMAGE_BYTES = 200_000_000
"""All pictures of one file together, read from the container, not from its declared sizes: a small office file
can declare two hundred pictures of twenty megabytes each (found by the review of 2026-09-27)."""
IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "emf", "wmf", "emz", "wmz", "svg", "webp", "heic",
              "heif", "avif", "ico")
PICTURE_KINDS = ("image", "svg")
"""Kinds of a top-level file that is held as one picture: a raster or metafile picture and an svg, whose text
nodes are read like html while the picture itself waits for a look."""
OFFICE_KINDS = ("docx", "xlsx", "pptx", "odt", "ods", "odp")
PDFIMAGES_TIMEOUT = 120
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,40}$")


class ImageError(Exception):
    """A picture cannot be held or released. The message carries no value."""


def folder(p: config.Paths, customer: str, file_id: str | None = None) -> Path:
    if not (isinstance(customer, str) and (codes.is_code(customer) and codes.kind_of(customer) == "CUST"
                                           or codes.is_project_code(customer))):
        raise ImageError("not a customer code")
    base = p.vault / QUARANTINE / customer
    if file_id is None:
        return base
    if not _FILE_ID_RE.match(file_id):
        raise ImageError("not a file id")
    return base / file_id


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def pictures(src: Path, kind: str) -> tuple[list[tuple[str, bytes]], int]:
    """(pictures as (extension, bytes), how many more were left out) of one file. Never raises for a file it
    cannot read: that file simply holds no picture."""
    found: list[tuple[str, bytes]] = []
    more = 0
    total = 0
    if kind in OFFICE_KINDS:
        try:
            with zipfile.ZipFile(src) as z:
                for info in z.infolist():
                    ext = _ext(info.filename)
                    if ext not in IMAGE_EXTS or info.is_dir():
                        continue
                    if len(found) >= MAX_IMAGES or info.file_size > MAX_IMAGE_BYTES \
                            or total + info.file_size > MAX_TOTAL_IMAGE_BYTES:
                        more += 1
                        continue
                    with z.open(info) as member:
                        data = member.read(MAX_IMAGE_BYTES + 1)
                    if len(data) > MAX_IMAGE_BYTES or total + len(data) > MAX_TOTAL_IMAGE_BYTES:
                        more += 1
                        continue
                    total += len(data)
                    found.append(("jpg" if ext == "jpeg" else ext, data))
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            return [], 0
    elif kind == "pdf":
        tmp = Path(tempfile.mkdtemp(prefix="awb-images-"))
        try:
            os.chmod(tmp, 0o700)
            try:
                subprocess.run(["pdfimages", "-all", str(src), str(tmp / "img")], capture_output=True,
                               timeout=PDFIMAGES_TIMEOUT, check=False)
            except (OSError, subprocess.SubprocessError):
                return [], 0
            for f in sorted(tmp.iterdir()):
                size = f.stat().st_size
                if len(found) >= MAX_IMAGES or size > MAX_IMAGE_BYTES or total + size > MAX_TOTAL_IMAGE_BYTES:
                    more += 1
                    continue
                total += size
                ext = _ext(f.name) or "bin"
                found.append(("jpg" if ext == "jpeg" else ext, f.read_bytes()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    elif kind in PICTURE_KINDS:
        try:
            data = Path(src).read_bytes()
        except OSError:
            return [], 0
        if len(data) <= MAX_IMAGE_BYTES:
            ext = _ext(Path(src).name)
            found.append((ext if ext in IMAGE_EXTS else ("svg" if kind == "svg" else "img"), data))
        else:
            more += 1
    return found, more


def _write_private(path: Path, data: bytes) -> None:
    safefile.write_new(path, data, mode=0o600)


def _save_manifest(where: Path, manifest: dict) -> None:
    tmp = where / (".%s.tmp" % MANIFEST)
    tmp.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, where / MANIFEST)


def load_manifest(where: Path) -> dict:
    try:
        data = json.loads((where / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ImageError("the manifest of the held pictures cannot be read") from None
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        raise ImageError("the manifest of the held pictures has no list of images")
    return data


def hold(p: config.Paths, customer: str, file_id: str, src: Path, kind: str, *, encrypted: bool = False,
         today: str | None = None) -> int:
    """Hold the pictures of one file of an intake. Returns how many were held. In an encrypted vault each
    picture is sealed through the vault daemon; one that cannot be sealed is removed again (never left in
    plaintext) and counted in the manifest as not held."""
    found, more = pictures(Path(src), kind)
    if not found and not more:
        return 0
    where = folder(p, customer, file_id)
    where.mkdir(parents=True, exist_ok=True)
    os.chmod(where, 0o700)
    for parent in (where.parent, where.parent.parent):
        os.chmod(parent, 0o700)
    rows = []
    not_held = 0
    for n, (ext, data) in enumerate(found, start=1):
        name = "img-%03d.%s" % (n, ext)
        path = where / name
        _write_private(path, data)
        sealed = False
        if encrypted:
            from awb import intake
            if intake.seal(p, path) is None:
                sealed = True
            else:
                path.unlink(missing_ok=True)
                not_held += 1
                continue
        rows.append({"n": n, "ext": ext, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                     "sealed": sealed, "state": "held", "released": ""})
    manifest = {"customer": customer, "file": file_id, "kind": kind, "date": today or datetime.date.today().isoformat(),
                "left_out": more, "not_held": not_held, "images": rows}
    _save_manifest(where, manifest)
    return len(rows)


def held(p: config.Paths, customer: str | None = None) -> list[dict]:
    """One row per file with held pictures: customer, file, kind, held, released, left out."""
    base = p.vault / QUARANTINE
    out: list[dict] = []
    try:
        customers = [customer] if customer else sorted(d.name for d in base.iterdir() if d.is_dir()) \
            if base.is_dir() else []
    except OSError:
        raise ImageError("the quarantine cannot be read") from None
    for cust in customers:
        cdir = base / cust
        if not cdir.is_dir():
            continue
        for fdir in sorted(d for d in cdir.iterdir() if d.is_dir()):
            try:
                m = load_manifest(fdir)
            except ImageError:
                continue
            images = m["images"]
            out.append({"customer": cust, "file": fdir.name, "kind": m.get("kind", ""),
                        "held": sum(1 for i in images if i.get("state") == "held"),
                        "released": sum(1 for i in images if i.get("state") == "released"),
                        "left_out": int(m.get("left_out") or 0) + int(m.get("not_held") or 0),
                        "path": str(fdir)})
    return out


_JPEG_DROP = frozenset(range(0xE1, 0xEE)) | {0xEF, 0xFE}
"""APP1 to APP13, APP15 and COM segments of a JPEG: EXIF, XMP, IPTC, comments. APP0 (JFIF) and APP14 (the
colour transform) stay, the picture needs them."""
_PNG_DROP = (b"tEXt", b"zTXt", b"iTXt", b"eXIf")


def _scrub_jpeg(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA:              # start of scan: the rest is the picture
            out += data[pos:]
            return bytes(out)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        segment = data[pos:pos + 2 + length]
        if marker not in _JPEG_DROP:
            out += segment
        pos += 2 + length
    out += data[pos:]
    return bytes(out)


def _scrub_png(data: bytes) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        return data
    out = bytearray(sig)
    pos = len(sig)
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        chunk = data[pos:pos + 12 + length]
        if kind not in _PNG_DROP:
            out += chunk
        pos += 12 + length
    out += data[pos:]
    return bytes(out)


def scrubbed(ext: str, data: bytes) -> bytes:
    """The picture without its metadata segments, where the format keeps them apart from the pixels."""
    if ext in ("jpg", "jpeg"):
        return _scrub_jpeg(data)
    if ext == "png":
        return _scrub_png(data)
    return data


def names_in(p: config.Paths, ext: str, data: bytes) -> int:
    """Registered names in what a picture carries besides its pixels: its text nodes (svg) or the printable
    strings of its bytes (metadata a viewer shows in a panel). Through the name check, positions only."""
    from awb import check
    from awb.extract.text import binary_strings

    if ext == "svg":
        text = data.decode("utf-8", errors="replace")
    else:
        text = "\n".join(binary_strings(data))
    if not text.strip():
        return 0
    return sum(1 for h in check.check_text(text, p.register) if h.get("cls") == "name")


def _data_of(p: config.Paths, path: Path, sealed: bool) -> bytes:
    if not sealed:
        return path.read_bytes()
    from awb import vault
    try:
        answer = vault.admin_call("open_file", p.admin_sock, path=str(path))
    except vault.VaultLocked:
        raise ImageError("the vault is locked, run awb vault unlock") from None
    except vault.VaultUnavailable:
        raise ImageError("no vault daemon") from None
    except vault.VaultError:
        raise ImageError("the vault daemon refused the request") from None
    try:
        return base64.b64decode(answer.get("data") or "", validate=True)
    except ValueError:
        raise ImageError("the vault daemon answered in an unexpected form") from None


def project_folder(p: config.Paths, customer: str, project: str | None = None) -> Path | None:
    """The folder of the project the pictures of `customer` go to: the project `project` names, the project that the
    code is or the only active project of the customer. None when the customer has no project (the outbox then).
    Several projects and none named is an error."""
    from awb import projects

    try:
        rows = projects.load(p)
    except projects.ProjectError:
        rows = []
    mine = [r for r in rows if r.code == customer or r.customer == customer]
    if project is not None:
        chosen = [r for r in mine if r.code == project]
        if not chosen:
            raise ImageError("%s is no project of %s" % (project, customer))
        return Path(chosen[0].path)
    active = [r for r in mine if r.state == "active"] or mine
    if len(active) > 1:
        raise ImageError("%s has %d projects: name one with --project CODE" % (customer, len(active)))
    return Path(active[0].path) if active else None


def _open_folder(parent_fd: int, name: str, gid: int = -1) -> int:
    """The folder `name` of the folder `parent_fd`, made when it is missing, never through a link. A folder this
    user made or owns gets the group `gid` (the group of the project folder) and g+rX, so the work user reads what
    the owner released (ask 2026-11-27-tcp-bj3o-4)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o750, dir_fd=parent_fd)
        fd = os.open(name, flags, dir_fd=parent_fd)
    _share(fd, gid)
    return fd


def _share(fd: int, gid: int) -> None:
    """Give an object of this user the group `gid` and group read (and search on a folder); others stay out."""
    st = os.fstat(fd)
    if gid < 0 or st.st_uid != os.geteuid():
        return
    if st.st_gid != gid:
        os.fchown(fd, -1, gid)
    mode = stat.S_IMODE(st.st_mode) | stat.S_IRGRP | (stat.S_IXGRP if stat.S_ISDIR(st.st_mode) else 0)
    os.fchmod(fd, mode & ~0o007)


def _land_in_project(folder_: Path, file_id: str, ready: list[tuple[int, str, bytes]]) -> list[Path]:
    """Write the pictures into input/<file id>-img/ of the project folder, each file new and private to the group."""
    root = os.open(folder_, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fds = [root]
    try:
        gid = os.fstat(root).st_gid
        fds.append(_open_folder(fds[-1], "input"))
        fds.append(_open_folder(fds[-1], "%s-img" % file_id, gid))
        written = []
        for _, name, data in ready:
            safefile.write_atomic(name, data, dir_fd=fds[-1], mode=0o640, chmod=True)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fds[-1])
            try:
                _share(fd, gid)
            finally:
                os.close(fd)
            written.append(folder_ / "input" / ("%s-img" % file_id) / name)
        return written
    finally:
        for fd in fds:
            os.close(fd)


def release(p: config.Paths, customer: str, file_id: str, numbers: list[int] | None = None, *,
            today: str | None = None, project: str | None = None) -> list[Path]:
    """Copy the chosen pictures (all held ones when `numbers` is None) into input/<file id>-img/ of the project of the
    customer (its outbox when it has no project) and mark them released. A number that was not held is an error and
    nothing is copied."""
    where = folder(p, customer, file_id)
    m = load_manifest(where)
    rows = {int(i["n"]): i for i in m["images"]}
    chosen = sorted(rows) if numbers is None else sorted(set(numbers))
    missing = [n for n in chosen if n not in rows]
    if missing:
        raise ImageError("%d of the numbers are not held pictures of that file" % len(missing))
    # every chosen picture is read, checked against its hash, scrubbed of its metadata and name checked before
    # the first one is written: a picture whose metadata or text carries a registered name is not released
    ready: list[tuple[int, str, bytes]] = []
    for n in chosen:
        row = rows[n]
        name = "img-%03d.%s" % (n, row["ext"])
        src = where / (name + (".gpg" if row.get("sealed") else ""))
        data = _data_of(p, src, bool(row.get("sealed")))
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ImageError("picture %d does not match its hash in the manifest" % n)
        data = scrubbed(row["ext"], data)
        if names_in(p, row["ext"], data):
            raise ImageError("picture %d carries a registered name in its metadata or text and is not released; "
                             "look at it on the vault side" % n)
        ready.append((n, name, data))
    target = project_folder(p, customer, project)
    if target is not None:
        written = _land_in_project(target, file_id, ready)
    else:
        outdir = config.make_dir(p.outbox / customer / IMAGES, 0o750, shared=True)
        written = []
        for n, name, data in ready:
            dest = outdir / ("%s-%s" % (file_id, name))
            tmp = outdir / (".%s.tmp" % dest.name)
            tmp.write_bytes(data)
            os.chmod(tmp, 0o640)
            os.replace(tmp, dest)
            written.append(dest)
    for n, _, _ in ready:
        rows[n]["state"] = "released"
        rows[n]["released"] = today or datetime.date.today().isoformat()
    _save_manifest(where, m)
    return written


def main(argv: list[str] | None = None) -> int:
    """`awb images list [CUST]` and `awb images release CUST FILEID N... | --all`. Exit 0, 1 refused, 2 error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb images", description="Pictures of the intake, held on the vault side until released.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("list", help="the files with held pictures")
    s.add_argument("customer", nargs="?", default=None, metavar="CUST")
    s = sub.add_parser("release", help="copy pictures into input/FILEID-img/ of the project after a look")
    s.add_argument("customer", metavar="CUST")
    s.add_argument("file", metavar="FILEID")
    s.add_argument("numbers", nargs="*", type=int, metavar="N")
    s.add_argument("--all", action="store_true", help="every held picture of the file")
    s.add_argument("--project", default=None, metavar="CODE", help="the project, when the customer has several")
    try:
        args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command not in ("list", "release"):
        ap.print_usage(sys.stderr)
        return 2
    if config.is_work_user():
        print("awb images: the pictures are held on the vault side, the owner lists and releases them", file=sys.stderr)
        return 1
    p = config.paths()
    try:
        if args.command == "release":
            if bool(args.numbers) == bool(args.all):
                print("awb images: give picture numbers or --all", file=sys.stderr)
                return 2
            out = release(p, args.customer, args.file, None if args.all else args.numbers, project=args.project)
            where = ("input/%s-img/ of the project %s" % (args.file, out[0].parents[2].name)
                     if out and out[0].parent.name == "%s-img" % args.file else "the outbox of %s" % args.customer)
            print("awb images: %d picture(s) of %s released to %s" % (len(out), args.file, where))
            return 0
        rows = held(p, args.customer)
        for r in rows:
            print("%s  %s  %s  held %d  released %d  left out %d  %s"
                  % (r["customer"], r["file"], r["kind"], r["held"], r["released"], r["left_out"], r["path"]))
        print("awb images: %d file(s) with pictures" % len(rows))
        return 0
    except ImageError as err:
        print("awb images: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb images: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
