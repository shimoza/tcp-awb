"""Public mirrors of T Cloud Public (TCP), so that facts are checked against sources on this host.

    awb mirror docs [--budget SECONDS] [--only REPO]...
    awb mirror sd
    awb mirror rn [--pages N] [--pause SECONDS]
    awb mirror status

docs: every repository of the GitHub organisation opentelekomcloud-docs that is not archived, cloned shallow into
<root>/docs/<repo> and brought up to date on every run (the default branch fetched at depth 1, then reset to it).
<root>/docs/MANIFEST.json records per repository the commit, its date and the state of the last run; it is written
after every repository, so a run that is cut off keeps what it did. A run is a job (awb/jobs.py): it stops at its
time budget and the same command resumes where it stopped.

sd: the service description behind its stable page address, the contractual list of what can be ordered. The PDF
and its text (pdftotext -layout) go to <root>/service-description/<revision>/, the revision read from its "Last
revised" line. A new revision gets a folder of its own and the older ones stay as the archive; CURRENT names the
newest. The text decides whether anything changed: another export of the same text is unchanged, other text under
the same revision date is reported as replaced.

rn: the release notes of T Cloud Public, the third public source. The list page is read one request per page, with a
pause between pages and a User-Agent that names the Workbench; the note pages, /neos/ and the images are never
fetched, because the list holds the date, title, text and link of each note. Each note becomes a file
<root>/release-notes/notes/<date>-<slug>.json (date, title, text, link, services) and INDEX.json lists them with the
day they were first seen. A note that is in the index is never written again. Until every page has been read once
(--pages or an error can cut the first run short) a run goes on to the last page; after that it stops at the first
page that brings nothing new, so a later run reads page 1.

<root> is AWB_MIRRORS or ~/tcp-mirrors on the owner side. The work user of the seal reads /srv/tcp-mirrors, where
the seal mounts them read-only (seal/setup.sh --mirrors <root>/docs <root>/service-description), and never updates
them. The documentation website is not crawled: it blocks robots, and the GitHub sources are what it is built from.
"""
from __future__ import annotations

import datetime
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from awb import config, jobs

ORG = "opentelekomcloud-docs"
ORG_API = "https://api.github.com/orgs/%s/repos?per_page=100&type=public&page=%d"
SD_PAGE = "https://www.t-cloud-public.com/service-description"
ROOT_ENV = "AWB_MIRRORS"
SEALED_ROOT = Path("/srv/tcp-mirrors")
RN_PAGE = "https://www.t-cloud-public.com/en/support/release-notes"
RN_PAUSE = 2.0
RN_MAX_PAGES = 200                  # the pager of a hostile page cannot lead a run past this
RN_MAX_BYTES = 2 * 1024 * 1024      # one list page
RN_MAX_SECONDS = 60                 # one list page, the whole read
RN_MAX_NOTES = 100                  # per list page
RN_MAX_TITLE, RN_MAX_TEXT, RN_MAX_TAGS, RN_MAX_TAG = 200, 2000, 10, 80
DOCS, SD, RN = "docs", "service-description", "release-notes"
INDEX = "INDEX.json"
MANIFEST = "MANIFEST.json"
CURRENT = "CURRENT"
GIT_TIMEOUT = 600
_REPO_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")   # .github is a name, . and .. are not
_REVISION_RE = re.compile(r"Last revised:\s*(\d{2})\.(\d{2})\.(\d{4})")
_AGENT = "awb-mirror (Architect Workbench)"


class MirrorError(Exception):
    """A mirror cannot be read or brought up to date. The message says why."""


def root() -> Path:
    """The mirrors: the host file's `mirrors` (SEALED_ROOT without one) for the work user, AWB_MIRRORS or
    ~/tcp-mirrors on the owner side."""
    if config.is_work_user():
        return Path(config.host_conf().get("mirrors") or SEALED_ROOT)
    return Path(os.environ.get(ROOT_ENV) or "~/tcp-mirrors").expanduser()


def owner_side() -> None:
    if config.is_work_user():
        raise MirrorError("the work user reads the mirrors and never updates them; the owner runs awb mirror")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the documentation sources


