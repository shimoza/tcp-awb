"""The intake: the only way customer material enters the Workbench.

`run` takes files from the vault inbox (or anywhere), reads each one with `awb.extract`, finds registered
forms and structured data in everything detection can see and writes one sanitised Markdown copy per file to
`<shared>/outbox/<CUST>/<file id>.md` (`<shared>/outbox/<tcp-xxxx>/` for a project without a customer): the
registered forms become their codes, structured data its tokens and every string the candidate rules of
`awb.wipe` recognise as a name a token of its class ([person 1], [company 1], [place 1], [name 1]), numbered per run; the forms of every wiped person and company are learned and wiped wherever
they stand again in the run (wipe mode, T4). Nothing stops for a name: `run(..., review=True)` is the one stop, for
the rare case where a name must stay distinguishable by a code. Every output is checked once more before it
counts. Originals move to
`<vault>/originals/<CUST>/` under their file id, only after that check passed. The public report goes next to
the outputs and carries codes, classes and counts. The private report goes to the vault and carries the rest.

When the vault is encrypted (register.tsv.gpg), the register is read through the vault daemon and every
original and private report of the run is sealed right after it is written (`seal_file` of the daemon writes
`<file>.gpg` and removes the plaintext). A file that cannot be sealed because no vault daemon answers stays in
plaintext inside the vault and is counted in `IntakeResult.unsealed`. When the daemon answers and refuses the seal
(locked, busy with a reload, refused), the run is undone: its originals go back where they came from, its outputs,
pictures and reports are removed and the run fails, so nothing of it stays in the vault in plaintext (D-T2h).

The original file name is treated as text: it is scanned and never used for an output. Nothing in this
module writes a matched value, a form or an original name into an exception, a log line or the public side.

`run_text` takes one text in the same way, the door of `awb import --paste` (awb/pastein.py): the door shapes the
text (a signature, a speaker, a table column) on the engine the run gives it; the self-test, the lock, the customer,
the final check, the reports and the undo are the run's.

The intake is a package (F1 step 8). This module holds the run and is the door to its parts in awb/intake/:
`engine` (detection, tokens and replacement, the candidates and the final check of an output), `keep` (the keep
list) and `wipe` (the candidate rules by family, also reached as `awb.wipe`). Every name of a part is reached here
(awb/parts.py).
"""
from __future__ import annotations

import base64
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from awb import codes, config, parts, register
from awb import extract as _extract
from awb import images as _images
from awb import report as _report

__path__ = [os.path.join(os.path.dirname(__file__), "intake")]
"""The parts of the intake live in awb/intake/: with this path this module is their package (F1 step 8) and stays
the one door every caller imports."""

from awb.intake import wipe  # noqa: E402
from awb.intake.engine import FILE_ID_RE, IntakeError, _Engine, _body_hits  # noqa: E402
from awb.intake.keep import load_keep  # noqa: E402

NEW = "new"


@dataclass
class IntakeResult:
    customer: str
    outputs: list[Path]
    public_report: Path
    private_report: Path
    states: dict[str, str]    # file id -> state
    unsealed: int = 0         # vault files of this run left in plaintext because sealing failed (encrypted vault)
    wiped: Counter = field(default_factory=Counter)   # token class (person, company, place, name) -> replacements
    held: list[str] = field(default_factory=list)     # file ids the final check withheld; their originals stay
    pictures: int = 0         # pictures held on the vault side, released only by awb images release
    reviewed: int = 0         # candidates handed to the review of --review
    candidates: int = 0       # distinct values the candidate rules found in detection (all of them wiped)

    @property
    def blocked(self) -> bool:
        """Always False: wipe mode never blocks (the callers and tests of the first release still ask)."""
        return False



# --------------------------------------------------------------------------- the run


