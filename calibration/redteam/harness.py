"""Red-team harness of the intake of the Architect Workbench.

    .venv/bin/python calibration/redteam/harness.py calibration/redteam/structured.py --work /tmp/rt-work \\
        --json /tmp/rt-structured.json [--expected calibration/redteam/EXPECTED.md] [--only ID ...] [--keep]

The cases file is a Python module that defines CASES, a list of dicts:

    id        short unique id (letters, digits, dashes)
    cls       the class the planted value belongs to: name | mail | url | ip | mac | iban | bic | vat | phone |
              hrb | tax | other
    carrier   where the value sits, in a few words ("docx comment author", "pdf XMP dc:creator")
    values    the planted strings; each must be unreadable in every output (the registered forms used, or the
              structured value). Registered forms come from tests/fixtures.py of the repository.
    visible   optional: innocent marker strings written into the same carrier next to the value (such as
              "stage two of the invented plan"); when no output carries any of them, the carrier was never read
              and the case is reported as UNSEEN (content that reaches nobody, so it is never reviewed)
    build     callable(inbox: pathlib.Path) -> pathlib.Path: writes ONE file into inbox, returns its path
    note      optional

Every case runs in a fresh throw-away Workbench (its own shared side, vault side and register of invented
names). The intake runs without --force first; when it blocks (unregistered name candidates), it runs again
with --force in another fresh Workbench, because --force is a real path of the owner. Then everything a
session could read on the shared side (the outbox, the public report, file names) is searched for every
planted value in the readers' views: raw, case folded, a reader's skeleton (compatibility forms and look-alike
letters folded, accents and separators dropped), whole-word for short values, leet substitutions, reversed
when a bidi control is present, and the decoded content of base64 and hex blocks. The private report and the
originals lie on the vault side and are not outputs.

The code under test is the repository that holds this file (AWB_REPO overrides it); it is put first on the
path and checked before any case module can put another checkout there.

Nothing here prints a planted value: results carry ids, classes, carriers and the view that read the value.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import html as _html
import unicodedata
import urllib.parse
from pathlib import Path

REPO = Path(os.environ.get("AWB_REPO") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(REPO))

_ENV_KEYS = ("AWB_SHARED", "AWB_VAULT", "AWB_PROJECTS", "AWB_KB", "AWB_CHECK_SOCKET", "AWB_CONF", "AWB_ADMIN_SOCKET")

_LOOKALIKE = str.maketrans(
    "аеорсухѕіјһԁԛԝӏкɡАВЕКМНОРСТУХЅІЈԚԜҺӀҮΑΒΕΖΗΙΚΜΝΟΡΤΥΧϹϿͿοαιικυχγϲνρϳ",
    "aeopcyxsijhdqwlkgABEKMHOPCTYXSIJQWHIYABEZHIKMNOPTYXCOJoaiikuxycvpj",
)
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_-])")
_HEX_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{10,}(?![0-9A-Fa-f])")
_TAG_RE = re.compile(r"<[^<>]*>")
_BIDI_RE = re.compile("[‪-‮⁦-⁩]")
_LEET_BASE = {"0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "|": "l", "!": "i", "8": "b",
              "6": "g", "2": "z", "9": "g"}
_LEET_L = str.maketrans({**_LEET_BASE, "1": "l"})
_LEET_I = str.maketrans({**_LEET_BASE, "1": "i"})


def code_under_test() -> Path:
    """The folder of the awb package that is imported, which must lie under REPO."""
    import awb

    folder = Path(awb.__file__).resolve().parent
    if not folder.is_relative_to(REPO.resolve()):
        raise SystemExit("the awb package under test is %s, not the one of %s" % (folder, REPO))
    return folder


def skeleton(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).translate(_LOOKALIKE)
    t = t.replace("œ", "oe").replace("æ", "ae").replace("ß", "ss")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", t.casefold())


def decoded_blocks(text: str) -> list[str]:
    out: list[str] = []
    for m in _B64_RE.finditer(text):
        raw = m.group(0)
        for candidate in (raw, raw.replace("-", "+").replace("_", "/")):
            pad = candidate + "=" * (-len(candidate) % 4)
            try:
                data = base64.b64decode(pad, validate=True)
            except (binascii.Error, ValueError):
                continue
            for enc in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
                try:
                    out.append(data.decode(enc))
                except UnicodeDecodeError:
                    pass
            break
    for m in _HEX_RE.finditer(text):
        h = m.group(0)
        if len(h) % 2:
            continue
        try:
            data = bytes.fromhex(h)
        except ValueError:
            continue
        for enc in ("utf-8", "latin-1"):
            try:
                out.append(data.decode(enc))
            except UnicodeDecodeError:
                pass
    return out


def views_of(text: str) -> dict[str, str]:
    """Readers' views of an output: as written, with html tags dropped, with entities and percent escapes
    decoded (a Markdown viewer renders them), with leet substitutions (0 for o, 3 for e ...) and, when bidi
    controls are present, reversed."""
    stripped = _TAG_RE.sub("", text)
    unescaped = urllib.parse.unquote(_html.unescape(stripped))
    views = {
        "raw": text,
        "case": text.casefold(),
        "skeleton": skeleton(text),
        "skeleton-notags": skeleton(stripped),
        "skeleton-unescaped": skeleton(unescaped),
        "leet": skeleton(unescaped.translate(_LEET_L)),
        "leet-i": skeleton(unescaped.translate(_LEET_I)),
        "word": " " + re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", unescaped).casefold()) + " ",
    }
    if _BIDI_RE.search(text):
        views["reversed"] = skeleton(unescaped)[::-1]
    return views


def readable(value: str, text: str) -> str | None:
    """The name of the first view in which `value` is readable in `text`, else None. A short value (an acronym,
    fewer than five letters and digits) counts only as a whole word, so that it is not read inside another word."""
    if not value:
        return None
    v = views_of(text)
    sk = skeleton(value)
    if len(sk) < 5:
        if sk and " %s " % sk in v["word"]:
            return "word"
        for block in decoded_blocks(text):
            if sk and " %s " % sk in views_of(block)["word"]:
                return "decoded"
        return None
    if value in text:
        return "raw"
    if value.casefold() in v["case"]:
        return "case"
    for name in ("skeleton", "skeleton-notags", "skeleton-unescaped", "leet", "leet-i", "reversed"):
        if name in v and sk in v[name]:
            return name
    for block in decoded_blocks(text):
        if value.casefold() in block.casefold() or sk in skeleton(block):
            return "decoded"
    return None


def load_cases(path: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("rt_cases_%d" % int(time.time() * 1000), path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    cases = list(getattr(mod, "CASES"))
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate case ids")
    return cases


def expected_ids(path: Path, dimension: str) -> set[str]:
    """The ids listed under the heading of `dimension` in EXPECTED.md (one "- id: reason" line each)."""
    ids: set[str] = set()
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip().split()[0]
        elif current == dimension and line.startswith("- "):
            ids.add(line[2:].split(":", 1)[0].strip())
    return ids


def fresh_workbench(root: Path):
    """A throw-away Workbench under `root` with the fixture register of invented names."""
    from awb import config
    from tests import fixtures

    base = Path(tempfile.mkdtemp(prefix="wb-", dir=str(root)))
    os.environ["AWB_SHARED"] = str(base / "tcp-shared")
    os.environ["AWB_VAULT"] = str(base / "tcp-vault")
    os.environ["AWB_PROJECTS"] = str(base / "projects")
    os.environ["AWB_KB"] = str(base / "tcp-kb")
    os.environ["AWB_CHECK_SOCKET"] = str(base / "run" / "check.sock")
    os.environ["AWB_CONF"] = str(base / "no-host-conf")
    os.environ.pop("AWB_ADMIN_SOCKET", None)
    p = config.ensure_layout(config.paths())
    p.register.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    os.chmod(p.register, 0o600)
    return base, p


def shared_texts(p) -> list[tuple[str, str]]:
    """(label, text) of everything on the shared side: file names and file contents."""
    out: list[tuple[str, str]] = []
    for f in sorted(p.shared.rglob("*")):
        rel = str(f.relative_to(p.shared))
        out.append(("path %s" % rel, rel))
        if f.is_file():
            try:
                out.append((rel, f.read_bytes().decode("utf-8", errors="replace")))
            except OSError:
                out.append((rel, ""))
    return out


def run_once(case: dict, root: Path, force: bool) -> dict:
    """One intake of the case in a fresh Workbench under `root`; the AWB_* environment is put back afterwards."""
    from awb import intake

    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        base, p = fresh_workbench(root)
        rec: dict = {"force": force, "blocked": None, "candidates": None, "error": None, "states": {},
                     "outputs": 0, "output_chars": 0, "where": [], "notes": 0, "leftover_tmp": 0, "workbench": str(base)}
        texts: list[tuple[str, str]] = []
        try:
            src = Path(case["build"](p.inbox))
            if not src.is_file():
                raise RuntimeError("build did not return a file")
            res = intake.run([src], "CUST-Q7M4", p, force=force)
            rec["blocked"] = res.blocked
            rec["candidates"] = res.candidates
            rec["states"] = dict(res.states)
            rec["outputs"] = len(res.outputs)
            rec["output_chars"] = sum(len(o.read_text(encoding="utf-8", errors="replace")) for o in res.outputs)
            if res.public_report.exists():
                rec["notes"] = res.public_report.read_text(encoding="utf-8", errors="replace").count("notes:")
        except Exception as err:  # noqa: BLE001
            rec["error"] = "%s: %s" % (type(err).__name__, str(err)[:300])
            texts.append(("exception text", str(err)))
        texts.extend(shared_texts(p))
        tmp = p.vault / "tmp"
        rec["leftover_tmp"] = sum(1 for _ in tmp.rglob("*")) if tmp.exists() else 0
        for i, value in enumerate(case.get("values") or []):
            for label, text in texts:
                how = readable(value, text)
                if how:
                    rec["where"].append({"value": i, "file": label, "how": how})
        # innocent marker strings planted next to the value: when none of the outputs carries one, the carrier
        # was not read at all (unseen content). A marker that is readable without its first letter was clipped:
        # the replacement of the form before it swallowed that letter (a defect of the sanitiser, not a leak).
        rec["unseen"] = []
        rec["clipped"] = []
        outputs = [text for label, text in texts if not label.startswith("path ")]
        for i, marker in enumerate(case.get("visible") or []):
            if not any(readable(marker, text) for text in outputs):
                rec["unseen"].append(i)
                if len(marker) > 6 and any(readable(marker[1:], text) for text in outputs):
                    rec["clipped"].append(i)
        return rec
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_case(case: dict, work: Path, keep: bool = False) -> dict:
    """The case without --force and, when that run blocked, once more with --force. Returns the result entry:
    id, cls, carrier, the runs, and the verdict fields escape, forced_only, unseen (marker indexes), error."""
    entry = {"id": case["id"], "cls": case.get("cls", "?"), "carrier": case.get("carrier", ""),
             "values": len(case.get("values") or [])}
    started = time.time()
    first = run_once(case, work, force=False)
    entry["unforced"] = first
    if first["blocked"]:
        entry["forced"] = run_once(case, work, force=True)
    entry["seconds"] = round(time.time() - started, 1)
    runs = [first] + ([entry["forced"]] if "forced" in entry else [])
    where = [w for r in runs for w in r["where"]]
    entry["escape"] = bool(where)
    entry["forced_only"] = bool(where) and not first["where"]
    entry["unseen"] = sorted({i for r in runs for i in r["unseen"]}) if all(r["unseen"] for r in runs) else []
    entry["clipped"] = sorted(i for i in entry["unseen"] if any(i in r["clipped"] for r in runs))
    entry["error"] = first["error"] or (entry.get("forced") or {}).get("error")
    entry["hows"] = sorted({w["how"] for w in where})
    if not keep:
        for r in runs:
            shutil.rmtree(r["workbench"], ignore_errors=True)
            r.pop("workbench", None)
    return entry


def line_of(entry: dict) -> str:
    state = "ESCAPE" if entry["escape"] else ("error" if entry["error"] else "caught")
    extra = ""
    if entry["unforced"]["blocked"]:
        extra += " blocked(%d)" % (entry["unforced"]["candidates"] or 0)
    if entry["forced_only"]:
        extra += " forced-only"
    if entry["error"]:
        extra += " error"
    if entry["unseen"]:
        extra += " UNSEEN(%d)" % len(entry["unseen"])
    if entry.get("clipped"):
        extra += " clipped(%d)" % len(entry["clipped"])
    hows = entry.get("hows") or []
    if entry.get("seconds", 0) >= 10:
        extra += " slow(%ds)" % entry["seconds"]
    return "%-28s %-6s %-8s%s%s" % (entry["id"], entry["cls"], state, extra, (" via " + ",".join(hows)) if hows else "")


def leftovers(results: list[dict]) -> set[str]:
    """The ids a run leaves over: an escape (forced-only included), an unseen carrier or an error."""
    return {r["id"] for r in results if r["escape"] or r["unseen"] or r["error"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the red-team cases of one dimension through the intake")
    ap.add_argument("cases", type=Path)
    ap.add_argument("--work", type=Path, required=True, help="folder for the throw-away Workbenches")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--skip", nargs="*", default=None, help="ids to leave out (a case that hangs the intake)")
    ap.add_argument("--keep", action="store_true", help="keep the throw-away Workbenches (default: removed)")
    ap.add_argument("--expected", type=Path, default=None,
                    help="EXPECTED.md: report leftovers not listed under this dimension (exit 1) and listed ids now clean")
    args = ap.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    os.chdir(REPO)
    # the code under test is imported before any cases file can put another checkout on the path
    import awb.intake  # noqa: F401
    import tests.fixtures  # noqa: F401
    code_under_test()
    cases = load_cases(args.cases.resolve())
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    if args.skip:
        cases = [c for c in cases if c["id"] not in set(args.skip)]
    results = []
    for case in cases:
        entry = run_case(case, args.work, keep=args.keep)
        results.append(entry)
        print(line_of(entry))
    escapes = [r["id"] for r in results if r["escape"]]
    summary = {"cases": len(results), "escapes": escapes, "errors": [r["id"] for r in results if r["error"]],
               "blocked": [r["id"] for r in results if r["unforced"]["blocked"]],
               "unseen": [r["id"] for r in results if r["unseen"]],
               "clipped": [r["id"] for r in results if r["clipped"]],
               "forced_only": [r["id"] for r in results if r["forced_only"]]}
    print("summary: %d cases, %d escapes (%d forced-only), %d unseen (%d of them clipped), %d blocked without "
          "--force, %d errors" % (len(results), len(escapes), len(summary["forced_only"]), len(summary["unseen"]),
                                  len(summary["clipped"]), len(summary["blocked"]), len(summary["errors"])))
    rc = 0
    if args.expected:
        listed = expected_ids(args.expected, args.cases.stem)
        ran = {r["id"] for r in results}
        new = sorted(leftovers(results) - listed)
        fixed = sorted((listed & ran) - leftovers(results))
        print("expected: %d listed, %d new leftover(s)%s, %d listed id(s) now clean%s"
              % (len(listed), len(new), (": " + ", ".join(new)) if new else "",
                 len(fixed), (": " + ", ".join(fixed)) if fixed else ""))
        rc = 1 if new else 0
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": summary, "results": results}, indent=1) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
