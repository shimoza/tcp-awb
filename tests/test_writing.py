"""Tests of awb/writing.py: the writing checker (`awb write check`) and the voice learner (`awb voice learn`).

Every planted text is invented. The vendor name is read from rules/vendor-names.txt, so this file does not spell
it. Decision tested here: fenced code blocks and inline code are skipped (a banned word in code is a literal), a
fence that is never closed is not code. Line 0 of a tell is the file name.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from awb import cli, writing
from tests import fixtures as fx

REPO = Path(__file__).resolve().parent.parent
RULES = REPO / "rules"


def _first_rule(name: str) -> str:
    for line in (RULES / name).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip()
    raise AssertionError("rules/%s is empty" % name)


VENDOR = _first_rule("vendor-names.txt")
PLANTED_LINE = 5


def planted(sentence: str) -> tuple[str, int]:
    """A small document with the sentence starting on line 5."""
    return "# Plan\n\nThe quota is fine.\n\n%s\n\nThe next step is a test.\n" % sentence, PLANTED_LINE


def blocking(tells) -> list[tuple[str, int]]:
    return [(t.cls, t.line) for t in tells if t.blocking]


PROBE = "This is a robust design."


def assert_only_the_probe(text: str, **kw) -> None:
    """A negative check that cannot pass on a checker that finds nothing: a known banned word is appended on its
    own line and must be the one blocking tell."""
    full = text.rstrip("\n") + "\n\n" + PROBE + "\n"
    line = full.rstrip("\n").count("\n") + 1
    assert blocking(writing.check_text(full, **kw)[0]) == [("banned-word", line)]


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'\u2019-]+", text.lower())


def assert_hint_quotes_at_most_one_word(hint: str, sentence: str) -> None:
    assert hint, "a tell without a hint"
    quoted = re.findall(r'"([^"]*)"', hint)
    assert len(quoted) <= 1, "the hint quotes more than once"
    assert all(len(q.split()) == 1 for q in quoted), "the hint quotes more than one word"
    s, h = _words(sentence), _words(hint)
    pairs = {tuple(s[i:i + 2]) for i in range(len(s) - 1)}
    assert not pairs & {tuple(h[i:i + 2]) for i in range(len(h) - 1)}, "the hint repeats the sentence"


# --------------------------------------------------------------------------- blocking classes

CASES = [
    ("em-dash", "The plan is simple \u2014 we test first.", "doc", None),
    ("em-dash", "The plan is simple \u2013 we test first.", "doc", None),
    ("comma-and-or", "We use the cluster, and we test it.", "doc", "and"),
    ("comma-and-or", "We test it today, or we test it next week.", "doc", "or"),
    ("banned-word", "This is a robust design.", "doc", "robust"),
    ("banned-phrase", "Let us circle back next week.", "doc", "circle"),
    ("we-in-mail", "Our team checked the quota.", "mail", "our"),
    ("commitment", "I will send the numbers on the next day.", "doc", "will"),
    ("earlier-mail", "As mentioned, the quota is fine.", "doc", "mentioned"),
    ("vendor-name", "The old stack ran on %s hardware." % VENDOR, "doc", None),
    ("certification", "The site has TISAX.", "doc", "TISAX"),
    ("certification", "The site is audited for ISO 27001 this year.", "doc", "ISO"),
    ("bold-lead-list", "- **Plan:** one\n- **Test:** two\n- **Run:** three", "doc", None),
]


@pytest.mark.parametrize("cls,sentence,mode,word", CASES, ids=["%s-%d" % (c[0], i) for i, c in enumerate(CASES)])
def test_each_blocking_class_is_found_once_on_its_line(cls, sentence, mode, word):
    text, line = planted(sentence)
    tells, metrics = writing.check_text(text, mode=mode)
    assert blocking(tells) == [(cls, line)]
    tell = next(t for t in tells if t.blocking)
    assert_hint_quotes_at_most_one_word(tell.hint, sentence)
    if word:
        assert '"%s"' % word in tell.hint
    assert VENDOR.lower() not in tell.hint.lower()
    assert metrics["blocking"] == 1


def test_every_class_is_known_and_blocking_classes_are_blocking():
    assert set(writing.BLOCKING) == {c[0] for c in CASES}
    assert not set(writing.BLOCKING) & set(writing.REPORTED)


def test_the_clean_document_has_no_blocking_tell():
    text, _ = planted("The cluster runs in two zones.")
    tells, metrics = writing.check_text(text)
    assert blocking(tells) == []
    assert metrics["blocking"] == 0 and metrics["sentences"] == 3


def test_every_banned_word_of_the_rules_file_is_found():
    words = writing.load_rules().banned_words
    assert "however" in words and len(words) > 20
    for w in words:
        tells, _ = writing.check_text("The %s is here." % w.upper())
        found = [t for t in tells if t.blocking]
        assert [(t.cls, t.line) for t in found] == [("banned-word", 1)], "rule word %d" % words.index(w)
        assert '"%s"' % w in found[0].hint


def test_every_banned_phrase_of_the_rules_file_is_found_once_as_a_phrase():
    phrases = writing.load_rules().banned_phrases
    assert len(phrases) > 10
    for n, ph in enumerate(phrases):
        # a line break inside the phrase still matches
        text = "The team said %s here." % ph.replace(" ", "\n", 1)
        tells, _ = writing.check_text(text)
        assert [t.cls for t in tells if t.blocking] == ["banned-phrase"], "rule phrase %d" % n
        assert_hint_quotes_at_most_one_word(tells[0].hint, ph)


def test_whole_words_only():
    for text in ("The robustness is fine.", "The ensured state is fine.", "We navigated it.",
                 "The ensure_state flag is set.", "The file docs/robust.md is short."):
        assert_only_the_probe(text)


def test_all_commitments_and_earlier_mail_pointers():
    for text in ("I'll send it.", "We will send it.", "We'll send it.", "I am raising the quota.",
                 "I\u2019m raising the quota.", "The team will come back to it.", "I promise a fix.",
                 "I commit to the date."):
        assert [c for c, _ in blocking(writing.check_text(text)[0])] == ["commitment"], text
    for text in ("As discussed, the quota is fine.", "In my last mail the quota was fine.",
                 "As per my previous note, the quota is fine.", "As I wrote, the quota is fine."):
        assert [c for c, _ in blocking(writing.check_text(text)[0])] == ["earlier-mail"], text


def test_all_certifications():
    for text in ("The BSI rules apply.", "It has SOC 2 Type II.", "It is KRITIS relevant.", "It has a C5 report.",
                 "It follows IT-Grundschutz.", "It has ISO/IEC 27001."):
        assert [c for c, _ in blocking(writing.check_text(text)[0])] == ["certification"], text
    # flavor names and lower-case letters are not a certification
    assert blocking(writing.check_text("The c5.large flavor runs the bsi test.")[0]) == []


# --------------------------------------------------------------------------- controls

CONTROLS = [
    "We use Terraform and Ansible for the setup.",
    "The setup is PoC-ready next week.",
    "The test takes 10\u201320 minutes.",
    "The link has low latency, bandwidth is fine.",
    "The bandwidth is fine for the test.",
    "- **Plan:** one\n- test two\n- run three\n- check four",
    "- **Plan:** one\n- **Test:** two",
    "```\nhowever this is code, and it stays\n```",
    "~~~text\nThe robust \u2014 code\n~~~",
    "Use `however` only in code.",
    "Use ``a robust, and odd`` literal.",
    "Read https://example.org/robust-however, then stop.",
    "The %s/provider path is a literal." % VENDOR.lower(),
    "The package `%s-sdk` is a literal." % VENDOR.lower(),
]


@pytest.mark.parametrize("text", CONTROLS, ids=["control-%d" % i for i in range(len(CONTROLS))])
def test_controls_do_not_trigger(text):
    assert_only_the_probe(planted(text)[0])


def test_the_counterparts_of_the_controls_do_trigger():
    assert blocking(writing.check_text("We use Terraform, and Ansible.")[0]) == [("comma-and-or", 1)]
    assert blocking(writing.check_text("The setup is ready \u2014 next week.")[0]) == [("em-dash", 1)]
    assert blocking(writing.check_text("The test takes 10 \u2013 20 minutes.")[0]) == [("em-dash", 1)]
    assert blocking(writing.check_text("Text.\n\nhowever it works.\n")[0]) == [("banned-word", 3)]


def test_code_blocks_and_inline_code_are_skipped_but_an_open_fence_is_not_code():
    text = "Intro.\n\n```python\nhowever = 1\n```\n\nThe `however` flag.\n\nHowever, it runs.\n"
    assert blocking(writing.check_text(text)[0]) == [("banned-word", 9)]
    text = "Intro.\n\n```\nhowever it runs.\n"
    assert blocking(writing.check_text(text)[0]) == [("banned-word", 4)]


def test_mode_mail_refuses_we_and_our_mode_doc_allows_them():
    text = "Intro.\n\nWe checked the quota and our team agrees with us.\n"
    tells, _ = writing.check_text(text, mode="mail")
    assert blocking(tells) == [("we-in-mail", 3)] * 3
    assert sorted(re.findall(r'"(\w+)"', " ".join(t.hint for t in tells))) == ["our", "us", "we"]
    assert blocking(writing.check_text(text, mode="doc")[0]) == []
    assert blocking(writing.check_text(text, mode="chat")[0]) == []
    # the country in capitals and a region name are not the pronoun
    assert_only_the_probe("The region us-east is in the US.", mode="mail")


def test_scope_hcs_allows_the_vendor_name():
    text, line = planted("The old stack ran on %s hardware." % VENDOR)
    assert blocking(writing.check_text(text, scope="tcp")[0]) == [("vendor-name", line)]
    assert blocking(writing.check_text(text, scope="hcs")[0]) == []


def test_chat_mode_reports_a_bold_lead_list_without_blocking():
    text = "- **Plan:** one\n- **Test:** two\n- **Run:** three\n- **Ship:** four\n"
    assert blocking(writing.check_text(text)[0]) == [("bold-lead-list", 1)]
    tells, _ = writing.check_text(text, mode="chat")
    assert blocking(tells) == []
    assert [(t.cls, t.line) for t in tells if not t.blocking] == [("bold-lead-list", 1)]


def test_headings_and_tables_are_checked():
    text = "# A robust plan\n\n| step | note |\n|---|---|\n| one | fast \u2014 cheap |\n"
    assert blocking(writing.check_text(text)[0]) == [("banned-word", 1), ("em-dash", 5)]


def test_unknown_mode_or_scope_is_an_error():
    with pytest.raises(writing.WritingError):
        writing.check_text("Text.", mode="letter")
    with pytest.raises(writing.WritingError):
        writing.check_text("Text.", scope="other")


# --------------------------------------------------------------------------- reported, not blocking


def test_metrics_median_and_p90_on_a_known_text():
    sentences = " ".join(" ".join(["data"] * n).capitalize() + "." for n in range(2, 21, 2))
    text = "# Title words here\n\n| a | b |\n|---|---|\n| c | d |\n\n%s\n" % sentences
    tells, m = writing.check_text(text)
    assert m["sentences"] == 10
    assert m["words"] == 110
    assert m["median_sentence_words"] == 11
    assert m["p90_sentence_words"] == 18      # nearest rank: the 9th of 10
    assert m["targets"]["median_sentence_words"] == 11 and m["targets"]["p90_sentence_words"] == 21
    assert m["long_words"] == 0
    assert [t for t in tells if t.cls == "sentence-length"] == []


def test_long_sentences_are_reported_not_blocking():
    long = " ".join(["data"] * 30) + "."
    tells, m = writing.check_text("Short one.\n\n%s\n" % long)
    assert m["median_sentence_words"] == 16 and m["p90_sentence_words"] == 30
    found = [t for t in tells if t.cls == "sentence-length"]
    assert len(found) == 1 and not found[0].blocking and found[0].line == 3
    assert blocking(tells) == []


def test_long_words_and_rates():
    tells, m = writing.check_text("The infrastructure documentation has bandwidth data.")
    assert m["long_words"] == 2
    _, m = writing.check_text("I think my test works for me.")
    assert m["i_my_me_per_1000"] == round(3 * 1000 / 7, 1)
    _, m = writing.check_text("We test it, and it runs.")
    assert m["comma_before_and_or_per_1000"] == round(1000 / 6, 1)


def test_lists_of_exactly_three_are_reported_not_blocking():
    text = ("# Lists\n\n- one\n- two\n- three\n\nThe setup is fast, cheap and simple.\n\n"
            "- one\n- two\n- three\n- four\n\nWe checked ecs, evs, obs and vpc.\n\n"
            "If it fails, we retry and log it.\n")
    tells, m = writing.check_text(text)
    assert blocking(tells) == []
    found = [(t.line, t.blocking) for t in tells if t.cls == "list-of-three"]
    assert found == [(3, False), (7, False)]
    assert m["lists_of_three"] == 2
    assert m["reported"] == len([t for t in tells if not t.blocking])


# --------------------------------------------------------------------------- files


def test_the_file_name_is_checked(tmp_path):
    body = "The cluster runs in two zones.\n"
    f = tmp_path / "robust-plan.md"
    f.write_text(body, encoding="utf-8")
    tells, metrics = writing.check_file(f)
    assert blocking(tells) == [("banned-word", 0)]
    assert tells[0].hint.startswith("file name:")
    assert metrics["blocking"] == 1
    g = tmp_path / "plan_for_the_test.md"
    g.write_text(body, encoding="utf-8")
    assert blocking(writing.check_file(g)[0]) == []
    v = tmp_path / ("%s_plan.md" % VENDOR.lower())
    v.write_text(body, encoding="utf-8")
    assert blocking(writing.check_file(v)[0]) == [("vendor-name", 0)]
    assert blocking(writing.check_file(v, scope="hcs")[0]) == []


def test_check_file_line_numbers_match_the_file(tmp_path):
    f = tmp_path / "note.md"
    f.write_bytes(b"# Note\r\n\r\nThe plan is fine.\r\nThis is a robust design.\r\n")
    assert blocking(writing.check_file(f)[0]) == [("banned-word", 4)]


def test_an_office_file_is_read_through_the_extractor(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("The plan is fine.")
    d.add_paragraph("This is a robust design.")
    f = tmp_path / "note.docx"
    d.save(f)
    tells, _ = writing.check_file(f)
    assert [c for c, _ in blocking(tells)] == ["banned-word"]


def test_a_file_without_text_is_an_error_never_clean(tmp_path):
    f = tmp_path / "picture.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(writing.WritingError):
        writing.check_file(f)
    with pytest.raises(OSError):
        writing.check_file(tmp_path / "missing.md")


# --------------------------------------------------------------------------- awb write check


def _tell_lines(out: str) -> list[tuple[str, int, str]]:
    return [(m.group(1), int(m.group(2)), m.group(3))
            for m in re.finditer(r"^([a-z-]+)  (\d+)  (.+)$", out, re.MULTILINE)]


def test_cli_write_check_prints_class_line_hint_and_metrics(tmp_path, capsys):
    f = tmp_path / "note.md"
    f.write_text("# Note\n\nThis is a robust design.\n", encoding="utf-8")
    assert cli.main(["write", "check", str(f)]) == 1
    out = capsys.readouterr().out
    lines = _tell_lines(out)
    assert lines[0][:2] == ("banned-word", 3) and '"robust"' in lines[0][2]
    assert "median words per sentence  5 (target 11)" in out
    assert "p90 words per sentence  5 (target 21)" in out
    assert "words longer than nine letters  0" in out
    assert "result: 1 blocking" in out
    f.write_text("# Note\n\nThe design is fine.\n", encoding="utf-8")
    assert cli.main(["write", "check", str(f)]) == 0
    assert _tell_lines(capsys.readouterr().out) == []


def test_cli_write_check_mode_and_scope(tmp_path, capsys):
    f = tmp_path / "note.md"
    f.write_text("Our team runs it on %s hardware.\n" % VENDOR, encoding="utf-8")
    assert cli.main(["write", "check", "--mode", "mail", "--scope", "hcs", str(f)]) == 1
    assert [c for c, _, _ in _tell_lines(capsys.readouterr().out)] == ["we-in-mail"]
    assert cli.main(["write", "check", "--scope", "hcs", str(f)]) == 0
    capsys.readouterr()
    assert cli.main(["write", "check", str(f)]) == 1
    assert [c for c, _, _ in _tell_lines(capsys.readouterr().out)] == ["vendor-name"]


def test_cli_write_check_reports_lists_of_three_without_failing(tmp_path, capsys):
    f = tmp_path / "note.md"
    f.write_text("- one\n- two\n- three\n", encoding="utf-8")
    assert cli.main(["write", "check", str(f)]) == 0
    out = capsys.readouterr().out
    assert _tell_lines(out)[0][:2] == ("list-of-three", 1) and "(not blocking)" in out


def test_cli_write_check_never_prints_the_file_name(tmp_path, capsys):
    f = tmp_path / ("%s-plan.md" % fx.CUSTOMER_FORMS[1])
    f.write_text("This is a robust design.\n", encoding="utf-8")
    assert cli.main(["write", "check", str(f)]) == 1
    missing = tmp_path / ("%s-gone.md" % fx.CUSTOMER_FORMS[1])
    assert cli.main(["write", "check", str(missing)]) == 2
    out, err = capsys.readouterr()
    fx.assert_no_fixture_name(out + err, "the output of awb write check")
    assert "operating system error" in err


def test_cli_write_usage_errors(tmp_path, capsys):
    assert cli.main(["write"]) == 2
    assert cli.main(["write", "check"]) == 2
    f = tmp_path / "note.md"
    f.write_text("Text.\n", encoding="utf-8")
    assert cli.main(["write", "check", "--mode", "letter", str(f)]) == 2
    png = tmp_path / "picture.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert cli.main(["write", "check", str(png)]) == 2
    assert "cannot be read as text" in capsys.readouterr().err


# --------------------------------------------------------------------------- awb voice learn

DRAFT = ("We will leverage the new cluster in order to move fast, and it is basically robust. The rollout is done "
         "in three careful steps over the next two weeks with the whole team.\n")
SENT = "I use the new cluster to move fast. The rollout takes three steps.\n"


@pytest.fixture
def day(monkeypatch) -> str:
    monkeypatch.setattr(writing, "today", lambda: date.fromisoformat(fx.TODAY))
    return fx.TODAY


def _pair(tmp_path: Path, draft: str, sent: str) -> tuple[Path, Path]:
    d, s = tmp_path / "in-draft.txt", tmp_path / "in-sent.txt"
    d.write_text(draft, encoding="utf-8")
    s.write_text(sent, encoding="utf-8")
    return d, s


def _section(report: str, heading: str, pair: str | None = None) -> list[str]:
    """The bullet lines under `heading` (inside the section of `pair` when given)."""
    if pair is not None:
        report = report.split("## Pair %s" % pair, 1)[1].split("\n## ", 1)[0]
    body = report.split(heading + "\n", 1)[1]
    out = []
    for line in body.splitlines()[1:]:
        if not line.startswith("- "):
            break
        out.append(line[2:])
    return out


def _rules_bytes() -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(RULES.glob("*.txt"))}


def test_voice_learn_writes_the_pair_and_a_suggestions_report(home, day, tmp_path, capsys):
    rules_before = _rules_bytes()
    d, s = _pair(tmp_path, DRAFT, SENT)
    assert cli.main(["voice", "learn", str(d), str(s)]) == 0
    pair = home.shared / "voice" / "pairs" / ("%s-1" % day)
    assert (pair / "draft.md").read_text(encoding="utf-8") == DRAFT
    assert (pair / "sent.md").read_text(encoding="utf-8") == SENT
    report = (home.shared / "voice" / ("suggestions-%s.md" % day)).read_text(encoding="utf-8")
    removed = _section(report, "### Words removed (in the draft, not in the sent text)")
    assert "leverage" in removed and "basically" in removed and "use" not in removed
    assert "use" in _section(report, "### Words added") and "takes" in _section(report, "### Words added")
    assert "we will leverage" in _section(report, "### Phrases removed")
    # draft: 16 and 17 words per sentence, sent: 8 and 5
    assert "- words per sentence, median: 16.5 in the draft, 6.5 as sent (change -10)" in report
    assert "- words per sentence, p90: 17 in the draft, 8 as sent (change -9)" in report
    assert re.search(r"^- commas before and/or per 1000 words: 30\.3 in the draft, 0 as sent \(change -30\.3\)$",
                     report, re.MULTILINE)
    assert _section(report, "### Words") == ["none"]
    out = capsys.readouterr().out
    assert "pair %s-1" % day in out and "report voice/suggestions-%s.md" % day in out
    assert _rules_bytes() == rules_before, "voice learn must never edit the rules"


def test_a_word_removed_in_two_pairs_is_a_candidate(home, day, tmp_path, capsys):
    d, s = _pair(tmp_path, DRAFT, SENT)
    assert cli.main(["voice", "learn", str(d), str(s)]) == 0
    d, s = _pair(tmp_path, "The fix is basically done and we leverage it.\n", "The fix is done.\n")
    assert cli.main(["voice", "learn", str(d), str(s)]) == 0
    assert (home.shared / "voice" / "pairs" / ("%s-2" % day)).is_dir()
    report = (home.shared / "voice" / ("suggestions-%s.md" % day)).read_text(encoding="utf-8")
    assert "## Pair %s-1" % day in report and "## Pair %s-2" % day in report
    assert "basically" in _section(report, "### Words removed (in the draft, not in the sent text)", "%s-2" % day)
    words = _section(report, "### Words")
    assert "basically (2 pairs)" in words
    # a banned word is no candidate, it is banned already; a function word is none either
    assert not [w for w in words if w.startswith(("leverage", "we ", "and "))]


@pytest.mark.parametrize("which", ["draft", "sent"])
def test_voice_learn_refuses_a_pair_with_a_fixture_form(home, day, tmp_path, capsys, which):
    bad = "I sent the plan to %s today.\n" % fx.PERSON_FORMS[0]
    d, s = _pair(tmp_path, bad if which == "draft" else DRAFT, bad if which == "sent" else SENT)
    assert cli.main(["voice", "learn", str(d), str(s)]) == 1
    assert not (home.shared / "voice").exists()
    out, err = capsys.readouterr()
    fx.assert_no_fixture_name(out + err, "the output of awb voice learn")
    assert "name check" in err and "nothing was written" in err


def test_voice_learn_refuses_structured_data(home, day, tmp_path, capsys):
    d, s = _pair(tmp_path, DRAFT, "Call me on +49 40 123456-78.\n")
    assert cli.main(["voice", "learn", str(d), str(s)]) == 1
    assert "123456" not in "".join(capsys.readouterr())
    assert not (home.shared / "voice").exists()


def test_voice_learn_without_a_register_cannot_check_and_stops(home, day, tmp_path, capsys):
    home.register.unlink()
    d, s = _pair(tmp_path, DRAFT, SENT)
    assert cli.main(["voice", "learn", str(d), str(s)]) == 2
    assert "names cannot be checked" in capsys.readouterr().err
    assert not (home.shared / "voice").exists()


def test_voice_learn_usage_and_read_errors(home, day, tmp_path, capsys):
    assert cli.main(["voice"]) == 2
    assert cli.main(["voice", "learn", str(tmp_path / "one.txt")]) == 2
    d, s = _pair(tmp_path, DRAFT, "")
    assert cli.main(["voice", "learn", str(d), str(s)]) == 2
    assert cli.main(["voice", "learn", str(d), str(tmp_path / "missing.txt")]) == 2
    assert not (home.shared / "voice").exists()


def test_the_learn_function_returns_what_it_wrote(home, day, tmp_path):
    d, s = _pair(tmp_path, DRAFT, SENT)
    res = writing.learn(d, s, home)
    assert res.pair == "%s-1" % day and res.pair_dir.is_dir() and res.report.is_file()
    assert res.removed_words >= 10 and res.added_words == 3 and res.candidates == 0


# --------------------------------------------------------------------------- rules


def test_rule_files_are_read_with_comments_and_repeats_dropped(tmp_path):
    for name in ("banned-words.txt", "banned-phrases.txt", "vendor-names.txt"):
        (tmp_path / name).write_text("# a comment\nFoo\nfoo\n\nbar  baz\n", encoding="utf-8")
    (tmp_path / "voice.txt").write_text("# targets\nmedian_sentence_words = 9\nconnectors = and, so\n",
                                        encoding="utf-8")
    rules = writing.load_rules(tmp_path)
    assert rules.banned_words == ("foo", "bar baz")
    assert rules.vendor_names == ("Foo", "bar baz")
    assert rules.targets["median_sentence_words"] == 9 and rules.targets["p90_sentence_words"] == 21
    (tmp_path / "banned-words.txt").unlink()
    with pytest.raises(writing.WritingError):
        writing.load_rules(tmp_path)


# --------------------------------------------------------------------------- allowed technical terms (2026-09-23)


@pytest.mark.parametrize("text", [
    "We run a Cloud Security Posture Management tool on the tenant.",
    "The SAP landscape has three systems.",
    "The agent has a small memory footprint.",
    "Control Tower guardrails stay as they are.",
])
def test_a_banned_word_inside_an_allowed_technical_term_is_not_reported(text):
    tells, _ = writing.check_text(text)
    assert not [t for t in tells if t.cls in ("banned-word", "banned-phrase")], [t.cls for t in tells]


@pytest.mark.parametrize("text", [
    "Our security posture is strong.",
    "This changes the whole landscape.",
    "We keep the footprint small.",
    "However, the SAP landscape is fine.",
])
def test_the_same_word_outside_an_allowed_term_is_still_reported(text):
    tells, _ = writing.check_text(text)
    assert [t for t in tells if t.cls == "banned-word"], "the banned word must still be found outside an allowed term"


def test_the_allow_list_is_a_rule_file_and_holds_no_style_phrase():
    rules = writing.load_rules()
    assert "sap landscape" in {t.lower() for t in rules.allowed_terms}
    banned_plain = {"however", "ensure", "leverage", "robust", "seamless"}
    for term in rules.allowed_terms:
        assert term.lower() not in banned_plain