def list_repos(*, fetch_json: Callable[[str], Any] | None = None) -> jobs.Listing:
    """The repositories of the organisation, as dicts (name, clone_url, archived, default_branch): list, empty
    or unknown. A page that is not a list of repositories makes the listing unknown."""
    def default_fetch(url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    fetch_json = fetch_json or default_fetch
    repos: list[dict] = []
    for page in range(1, 20):
        try:
            data = jobs.retry(lambda: fetch_json(ORG_API % (ORG, page)), attempts=3, delay=2.0,
                              retry_on=(urllib.error.URLError, TimeoutError, ConnectionError))
        except urllib.error.HTTPError as exc:
            return jobs.Listing.unknown("the repository list answered HTTP %d" % exc.code)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            return jobs.Listing.unknown("the repository list could not be read (%s)" % type(exc).__name__)
        if not isinstance(data, list):
            return jobs.Listing.unknown("the repository list is not a list")
        for r in data:
            name = jobs.dig(r, "name")
            url = jobs.dig(r, "clone_url")
            if not isinstance(name, str) or not _REPO_RE.match(name) or not isinstance(url, str):
                return jobs.Listing.unknown("a repository of the list has no usable name or address")
            size = jobs.dig(r, "size", default=None)
            repos.append({"name": name, "clone_url": url, "archived": bool(jobs.dig(r, "archived", default=False)),
                          "default_branch": str(jobs.dig(r, "default_branch", default="main") or "main"),
                          "size": size if isinstance(size, int) else None})
        if len(data) < 100:
            break
    return jobs.Listing.of(sorted(repos, key=lambda r: r["name"]))


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env,
                          stdin=subprocess.DEVNULL)


def _head(dest: Path) -> tuple[str, str]:
    r = _git(["log", "-1", "--format=%H%x09%cI"], cwd=dest)
    if r.returncode != 0 or "\t" not in r.stdout:
        return "", ""
    commit, date = r.stdout.strip().split("\t", 1)
    return commit, date


