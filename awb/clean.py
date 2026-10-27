"""`awb clean`: the anonymization of project material on the owner's laptop, before anything reaches the host (task
lc-laptop-clean, his yes of 2026-10-22).

    awb clean use tcp-xxxx
    awb clean FILE... [--project tcp-xxxx] [--kind docs|logs] [--out DIR] [--put]
    awb clean put [--project tcp-xxxx]
    awb clean serve [--port 8765]
    awb clean register CUST-XXXX
    awb clean init
    awb clean fetch [tcp-xxxx] [--to DIR] [--save]
    awb clean update
    awb clean doctor

The project is named once (task LC4): `awb clean use tcp-xxxx` keeps it as the current project of the laptop and
makes its work folder `~/work/tcp-xxxx` (AWB_WORK_HOME names another root). A run takes `--project` first, then
the project whose work folder holds the current directory, then the current project, and prints it in its first
line. Without `--project` the copies go into `clean/` of the work folder (with it into ./clean/, as before).
`--kind` defaults to logs for a file ending in .log and to docs for every other file. A customer that is not
registered on the laptop is asked for once on the terminal, as `awb clean register` asks (no terminal: refused).
`awb clean put` sends the clean copies listed in the reports of the last run of the project, without cleaning
again. A Windows path (D:\\Documents\\x.docx or D:/Documents/x.docx) is read as /mnt/d/Documents/x.docx.
`awb clean serve` is the same flow as a local page (awb/cleanserve.py, task LC5).

It runs on a plain Linux (Ubuntu under WSL) with no vault daemon, no key service and no work user, and opens no
network connection: the only network use is his own `--put`, `awb clean put`, `awb clean fetch` and `awb clean
update` (`awb clean serve` listens on 127.0.0.1 only and connects nowhere).

- `docs` (docx, pptx, xlsx, odt, ods, odp, pdf, and text, Markdown, html and mail files): the body text only.
  Pictures (logos), document properties, headers, footers, comments, tracked changes and hidden parts are read for
  detection and never written. The registered forms of his customers become their codes, persons, companies,
  places, mail addresses and phone numbers their tokens by the rules of wipe mode (awb/intake/wipe), secrets
  [secret]. One Markdown file per input, `<name>.clean.md`.
- `logs` (text, log, csv, json, yaml, conf and the like): mail addresses, phone numbers, user names, IP
  addresses, internal host names and FQDNs that are no public product or service host, keys, tokens and passwords
  are wiped (awb/cleanlogs.py); no name is guessed from the shape of a word, so the technical words stay. One file
  per input with its own suffix, `<name>.clean.<suffix>`.

The customer of the project is CUST- and its four characters (one code per project). Its written forms are typed
once with `awb clean register`; they stay in the laptop's vault folder (awb/laptop.py) with the mapping of every
run. Every output gets the final check of the intake (the registered forms, the structured data, the second check
and, for docs, the candidates); an output that fails it is not written. A report per run in the output folder,
`clean-report-<stamp>.md`, gives kinds and counts per file, never a value.

`--put` puts the clean copies, never the originals, into `<YYYY-MM>/tcp-xxxx/in/` of the bucket awb (region eu-de)
with his own key from AWB_OBS_AK and AWB_OBS_SK (AWB_OBS_BUCKET and AWB_OBS_REGION name another bucket or region).
The project's folder is looked for under every month and used where it is; a missing one is created under this
month with its in/ and out/ (awb/bucket_layout.py). On the host `awb import --auto` takes them in.

`awb clean fetch tcp-xxxx [--to DIR] [--save]` brings the revealed deliverables of `out/` back to the laptop
(awb/cleanfetch.py), through the same key and the same proxy as `--put`.

`awb clean update` brings the lists of public words up to date (rules/common-words.txt, rules/tech-terms.txt,
rules/allowed-terms.txt, rules/allow-domains.txt and the other allow lists) from the public repository: a
`git pull --ff-only` of the clone, or for an install without git a download of those files over HTTPS checked
against the blob ids of the repository tree. It sends nothing of his files and prints the counts that changed.

`awb clean doctor` prints the proxy variables (the host and port only, never a user or a password), sends one HEAD
request through the environment's proxy to the object storage of the region and to github.com and names the fix
when one fails: the proxy cannot be reached, the proxy wants a login, or TLS verification failed (a TLS-inspecting
corporate proxy: its root certificate through update-ca-certificates, then SSL_CERT_FILE if Python needs it).
`--put` and `awb clean update` go through the same proxy (urllib and git read HTTPS_PROXY and https_proxy).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from awb import cleanlogs, codes, laptop

EXIT_OK, EXIT_HELD, EXIT_ERROR = 0, 1, 2
KINDS = ("docs", "logs")
LOG_SUFFIXES = (".txt", ".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".conf", ".cfg",
                ".ini", ".xml", ".out", ".err", ".toml", ".properties", ".env", ".trace")
DOC_EXTRACT_KINDS = ("docx", "xlsx", "pptx", "odt", "ods", "odp", "pdf", "text", "html", "csv", "json", "eml",
                     "mbox")
CLEAN_NAME_RE = re.compile(r"\.clean(?: \(\d+\))?(?:\.md|%s)$" % "|".join(re.escape(s) for s in LOG_SUFFIXES))
"""The name of a clean copy, also after the bucket download renamed it ("x.clean (2).md"): `awb import --auto`
takes such a file with --trusted."""
REMOVED_META = (("image_count", "pictures"), ("header_footer_parts", "header and footer parts"),
                ("comment_count", "comments"), ("deleted_count", "tracked deletions"),
                ("revision_authors", "revision authors"))
MAX_LOG_BYTES = 200 * 1024 * 1024

UPDATE_FILES = ("common-words.txt", "tech-terms.txt", "allowed-terms.txt", "allow-domains.txt", "known-words.txt",
                "known-phrases.txt", "known-words-de.txt", "known-words-ru.txt", "vendor-names.txt", "standards.txt",
                "source-platforms.txt")
"""The lists of public terms `awb clean update` fetches: every one only ever keeps a word, none adds a candidate."""
PUBLIC_REPO = "https://github.com/shimoza/tcp-awb.git"
PUBLIC_BRANCH = "main"
TREE_URL = "https://api.github.com/repos/shimoza/tcp-awb/git/trees/main?recursive=1"
RAW_URL = "https://raw.githubusercontent.com/shimoza/tcp-awb/main/rules/%s"
USER_AGENT = "tcp-awb-clean-update"


WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)


def windows_path(text: str) -> str:
    """A Windows path as WSL sees it: D:\\Documents\\x.docx and D:/Documents/x.docx are /mnt/d/Documents/x.docx.
    Any other text is returned as it is."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    m = WINDOWS_PATH_RE.match(text)
    if not m:
        return text
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/").lstrip("/"))


