"""The intake as a package (F1 step 8): the door of awb/intake.py, the wipe rules by family under awb/intake/wipe/
with the old path awb.wipe, and the public `intake.run_text` that the paste door runs through.

Every name is an invented fixture name.
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

import awb
from awb import intake, pastein, wipe
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
FAMILIES = ("basic", "lexicon", "proposed", "learned", "runstate")


# --------------------------------------------------------------------------- the door


def test_the_old_path_of_the_wipe_rules_is_the_package_itself():
    from awb.intake import wipe as package

    assert wipe is package is intake.wipe
    assert Path(wipe.__file__).parent == ROOT / "awb" / "intake" / "wipe"
    for family in FAMILIES:
        module = sys.modules["awb.intake.wipe.%s" % family]
        own = [k for k, v in vars(module).items()
               if not k.startswith("__") and getattr(v, "__module__", None) in (module.__name__, wipe.__name__)]
        assert own, family
        for name in own:
            assert getattr(wipe, name) is getattr(module, name), (family, name)


def test_every_part_of_the_intake_answers_through_the_old_module():
    from awb.intake import engine, keep

    for module in (engine, keep):
        for name, value in vars(module).items():
            if not name.startswith("__"):
                assert getattr(intake, name) is value, (module.__name__, name)
    assert intake.IntakeError is engine.IntakeError and intake.load_keep is keep.load_keep
    assert intake.run.__module__ == "awb.intake" and intake.run_text.__module__ == "awb.intake"


def test_the_classes_of_the_parts_name_the_old_module():
    """Their repr and the look of the suite's self-test cache over the classes of the old module stay as they were."""
    for cls in (intake._Engine, intake.IntakeError, intake.IntakeResult):
        assert cls.__module__ == "awb.intake", cls
    for cls in (wipe._Learned, wipe.WipeState, wipe.WipeSpan, wipe._Mask):
        assert cls.__module__ == wipe.__name__, cls


def test_a_name_set_on_the_door_reaches_the_family_that_reads_it(monkeypatch):
    lexicon = sys.modules["awb.intake.wipe.lexicon"]
    runstate = sys.modules["awb.intake.wipe.runstate"]
    word = fx.PLANTED_PERSON.split()[1]
    assert not lexicon._known_word(word)
    monkeypatch.setattr(wipe, "_VOCAB", wipe._VOCAB | {word.casefold()})
    assert lexicon._VOCAB is runstate._VOCAB is wipe._VOCAB
    assert lexicon._known_word(word)
    monkeypatch.undo()
    assert not lexicon._known_word(word)


def test_the_words_of_a_bound_run_reach_every_family():
    """`bind` rebinds _RUN_EXEMPT in runstate; the lexicon and the learned forms read it there, never a copy."""
    word = fx.SECOND_PERSON.split()[1]
    for family in ("lexicon", "learned"):
        assert "_RUN_EXEMPT" not in vars(sys.modules["awb.intake.wipe.%s" % family]), family
    state = wipe.WipeState()
    wipe.bind(state)
    try:
        assert not wipe._known_word(word)
        state.run_exempt.add(word.casefold())
        assert wipe._known_word(word)
    finally:
        wipe.bind(wipe.WipeState())


@pytest.mark.parametrize("first", ["awb.wipe", "awb.intake", "awb.intake.wipe.learned"])
def test_any_first_import_gives_the_one_package_and_a_passing_self_test(first):
    code = ("import %s, sys\nimport awb.wipe, awb.intake\n"
            "assert sys.modules['awb.wipe'] is awb.intake.wipe is awb.wipe\n"
            "assert awb.intake.selftest() == []\nprint('ok')\n" % first)
    res = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr[-2000:]