def sync_repo(repo: dict, dest: Path) -> dict:
    """Clone `repo` shallow into `dest`, or bring `dest` up to date. Returns state (cloned, updated, unchanged or
    failed), commit and date; a failure carries the step and git's exit code, never git's text."""
    branch = repo.get("default_branch") or "main"
    before = _head(dest)[0] if (dest / ".git").is_dir() else ""
    try:
        if before:
            for step, args in (("fetch", ["fetch", "--depth", "1", "--quiet", "origin", branch]),
                               ("reset", ["reset", "--hard", "--quiet", "FETCH_HEAD"]),
                               ("clean", ["clean", "-fdxq"])):
                r = _git(args, cwd=dest)
                if r.returncode != 0:
                    return {"state": "failed", "step": step, "code": r.returncode}
        else:
            if dest.exists():
                return {"state": "failed", "step": "clone", "code": -1, "why": "the folder exists without git"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            r = _git(["clone", "--depth", "1", "--single-branch", "--branch", branch, "--quiet", repo["clone_url"],
                      str(dest)])
            if r.returncode != 0:
                return {"state": "failed", "step": "clone", "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"state": "failed", "step": "git", "code": -2, "why": "timeout"}
    commit, date = _head(dest)
    state = "cloned" if not before else ("unchanged" if commit == before else "updated")
    return {"state": state, "commit": commit, "date": date}


def load_manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"repos": {}}
    return data if isinstance(data.get("repos"), dict) else {"repos": {}}


def sync_docs(base: Path, repos: list[dict], job: jobs.Job, *, only: set[str] | None = None) -> dict:
    """Every repository that is not archived, as a step of `job`. Returns the counts of the states."""
    folder = base / DOCS
    manifest = load_manifest(folder)
    manifest.update({"org": ORG, "source": "https://github.com/" + ORG})
    counts: dict[str, int] = {}
    wanted = [r for r in repos if not only or r["name"] in only]
    known = {r["name"] for r in repos}
    for repo in wanted:
        if repo["archived"]:
            manifest["repos"].pop(repo["name"], None)
            counts["archived"] = counts.get("archived", 0) + 1
            continue
        if repo.get("size") == 0:                      # a repository without a single commit: nothing to clone
            manifest["repos"][repo["name"]] = {"state": "empty in the organisation", "at": _now()}
            counts["empty"] = counts.get("empty", 0) + 1
            continue
        result = job.step(repo["name"], lambda repo=repo: sync_repo(repo, folder / repo["name"]), cost=5)
        result = dict(result, at=_now())
        manifest["repos"][repo["name"]] = result
        counts[result["state"]] = counts.get(result["state"], 0) + 1
        _write_json(folder / MANIFEST, manifest)
        job.say("%-45s %s" % (repo["name"], result["state"]))
    gone = sorted(n for n in manifest["repos"] if n not in known)
    for name in gone:
        manifest["repos"][name]["state"] = "gone from the organisation"
    counts["gone"] = len(gone)
    manifest["last_run"] = _now()
    _write_json(folder / MANIFEST, manifest)
    return counts


# --------------------------------------------------------------------------- the service description


def _fetch_pdf(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read(), resp.geturl()


def pdf_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="awb-sd-") as tmp:
        pdf = Path(tmp) / "sd.pdf"
        pdf.write_bytes(data)
        r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, timeout=300,
                           stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise MirrorError("pdftotext could not read the service description (exit %d)" % r.returncode)
    return r.stdout.decode("utf-8", "replace")


def sync_sd(base: Path, *, fetch: Callable[[str], tuple[bytes, str]] | None = None,
            page: str | None = None) -> dict:
    """The current service description into its revision folder. Returns state (new, unchanged, replaced),
    revision and folder. Nothing is written unless the answer is a PDF with a revision line. `page` defaults to
    SD_PAGE as it is at the call, so a test that points SD_PAGE elsewhere is obeyed."""
    fetch = fetch or _fetch_pdf
    page = page or SD_PAGE
    try:
        data, final_url = jobs.retry(lambda: fetch(page), attempts=3, delay=2.0,
                                     retry_on=(urllib.error.URLError, TimeoutError, ConnectionError))
    except urllib.error.HTTPError as exc:
        raise MirrorError("the service description answered HTTP %d" % exc.code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise MirrorError("the service description could not be fetched (%s)" % type(exc).__name__) from None
    if not data.startswith(b"%PDF"):
        raise MirrorError("the service description page did not answer with a PDF")
    text = pdf_text(data)
    m = _REVISION_RE.search(text)
    if not m:
        raise MirrorError("the service description carries no 'Last revised' line")
    revision = "%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
    folder = base / SD / revision
    # the text decides: a new export of the same revision is unchanged, other text under the same date is not
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state = "new"
    if folder.exists():
        old = (folder / "TEXT_SHA256").read_text(encoding="utf-8").strip() if (folder / "TEXT_SHA256").exists() else ""
        state = "unchanged" if old == digest else "replaced"
    if state != "unchanged":
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "service-description.pdf").write_bytes(data)
        (folder / "service-description.txt").write_text(text, encoding="utf-8")
        (folder / "TEXT_SHA256").write_text(digest + "\n", encoding="utf-8")
        _write_json(folder / "SOURCE.json", {"page": page, "url": final_url, "fetched": _now(), "text_sha256": digest,
                                             "pdf_sha256": hashlib.sha256(data).hexdigest()})
    revisions = sorted(p.name for p in (base / SD).iterdir() if p.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", p.name))
    (base / SD / CURRENT).write_text(revisions[-1] + "\n", encoding="utf-8")
    return {"state": state, "revision": revision, "folder": str(folder), "current": revisions[-1],
            "archive": len(revisions) - 1}


def current_sd(base: Path) -> Path | None:
    """The text of the newest service description, or None."""
    try:
        rev = (base / SD / CURRENT).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = base / SD / rev / "service-description.txt"
    return path if path.exists() else None


# --------------------------------------------------------------------------- the release notes


class _ListParser(HTMLParser):
    """The articles of the list page (<article class="page-teaser">): date, title, text, link and tags, and the
    highest page number of the pager."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.notes: list[dict] = []
        self.last_page = 1
        self._note: dict | None = None
        self._field = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        href = a.get("href") or ""
        if tag == "a":
            m = re.search(r"[?&]p=([0-9]{1,6})(?![0-9])", href)
            if m:
                self.last_page = min(RN_MAX_PAGES, max(self.last_page, int(m.group(1))))
        if tag == "article" and "page-teaser" in cls:
            self._note = {"date": "", "title": "", "text": "", "link": "", "services": [], "types": []}
        elif self._note is None:
            return
        elif tag == "time":
            self._note["date"] = (a.get("datetime") or "")[:10]
        elif tag in ("h2", "p"):
            self._field = {"h2": "title", "p": "text"}[tag]
            self._buf = []
        elif tag == "a" and "teaser-link" in cls:
            self._note["link"] = href
        elif tag == "a" and "tag" in cls:
            self._field = "tag-type" if "type=" in href else "tag-service"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._note is None:
            return
        if tag in ("h2", "p", "a") and self._field:
            text = " ".join("".join(self._buf).split())
            if self._field in ("title", "text") and tag in ("h2", "p"):
                cap = RN_MAX_TITLE if self._field == "title" else RN_MAX_TEXT
                self._note[self._field] = self._note[self._field] or text[:cap]
            elif self._field == "tag-service" and tag == "a" and text:
                if len(self._note["services"]) < RN_MAX_TAGS:
                    self._note["services"].append(text[:RN_MAX_TAG])
            elif self._field == "tag-type" and tag == "a" and text:
                if len(self._note["types"]) < RN_MAX_TAGS:
                    self._note["types"].append(text[:RN_MAX_TAG])
            else:
                return
            self._field = ""
        elif tag == "article":
            self.notes.append(self._note)
            self._note = None


def _note_link(href: str, page: str) -> str:
    """The absolute link of a note, or "" when it leaves the site or points into /neos/."""
    url = urllib.parse.urljoin(page + "/", href) if href else ""
    u, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(page)
    if u.scheme != "https" or u.netloc != base.netloc or u.path.startswith("/neos/"):
        return ""
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))


def _is_date(text: str) -> bool:
    """A real calendar date written YYYY-MM-DD in ASCII digits."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def parse_release_notes(html: str, page: str | None = None) -> tuple[list[dict], int]:
    """The notes of one list page and the number of the last page. A note without a date, a title or a usable link
    is left out. Each note carries its id (date and the last part of the link), the link made absolute and the
    services and types of its tags."""
    page = page or RN_PAGE
    parser = _ListParser()
    parser.feed(html)
    parser.close()
    notes = []
    for n in parser.notes[:RN_MAX_NOTES]:
        link = _note_link(n["link"], page)
        slug = link.rstrip("/").rsplit("/", 1)[-1].lower()
        if not (_is_date(n["date"]) and n["title"] and link
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,150}", slug)):
            continue
        notes.append(dict(n, link=link, id="%s-%s" % (n["date"], slug)))
    return notes, parser.last_page


def _fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "text/html"})
    end = time.monotonic() + RN_MAX_SECONDS
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > RN_MAX_BYTES:
                raise MirrorError("the release notes page is larger than %d bytes" % RN_MAX_BYTES)
            if time.monotonic() > end:
                raise MirrorError("the release notes page took longer than %d seconds" % RN_MAX_SECONDS)
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def load_rn_index(base: Path) -> dict:
    try:
        data = json.loads((base / RN / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"notes": {}}
    return data if isinstance(data.get("notes"), dict) else {"notes": {}}


def sync_release_notes(base: Path, *, fetch: Callable[[str], str] | None = None, pages: int | None = None,
                       pause: float = RN_PAUSE, sleep: Callable[[float], None] = time.sleep,
                       page: str | None = None) -> dict:
    """The new notes of the release-notes list into <base>/release-notes/. One request per page, `pause` seconds
    between two pages, and the run stops at the first page without a new note, at the last page or after `pages`
    pages. The index is written after every page, so a run that is cut off keeps what it did. Returns the counts
    (new, known, pages) and the ids of the new notes. A page that holds no note at all is an error: the page changed
    its shape."""
    fetch = fetch or _fetch_page
    page = page or RN_PAGE
    folder = base / RN
    index = load_rn_index(base)
    index.update({"source": page})
    complete = bool(index.get("complete"))
    new: list[str] = []
    known = read = 0
    last = 1
    number = 1
    while number <= last and (pages is None or read < pages):
        url = page if number == 1 else "%s?p=%d" % (page, number)
        try:
            html = fetch(url)
        except urllib.error.HTTPError as exc:
            raise MirrorError("the release notes answered HTTP %d (page %d)" % (exc.code, number)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            raise MirrorError("the release notes could not be fetched (%s, page %d)"
                              % (type(exc).__name__, number)) from None
        read += 1
        notes, last = parse_release_notes(html, page)
        if not notes:
            raise MirrorError("the release notes page %d holds no note: its shape changed" % number)
        fresh = [n for n in notes if n["id"] not in index["notes"]]
        known += len(notes) - len(fresh)
        for n in fresh:
            name = "notes/%s.json" % n["id"]
            _write_json(folder / name, {k: n[k] for k in ("id", "date", "title", "text", "link", "services", "types")})
            index["notes"][n["id"]] = {"date": n["date"], "title": n["title"], "services": n["services"],
                                       "file": name, "seen": _now()}
            new.append(n["id"])
        index["last_run"] = _now()
        if not fresh and complete:
            _write_json(folder / INDEX, index)
            break
        number += 1
        if number > last:                              # the last page is read: every page has been seen
            index["complete"] = complete = True
        _write_json(folder / INDEX, index)
        if number <= last and (pages is None or read < pages):
            sleep(pause)
    return {"new": len(new), "known": known, "pages": read, "ids": new, "total": len(index["notes"])}


def release_notes(base: Path) -> list[dict]:
    """Every note of the index, newest first, with its id and the day it was first seen."""
    out = [dict(v, id=k) for k, v in load_rn_index(base)["notes"].items()]
    return sorted(out, key=lambda n: (n.get("date", ""), n["id"]), reverse=True)


def status(base: Path) -> list[str]:
    m = load_manifest(base / DOCS)
    states: dict[str, int] = {}
    for r in m["repos"].values():
        states[r.get("state", "?")] = states.get(r.get("state", "?"), 0) + 1
    lines = ["docs: %d repositories in %s, last run %s, %s" % (
        len(m["repos"]), base / DOCS, m.get("last_run", "never"),
        ", ".join("%d %s" % (n, s) for s, n in sorted(states.items())) or "none")]
    sd = current_sd(base)
    lines.append("service description: %s" % (("revision %s in %s" % (sd.parent.name, sd.parent)) if sd else "none"))
    notes = release_notes(base)
    lines.append("release notes: %s" % (("%d notes, newest %s, in %s" % (len(notes), notes[0]["date"], base / RN))
                                        if notes else "none"))
    return lines


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """`awb mirror docs|sd|rn|status`. Exit 0 done, 1 some repository failed, 2 an error or refused."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb mirror", description="Public mirrors of TCP: the documentation sources, the "
                                                    "service description and the release notes (owner side).")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    d = sub.add_parser("docs", help="clone or update every documentation repository")
    d.add_argument("--budget", type=float, default=jobs.DEFAULT_BUDGET, help="seconds before the run stops")
    d.add_argument("--only", action="append", default=[], help="one repository (repeat for more)")
    sub.add_parser("sd", help="fetch the service description, a folder per revision")
    r = sub.add_parser("rn", help="fetch the release notes list, a file per new note")
    r.add_argument("--pages", type=int, default=None, help="read at most this many pages")
    r.add_argument("--pause", type=float, default=RN_PAUSE, help="seconds between two pages")
    sub.add_parser("status", help="what the mirrors hold")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    base = root()
    try:
        if args.command == "status":
            for line in status(base):
                print(line)
            return 0
        owner_side()
        if args.command == "sd":
            r = sync_sd(base)
            print("awb mirror sd: revision %s %s, current %s, %d older revision(s) kept -> %s"
                  % (r["revision"], r["state"], r["current"], r["archive"], r["folder"]))
            return 0
        if args.command == "rn":
            r = sync_release_notes(base, pages=args.pages, pause=args.pause)
            print("awb mirror rn: %d new note(s), %d known, %d page(s) read, %d in the index -> %s"
                  % (r["new"], r["known"], r["pages"], r["total"], base / RN))
            return 0
        listing = list_repos()
        if listing.state != jobs.LIST:
            print("awb mirror docs: the repository list is %s" % listing.describe(), file=sys.stderr)
            return 2
        job = jobs.Job("docs-" + datetime.date.today().isoformat(), base / DOCS / ".job", budget=args.budget)
        try:
            counts = sync_docs(base, list(listing.items), job, only=set(args.only) or None)
        except jobs.JobStopped as stop:
            print("awb mirror docs: %s" % stop, file=sys.stderr)
            return jobs.EXIT_STOPPED
        job.finish()
        print("awb mirror docs: %s -> %s" % (", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())),
                                              base / DOCS))
        return 1 if counts.get("failed") else 0
    except (MirrorError, OSError, subprocess.SubprocessError) as err:
        print("awb mirror: %s" % (err if isinstance(err, MirrorError) else type(err).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
