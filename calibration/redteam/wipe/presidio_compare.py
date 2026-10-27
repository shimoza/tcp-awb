"""The wipe red team with the open source analyzer Presidio next to `awb clean --kind docs` (task PP, 2026-10-27).

    PYTHON calibration/redteam/wipe/presidio_compare.py --work DIR [--json OUT] [--only DIMENSION ...] [--langs en de]
        [--entities TYPE ...]

PYTHON is a scratch environment outside the repository that holds presidio-analyzer, presidio-anonymizer and the
spaCy models (en_core_web_lg, de_core_news_lg) next to the test packages of pyproject.toml. Nothing under awb/
imports Presidio: this file is a measurement, not a part of the Workbench, and pytest never collects it.

Every case of the pack (cases_<dimension>.py) is read three ways:

    ours      `clean.run(..., kind="docs")` in a throw-away laptop folder with the invented register, as
              clean_compare.py builds it
    presidio  Presidio alone, with no register: the text the readers of awb/extract give for the original file
              (every part, as the docs copy writes them) and the file name, each replaced by its entity type
    both      Presidio as a second pass over our copies and their names; the report stays as written

Leaks and losses are read with the readers' views of calibration/redteam/harness.py over every output, its name
and (for ours and both) the report, the same way clean_compare.py reads them. With more than one language the
analyzer runs once per language and the spans are joined, since a second opinion cannot know the language of a
text. For every leak of ours that the second pass closes and every term that Presidio destroys, the entity types
of the spans that cover it are recorded.

Invented values only. Nothing here prints a planted value: lines carry ids, counts, entity types and seconds. A
lost term is a public technical term of the pack and may be named in the record.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(os.environ.get("AWB_REPO") or Path(__file__).resolve().parents[3])
sys.path.insert(0, str(REPO))

from calibration.redteam import harness as rt  # noqa: E402
from calibration.redteam.wipe import clean_compare as cc  # noqa: E402
from calibration.redteam.wipe import harness as wh  # noqa: E402

MODELS = {"en": "en_core_web_lg", "de": "de_core_news_lg"}


class Second:
    """Presidio with its default recognizers for each language; the pattern recognizers of the default registry
    are English only, so for German they are added again under "de"."""

    def __init__(self, langs, entities=None):
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        self.langs = list(langs)
        self.entities = list(entities) if entities else None
        conf = {"nlp_engine_name": "spacy",
                "models": [{"lang_code": l, "model_name": MODELS[l]} for l in self.langs]}
        nlp = NlpEngineProvider(nlp_configuration=conf).create_engine()
        registry = RecognizerRegistry(supported_languages=self.langs)
        registry.load_predefined_recognizers(languages=self.langs, nlp_engine=nlp)
        if "de" in self.langs:
            self._german_patterns(registry)
        self.analyzer = AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=self.langs)
        self.anonymizer = AnonymizerEngine()
        self.OperatorConfig = OperatorConfig
        self.seconds = 0.0
        self.calls = 0

    @staticmethod
    def _german_patterns(registry):
        from presidio_analyzer import PatternRecognizer

        have = {(r.name, l) for r in registry.recognizers for l in [r.supported_language]}
        for r in list(registry.recognizers):
            if r.supported_language != "en" or (r.name, "de") in have or "Spacy" in r.name:
                continue
            if isinstance(r, PatternRecognizer):
                registry.add_recognizer(PatternRecognizer(
                    supported_entity=r.supported_entities[0], name=r.name, supported_language="de",
                    patterns=r.patterns, context=r.context, deny_list=getattr(r, "deny_list", None) or None))
                continue
            try:
                clone = type(r)(supported_language="de")
            except TypeError:
                continue
            registry.add_recognizer(clone)

    def spans(self, text: str) -> list:
        if not text.strip():
            return []
        started = time.time()
        found = []
        for lang in self.langs:
            found += self.analyzer.analyze(text=text, language=lang, entities=self.entities)
        self.seconds += time.time() - started
        self.calls += 1
        return found

    def redact(self, text: str) -> tuple[str, list]:
        found = self.spans(text)
        if not found:
            return text, []
        out = self.anonymizer.anonymize(text=text, analyzer_results=found,
                                        operators={"DEFAULT": self.OperatorConfig("replace", {})}).text
        return out, found


def _types_covering(term: str, text: str, spans: list) -> list[str]:
    """The entity types of the spans that overlap an occurrence of `term` in `text` (case folded)."""
    low, t = text.casefold(), term.casefold()
    types: set[str] = set()
    start = low.find(t)
    while start >= 0:
        end = start + len(t)
        types |= {s.entity_type for s in spans if s.start < end and start < s.end}
        start = low.find(t, start + 1)
    return sorted(types)


def _read(case: dict, texts: list[tuple[str, str]], outputs: list[str]) -> dict:
    rec = {"leaks": [], "losses": [], "kept": 0}
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
    return rec


def ours_and_both(case: dict, root: Path, second: Second) -> tuple[dict, dict]:
    from awb import clean

    base = cc.fresh_laptop(root)
    names, bodies, report, error = [], [], None, None
    started = time.time()
    try:
        inbox = base / "inbox"
        inbox.mkdir()
        src = Path(case["build"](inbox))
        res = clean.run([src], cc.PROJECT, "docs", base / "out", say=lambda s: None)
        for o in res.outputs:
            names.append(o.name)
            bodies.append(o.read_text(encoding="utf-8", errors="replace"))
        if res.report:
            report = res.report.read_text(encoding="utf-8")
    except Exception as err:  # noqa: BLE001
        error = err
    t_ours = time.time() - started
    shutil.rmtree(base, ignore_errors=True)

    def texts_of(ns, bs):
        t = [x for n, b in zip(ns, bs) for x in (("name", n), ("copy", b))]
        if report:
            t.append(("report", report))
        if error is not None:
            t.append(("exception text", str(error)))
        return t

    ours = _read(case, texts_of(names, bodies), bodies)
    ours.update({"error": type(error).__name__ if error else None, "seconds": t_ours, "outputs": len(bodies)})
    started = time.time()
    s_names, s_bodies, spans_of = [], [], []
    for n, b in zip(names, bodies):
        sn, _ = second.redact(n)
        sb, sp = second.redact(b)
        s_names.append(sn)
        s_bodies.append(sb)
        spans_of.append((b, sp))
    both = _read(case, texts_of(s_names, s_bodies), s_bodies)
    both.update({"error": ours["error"], "seconds": time.time() - started, "outputs": len(s_bodies)})
    _attribute(case, ours, both, spans_of)
    return ours, both


def presidio_alone(case: dict, root: Path, second: Second) -> dict:
    from awb import extract as _extract
    from awb import intake

    base = Path(tempfile.mkdtemp(prefix="pp-", dir=str(root)))
    rec_error, names, bodies, spans_of = None, [], [], []
    started = time.time()
    try:
        inbox = base / "inbox"
        inbox.mkdir()
        src = Path(case["build"](inbox))
        with _extract.temp_root(base / "tmp"):
            ex = _extract.extract(src)
        parts = []
        for i, (_pid, pex) in enumerate(intake._walk(ex, "1")):
            if (pex.text or "").strip():
                parts.append(pex.text.strip("\n") if i == 0 else "## part %d\n\n%s" % (i, pex.text.strip("\n")))
        if parts:
            text = "\n\n".join(parts) + "\n"
            body, sp = second.redact(text)
            name, _ = second.redact(src.name)
            names.append(name)
            bodies.append(body)
            spans_of.append((text, sp))
    except Exception as err:  # noqa: BLE001
        rec_error = err
    t = time.time() - started
    shutil.rmtree(base, ignore_errors=True)
    texts = [x for n, b in zip(names, bodies) for x in (("name", n), ("copy", b))]
    if rec_error is not None:
        texts.append(("exception text", str(rec_error)))
    rec = _read(case, texts, bodies)
    rec.update({"error": type(rec_error).__name__ if rec_error else None, "seconds": t, "outputs": len(bodies)})
    rec["loss_types"] = []
    for li in rec["losses"]:
        term = case["keep"][li["term"]]
        types = sorted({ty for text, sp in spans_of for ty in _types_covering(term, text, sp)})
        rec["loss_types"].append({"term": li["term"], "types": types})
    return rec


def _attribute(case: dict, ours: dict, both: dict, spans_of: list) -> None:
    """Which entity types closed a leak of ours and which destroyed a term ours kept."""
    leaked_ours = {l["value"] for l in ours["leaks"]}
    leaked_both = {l["value"] for l in both["leaks"]}
    both["closed"] = []
    for i in sorted(leaked_ours - leaked_both):
        value = case["values"][i]
        types = sorted({ty for text, sp in spans_of for ty in _types_covering(value, text, sp)})
        if not types:  # read through a skeleton or leet view: name every type of a span inside the copy
            types = sorted({s.entity_type for _t, sp in spans_of for s in sp})
        both["closed"].append({"value": i, "types": types})
    lost_ours = {l["term"] for l in ours["losses"]}
    both["new_loss_types"] = []
    for li in both["losses"]:
        if li["term"] in lost_ours:
            continue
        term = case["keep"][li["term"]]
        types = sorted({ty for text, sp in spans_of for ty in _types_covering(term, text, sp)})
        both["new_loss_types"].append({"term": li["term"], "types": types})


def _tally(runs: list[dict]) -> dict:
    return {"cases": len(runs), "leak_cases": sum(1 for r in runs if r["leaks"]),
            "loss_cases": sum(1 for r in runs if r["losses"]), "lost_terms": sum(len(r["losses"]) for r in runs),
            "errors": sum(1 for r in runs if r["error"]), "no_output": sum(1 for r in runs if not r["outputs"]),
            "seconds": round(sum(r["seconds"] for r in runs), 1)}


def compare(work: Path, dimensions, langs, entities=None) -> dict:
    second = Second(langs, entities)
    out: dict = {"langs": list(langs), "entities": entities or "default", "dimensions": {}, "cases": []}
    for dim in dimensions:
        cases = wh.load_cases(wh.module_path(dim))
        runs = {"ours": [], "presidio": [], "both": []}
        for case in cases:
            ours, both = ours_and_both(case, work, second)
            alone = presidio_alone(case, work, second)
            runs["ours"].append(ours)
            runs["both"].append(both)
            runs["presidio"].append(alone)

            def word(r):
                return "+".join(w for w, on in (("leak", r["leaks"]), ("loss", r["losses"])) if on) or "clean"

            entry = {"id": case["id"], "dimension": dim, "ours": word(ours), "presidio": word(alone),
                     "both": word(both), "closed": both["closed"], "new_losses": [
                         {"term": case["keep"][x["term"]], "types": x["types"]} for x in both["new_loss_types"]],
                     "presidio_losses": [
                         {"term": case["keep"][x["term"]], "types": x["types"]} for x in alone["loss_types"]],
                     "seconds": {"ours": round(ours["seconds"], 3), "presidio": round(alone["seconds"], 3),
                                 "second_pass": round(both["seconds"], 3)}}
            out["cases"].append(entry)
        out["dimensions"][dim] = {k: _tally(v) for k, v in runs.items()}
        d = out["dimensions"][dim]
        print("%-12s ours leak %3d loss %3d | presidio leak %3d loss %3d | both leak %3d loss %3d"
              % (dim, d["ours"]["leak_cases"], d["ours"]["loss_cases"], d["presidio"]["leak_cases"],
                 d["presidio"]["loss_cases"], d["both"]["leak_cases"], d["both"]["loss_cases"]), flush=True)
    out["analyzer_seconds"] = round(second.seconds, 1)
    out["analyzer_calls"] = second.calls
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--json")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--langs", nargs="*", default=["en", "de"])
    ap.add_argument("--entities", nargs="*", default=None, help="only these entity types (default: all)")
    args = ap.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    rt.code_under_test()
    started = time.time()
    result = compare(work, args.only or wh.DIMENSIONS, args.langs, args.entities)
    result["load_and_run_seconds"] = round(time.time() - started, 1)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
