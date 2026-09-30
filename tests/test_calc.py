"""Tests of awb/calc.py (R-005) and of the calc evidence of the review.

A number an offer computes is computed by a program and recorded in the project; the review accepts a total only
with `calc:K-N` as its evidence and only when the recorded result is in the sentence.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from awb import calc, review
from tests.test_review import deliver, fill, project, row_with, run, start, writing  # noqa: F401 (fixtures)

SENT_TOTAL = "We size 20 VMs with 8 vCPU each, 160 vCPU in total."
TEXT = "# Sizing for CUST-Q7M4\n\n%s\n" % SENT_TOTAL


# --------------------------------------------------------------------------- the arithmetic


@pytest.mark.parametrize("expr, want", [
    ("20 * 8", "160"),
    ("0.1 + 0.2", "0.3"),
    ("730 * 0.0418 * 3", "91.542"),
    ("sum(12.5, 7.25, 0.25)", "20"),
    ("pct(30, 120)", "25"),
    ("round(2.345, 2)", "2.35"),
    ("round(2.5)", "3"),
    ("ceil(10 / 3)", "4"),
    ("floor(10 / 3)", "3"),
    ("max(3, 9, 4) - min(3, 9, 4)", "6"),
    ("-(4 - 10)", "6"),
    ("2 ** 10", "1024"),
    ("1_000 * 1.19", "1190"),
])
def test_exact_decimal_arithmetic(expr, want):
    assert calc.text(calc.evaluate(expr)) == want


def test_places_round_half_up_and_keep_zeros():
    assert calc.text(Decimal("91.545"), 2) == "91.55"
    assert calc.text(Decimal("20"), 2) == "20.00"


def test_lets_bind_in_order():
    names = calc.bind(["hours=730", "rate=0.0418", "month=hours * rate"])
    assert calc.text(calc.evaluate("month * 12", names)) == "366.168"


@pytest.mark.parametrize("expr", [
    "__import__('os').system('true')",
    "open('x')",
    "(1).real",
    "[1, 2]",
    "'12' * 3",
    "True + 1",
    "10 % 3",
    "2 ** 1000",
    "2 ** 0.5",
    "1 / 0",
    "pct(1, 0)",
    "x + 1",
    "lambda: 1",
    "1 if 2 else 3",
    "10 ** 29 * 10",
    "",
])
def test_anything_but_plain_arithmetic_is_refused(expr):
    with pytest.raises(calc.CalcError):
        calc.evaluate(expr)


@pytest.mark.parametrize("lets", [["X=1"], ["sum=1"], ["a=1", "a=2"], ["a"], ["a=b"]])
def test_bad_lets_are_refused(lets):
    with pytest.raises(calc.CalcError):
        calc.bind(lets)


def test_a_percent_sign_gets_a_hint():
    with pytest.raises(calc.CalcError, match=r"\* 0.19"):
        calc.evaluate("100 % 19")


# --------------------------------------------------------------------------- a result inside a sentence


@pytest.mark.parametrize("result, sentence", [
    ("160", SENT_TOTAL),
    ("1600", "It costs EUR 1,600 a month."),
    ("1600", "It costs 1.600 EUR a month."),
    ("1234.5678", "It costs 1.234,57 EUR."),
    ("1234.5678", "It costs 1,234.57 EUR."),
    ("91.542", "The month is 91.54 EUR."),
    ("-35", "It saves 35 EUR."),
    ("0.3", "Only 0,3 of it."),
])
def test_the_result_is_found_as_written(result, sentence):
    assert calc.appears_in(Decimal(result), sentence)


@pytest.mark.parametrize("result, sentence", [
    ("161", SENT_TOTAL),
    ("1.6", "It costs EUR 1,600.00 a month."),
    ("91.542", "The month is 91.6 EUR and more."),
    ("0.4", "No number but 0 here."),
    ("16", SENT_TOTAL),
])
def test_a_different_number_is_not_the_result(result, sentence):
    assert not calc.appears_in(Decimal(result), sentence)


# --------------------------------------------------------------------------- the record and the command


def test_the_command_records_in_the_project_with_ids_in_order(project, capsys):
    code, out, err = run(["calc", "20 * 8", "--project", str(project), "--label", "web tier vcpu"], capsys)
    assert code == 0, err
    assert out.splitlines()[0] == "K-1  20 * 8 = 160"
    assert "calc:K-1" in out
    code, out, _ = run(["calc", "hours * rate", "--let", "hours=730", "--let", "rate=0.0418", "--places", "2",
                        "--project", str(project)], capsys)
    assert code == 0 and out.startswith("K-2  hours * rate = 30.51")
    recs = calc.load(project)
    assert [r.id for r in recs] == ["K-1", "K-2"]
    assert recs[1].lets == "hours=730; rate=0.0418" and recs[0].label == "web tier vcpu"
    code, out, _ = run(["calc", "list", "--project", str(project)], capsys)
    assert code == 0 and "K-2  hours * rate = 30.51" in out
    code, out, _ = run(["calc", "show", "K-1", "--project", str(project)], capsys)
    assert code == 0 and "result: 160" in out


def test_dry_records_nothing_and_outside_a_project_refuses(project, tmp_path, capsys):
    code, out, _ = run(["calc", "2 + 2", "--dry", "--project", str(project)], capsys)
    assert code == 0 and "4  (not recorded)" in out
    assert calc.load(project) == []
    code, _, err = run(["calc", "2 + 2", "--project", str(tmp_path)], capsys)
    assert code == 2 and "no SCOPE.md" in err


def test_errors_never_echo_the_expression(project, capsys):
    code, _, err = run(["calc", "secretword + 1", "--project", str(project)], capsys)
    assert code == 2 and "secretword" not in err
    code, _, err = run(["calc", "1+1", "--label", "bad_label!", "--project", str(project)], capsys)
    assert code == 2 and "bad_label" not in err


def test_a_malformed_record_is_an_error_not_an_empty_list(project):
    (project / "calc").mkdir()
    (project / "calc" / "calc.tsv").write_text("id\tresult\nK-1\t3\n", encoding="utf-8")
    with pytest.raises(calc.CalcError):
        calc.load(project)


def test_a_linked_record_is_refused(project, tmp_path):
    target = tmp_path / "elsewhere.tsv"
    target.write_text("\t".join(calc.HEADER) + "\n", encoding="utf-8")
    (project / "calc").mkdir()
    (project / "calc" / "calc.tsv").symlink_to(target)
    with pytest.raises((calc.CalcError, OSError)):
        calc.record(project, "1+1", [], "2")
    with pytest.raises(calc.CalcError):
        calc.load(project)


# --------------------------------------------------------------------------- the review


def _total_row(project):
    return row_with(project, "160 vCPU in total")


def test_a_computed_total_with_a_file_as_evidence_blocks(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, text=TEXT)
    fill(project)
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "a computed number needs its calculation as evidence" in out


def test_a_computed_total_with_its_calculation_passes(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, text=TEXT)
    rec = calc.record(project, "20 * 8", [], "160")
    fill(project, evidence="calc:%s" % rec.id)
    code, out, err = run(["review", "l0", str(d)], capsys)
    assert code == 0, out + err


def test_a_calculation_with_another_result_blocks(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, text=TEXT)
    rec = calc.record(project, "20 * 7", [], "140")
    fill(project, evidence="calc:%s" % rec.id)
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "the result of the calculation given as evidence is not in the sentence" in out


def test_a_calculation_that_is_not_recorded_blocks(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, text=TEXT)
    fill(project, evidence="calc:K-9")
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "not recorded in the project" in out


def test_a_fetched_price_needs_no_calculation(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, text="# Offer\n\nThe price is 0.12 EUR per hour.\n")
    fill(project)
    code, out, err = run(["review", "l0", str(d)], capsys)
    assert code == 0, out + err


def test_the_arithmetic_detector():
    assert review._ARITH_RE.search(SENT_TOTAL)
    assert review._ARITH_RE.search("It runs 3 x 4 nodes.")
    assert review._ARITH_RE.search("A saving of 35 EUR.")
    assert not review._ARITH_RE.search("We propose s3.large.2 for 2 nodes.")
    assert not review._ARITH_RE.search("The SLA is 99.95 percent.")