def default_kind(path: Path) -> str:
    """logs for a file ending in .log, docs for every other file (nothing else is guessed)."""
    return "logs" if Path(path).suffix.lower() == ".log" else "docs"


def kind_groups(files: list[Path], kind: str | None) -> list[tuple[str, list[Path]]]:
    """The runs of one call: one with --kind, else docs and logs by the default rule, in this order."""
    if kind:
        return [(kind, list(files))]
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(default_kind(f), []).append(f)
    return [(k, groups[k]) for k in KINDS if k in groups]


def report_outputs(report: Path) -> list[Path]:
    """The clean copies a report lists (the rows in state clean), where they are now."""
    out = []
    try:
        lines = Path(report).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[2] == "clean" and is_clean_name(cells[0]) and "/" not in cells[0]:
            path = Path(report).parent / cells[0]
            if path.is_file() and not path.is_symlink():
                out.append(path)
    return out


def put_last(project: str, *, client=None, say=print) -> list[str]:
    """`awb clean put`: the clean copies of the last run of `project` into its in/, nothing cleaned again."""
    reports = laptop.last_run(project)
    if not reports:
        raise CleanError("%s has no run on this laptop yet: awb clean FILE... first" % project)
    files = [f for r in reports for f in report_outputs(r)]
    if not files:
        raise CleanError("the last run of %s left no clean copy to send" % project)
    say("put: %d clean cop%s of the last run" % (len(files), "y" if len(files) == 1 else "ies"))
    return put_files(project, files, client=client, say=say)


def keys_present() -> bool:
    return all(os.environ.get(k) for k in laptop.PUT_ENV)


