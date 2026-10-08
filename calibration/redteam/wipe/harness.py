"""Red-team harness of wipe mode: the cases of 2026-10-07 through the intake as it is built.

    .venv/bin/python calibration/redteam/wipe/harness.py CASES.py --work DIR [--json OUT] [--only ID ...] [--keep]
    .venv/bin/python calibration/redteam/wipe/harness.py --selfcheck --work DIR

A cases module (cases_<dimension>.py next to this file) defines CASES, a list of dicts:

    id        short unique id
    carrier   where the names sit, in a few words
    values    strings that must be unreadable in every output: a leak when one is readable (the readers' views
              of calibration/redteam/harness.py: raw, case folded, skeleton, leet, decoded, whole word for short ones)
    keep      strings that must stay readable in the outputs: a loss when one is not
    code      optional: True when the customer code CUST-Q7M4 must stand in an output (a registered form became
              the code)
    expect    optional: {value: class} the class the private report must give the value (person, company,
              place, name); reported as "class" when it differs
    build     callable(inbox) -> path: writes ONE file into the inbox and returns its path

Every case runs in a fresh throw-away Workbench with the invented register of tests/fixtures.py, through the real
`intake.run`: nothing is patched in, the harness installs nothing. The result per case: leaks (value index, file,
view), losses (keep index), withheld (the final check deleted the output), the token counts and the class of every
value as the private report of the throw-away vault recorded it. `--selfcheck` first shows that the harness can
fail: a lone lower-case first name that no rule sees must be reported as a leak, an engine broken on purpose (the
stop phrases wiped too) must be reported as a loss, and a caught case must come out clean.

The record of the red team is presentations/names/REDTEAM.md (outside the repository); EXPECTED.md of the pack lists
what is left per dimension. Invented values only. Nothing here prints a planted value: lines carry ids, indexes,
files and views.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(os.environ.get("AWB_REPO") or Path(__file__).resolve().parents[3])
sys.path.insert(0, str(REPO))

from calibration.redteam import harness as rt  # noqa: E402

HERE = Path(__file__).resolve().parent
TOKEN_RE = re.compile(r"\[(person|company|place|name)(?: \d+)?\]")
DIMENSIONS = ("transcripts", "mail_chains", "decks", "code_files", "tables", "german", "russian", "disguised",
              "losses")


def load_cases(path: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("wipe_cases_%s_%d" % (path.stem, int(time.time() * 1000)), path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    cases = list(getattr(mod, "CASES"))
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate case ids")
    return cases


def module_path(dimension: str) -> Path:
    return HERE / ("cases_%s.py" % dimension)


def _private_classes(report_text: str) -> dict[str, tuple[str, str]]:
    """value (casefold) -> (token, class) from the Wiped table of a private report (the token word as the class:
    person, company, place, name)."""
    out: dict[str, tuple[str, str]] = {}
    inside = False
    for line in report_text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Wiped"
            continue
        if not inside or not line.startswith("|") or line.startswith("| value") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip()[1:-1].split("|")]
        if len(cells) >= 3:
            m = TOKEN_RE.fullmatch(cells[2])
            out.setdefault(cells[0].casefold(), (cells[2], m.group(1) if m else cells[1]))
    return out


@contextlib.contextmanager
def broken(kind: str | None):
    """The engine broken on purpose for the selfcheck: "wipe-stop-phrases" wipes the stop phrases too."""
    if kind is None:
        yield
        return
    from awb import wipe
    from awb.matcher import Span

    if kind != "wipe-stop-phrases":
        raise ValueError("unknown break")
    original = wipe.candidate_spans

    def candidate_spans(state, text, known):
        spans = original(state, text, known)
        stops = [Span(s.start, s.end, "unknown", None, "broken on purpose") for s in wipe._stop_spans(text)]
        return [s for s in spans if not any(s.start < t.end and t.start < s.end for t in stops)] + stops

    wipe.candidate_spans = candidate_spans
    try:
        yield
    finally:
        wipe.candidate_spans = original


def run_once(case: dict, root: Path, broken_kind: str | None = None) -> dict:
    from awb import intake

    saved = {k: os.environ.get(k) for k in rt._ENV_KEYS}
    try:
        base, p = rt.fresh_workbench(root)
        rec: dict = {"error": None, "states": {}, "outputs": 0, "withheld": False, "leaks": [], "losses": [],
                     "kept": 0, "tokens": {}, "classes": {}, "code": None, "workbench": str(base)}
        texts: list[tuple[str, str]] = []
        outputs: list[str] = []
        try:
            src = Path(case["build"](p.inbox))
            if not src.is_file():
                raise RuntimeError("build did not return a file")
            with broken(broken_kind):
                res = intake.run([src], "CUST-Q7M4", p)
            rec["states"] = dict(res.states)
            rec["outputs"] = len(res.outputs)
            rec["withheld"] = bool(res.held)
            outputs = [o.read_text(encoding="utf-8", errors="replace") for o in res.outputs]
            if res.private_report.exists() and res.private_report.suffix != ".gpg":
                classes = _private_classes(res.private_report.read_text(encoding="utf-8", errors="replace"))
                for i, value in enumerate(case.get("values") or []):
                    hit = classes.get(value.casefold())
                    if hit:
                        rec["classes"][str(i)] = hit[1]
        except Exception as err:  # noqa: BLE001
            rec["error"] = type(err).__name__
            texts.append(("exception text", str(err)))
        texts.extend(rt.shared_texts(p))
        for i, value in enumerate(case.get("values") or []):
            for label, text in texts:
                how = rt.readable(value, text)
                if how:
                    rec["leaks"].append({"value": i, "file": label, "how": how})
        joined = "\n".join(outputs)
        for i, term in enumerate(case.get("keep") or []):
            if rt.readable(term, joined):
                rec["kept"] += 1
            else:
                rec["losses"].append({"term": i})
        rec["tokens"] = dict(Counter(m.group(1) for m in TOKEN_RE.finditer(joined)))
        if case.get("code"):
            rec["code"] = "CUST-Q7M4" in joined
        return rec
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_case(case: dict, work: Path, keep: bool = False) -> dict:
    started = time.time()
    rec = run_once(case, work)
    if not keep:
        shutil.rmtree(rec.pop("workbench"), ignore_errors=True)
    expect = case.get("expect") or {}
    wrong = []
    for i, value in enumerate(case.get("values") or []):
        want = expect.get(value)
        got = rec["classes"].get(str(i))
        if want and got and got != want:
            wrong.append({"value": i, "want": want, "got": got})
    rec["wrong_class"] = wrong
    return {"id": case["id"], "carrier": case.get("carrier", ""), "values": len(case.get("values") or []),
            "keeps": len(case.get("keep") or []), "run": rec, "seconds": round(time.time() - started, 1)}


def outcome(entry: dict) -> str:
    """One word per case: clean, leak, loss, leak+loss, withheld (any with an error is "error")."""
    rec = entry["run"]
    if rec["error"]:
        return "error"
    if rec["withheld"]:
        return "withheld"
    bits = [w for w, on in (("leak", rec["leaks"]), ("loss", rec["losses"])) if on]
    return "+".join(bits) or "clean"


def line_of(entry: dict) -> str:
    rec = entry["run"]
    bits = [outcome(entry), "leak %d" % len({l["value"] for l in rec["leaks"]}), "loss %d" % len(rec["losses"])]
    if rec["code"] is False:
        bits.append("NO-CODE")
    if rec.get("wrong_class"):
        bits.append("class %d" % len(rec["wrong_class"]))
    hows = sorted({l["how"] for l in rec["leaks"]})
    if hows:
        bits.append("via " + ",".join(hows))
    return "%-40s %s" % (entry["id"], " ".join(bits))


def summary_of(results: list[dict]) -> dict:
    runs = [e["run"] for e in results]
    return {
        "cases": len(runs),
        "leak_cases": sum(1 for x in runs if x["leaks"]),
        "leaked_values": sum(len({l["value"] for l in x["leaks"]}) for x in runs),
        "loss_cases": sum(1 for x in runs if x["losses"]),
        "lost_terms": sum(len(x["losses"]) for x in runs),
        "kept_terms": sum(x["kept"] for x in runs),
        "withheld": sum(1 for x in runs if x["withheld"]),
        "no_code": sum(1 for x in runs if x["code"] is False),
        "wrong_class": sum(len(x.get("wrong_class") or []) for x in runs),
        "errors": sum(1 for x in runs if x["error"]),
    }


# --------------------------------------------------------------------------- the proof that the harness can fail

def _txt(name, text):
    def build(inbox):
        p = inbox / name
        p.write_text(text, encoding="utf-8")
        return p
    return build


def sentinel_cases() -> dict[str, dict]:
    from tests import fixtures as fx

    first = fx.PLANTED_PERSON.split()[0]
    return {
        # a lone lower-case first name in prose: no rule sees it, so it must be reported as a leak
        "sentinel-leak": {"id": "sentinel-leak", "values": [first], "keep": ["Load Balancer"],
                          "build": _txt("note.txt", "wir haben mit %s gesprochen, der Load Balancer steht.\n"
                                        % first.lower())},
        # a title in front of a planted person: caught, the stop phrase stays, the customer becomes the code
        "sentinel-caught": {"id": "sentinel-caught", "values": [fx.PLANTED_PERSON], "keep": ["Load Balancer"],
                            "code": True,
                            "build": _txt("note.txt", "Sehr geehrter Herr %s, der Load Balancer von %s steht.\n"
                                          % (fx.PLANTED_PERSON, fx.CUSTOMER_FORMS[0]))},
    }


def selfcheck(work: Path) -> list[str]:
    """The failures of the proof, empty when the harness reports the planted leak, the forced loss and a clean
    caught case."""
    failures = []
    cases = sentinel_cases()
    rec = run_once(cases["sentinel-leak"], work)
    shutil.rmtree(rec.pop("workbench"), ignore_errors=True)
    if not rec["leaks"]:
        failures.append("the sentinel leak was not reported")
    if rec["losses"]:
        failures.append("the sentinel's kept term was reported lost")
    rec = run_once(cases["sentinel-caught"], work, broken_kind="wipe-stop-phrases")
    shutil.rmtree(rec.pop("workbench"), ignore_errors=True)
    if not rec["losses"]:
        failures.append("a wiped stop phrase was not reported as a loss")
    rec = run_once(cases["sentinel-caught"], work)
    shutil.rmtree(rec.pop("workbench"), ignore_errors=True)
    if rec["leaks"] or rec["losses"] or rec["code"] is not True or rec["error"]:
        failures.append("the caught sentinel was not clean (leaks %d, losses %d, code %s, error %s)"
                        % (len(rec["leaks"]), len(rec["losses"]), rec["code"], rec["error"]))
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the red-team cases of wipe mode through the built intake")
    ap.add_argument("cases", type=Path, nargs="?")
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--keep", action="store_true", help="keep the throw-away Workbenches")
    ap.add_argument("--selfcheck", action="store_true", help="prove the harness can fail, then run the cases")
    args = ap.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    os.chdir(REPO)
    rt.code_under_test()
    if args.selfcheck or args.cases is None:
        failures = selfcheck(args.work)
        if failures:
            print("SELFCHECK FAILED: " + "; ".join(failures))
            return 2
        print("selfcheck: the harness reports the planted leak and the forced loss, and clears the caught case")
        if args.cases is None:
            return 0
    cases = load_cases(args.cases.resolve())
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    results = []
    for case in cases:
        entry = run_case(case, args.work, keep=args.keep)
        results.append(entry)
        print(line_of(entry), flush=True)
    s = summary_of(results)
    print("summary: %d cases, %d with a leak (%d values), %d with a loss (%d terms, %d kept), %d withheld, "
          "%d without the code, %d wrong classes, %d errors"
          % (s["cases"], s["leak_cases"], s["leaked_values"], s["loss_cases"], s["lost_terms"], s["kept_terms"],
             s["withheld"], s["no_code"], s["wrong_class"], s["errors"]))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": s, "results": results}, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
