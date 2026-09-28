"""Red-team harness of the commit gate of the Architect Workbench.

    .venv/bin/python calibration/redteam/gate_harness.py calibration/redteam/gate.py --json /tmp/rt-gate.json \\
        [--expected calibration/redteam/EXPECTED.md] [--only ID ...]

The cases file defines CASES, a list of dicts:

    id        short unique id
    cls       the class the gate promises for it: name | secret | private-key | token | homepath | blocklist |
              identifier | opaque
    carrier   where and how the thing is written, in a few words
    path      the file name the content is committed under (relative, may carry folders)
    content   str or bytes, the file content
    note      optional

Each case is written under its path into a fresh temporary folder and scanned with awb.gate.scan_files against
the fixture register of invented names (tests/fixtures.py). A case escapes when no finding of its class is
reported for that file; the findings of every other class are listed too, because some shapes are reported
under a more specific class by design (a JWT as a token, a PEM in a kubeconfig as a private key). The code
under test is the repository that holds this file (AWB_REPO overrides it). Nothing here prints a value.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(os.environ.get("AWB_REPO") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(REPO))


def load_cases(path: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("rt_gate_cases_%d" % int(time.time() * 1000), path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    cases = list(getattr(mod, "CASES"))
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate case ids")
    return cases


def expected_ids(path: Path, dimension: str = "gate") -> set[str]:
    """The ids listed under the heading of `dimension` in EXPECTED.md (one "- id: reason" line each)."""
    ids: set[str] = set()
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip().split()[0]
        elif current == dimension and line.startswith("- "):
            ids.add(line[2:].split(":", 1)[0].strip())
    return ids


def fixture_register(root: Path) -> Path:
    from tests import fixtures

    reg = root / "register.tsv"
    reg.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    os.chmod(reg, 0o600)
    return reg


def scan_case(case: dict, register: Path, folder: Path) -> dict:
    """The case written under its path into `folder` and scanned; the result entry with the classes found."""
    from awb import gate

    f = folder / case["path"]
    f.parent.mkdir(parents=True, exist_ok=True)
    content = case["content"]
    f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    error = None
    try:
        findings = gate.scan_files([f], register)
    except Exception as err:  # noqa: BLE001
        findings, error = [], "%s: %s" % (type(err).__name__, str(err)[:200])
    classes = sorted({x.cls for x in findings})
    lines = sorted({(x.cls, x.line) for x in findings})
    caught = case["cls"] in classes
    return {"id": case["id"], "cls": case["cls"], "carrier": case.get("carrier", ""), "path": case["path"],
            "found": classes, "lines": [list(t) for t in lines], "escape": not caught, "error": error}


def line_of(entry: dict) -> str:
    return "%-28s %-12s %-8s found: %s%s" % (entry["id"], entry["cls"], "ESCAPE" if entry["escape"] else "caught",
                                             ",".join(entry["found"]) or "nothing", " error" if entry["error"] else "")


def main(argv=None) -> int:
    from awb import gate

    ap = argparse.ArgumentParser(description="the red-team cases of the commit gate")
    ap.add_argument("cases", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--expected", type=Path, default=None,
                    help="EXPECTED.md: report escapes not listed under gate (exit 1) and listed ids now caught")
    args = ap.parse_args(argv)
    os.chdir(REPO)
    # the invented blocklist of the tests, never the owner's
    gate.BLOCKLIST_FILES = (Path("/nonexistent/awb-blocklist"), Path("/nonexistent/awb-blocklist-home"))
    os.environ["AWB_BLOCKLIST"] = str(REPO / "tests" / "blocklist.txt")
    cases = load_cases(args.cases.resolve())
    assert Path(gate.__file__).resolve().is_relative_to(REPO.resolve()), "the gate under test is not the one of REPO"
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    root = Path(tempfile.mkdtemp(prefix="gate-rt-"))
    results = []
    try:
        reg = fixture_register(root)
        for i, case in enumerate(cases):
            entry = scan_case(case, reg, root / ("c%03d" % i))
            results.append(entry)
            print(line_of(entry))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    escapes = [r["id"] for r in results if r["escape"]]
    summary = {"cases": len(results), "escapes": escapes, "errors": [r["id"] for r in results if r["error"]]}
    print("summary: %d cases, %d escapes, %d errors" % (len(results), len(escapes), len(summary["errors"])))
    rc = 0
    if args.expected:
        listed = expected_ids(args.expected, args.cases.stem)
        ran = {r["id"] for r in results}
        left = {r["id"] for r in results if r["escape"] or r["error"]}
        new = sorted(left - listed)
        fixed = sorted((listed & ran) - left)
        print("expected: %d listed, %d new escape(s)%s, %d listed id(s) now caught%s"
              % (len(listed), len(new), (": " + ", ".join(new)) if new else "",
                 len(fixed), (": " + ", ".join(fixed)) if fixed else ""))
        rc = 1 if new else 0
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": summary, "results": results}, indent=1) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