class CleanError(Exception):
    """A run cannot start. The message carries codes, kinds and counts, never a form, a value or a file name."""


def is_clean_name(name: str) -> bool:
    return bool(CLEAN_NAME_RE.search(name or ""))


@dataclass
class FileResult:
    number: int
    kind: str
    output: Path | None = None
    counts: Counter = field(default_factory=Counter)
    removed: Counter = field(default_factory=Counter)
    state: str = "clean"
    why: str = ""


@dataclass
class RunResult:
    files: list[FileResult]
    report: Path | None
    mapping: Path | None
    put: list[str] = field(default_factory=list)

    @property
    def outputs(self) -> list[Path]:
        return [f.output for f in self.files if f.output is not None]


# --------------------------------------------------------------------------- one run

_SAFE_NAME_RE = re.compile(r"[^\w.-]+")


def _safe_stem(engine, src: Path, taken: set[str], number: int) -> str:
    """The stem of the copy: the original stem with its registered forms, names and data replaced, in plain file
    name characters; `file-N` when the replaced stem still carries something."""
    clean, _ = engine.sanitize(re.sub(r"[_]+", " ", src.stem), "file name")
    stem = _SAFE_NAME_RE.sub("-", clean).strip("-.")[:80] or "file-%d" % number
    if engine.leaks(stem.replace("-", " "), candidates=False):
        stem = "file-%d" % number
    base, n = stem, 1
    while stem.casefold() in taken:
        n += 1
        stem = "%s-%d" % (base, n)
    taken.add(stem.casefold())
    return stem


def _read_log(src: Path) -> str | None:
    data = src.read_bytes()
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _final_hits(engine, text: str, docs: bool) -> Counter:
    from awb.intake.engine import _body_hits

    hits = engine.leaks(text, candidates=False)
    if docs:
        hits += _body_hits(engine, text)
    return Counter(h.cls for h in hits)


def _secret_pass(text: str, counts: Counter) -> str:
    return cleanlogs.wipe_secrets(text, counts)