def safe_new_code(entries: list[register.Entry], kind: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh top-level code (register.next_code) that carries no register form and no structured shape."""
    engine = _Engine(entries)
    engine.used |= set(taken)
    for _ in range(1000):
        code = register.next_code(list(entries), kind)
        if code not in engine.used and engine.safe(code):
            return code
    raise IntakeError("could not find a free %s code" % kind)


def project_customer_code(code: str) -> str:
    """CUST- and the four characters of the project code (tcp-q7m4 -> CUST-Q7M4)."""
    return "CUST-" + code.split("-", 1)[1].upper()


def pair_is_free(p: config.Paths, entries, engine, cust: str) -> bool:
    """The customer code is in no register entry, no issued folder and no row of the project list, and is safe."""
    from awb import projects

    taken = issued_codes(p) | set(register.codes(entries)) | {r.customer for r in projects.load(p)}
    return codes.is_code(cust) and cust not in taken and engine.safe(cust)


def new_pair(p: config.Paths) -> tuple[str, str]:
    """A fresh pair (project code, customer code) of the same four characters: tcp-xxxx and CUST-XXXX. A pair of
    which either code is taken is drawn again, never one code without the other."""
    from awb import projects

    entries = register.load(p.register)
    engine = _Engine(entries)
    taken = {r.code for r in projects.load(p)}
    for _ in range(1000):
        code = codes.new_project_code("tcp", taken)
        cust = project_customer_code(code)
        if pair_is_free(p, entries, engine, cust):
            return code, cust
        taken.add(code)
    raise IntakeError("could not find a free pair of project and customer code")


def issued_codes(p: config.Paths) -> set[str]:
    """Customer codes that earlier intakes used: the folders on both sides."""
    out: set[str] = set()
    for base in (p.private_reports, p.originals, p.outbox):
        try:
            out.update(d.name for d in base.iterdir() if d.is_dir() and codes.is_code(d.name))
        except OSError:
            pass
    return out


def _resolve_customer(customer: str, entries: list[register.Entry], p: config.Paths) -> tuple[str, bool]:
    if isinstance(customer, str) and codes.is_project_code(customer):
        # a project without a customer: its copies go out under the project code and nothing becomes a customer
        # code; registered forms of any customer still become their codes
        return customer, False
    if customer == NEW:
        return safe_new_code(entries, "CUST", issued_codes(p)), True
    if (not isinstance(customer, str) or not codes.is_code(customer) or customer.count("-") != 1
            or codes.kind_of(customer) != "CUST"):
        raise IntakeError("customer must be a top-level CUST code or new")
    states = {e.status for e in entries if e.code == customer}
    if states and "active" not in states:
        raise IntakeError("customer %s is retired in the register" % customer)
    if not states and customer not in issued_codes(p):
        raise IntakeError("customer %s is not in the register and no earlier intake issued it" % customer)
    return customer, False


def _taken_ids(p: config.Paths, customer: str) -> set[str]:
    out: set[str] = set()
    for base in (p.originals / customer, p.outbox / customer):
        try:
            for f in base.iterdir():
                m = FILE_ID_RE.match(f.name)
                if m:
                    out.add(m.group(0))
        except OSError:
            pass
    return out


def _meta_text(meta: dict) -> str:
    lines = []
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        if isinstance(value, dict):
            value = " ".join("%s %s" % kv for kv in value.items())
        if isinstance(value, str) and value.strip():
            lines.append("%s: %s" % (key, value))
    return "\n".join(lines)


def _label_of(ex) -> str:
    return str(ex.meta.get("member") or ex.meta.get("attachment") or "")


def _walk(ex, part_id: str):
    """(part id, extraction) for an extraction and all its children, parent first."""
    yield part_id, ex
    for i, child in enumerate(ex.children or [], start=1):
        yield from _walk(child, "%s.%d" % (part_id, i))


def _suffix(name: str, engine: _Engine) -> str:
    """The original suffix when it is a plain short suffix that carries nothing, else none."""
    suffix = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) and not engine.final_hits(suffix[1:], candidates=False):
        return suffix.lower()
    return ""


def _output_text(record: _report.FileRecord, bodies: dict[str, str]) -> str:
    lines: list[str] = []
    for i, part in enumerate(record.parts):
        if i == 0:
            lines += ["# %s" % part.part_id, ""]
        else:
            lines += ["", "## %s" % part.part_id, ""]
        lines.append("- file id: %s" % part.part_id)
        lines.append("- kind: %s" % part.kind)
        lines.append("- state: %s" % part.state)
        lines.append("- notes: %s" % ("; ".join(part.public_notes) or "none"))
        body = bodies.get(part.part_id, "")
        if body.strip():
            lines += ["", body.strip("\n")]
    return "\n".join(lines) + "\n"


def vault_encrypted(p: config.Paths) -> bool:
    """True when the vault is encrypted: the register exists as register.tsv.gpg only."""
    try:
        return p.register_encrypted.is_file()
    except OSError:
        return False


def seal(p: config.Paths, path: Path) -> str | None:
    """Seal a plaintext file of the vault through the vault daemon (`seal_file`): it writes `<path>.gpg` and
    removes the plaintext. None when that worked, else a short reason without a value; the plaintext then stays
    where it is, inside the vault."""
    from awb import vault

    try:
        vault.admin_call("seal_file", p.admin_sock, path=str(path))
    except vault.VaultLocked:
        return "vault locked"
    except vault.VaultUnavailable:
        return "no vault daemon"
    except vault.VaultBusy:
        return BUSY_SEAL
    except vault.VaultError:
        return "refused by the vault daemon"
    return None


BUSY_SEAL = "the vault daemon is reloading, try again"
UNDO_SEALS = ("vault locked", BUSY_SEAL, "refused by the vault daemon")
"""Seal failures that undo the run: the daemon answered and would not seal. Without a daemon the plaintext stays
inside the vault and the run names it."""
UNDONE = ("the vault daemon is busy or refused the seal, nothing of this run stays in the vault in plaintext, run "
          "the intake again")


def _undo(p: config.Paths, cust: str, moved: list[tuple[Path, Path, str]], outputs: list[Path],
          extra: list[Path]) -> None:
    """Take a run back after a refused or busy seal: every original of the run goes back to where it came from (a
    sealed one through open_file; one that cannot be opened stays sealed, never in plaintext), the outputs, the
    pictures and the reports of the run are removed."""
    from awb import vault

    for src, dst, fid in moved:
        try:
            if _is_file(dst):
                shutil.move(str(dst), str(src))
            elif _is_file(_sealed(dst)):
                answer = vault.admin_call("open_file", p.admin_sock, path=str(_sealed(dst)))
                fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(base64.b64decode(answer.get("data") or ""))
                _sealed(dst).unlink()
        except (OSError, ValueError, vault.VaultError):
            pass
        shutil.rmtree(_images.folder(p, cust, fid), ignore_errors=True)
    for f in list(outputs) + list(extra):
        for g in (f, _sealed(f)):
            try:
                g.unlink()
            except OSError:
                pass


def _is_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _sealed(path: Path) -> Path:
    return path.with_name(path.name + ".gpg")


def _free_private_path(path: Path) -> Path:
    """`path` or path-2, path-3 ... when the name or its sealed copy is taken: sealing must never replace an
    older sealed report."""
    candidate, n = path, 1
    while candidate.exists() or _sealed(candidate).exists():
        n += 1
        candidate = path.with_name("%s-%d%s" % (path.stem, n, path.suffix))
    return candidate


def _write_private_report(p: config.Paths, path: Path, rec, encrypted: bool) -> tuple[Path, int]:
    """Write the private report and seal it when the vault is encrypted. Returns the path of the report as it
    lies in the vault and 1 when it stayed in plaintext, else 0."""
    written = _report.write_private(_free_private_path(path) if encrypted else path, rec)
    if not encrypted:
        return written, 0
    why = seal(p, written)
    if why in UNDO_SEALS:
        written.unlink(missing_ok=True)
        raise _Undo(written)
    if why is not None:
        return written, 1
    return _sealed(written), 0


class _Undo(Exception):
    """A private report whose seal was refused or busy: it is removed and the run is taken back."""

    def __init__(self, path: Path):
        super().__init__("undo")
        self.path = path


def _move_original(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dst.parent, 0o700)
    if dst.exists():
        raise FileExistsError(dst.name)
    shutil.move(str(src), str(dst))
    os.chmod(dst, 0o600)


def _readable(word: str, text: str) -> bool:
    return word.casefold() in text.casefold()


def selftest() -> list[str]:
    """Every case of awb.planted, run through an engine of its own with an invented register: the matcher must
    report the case's class and the second check must find the cases marked for it. After the sanitising
    neither may find anything. Then the wipe cases: each planted name must come out as a token of its class and
    nothing of it readable, every control term must come out untouched, and the self-test's customer inside a
    company shape must become the code. Returns the failures, empty when all pass (R-004, T-94)."""
    from awb import planted

    failures: list[str] = []
    engine = _Engine(planted.entries())
    for label, text, cls, second in planted.CASES:
        found = {h.cls for h in engine.final_hits(text)}
        if cls not in found:
            failures.append("%s: the matcher does not report %s" % (label, cls))
        if second and not engine.second_hits(text):
            failures.append("%s: the second check does not find it" % label)
        clean, _ = engine.sanitize(text, "selftest")
        if engine.leaks(clean):
            failures.append("%s: something is left after the sanitising" % label)
    if engine.leaks("release 1.4.2 for %s in tcp-q7m4, two app clusters, one per zone" % planted.CODE):
        failures.append("clean text: a check reports a hit")
    for label, text, words, cls in planted.WIPE_CASES:
        e = _Engine(planted.entries())
        e.detect("selftest detect", text)
        clean, _ = e.sanitize(text, "selftest")
        if any(_readable(w, clean) for w in words):
            failures.append("%s: a planted name is left after the sanitising" % label)
        if not re.search(r"\[%s \d+\]" % wipe.TOKEN_TEXT[cls if cls != "name" else "unknown"], clean):
            failures.append("%s: no %s token" % (label, cls))
        if e.leaks(clean):
            failures.append("%s: a check reports a hit after the sanitising" % label)
    for i, term in enumerate(planted.CONTROL_TERMS, start=1):
        e = _Engine(planted.entries())
        for text in ("Wir prüfen %s heute." % term, "| %s | 2 |" % term):
            e.detect("selftest detect", text)
            clean, _ = e.sanitize(text, "selftest")
            if term not in clean:
                failures.append("control term %d is wiped" % i)
                break
    e = _Engine(planted.entries())
    label, text = planted.CUSTOMER_CASE
    e.detect("selftest detect", text)
    clean, _ = e.sanitize(text, "selftest")
    if planted.CUSTOMER_CODE not in clean or "[" in clean or _readable(planted.CUSTOMER_SHORT, clean) \
            or _readable("Spedition", clean):
        failures.append("%s: the customer form does not become the code" % label)
    return failures


def run(files: list[Path], customer: str, p: config.Paths, *, review: bool = False, edit=None,
        confirm=None, redo: dict | None = None, trusted: bool = False) -> IntakeResult:
    """Take `files` in for `customer` (a CUST code, "new" or the code of a project without a customer, tcp-xxxx)
    in wipe mode. See the module text for the steps.
    Nothing is read before the self-test passed. With `review` the run first stops on the candidates and hands
    them to the review of `awb register review` (its editor, marks, register and keep list; `edit` and `confirm`
    stand in for the editor and the question in the tests), then goes on in wipe mode with the register and the
    keep list of after the review: a candidate left for later is wiped like any other. `redo` maps each file to
    (file id, its original in the vault): a copy of an original taken out of the vault runs again under its file id,
    its output replaces the one in the outbox, and nothing moves into the vault (`awb import --redo`)."""
    files = [Path(f) for f in files]
    if not files:
        raise IntakeError("no files given")
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test and takes nothing in: %s" % "; ".join(failed))
    for i, f in enumerate(files, start=1):
        # a link would move only the link and change the mode of what it points to
        if f.is_symlink():
            raise IntakeError("file %d of %d is a symbolic link, give the file itself" % (i, len(files)))
        if not f.is_file():
            raise IntakeError("file %d of %d is not a regular file" % (i, len(files)))
    if len({f.resolve() for f in files}) != len(files):
        raise IntakeError("the same file is given more than once")

    config.ensure_layout(p)
    from awb import vault

    # one intake at a time and never while `awb vault encrypt` runs: an original written in plaintext after the
    # encryption passed its folder would stay in plaintext
    reviewed = 0
    try:
        if review:
            with vault.vault_lock(p):
                customer, report, reviewed = _review_stop(files, customer, p)
            if reviewed:
                from awb import candidates

                try:
                    candidates.review(p, customer, report, edit=edit, confirm=confirm, next_step=False)
                except candidates.ReviewError as err:
                    raise IntakeError("review: %s" % err) from None
        with vault.vault_lock(p):
            return _run(files, customer, p, reviewed=reviewed, redo=redo, trusted=trusted)
    except vault.VaultError as err:
        raise IntakeError(str(err)) from None


def _detect_all(files: list[Path], customer: str, p: config.Paths, redo: dict | None = None, trusted: bool = False):
    """Steps 2 to 4: the customer, file ids, extraction, detection and the forms the run learns. No stop."""
    entries = register.load(p.register)
    cust, is_new = _resolve_customer(customer, entries, p)
    engine = _Engine(entries)
    engine.keep = tuple(load_keep(p))
    engine.wipe.trusted = trusted
    now = datetime.now()
    rec = _report.Run(date=now.date().isoformat(), time=now.strftime("%H:%M:%S"), customer=cust,
                      new_customer=is_new, register_forms=engine.forms)
    taken = _taken_ids(p, cust)
    extractions: dict[str, list[tuple[str, object]]] = {}
    for src in files:
        if redo is not None:
            fid = redo[src][0]
            if not FILE_ID_RE.fullmatch(fid):
                raise IntakeError("a file of the redo has no file id")
        else:
            fid = engine.fresh_file_id(taken)
        with _extract.temp_root(p.vault / "tmp"):
            ex = _extract.extract(src)
        fr = _report.FileRecord(file_id=fid, original_name=src.name, kind=ex.kind, state=ex.state)
        name_text = re.sub(r"_+", " ", src.name)
        engine.detect("%s file name" % fid, name_text)
        parts = list(_walk(ex, fid))
        extractions[fid] = parts
        for part_id, pex in parts:
            label = src.name if part_id == fid else _label_of(pex)
            fr.parts.append(_report.Part(part_id=part_id, label=label, kind=pex.kind, state=pex.state,
                                         notes=list(pex.notes), meta=dict(pex.meta)))
            pieces = [pex.detect_text or ""]
            if pex.text and pex.text not in pieces[0]:
                pieces.append(pex.text)
            pieces.append(_meta_text(pex.meta))
            for c in engine.detect("%s detect" % part_id, "\n".join(t for t in pieces if t)):
                rec.candidates.append((part_id, c))
            engine.detect("%s raw parts" % part_id, getattr(pex, "scan_text", "") or "", names_only=True)
        rec.files.append(fr)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for pid, c in rec.candidates:
        if c.casefold() not in seen:
            seen.add(c.casefold())
            unique.append((pid, c))
    rec.candidates = unique
    return cust, engine, rec, extractions, now


def _review_stop(files: list[Path], customer: str, p: config.Paths) -> tuple[str, Path, int]:
    """The stop of `--review`: detection only, the candidates into a private report for the review; nothing is
    written to the outbox and no original moves. Returns the customer code, the report and the number of
    candidates."""
    encrypted = vault_encrypted(p)
    cust, engine, rec, _, now = _detect_all(files, customer, p)
    rec.stopped = True
    rec.hits = list(engine.hits.values())
    path = p.private_reports / cust / ("%s.md" % now.strftime("%Y-%m-%d-%H%M%S"))
    try:
        written, _ = _write_private_report(p, path, rec, encrypted)
    except _Undo:
        raise IntakeError(UNDONE) from None
    return cust, written, len(rec.candidates)


def _run(files: list[Path], customer: str, p: config.Paths, *, reviewed: int = 0,
         redo: dict | None = None, trusted: bool = False) -> IntakeResult:
    # an encrypted register is read through the vault daemon; originals and private reports are then sealed
    encrypted = vault_encrypted(p)
    cust, engine, rec, extractions, now = _detect_all(files, customer, p, redo, trusted)
    rec.reviewed = reviewed
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    private_path = p.private_reports / cust / ("%s.md" % stamp)
    public_path = p.outbox / cust / _report.PUBLIC_NAME

    # steps 6 and 7: sanitise, write, check each output once more
    outdir = config.make_dir(p.outbox / cust, 0o750, shared=True)
    outputs: list[Path] = []
    check_failed: set[str] = set()
    try:
        _write_outputs(rec, extractions, engine, outdir, outputs, check_failed, replace=redo is not None)
    except BaseException:
        # never leave a half run behind: the outputs of this run go, the originals stay in the inbox
        for out in outputs:
            out.unlink(missing_ok=True)
        raise

    # step 8: move the originals of every file that passed the final check; seal them in an encrypted vault
    unsealed = 0
    pictures_total = 0
    moved: list[tuple[Path, Path, str]] = []
    for src, fr in zip(files, rec.files):
        if redo is not None:
            # the original has lain in the vault since its first import, its pictures are held
            fr.original = redo[src][1]
            if fr.file_id in check_failed:
                fr.reasons.append("the redo of this file failed the final check, no copy in the outbox")
            continue
        if fr.file_id in check_failed:
            fr.reasons.append("original left in the inbox because the final check failed")
            continue
        dst = p.originals / cust / ("%s%s" % (fr.file_id, _suffix(src.name, engine)))
        try:
            pictures = _images.hold(p, cust, fr.file_id, src, fr.kind, encrypted=encrypted)
        except (_images.ImageError, OSError) as err:
            pictures = 0
            fr.reasons.append("pictures not held (%s)" % type(err).__name__)
        if pictures:
            pictures_total += pictures
            fr.parts[0].public_notes.append("%d picture(s) held on the vault side, released only by awb images "
                                            "release after a look" % pictures)
        try:
            _move_original(src, dst)
            fr.original = dst
        except OSError as err:
            fr.reasons.append("original not moved (%s)" % type(err).__name__)
            continue
        moved.append((src, dst, fr.file_id))
        if encrypted:
            why = seal(p, dst)
            if why is None:
                fr.original = _sealed(dst)
            elif why in UNDO_SEALS:
                _undo(p, cust, moved, outputs, [])
                raise IntakeError(UNDONE)
            else:
                unsealed += 1
                fr.reasons.append("original not sealed (%s), it lies in plaintext in the vault" % why)

    # step 9: reports
    rec.hits = list(engine.hits.values())
    rec.learned = engine.learned_forms()
    public_text = _public_text(rec, engine)
    public_written = _report.write_public(public_path, public_text)
    try:
        private_written, report_unsealed = _write_private_report(p, private_path, rec, encrypted)
    except _Undo as undo:
        _undo(p, cust, moved, outputs, [public_written, undo.path])
        raise IntakeError(UNDONE) from None
    from awb import intake_counts
    intake_counts.record_intake(p, cust, len(rec.candidates), extractor=_report.extractor_word(rec))
    wiped: Counter = Counter()
    for f in rec.files:
        for part in f.parts:
            for cls, n in part.counts.items():
                if cls in wipe.WIPE_CLASSES:
                    wiped[wipe.TOKEN_TEXT[cls]] += n
    return IntakeResult(customer=cust, outputs=outputs, public_report=public_written,
                        private_report=private_written, states={f.file_id: f.state for f in rec.files},
                        unsealed=unsealed + report_unsealed, wiped=wiped, held=sorted(check_failed),
                        pictures=pictures_total, reviewed=reviewed, candidates=len(rec.candidates))


def run_text(text: str, customer: str, p: config.Paths, shape, write, *, label: str, kind: str, screen=None,
             held=None) -> IntakeResult:
    """Take one text in for `customer` (a CUST code, "new" or the code of a project without a customer), the door
    of `awb import --paste` (awb/pastein.py). The steps of `run` for one file that has no original:
    the self-test; `screen(text)`, the door's own refusals (it raises, or returns what `shape` takes; without it the
    text); the vault lock; the customer and an engine with the register and the keep list; `shape(engine,
    screened)` -> (body, reader, counts, notes, size): the sanitised copy, the text a reader sees of it when that is
    another one (None: the copy itself), the replacements per class, the notes of the reports and the size they
    name; the final check over the copy and the reader's text, which writes nothing when it finds something and
    raises `held(classes)` (without `held` an IntakeError); `write(outdir, body)` -> the path of the copy; the public
    and the private report as for a file. A failure of the reports removes the copy again; nothing moves into the
    vault."""
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test and takes nothing in: %s" % "; ".join(failed))
    screened = screen(text) if screen is not None else text
    config.ensure_layout(p)
    from awb import vault

    try:
        with vault.vault_lock(p):
            return _run_text(customer, p, screened, shape, write, label, kind, held)
    except vault.VaultError as err:
        raise IntakeError(str(err)) from None


def _run_text(customer: str, p: config.Paths, screened, shape, write, label: str, kind: str, held) -> IntakeResult:
    encrypted = vault_encrypted(p)
    entries = register.load(p.register)
    cust, is_new = _resolve_customer(customer, entries, p)
    engine = _Engine(entries)
    engine.keep = tuple(load_keep(p))
    now = datetime.now()
    body, reader, counts, notes, size = shape(engine, screened)
    hits = engine.leaks(body, candidates=False)
    if reader is not None:
        hits += engine.leaks(reader, candidates=False)
    hits += _body_hits(engine, body if reader is None else reader)
    if hits:
        found = _report.counts_text(Counter(h.cls for h in hits))
        if held is not None:
            raise held(found)
        raise IntakeError("the final check withheld the text (%s left in it): nothing was written" % found)

    out = write(p.outbox / cust, body)
    stem = out.name
    rec = _report.Run(date=now.date().isoformat(), time=now.strftime("%H:%M:%S"), customer=cust, new_customer=is_new,
                      register_forms=engine.forms)
    fr = _report.FileRecord(file_id=stem, original_name=label, kind=kind, state="ok")
    part = _report.Part(part_id=stem, label=fr.original_name, kind=fr.kind, state="ok", notes=list(notes),
                        public_notes=list(notes), meta={"size": size})
    part.counts.update(counts)
    fr.parts.append(part)
    fr.output = out
    rec.files.append(fr)
    rec.hits = list(engine.hits.values())
    rec.learned = engine.learned_forms()
    try:
        public_written = _report.write_public(p.outbox / cust / _report.PUBLIC_NAME, _public_text(rec, engine))
        private_path = p.private_reports / cust / ("%s.md" % now.strftime("%Y-%m-%d-%H%M%S"))
        private_written, unsealed = _write_private_report(p, private_path, rec, encrypted)
    except _Undo:
        out.unlink(missing_ok=True)
        raise IntakeError(UNDONE) from None
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    wiped = Counter({wipe.TOKEN_TEXT[cls]: n for cls, n in counts.items() if cls in wipe.WIPE_CLASSES and n})
    return IntakeResult(customer=cust, outputs=[out], public_report=public_written,
                        private_report=private_written, states={stem: "ok"}, unsealed=unsealed, wiped=wiped)


def _public_text(rec: _report.Run, engine: _Engine) -> str:
    """The public report, checked like an output against the registered forms and the structured patterns only:
    it carries no customer text, and a learned word its own template carries (Candidates, Files) must not
    withhold it (decision 22). Notes that carry a hit are withheld; when the report still carries one, only the
    counts go out."""
    text = _report.render_public(rec)
    if not engine.leaks(text, candidates=False):
        return text
    for fr in rec.files:
        for part in fr.parts:
            part.public_notes = ["notes withheld, see the private report"] if part.notes else []
    text = _report.render_public(rec)
    if not engine.leaks(text, candidates=False):
        return text
    text = _report.render_minimal(rec)
    if engine.leaks(text, candidates=False):
        raise IntakeError("the public report of %s does not pass the final check" % rec.customer)
    return text


def _write_outputs(rec, extractions, engine: _Engine, outdir: Path, outputs: list[Path],
                   check_failed: set[str], replace: bool = False) -> None:
    """Steps 6 and 7: sanitise every file, write its output and check it before and after writing: the
    registered forms, the structured data and the second check over the whole text, the candidate rules and the
    learned forms over the body."""
    for fr in rec.files:
        bodies: dict[str, str] = {}
        by_id = dict(extractions[fr.file_id])
        for part in fr.parts:
            pex = by_id[part.part_id]
            body, counts = engine.sanitize(pex.text or "", "%s output" % part.part_id)
            bodies[part.part_id] = body
            part.counts.update(counts)
            for note in part.notes:
                clean, _ = engine.sanitize(note, "%s note" % part.part_id)
                part.public_notes.append(clean)
        if not any(b.strip() for b in bodies.values()):
            fr.reasons.append("no text to write, no output")
            continue
        out = outdir / ("%s.md" % fr.file_id)
        text = _output_text(fr, bodies)
        hits = engine.leaks(text, candidates=False) + _body_hits(engine, text)
        if not hits:
            if replace:
                written = _report.write_replace(out, text, 0o640, 0o750)
            else:
                written = _report.write_new(out, text, 0o640, 0o750)
            if written != out:  # the file id was taken meanwhile: never write under another name
                written.unlink(missing_ok=True)
                raise IntakeError("output %s already exists" % fr.file_id)
            outputs.append(out)   # from here on a failure of the run removes it again
            written_text = out.read_text(encoding="utf-8")
            hits = engine.leaks(written_text, candidates=False) + _body_hits(engine, written_text)
            if hits:
                out.unlink(missing_ok=True)
                outputs.remove(out)
        if hits:
            check_failed.add(fr.file_id)
            fr.state = "failed"
            classes = _report.counts_text(Counter(h.cls for h in hits))
            fr.reasons.append("final check found %s, output deleted" % classes)
            fr.parts[0].public_notes.append("final check failed, output deleted")
            continue
        fr.output = out


def wipe_text(text: str, p: config.Paths) -> str:
    """One text through wipe mode without an import (the task description of a lab project, decision 16): an
    engine of its own with the register and the keep list, the forms its candidates teach, then the sanitising.
    The caller has checked the text against the register before; the self-test runs first."""
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test: %s" % "; ".join(failed))
    engine = _Engine(register.load(p.register))
    engine.keep = tuple(load_keep(p))
    engine.detect("text detect", text)
    clean, _ = engine.sanitize(text, "text output")
    if _body_hits(engine, clean) or engine.leaks(clean, candidates=False):
        raise IntakeError("the final check found something left in the text")
    return clean


def wiped_text(counts: Counter) -> str:
    """The wiped values per token class in one line: "person 3, company 1, name 2", or "nothing"."""
    items = [(word, counts.get(word, 0)) for word in ("person", "company", "place", "name")]
    return ", ".join("%s %d" % kv for kv in items if kv[1]) or "nothing"


def inbox_files(p: config.Paths) -> list[Path]:
    """Every regular, not hidden file directly in the vault inbox, sorted."""
    try:
        return sorted(f for f in p.inbox.iterdir() if f.is_file() and not f.name.startswith("."))
    except OSError:
        return []



# --------------------------------------------------------------------------- the parts (F1 step 8)

parts.join(__name__, "awb.intake.engine", "awb.intake.keep", classes=True)