def test_the_package_never_imports_the_old_path():
    """awb/wipe.py would run half way through the intake's own import; no unit reaches it (deploy COMMAND_ONLY)."""
    for path in sorted((ROOT / "awb" / "intake").rglob("*.py")) + [ROOT / "awb" / "intake.py"]:
        text = path.read_text(encoding="utf-8")
        assert "from awb import wipe" not in text and "import awb.wipe" not in text, path
        assert not any(line.startswith("from awb import") and "wipe" in line.split("import", 1)[1].split()
                       for line in text.splitlines()), path


# --------------------------------------------------------------------------- run_text


def _shape(engine, text):
    engine.detect("note detect", text)
    body, counts = engine.sanitize(text, "note output")
    return body, None, counts, ["one note"], "%d lines" % body.count("\n")


def _write(written):
    def write(outdir: Path, body: str) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / ("note-%d.md" % (len(written) + 1))
        path.write_text(body, encoding="utf-8")
        written.append(path)
        return path
    return write


def test_run_text_takes_one_text_with_the_reports_of_a_file(home):
    text = "Vertrag mit %s, Kontakt %s.\n" % (fx.CUSTOMER_FORMS[0], fx.PLANTED_PERSON)
    written: list[Path] = []
    res = intake.run_text(text, fx.CUSTOMER_CODE, home, _shape, _write(written), label="note (text)", kind="note")
    assert res.outputs == written and res.customer == fx.CUSTOMER_CODE
    body = written[0].read_text(encoding="utf-8")
    assert fx.CUSTOMER_CODE in body and wipe.TOKEN_RE.search(body)
    fx.assert_no_fixture_name(body, "the copy of run_text")
    assert res.states == {written[0].name: "ok"} and sum(res.wiped.values()) >= 1
    public = res.public_report.read_text(encoding="utf-8")
    assert "note (text)" not in public and written[0].name in public
    fx.assert_no_fixture_name(public, "the public report of run_text")
    assert res.private_report.is_file() and "note (text)" in res.private_report.read_text(encoding="utf-8")


def test_run_text_writes_nothing_when_the_final_check_finds_something(home):
    text = "Vertrag mit %s.\n" % fx.CUSTOMER_FORMS[0]
    written: list[Path] = []

    def blind(engine, screened):
        return screened, None, Counter(), [], "1 lines"

    class Held(Exception):
        pass

    with pytest.raises(Held, match="name"):
        intake.run_text(text, fx.CUSTOMER_CODE, home, blind, _write(written), label="x", kind="x", held=Held)
    with pytest.raises(intake.IntakeError, match="withheld"):
        intake.run_text(text, fx.CUSTOMER_CODE, home, blind, _write(written), label="x", kind="x")
    assert written == [] and not (home.outbox / fx.CUSTOMER_CODE).exists()


def test_the_screen_refuses_before_the_register_is_read(home):
    home.register.write_text("not a register line\n", encoding="utf-8")

    def screen(text):
        raise ValueError("refused by the door")

    with pytest.raises(ValueError, match="refused by the door"):
        intake.run_text("x\n", fx.CUSTOMER_CODE, home, _shape, _write([]), label="x", kind="x", screen=screen)


def test_a_failed_report_takes_the_copy_back(home, monkeypatch):
    written: list[Path] = []

    def broken(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(intake._report, "write_public", broken)
    with pytest.raises(OSError):
        intake.run_text("nichts besonderes\n", fx.CUSTOMER_CODE, home, _shape, _write(written), label="x",
                        kind="x")
    assert len(written) == 1 and not written[0].exists()


def test_the_paste_door_reaches_no_private_name_of_the_run():
    """F1 section 1e: pastein repeated the driver of intake.run through _Engine, _body_hits, _public_text and
    _write_private_report. It runs through run_text now; what it still names of the intake is the engine it shapes
    on and the public names."""
    text = Path(pastein.__file__).read_text(encoding="utf-8")
    for name in ("_body_hits", "_public_text", "_write_private_report", "_resolve_customer", "_Undo", "UNDONE",
                 "load_keep", "vault_encrypted", "selftest"):
        assert "intake.%s" % name not in text, name
    assert "intake.run_text(" in text