def run(files: list[Path], project: str, kind: str, out: Path | None = None, *, put: bool = False,
        say=print) -> RunResult:
    """Clean `files` for `project` into `out` (default ./clean/). See the module text."""
    from awb import intake
    from awb import extract as _extract
    from awb.intake.engine import _Engine

    if kind not in KINDS:
        raise CleanError("--kind is docs or logs")
    customer = laptop.customer_of(project)
    if not laptop.known(customer):
        raise CleanError("%s has no form on this laptop yet: run awb clean register %s once" % (customer, customer))
    files = [Path(f) for f in files]
    if not files:
        raise CleanError("no file given")
    for i, f in enumerate(files, start=1):
        if f.is_symlink() or not f.is_file():
            raise CleanError("file %d of %d is not a regular file" % (i, len(files)))
    failed = intake.selftest()
    if failed:
        raise CleanError("the intake failed its self-test, nothing is cleaned: %s" % "; ".join(failed))
    out = Path(out) if out is not None else Path.cwd() / "clean"
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    engine = _Engine(laptop.entries())
    engine.wipe.trusted = kind == "logs"
    if kind == "logs":
        cleanlogs.log_view(engine)
    tokens = cleanlogs.Tokens()
    results: list[FileResult] = []
    sources: list[tuple[FileResult, Path, object]] = []

    # detection over every file first: a name one file teaches (a comment author, a mail header) is wiped in all
    for number, src in enumerate(files, start=1):
        fr = FileResult(number=number, kind=kind)
        results.append(fr)
        if kind == "logs":
            if src.suffix.lower() not in LOG_SUFFIXES:
                fr.state, fr.why = "skipped", "not a log, config or ticket file (its suffix), use --kind docs"
                continue
            if src.stat().st_size > MAX_LOG_BYTES:
                fr.state, fr.why = "skipped", "larger than %d MB" % (MAX_LOG_BYTES // (1024 * 1024))
                continue
            text = _read_log(src)
            if text is None:
                fr.state, fr.why = "skipped", "not text"
                continue
            engine.detect("%d detect" % number, text, names_only=True)
            sources.append((fr, src, text))
            continue
        with _extract.temp_root(laptop.tmp_dir()):
            ex = _extract.extract(src)
        fr.kind = ex.kind
        if ex.kind not in DOC_EXTRACT_KINDS or ex.state == "failed" or not (ex.text or "").strip():
            fr.state, fr.why = "skipped", "no text could be read (%s)" % ex.kind
            continue
        for part_id, pex in intake._walk(ex, str(number)):
            pieces = [pex.detect_text or ""]
            if pex.text and pex.text not in pieces[0]:
                pieces.append(pex.text)
            pieces.append(intake._meta_text(pex.meta))
            engine.detect("%s detect" % part_id, "\n".join(t for t in pieces if t))
            engine.detect("%s raw parts" % part_id, getattr(pex, "scan_text", "") or "", names_only=True)
            for key, label in REMOVED_META:
                value = pex.meta.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value:
                    fr.removed[label] += value
            props = [k for k, v in pex.meta.items() if isinstance(v, str) and v.strip() and k != "extractor"]
            if props:
                fr.removed["document properties"] += len(props)
        sources.append((fr, src, ex))

    taken: set[str] = set()
    written: list[Path] = []
    for fr, src, item in sources:
        stem = _safe_stem(engine, src, taken, fr.number)
        if kind == "logs":
            suffix = src.suffix.lower()
            name = "%s.clean%s" % (stem, suffix)
            text = _secret_pass(item, fr.counts)
            text = cleanlogs.wipe_log(text, tokens, fr.counts)
            body, c = engine.sanitize(text, name)
            fr.counts.update(c)
        else:
            name = "%s.clean.md" % stem
            bodies = []
            for i, (part_id, pex) in enumerate(intake._walk(item, str(fr.number))):
                text = _secret_pass(pex.text or "", fr.counts)
                clean, c = engine.sanitize(text, name)
                fr.counts.update(c)
                if clean.strip():
                    bodies.append(clean.strip("\n") if i == 0 else "## part %d\n\n%s" % (i, clean.strip("\n")))
            body = "\n\n".join(bodies) + "\n"
        body = cleanlogs.drop_secret_lines(body, fr.counts)
        left = _final_hits(engine, body, kind == "docs")
        if left:
            fr.state = "withheld"
            fr.why = "the final check found %s, no copy written" % ", ".join("%s %d" % kv for kv in sorted(left.items()))
            continue
        path = out / name
        if path.exists():
            fr.state, fr.why = "skipped", "a copy of that name is in the output folder already"
            continue
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        fr.output = path
        written.append(path)

    rows = [(h.token, h.cls, h.value, ",".join(sorted(h.where))) for h in engine.hits.values()]
    rows += [(tok, cls, value, "") for tok, cls, value in tokens.rows]
    mapping = laptop.write_map(project, rows)
    report = _write_report(out, project, customer, kind, results)
    res = RunResult(files=results, report=report, mapping=mapping)
    if put and written:
        res.put = put_files(project, written, say=say)
    return res


def _counts_text(counts: Counter) -> str:
    return ", ".join("%s %d" % kv for kv in sorted(counts.items()) if kv[1]) or "nothing"


def _write_report(out: Path, project: str, customer: str, kind: str, results: list[FileResult]) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path, n = out / ("clean-report-%s.md" % stamp), 1
    while path.exists():
        n += 1
        path = out / ("clean-report-%s-%d.md" % (stamp, n))
    lines = ["# awb clean %s" % stamp, "", "- project: %s" % project, "- customer: %s" % customer,
             "- kind: %s" % kind, "- files: %d" % len(results), "", "| file | read as | state | replaced | removed |",
             "| --- | --- | --- | --- | --- |"]
    for fr in results:
        label = fr.output.name if fr.output else "file %d" % fr.number
        state = fr.state + (" (%s)" % fr.why if fr.why else "")
        lines.append("| %s | %s | %s | %s | %s |" % (label, fr.kind, state, _counts_text(fr.counts),
                                                      _counts_text(fr.removed)))
    lines += ["", "Kinds and counts only. The values and their tokens stay in the vault folder of this laptop."]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- the bucket


def _client(what: str = "--put"):
    """His own key from the environment, the bucket awb in eu-de unless the environment names others."""
    from awb import bucket_layout, obs

    ak, sk = (os.environ.get(k) or "" for k in laptop.PUT_ENV)
    if not ak or not sk:
        raise CleanError("%s needs his own key in %s and %s" % ((what,) + laptop.PUT_ENV))
    return obs.Client(os.environ.get("AWB_OBS_BUCKET") or bucket_layout.OWNER_BUCKET, obs.Keys(ak, sk),
                      region=os.environ.get("AWB_OBS_REGION") or bucket_layout.REGION)


def put_files(project: str, files: list[Path], *, client=None, today: date | None = None, say=print) -> list[str]:
    """The clean copies into <YYYY-MM>/<project>/in/: the folder under any month, else a new one under this month
    with its in/ and out/. A copy of a name that is in in/ already is not replaced. Returns the keys written."""
    from awb import bucket, bucket_layout, obs

    for f in files:
        if not is_clean_name(f.name):
            raise CleanError("--put takes the clean copies of awb clean only")
    c = client or _client()
    try:
        folder = bucket.find_folder(c, project)
        if folder is None:
            folder = bucket_layout.folder_of((today or date.today()).strftime("%Y-%m"), project)
            for key in (folder, folder + bucket_layout.IN, folder + bucket_layout.OUT):
                if c.head(key) is None:
                    c.put_bytes(key, b"")
            say("bucket: created %s" % folder)
        written = []
        for f in files:
            key = folder + bucket_layout.IN + f.name
            if c.head(key) is not None:
                say("bucket: %s is in %sin/ already, not replaced" % (f.name, folder))
                continue
            ctype = "text/markdown" if f.suffix == ".md" else "text/plain"
            c.put_file(key, f, ctype)
            written.append(key)
        say("bucket: %d clean cop%s into %s" % (len(written), "y" if len(written) == 1 else "ies",
                                                 folder + bucket_layout.IN))
        return written
    except (obs.OBSError, bucket.BucketError) as err:
        raise CleanError("the bucket: %s" % err) from None


# --------------------------------------------------------------------------- awb clean update


def _list_count(data: bytes) -> int:
    return sum(1 for line in data.decode("utf-8", errors="replace").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))


