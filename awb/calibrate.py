"""The calibration set of the review (document 06, section 8): a review step is worth nothing until it has shown
that it can fail.

    awb review calibrate [--json]

`calibration/cases.json` holds invented texts. Each defect case plants one known failure and names the step that
must catch it; the clean cases plant nothing, so every finding on them is a false alarm. The runner takes the
script steps and runs them here, in a temporary folder that is removed afterwards:

    writing   the writing check of the text in its mode; caught when a blocking tell of the expected class shows
    names     the name check against a temporary register of names invented for the run from random letters
    l0        the level-0 check of a small invented project: contract, claim list filled as the case says,
              evidence file; caught when l0 blocks with the expected problem
    keep      `writing.keep` over the text before and after a voice pass; caught when the expected kind differs

The steps with a model (the blind verification, the lenses, the request check) run in the review workflow and the
price rows in `awb price check` against the live price API. Those cases are listed as not run: running them means
running the workflow on the set, which only he starts.

The report names case ids, classes, steps and counts, never a text. Exit 0 when every script case was caught and
the clean cases raised at most MAX_FALSE_ALARMS findings, else 1; 2 when the set cannot be read.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES_FILE = REPO / "calibration" / "cases.json"
RULES_DIR = REPO / "rules"
SCRIPT_STEPS = ("writing", "names", "l0", "keep")
OTHER_STEPS = ("workflow", "price")
MAX_FALSE_ALARMS = 2
DATE = "2026-09-25"
FILLED = {"evidence": None, "grade": "docs", "verdict": "supported"}
"""How the runner fills every row of a claim list before the case changes some: the clean baseline."""


class CalibrationError(Exception):
    """The set cannot be read or a case is malformed. Exit 2."""


@dataclass
class Row:
    id: str
    cls: str
    step: str
    state: str        # caught, missed, not run, clean, false alarm
    detail: str


@dataclass
class Report:
    rows: list[Row]
    caught: int
    missed: int
    not_run: int
    clean: int
    false_alarms: int

    @property
    def ok(self) -> bool:
        return self.missed == 0 and self.false_alarms <= MAX_FALSE_ALARMS


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads(Path(path or CASES_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise CalibrationError("the calibration set cannot be read") from None
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise CalibrationError("the calibration set holds no list of cases")
    seen = set()
    for c in data["cases"]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or c["id"] in seen:
            raise CalibrationError("a case has no id or an id is used twice")
        seen.add(c["id"])
        if c.get("step") not in SCRIPT_STEPS + OTHER_STEPS + ("clean",):
            raise CalibrationError("case %s names an unknown step" % c["id"])
    return data


_CONSONANTS = "bcdfghjklmnpqrstvwxz"
_VOWELS = "aeiouy"


def invented_names(rng: random.Random | None = None) -> dict[str, str]:
    """Fresh names for one run: a customer, a person, a mail address and a mail domain, made of random letters. No
    name of the set lives in the repository and no check can be tuned to a known one."""
    rng = rng or random.SystemRandom()

    def word(n: int) -> str:
        return "".join(rng.choice(_CONSONANTS if i % 2 == 0 else _VOWELS) for i in range(n)).capitalize()

    first, last, company = word(5), word(8), word(7)
    domain = "%s.example" % word(7).lower()
    return {"customer": "%s %s GmbH" % (company, word(6)), "person": "%s %s" % (first, last),
            "mail": "%s.%s@%s" % (first.lower(), last.lower(), domain), "domain": domain}


def _vendor() -> str:
    for line in (RULES_DIR / "vendor-names.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip()
    raise CalibrationError("rules/vendor-names.txt is empty")


_NAMES: dict[str, str] = {}
"""The invented names of the run in progress (set by `run`)."""


def _fill(text: str) -> str:
    if "{vendor}" in text:
        text = text.replace("{vendor}", _vendor())
    for key, value in _NAMES.items():
        text = text.replace("{%s}" % key, value)
    return text


def _text(case: dict, key: str = "text") -> str:
    text = case.get(key)
    if not isinstance(text, str):
        raise CalibrationError("case %s has no %s" % (case["id"], key))
    return _fill(text)


def _register(folder: Path, names: list[dict]) -> Path:
    from awb import register

    entries = [register.Entry(code=n["code"], kind=n["kind"], form=_fill(n["form"]), added=DATE, status="active")
               for n in names]
    path = folder / "register.tsv"
    path.write_text(register.render(entries), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- the script steps


def _writing(case: dict) -> tuple[bool, str]:
    """(found anything blocking, classes found) of the writing check."""
    from awb import writing

    tells, _ = writing.check_text(_text(case), mode=case.get("mode", "doc"))
    classes = sorted({t.cls for t in tells if t.blocking})
    return case.get("expect") in classes if case.get("step") == "writing" else bool(classes), ", ".join(classes)


def _names(case: dict, reg: Path) -> tuple[bool, str]:
    from awb import check

    hits = check.check_text(_text(case), reg)
    classes = sorted({h.get("cls", "") for h in hits})
    if case.get("step") == "names":
        return case.get("expect") in classes, ", ".join(classes)
    return bool(hits), ", ".join(classes)


def _l0(case: dict, folder: Path, reg: Path, evidence: str) -> tuple[bool, str]:
    from awb import review

    project = folder / ("tcp-cal%d" % (len(list(folder.glob("tcp-cal*"))) + 1))
    (project / "deliverables").mkdir(parents=True)
    ev = project / evidence
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("An invented source for the calibration set.\n", encoding="utf-8")
    name = case["id"].lower() + ".md"
    deliverable = project / "deliverables" / name
    text = _text(case)
    deliverable.write_text(text, encoding="utf-8")
    rdir = review.review_dir(project, name)
    rdir.mkdir(parents=True)
    contract = review.render_contract(name, int(case.get("tier", 1)), int(case.get("budget", 400)),
                                      case.get("mode", "doc"), "tcp", "", "Size the cluster of the test.", DATE)
    (rdir / review.CONTRACT).write_text(contract, encoding="utf-8")
    spec = case.get("claims") or {}
    drop = set(spec.get("drop") or ())
    rows = []
    for c in review.find_claims(text):
        kinds = set(c.kind.split(","))
        if kinds & drop:
            continue
        values = dict(FILLED, evidence=evidence)
        for kind, change in spec.items():
            if kind != "drop" and kind in kinds and isinstance(change, dict):
                values.update(change)
        rows.append(review.Claim(id=c.id, line=c.line, kind=c.kind, risk=c.risk, sentence=c.sentence,
                                 evidence=values["evidence"], grade=values["grade"], verdict=values["verdict"]))
    (rdir / review.CLAIMS).write_text(review.render_claims(rows), encoding="utf-8")
    res = review.l0(deliverable, register_path=reg, kb_where=folder / "no-kb")
    if case.get("step") == "l0":
        expect = case.get("expect", "")
        return any(expect in p for p in res.problems), "%d problems" % len(res.problems)
    return res.blocked, "%d problems" % len(res.problems)


def _keep(case: dict) -> tuple[bool, str]:
    from awb import writing

    differ = [k.kind for k in writing.keep(_text(case, "before"), _text(case, "after")) if not k.same]
    if case.get("step") == "keep":
        return case.get("expect") in differ, ", ".join(differ)
    return bool(differ), ", ".join(differ)


def _clean(case: dict, folder: Path, reg: Path, evidence: str) -> tuple[int, str]:
    """(false alarms, where) of a clean case: every script step that applies runs, every step that fires counts."""
    fired: list[str] = []
    if "text" in case:
        if _writing(case)[0]:
            fired.append("writing")
        if _names(case, reg)[0]:
            fired.append("names")
        if "tier" in case and _l0(case, folder, reg, evidence)[0]:
            fired.append("l0")
    if "before" in case and _keep(case)[0]:
        fired.append("keep")
    return len(fired), ", ".join(fired)


def run(path: Path | None = None, names: dict[str, str] | None = None) -> Report:
    data = load(path)
    evidence = data.get("evidence") or "evidence/source.md"
    rows: list[Row] = []
    _NAMES.clear()
    _NAMES.update(names or invented_names())
    folder = Path(tempfile.mkdtemp(prefix="awb-calibrate-"))
    try:
        reg = _register(folder, data.get("names") or [])
        for case in data["cases"]:
            step = case["step"]
            if step in OTHER_STEPS:
                rows.append(Row(case["id"], case.get("cls", ""), step, "not run",
                                "needs %s" % ("the review workflow" if step == "workflow" else "the live price API")))
                continue
            if step == "clean":
                alarms, where = _clean(case, folder, reg, evidence)
                rows.append(Row(case["id"], "clean", step, "false alarm" if alarms else "clean", where))
                continue
            if step == "writing":
                caught, detail = _writing(case)
            elif step == "names":
                caught, detail = _names(case, reg)
            elif step == "l0":
                caught, detail = _l0(case, folder, reg, evidence)
            else:
                caught, detail = _keep(case)
            rows.append(Row(case["id"], case.get("cls", ""), step, "caught" if caught else "missed", detail))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _NAMES.clear()
    count = lambda state: sum(1 for r in rows if r.state == state)   # noqa: E731
    false_alarms = sum(1 for r in rows if r.state == "false alarm")
    return Report(rows, count("caught"), count("missed"), count("not run"), count("clean") + false_alarms,
                  false_alarms)


WORKFLOW_CASES = REPO / "calibration" / "workflow-cases.json"
LENS_FILES = ("verify", "platform-tcp", "partner", "request-check")
EXTRA_CLASSES = ("content-nobody-asked-for", "review-record-in-text")
"""Defects the design reports without blocking: an EXTRA finding on them counts as caught."""


@dataclass
class LensRow:
    id: str
    cls: str
    state: str               # caught, missed, clean, false alarm, incomplete, pending
    by: list[str]            # lens: classes of its open blocking (or EXTRA) findings


def score_lenses(project: Path, path: Path | None = None) -> list[LensRow]:
    """The model steps of a calibration run: read the lens files the workflows wrote for each case in `project`
    (reviews/<id>.md/lenses/) and say per case which lens raised an open blocking finding. A defect of
    EXTRA_CLASSES also counts as caught by an EXTRA finding, which never blocks. Whether a finding names the
    planted defect is read by a person; this score only says where to look."""
    try:
        cases = json.loads(Path(path or WORKFLOW_CASES).read_text(encoding="utf-8"))["cases"]
    except (OSError, ValueError, KeyError):
        raise CalibrationError("the workflow cases cannot be read") from None
    rows: list[LensRow] = []
    for c in cases:
        by: list[str] = []
        missing = 0
        for lens in LENS_FILES:
            f = Path(project) / "reviews" / ("%s.md" % c["id"]) / "lenses" / ("%s.json" % lens)
            try:
                findings = json.loads(f.read_text(encoding="utf-8"))["findings"]
            except (OSError, ValueError, KeyError):
                missing += 1
                continue
            hit = sorted({str(x.get("class", "")) for x in findings if isinstance(x, dict)
                          and (x.get("blocking") is True or x.get("severity") == "blocking")
                          and x.get("outcome") not in ("refuted", "confirmed-fixed", "softened")})
            if c.get("cls") in EXTRA_CLASSES:
                hit += [k for k in sorted({str(x.get("class", "")) for x in findings if isinstance(x, dict)
                                           and x.get("class") == "EXTRA"}) if k not in hit]
            if hit:
                by.append("%s: %s" % (lens, ", ".join(hit)))
        if missing == len(LENS_FILES):
            state = "pending"
        elif c.get("cls") == "clean":
            state = "false alarm" if by else ("incomplete" if missing else "clean")
        else:
            state = "caught" if by else ("incomplete" if missing else "missed")
        rows.append(LensRow(c["id"], c.get("cls", ""), state, by))
    return rows


def main(argv: list[str] | None = None) -> int:
    """`awb review calibrate [--json]` and `awb review calibrate --lenses PROJECT`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--lenses"] and len(argv) == 2:
        try:
            rows = score_lenses(Path(argv[1]))
        except CalibrationError as err:
            print("awb review calibrate: %s" % err, file=sys.stderr)
            return 2
        for r in rows:
            print("%-4s  %-26s  %-11s  %s" % (r.id, r.cls, r.state, "; ".join(r.by)))
        count = lambda state: sum(1 for r in rows if r.state == state)   # noqa: E731
        print("calibrate: the workflows caught %d of %d seeded defects; %d false alarm(s) on %d clean texts; "
              "%d case(s) pending or incomplete" % (count("caught"), count("caught") + count("missed"),
                                                    count("false alarm"), count("clean") + count("false alarm"),
                                                    count("pending") + count("incomplete")))
        return 0 if not count("missed") and count("false alarm") <= MAX_FALSE_ALARMS else 1
    as_json = argv == ["--json"]
    if argv and not as_json:
        print("usage: awb review calibrate [--json] | --lenses PROJECT", file=sys.stderr)
        return 2
    try:
        report = run()
    except CalibrationError as err:
        print("awb review calibrate: %s" % err, file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps({"rows": [asdict(r) for r in report.rows], "caught": report.caught,
                          "missed": report.missed, "not_run": report.not_run, "clean": report.clean,
                          "false_alarms": report.false_alarms, "ok": report.ok}, indent=1))
    else:
        for r in report.rows:
            print("%-4s  %-26s  %-8s  %-11s  %s" % (r.id, r.cls, r.step, r.state, r.detail))
        script = report.caught + report.missed
        print("calibrate: the script steps caught %d of %d seeded defects; %d clean texts, %d false alarm(s) "
              "(at most %d); %d case(s) need the review workflow or the price API and were not run"
              % (report.caught, script, report.clean, report.false_alarms, MAX_FALSE_ALARMS, report.not_run))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