def _snapshot(rules: Path) -> dict[str, bytes]:
    out = {}
    for name in UPDATE_FILES:
        try:
            out[name] = (rules / name).read_bytes()
        except OSError:
            out[name] = b""
    return out


def _changes(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    lines = []
    for name in UPDATE_FILES:
        a, b = before.get(name, b""), after.get(name, b"")
        if a == b:
            continue
        old = {x.strip() for x in a.decode("utf-8", "replace").splitlines() if x.strip() and not x.startswith("#")}
        new = {x.strip() for x in b.decode("utf-8", "replace").splitlines() if x.strip() and not x.startswith("#")}
        lines.append("%s: %d -> %d entries (%d added, %d removed)" % (name, len(old), len(new), len(new - old),
                                                                     len(old - new)))
    return lines


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _fetch(url: str, limit: int = 32 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # a fresh opener reads the proxy of the environment as it is now (urlopen keeps the first one it built)
    with urllib.request.build_opener().open(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise CleanError("an answer of the public repository is larger than the limit")
    return data


def _update_git(repo: Path, run=subprocess.run) -> None:
    dirty = run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                text=True)
    if dirty.returncode != 0:
        raise CleanError("git cannot read the clone (exit %d)" % dirty.returncode)
    if dirty.stdout.strip():
        raise CleanError("the clone has local changes: commit or drop them, then awb clean update")
    got = run(["git", "-C", str(repo), "pull", "--ff-only", "--quiet", PUBLIC_REPO, PUBLIC_BRANCH],
              capture_output=True, text=True)
    if got.returncode != 0:
        raise CleanError("git pull --ff-only of the public repository failed (exit %d)" % got.returncode)


def _update_https(rules: Path, fetch=_fetch) -> None:
    tree = json.loads(fetch(TREE_URL).decode("utf-8"))
    blobs = {e.get("path"): e.get("sha") for e in tree.get("tree", []) if isinstance(e, dict)}
    fresh = {}
    for name in UPDATE_FILES:
        want = blobs.get("rules/" + name)
        if not want:
            continue
        data = fetch(RAW_URL % name)
        if git_blob_id(data) != want:
            raise CleanError("%s does not match the checksum of the repository tree: nothing was changed" % name)
        fresh[name] = data
    for name, data in fresh.items():
        tmp = rules / (".%s.tmp" % name)
        tmp.write_bytes(data)
        os.replace(tmp, rules / name)


def update(rules: Path | None = None, *, run=subprocess.run, fetch=_fetch) -> list[str]:
    """`awb clean update`: the lists of public terms from the public repository. Returns the lines of what changed."""
    from awb import patterns

    rules = Path(rules or patterns.RULES_DIR)
    repo = rules.parent
    before = _snapshot(rules)
    if (repo / ".git").exists():
        _update_git(repo, run=run)
        how = "git pull --ff-only of %s" % PUBLIC_REPO
    else:
        _update_https(rules, fetch=fetch)
        how = "download over HTTPS, checked against the blob ids of the repository tree"
    changed = _changes(before, _snapshot(rules))
    return ["update: %s" % how] + (changed or ["the lists are up to date: nothing changed"])


# --------------------------------------------------------------------------- awb clean doctor

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
FIX_UNREACHABLE = ("the proxy cannot be reached: check the host and port in HTTPS_PROXY and https_proxy (ask IT for "
                   "the proxy of the office network and of the VPN)")
FIX_NO_ROUTE = ("no direct way out: this network needs a proxy, export HTTPS_PROXY and https_proxy (the proxy "
                "section of the WSL guide)")
FIX_LOGIN = ("the proxy wants a login: put user and password into the proxy URL for this shell only "
             "(read -s, the WSL guide), never into a file")
FIX_TLS = ("TLS verification failed: a TLS-inspecting corporate proxy. Copy its root certificate (.crt, PEM) to "
           "/usr/local/share/ca-certificates/, run sudo update-ca-certificates, and if Python still fails export "
           "SSL_CERT_FILE=%s" % CA_BUNDLE)


def proxy_host(value: str | None) -> str:
    """The host and port of a proxy URL, never the user or the password in it."""
    import urllib.parse

    if not value:
        return "not set"
    try:
        u = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
        host = u.hostname or "?"
        port = u.port
    except ValueError:
        return "set, not a URL"
    login = " (with a login)" if u.username or u.password else ""
    return "%s%s%s" % (host, ":%d" % port if port else "", login)


def classify(err: BaseException) -> str:
    """The fix for the failure of one doctor request. Never the text of the error: it can carry the proxy URL."""
    import ssl
    import urllib.error

    reason = getattr(err, "reason", err) if isinstance(err, urllib.error.URLError) else err
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return FIX_TLS
    if "407" in text:
        return FIX_LOGIN
    proxied = any(os.environ.get(k) for k in PROXY_VARS[:4])
    if isinstance(reason, (ConnectionRefusedError, TimeoutError, OSError)) or "timed out" in text:
        return FIX_UNREACHABLE if proxied else FIX_NO_ROUTE
    return FIX_UNREACHABLE if proxied else FIX_NO_ROUTE


def doctor_targets() -> list[tuple[str, str]]:
    from awb import bucket_layout, obs

    region = os.environ.get("AWB_OBS_REGION") or bucket_layout.REGION
    return [("object storage %s" % region, "https://obs.%s.%s/" % (region, obs.DOMAIN)),
            ("github.com", "https://github.com/")]


def doctor(opener=None) -> tuple[list[str], int]:
    """`awb clean doctor`: the proxy settings and one HEAD request to each target through the environment's proxy.
    Returns the lines and the exit code (0 all reachable, 1 a target failed)."""
    import urllib.error

    lines = ["%s: %s" % (k, proxy_host(os.environ.get(k)) if "NO_PROXY" not in k.upper()
                         else (os.environ.get(k) or "not set")) for k in PROXY_VARS]
    if os.environ.get("SSL_CERT_FILE"):
        lines.append("SSL_CERT_FILE: set")
    opener = opener or urllib.request.build_opener()
    code = EXIT_OK
    for label, url in doctor_targets():
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=20) as resp:
                lines.append("%s: reachable (HTTP %d)" % (label, resp.status))
        except urllib.error.HTTPError as err:
            if err.code == 407:
                lines.append("%s: failed, %s" % (label, FIX_LOGIN))
                code = EXIT_HELD
            else:
                lines.append("%s: reachable (HTTP %d)" % (label, err.code))
        except Exception as err:  # noqa: BLE001 - every failure gets its fix, never its text
            lines.append("%s: failed, %s" % (label, classify(err)))
            code = EXIT_HELD
    return lines, code


# --------------------------------------------------------------------------- the command


def _parser_files():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean", description="Clean project material on the laptop, offline: docs (office "
                    "and pdf files) or logs (logs, configs, tickets); also awb clean use tcp-xxxx, put, serve, "
                    "register CUST-XXXX, init, fetch, update and doctor.")
    ap.add_argument("files", nargs="+", metavar="FILE", help="the files to clean; the originals are never changed")
    ap.add_argument("--project", default=None, metavar="tcp-xxxx", help="the project code (default: the work "
                    "folder of the current directory, else the project of awb clean use); its customer is CUST- and "
                    "the four characters")
    ap.add_argument("--kind", default=None, choices=KINDS, help="docs: body text of documents, logos, properties, "
                    "headers, footers and comments out, names wiped; logs: users, hosts, addresses and secrets wiped, "
                    "technical words kept, no name guessed (default: logs for a .log file, docs for any other)")
    ap.add_argument("--out", default=None, metavar="DIR", help="the folder of the clean copies (default: clean/ of "
                    "the work folder; with --project ./clean/)")
    ap.add_argument("--put", action="store_true", help="put the clean copies into <YYYY-MM>/tcp-xxxx/in/ of the "
                    "bucket awb with the key of AWB_OBS_AK and AWB_OBS_SK")
    return ap


def _fetch_main(argv: list[str]) -> int:
    from awb import cleanfetch
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean fetch", description="The finished files of out/ of a project from the bucket "
                    "into a folder of this laptop, with the key of AWB_OBS_AK and AWB_OBS_SK; in/ is never touched.")
    ap.add_argument("project", metavar="tcp-xxxx", nargs="?", default=None,
                    help="the project (default: the work folder of the current directory, else awb clean use)")
    ap.add_argument("--to", default=None, metavar="DIR", help="the folder (default: the one saved with --save)")
    ap.add_argument("--save", action="store_true", help="keep --to as the default folder")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    if args.save and not args.to:
        print("awb clean fetch: --save goes with --to DIR", file=sys.stderr)
        return EXIT_ERROR
    try:
        project, source = laptop.resolve_project(args.project)
        print(_project_line(project, source))
        failed = cleanfetch.fetch(project, Path(windows_path(args.to)) if args.to else None, save=args.save)
    except (laptop.LaptopError, CleanError, cleanfetch.FetchError) as err:
        print("awb clean fetch: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb clean fetch: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    return EXIT_HELD if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from awb import register
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["register"]:
            ap = SafeParser(prog="awb clean register", description="Type the written forms of a customer once, on "
                            "this laptop only.")
            ap.add_argument("customer", metavar="CUST-XXXX|tcp-xxxx")
            args = ap.parse_args(argv[1:])
            cust = args.customer if not codes.is_project_code(args.customer) else laptop.customer_of(args.customer)
            laptop.ask_forms(cust)
            return EXIT_OK
        if argv[:1] == ["init"]:
            SafeParser(prog="awb clean init", description="The folders of the laptop.").parse_args(argv[1:])
            print("\n".join(laptop.init()))
            return EXIT_OK
        if argv[:1] == ["doctor"]:
            SafeParser(prog="awb clean doctor", description="The proxy settings and whether the object storage and "
                       "github.com can be reached through them, with the fix when not.").parse_args(argv[1:])
            lines, code = doctor()
            print("\n".join(lines))
            return code
        if argv[:1] == ["fetch"]:
            return _fetch_main(argv[1:])
        if argv[:1] == ["use"]:
            return _use_main(argv[1:])
        if argv[:1] == ["put"]:
            return _put_main(argv[1:])
        if argv[:1] == ["serve"]:
            return _serve_main(argv[1:])
        if argv[:1] == ["update"]:
            SafeParser(prog="awb clean update", description="The lists of public terms from the public "
                       "repository.").parse_args(argv[1:])
            print("\n".join(update()))
            return EXIT_OK
        args = _parser_files().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    except (laptop.LaptopError, CleanError, register.RegisterError) as err:
        print("awb clean: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb clean: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    try:
        project, source = laptop.resolve_project(args.project)
        files = [Path(windows_path(f)) for f in args.files]
        if args.out:
            out = Path(windows_path(args.out))
        elif source == "--project":
            out = Path.cwd() / "clean"                        # as before LC4
        else:
            out = laptop.work_dir(project) / "clean"
        print(_project_line(project, source))
        customer = laptop.customer_of(project)
        if not laptop.known(customer) and _interactive():
            print("%s is not registered on this laptop yet." % customer)
            laptop.ask_forms(customer, tty=True)
        started = time.monotonic()
        runs = run_files(files, project, args.kind, out, put=args.put)
    except (laptop.LaptopError, CleanError, register.RegisterError) as err:
        print("awb clean: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb clean: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    ok, copies = True, 0
    for kind, how, res in runs:
        print("kind: %s (%s)" % (kind, how))
        for fr in res.files:
            label = fr.output.name if fr.output else "file %d" % fr.number
            print("%s: %s; replaced %s; removed %s%s" % (label, fr.state, _counts_text(fr.counts),
                                                         _counts_text(fr.removed), " (%s)" % fr.why if fr.why else ""))
        print("clean copies: %d of %d in %.1f s; report %s" % (len(res.outputs), len(res.files),
                                                               time.monotonic() - started, res.report))
        ok = ok and all(fr.output for fr in res.files)
        copies += len(res.outputs)
    if copies and not args.put:
        print("next: awb clean put sends the %d clean cop%s of this run" % (copies, "y" if copies == 1 else "ies"))
    return EXIT_OK if ok else EXIT_HELD


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _project_line(project: str, source: str) -> str:
    return "project: %s (from %s), customer %s" % (project, source, laptop.customer_of(project))


def run_files(files: list[Path], project: str, kind: str | None, out: Path | None, *, put: bool = False,
              say=print) -> list[tuple[str, str, RunResult]]:
    """One call of `awb clean` or of the page: a run per kind (--kind, else the default rule), its reports kept as
    the last run of the project. Returns (kind, how it was chosen, result) per run."""
    if not files:
        raise CleanError("no file given")
    how = "--kind" if kind else None
    runs = []
    for k, group in kind_groups(files, kind):
        res = run(group, project, k, out, put=put, say=say)
        runs.append((k, how or ("a .log file" if k == "logs" else "default for a file not ending in .log"), res))
    laptop.record_run(project, [r.report for _, _, r in runs if r.report is not None])
    return runs


def _use_main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean use", description="Name the project once: it becomes the current project of "
                    "this laptop and its work folder ~/work/tcp-xxxx is made; without a code it prints the current "
                    "one.")
    ap.add_argument("project", metavar="tcp-xxxx", nargs="?", default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    try:
        if not args.project:
            project, source = laptop.resolve_project(None)
            print(_project_line(project, source))
            return EXIT_OK
        folder = laptop.use(args.project)
    except laptop.LaptopError as err:
        print("awb clean use: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    customer = laptop.customer_of(args.project)
    print(_project_line(args.project, "awb clean use"))
    print("folder: %s" % folder)
    print("customer: %s" % ("registered on this laptop" if laptop.known(customer)
                            else "not registered yet, the first run asks its forms"))
    return EXIT_OK


def _put_main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean put", description="Send the clean copies of the last run of the project (the "
                    "ones its report lists) into <YYYY-MM>/tcp-xxxx/in/ of the bucket, without cleaning again; the "
                    "key of AWB_OBS_AK and AWB_OBS_SK.")
    ap.add_argument("--project", default=None, metavar="tcp-xxxx", help="the project (default: the work folder of "
                    "the current directory, else awb clean use)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    try:
        project, source = laptop.resolve_project(args.project)
        print(_project_line(project, source))
        put_last(project)
    except (laptop.LaptopError, CleanError) as err:
        print("awb clean put: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb clean put: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _serve_main(argv: list[str]) -> int:
    from awb import cleanserve
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean serve", description="The same flow as a local page on 127.0.0.1: open the URL "
                    "it prints (with its token) in the Windows browser; Ctrl-C ends it.")
    ap.add_argument("--port", type=int, default=cleanserve.DEFAULT_PORT, help="the port (default %d)"
                    % cleanserve.DEFAULT_PORT)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    return cleanserve.serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
