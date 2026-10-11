"""The tells of the work profile (presentations/work-profile/REPORT.md, section 3 and proposal 4): what he struck out
most in drafts is a blocking tell of `awb write check` in mode mail and doc, and a section the request did not ask
for is reported with its heading.

Every planted text is invented. Each tell is shown to block on a planted draft in both modes and to stay quiet on
its near miss, so a tell that finds nothing cannot pass.
"""
from __future__ import annotations

import pytest

from awb import cli, writing
from tests.test_writing import assert_hint_quotes_at_most_one_word, assert_only_the_probe, blocking, planted

MODES = ("mail", "doc")


def classes(text: str, mode: str) -> list[str]:
    return [c for c, _ in blocking(writing.check_text(text, mode=mode)[0])]


# (class, planted sentence, the one word the hint quotes or None); each one alone on line 5 of a small draft
STRUCK = [
    # invented labels and jargon (22 deletions)
    ("banned-word", "The cadence of the backups is daily.", "cadence"),
    ("banned-word", "The gotchas of the setup are listed below.", "gotchas"),
    ("banned-word", "The gotcha is the quota.", "gotcha"),
    ("banned-word", "One quirk of the API is the paging.", "quirk"),
    ("banned-word", "The API has quirks in the paging.", "quirks"),
    ("banned-phrase", "The sharp edges are in the network part.", "sharp"),
    ("banned-phrase", "The whole trick is the second subnet.", "whole"),
    ("banned-phrase", "That is the whole trick here.", "whole"),
    ("banned-word", "The estate has forty servers.", "estate"),
    ("banned-word", "I copied the error verbatim.", "verbatim"),
    ("banned-word", "The roles are granular enough.", "granular"),
    ("banned-phrase", "The second region runs as a pilot light.", "pilot"),
    ("invented-label", "The “Fast Lane” option costs less.", None),
    ("invented-label", "I suggest the \"Quick Start\" approach for the test.", None),
    ("invented-label", "Variante „Schnellstart“ kostet weniger.", None),
    # punctuation and markup (17 deletions)
    ("comma-and-or", "The cluster needs nodes, disks, and an EIP.", "and"),
    ("em-dash", "The cluster is ready — the test can start.", None),
    ("bold-lead", "**Network:** the VPC has two subnets.", None),
    ("bold-lead", "**Network**: the VPC has two subnets.", None),
    ("bold-lead", "__Note.__ The VPC has two subnets.", None),
    ("bold-lead-list", "- **Plan:** one\n- **Test:** two\n- **Run:** three", None),
    # admissions of weakness and hedges (14 deletions)
    ("weakness", "To be honest, the quota is tight.", "honest"),
    ("weakness", "I am not sure the quota is enough.", "sure"),
    ("weakness", "I may be wrong about the quota.", "wrong"),
    ("weakness", "Sorry for the late answer about the quota.", "sorry"),
    ("weakness", "Hopefully the quota is enough.", "hopefully"),
    ("weakness", "I'm not an expert in the network part.", "expert"),
    ("weakness", "Ehrlich gesagt ist die Quota knapp.", "ehrlich"),
    # sales tone (7 deletions)
    ("sales-tone", "The cluster is a game changer for the team.", "changer"),
    ("sales-tone", "TCP offers a state-of-the-art backup.", "state-of-the-art"),
    ("sales-tone", "The setup is the perfect fit for the test.", "perfect"),
    ("sales-tone", "The managed database gives peace of mind.", "peace"),
    ("sales-tone", "The setup is hassle-free.", "hassle-free"),
    # promises in his name (6 deletions)
    ("commitment", "I guarantee the quota for the test.", "guarantee"),
    ("commitment", "Rest assured the quota is fine.", "assured"),
    ("commitment", "Ich werde die Quota prüfen.", "werde"),
    # the history of drafts (6 deletions)
    ("draft-history", "Compared to the previous draft the quota is lower.", "draft"),
    ("draft-history", "I have shortened the text after the call.", "changed"),
    ("draft-history", "Based on your feedback the quota is lower.", "feedback"),
    ("draft-history", "The updated version lists the second subnet.", "version"),
    # pointers to earlier mails, in German too
    ("earlier-mail", "Wie besprochen ist die Quota knapp.", "besprochen"),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cls,sentence,word", STRUCK, ids=["%s-%d" % (c[0], i) for i, c in enumerate(STRUCK)])
def test_each_struck_out_tell_blocks_in_mail_and_doc(cls, sentence, word, mode):
    text, line = planted(sentence)
    tells, metrics = writing.check_text(text, mode=mode)
    assert blocking(tells) == [(cls, line)], sentence
    tell = next(t for t in tells if t.blocking)
    assert_hint_quotes_at_most_one_word(tell.hint, sentence)
    if word:
        assert '"%s"' % word in tell.hint
    assert metrics["blocking"] == 1


WE = [("We checked the quota.", "we"), ("Our quota is fine.", "our"), ("The team sent it to us.", "us"),
      ("Wir haben die Quota geprüft.", "wir"), ("Unsere Quota reicht.", "unsere"),
      ("Die Quota reicht für uns.", "uns")]


@pytest.mark.parametrize("sentence,word", WE)
def test_we_our_and_wir_block_in_mail_and_doc_and_not_in_chat(sentence, word):
    text, line = planted(sentence)
    assert blocking(writing.check_text(text, mode="mail")[0]) == [("we-in-mail", line)]
    assert blocking(writing.check_text(text, mode="doc")[0]) == [("we-in-doc", line)]
    tell = next(t for t in writing.check_text(text, mode="doc")[0] if t.blocking)
    assert '"%s"' % word in tell.hint and "speaks as I" in tell.hint
    assert blocking(writing.check_text(text, mode="chat")[0]) == []


NEAR_MISSES = [
    "I checked the quota in the US and in us-east.",           # the country and a region are not the pronoun
    "The Direct Connect option costs less.",                   # a real service name, not quoted as a coinage
    "The \"fast lane\" option costs less.",                     # a quoted phrase in lower case is no coinage
    "The quota is an honest number.",                           # honest alone is no admission
    "The note is **bold** in the middle.",                      # bold inside a sentence is no lead
    "**Note** that the quota is tight.",                        # bold without a colon or a full stop
    "The draft contract lists two subnets.",                    # a draft of the customer's is no draft history
    "The new version of the operating system has the fix.",     # a product version is no draft history
    "I update the text of the banner in the console.",          # the present tense describes work, not the draft
    "The cluster runs in two zones.",
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("text", NEAR_MISSES, ids=["miss-%d" % i for i in range(len(NEAR_MISSES))])
def test_the_near_misses_do_not_block(text, mode):
    assert_only_the_probe(planted(text)[0], mode=mode)


def test_chat_reports_the_drafting_tells_without_blocking():
    for cls, sentence in (("bold-lead", "**Network:** the VPC has two subnets."),
                          ("weakness", "To be honest, the quota is tight."),
                          ("sales-tone", "The cluster is a game changer for the team."),
                          ("draft-history", "Compared to the previous draft the quota is lower."),
                          ("invented-label", "The “Fast Lane” option costs less.")):
        tells, _ = writing.check_text(planted(sentence)[0], mode="chat")
        assert blocking(tells) == [], cls
        assert [t.cls for t in tells if t.cls == cls] == [cls]


def test_a_bold_lead_in_a_short_bullet_list_stays_a_list_matter():
    # two bold bullets are fine (the list rule starts at three); a paragraph with a bold lead is not
    assert classes("- **Plan:** one\n- **Test:** two\n", "doc") == []
    assert classes("Intro text.\n\n**Plan:** one step.\n", "doc") == ["bold-lead"]


def test_the_new_classes_are_blocking_and_the_section_is_reported():
    for cls in ("we-in-doc", "bold-lead", "invented-label", "weakness", "sales-tone", "draft-history"):
        assert cls in writing.BLOCKING
    assert "extra-section" in writing.REPORTED and "extra-section" not in writing.BLOCKING


# --------------------------------------------------------------------------- a section nobody asked for

DRAFT = """# Offer for the backup

## Costs of the backup

The backup costs 120 EUR per month.

## Background

The team asked about it.

## Next steps

I send the sizing on request.

## Retention

The retention is 30 days.
"""


def extra(text: str, request: str | None = None) -> list:
    return [t for t in writing.check_text(text, request=request)[0] if t.cls == "extra-section"]


def test_a_section_the_request_did_not_ask_for_is_named_with_its_heading():
    found = extra(DRAFT, request="What does the backup cost per month and how long is the retention?")
    assert len(found) == 1 and not found[0].blocking
    assert found[0].line == 7
    assert "2 sections the request did not ask for: Background; Next steps" in found[0].hint
    assert writing.check_text(DRAFT, request="cost and retention of the backup")[1]["extra_sections"] == 2


def test_without_the_request_the_usual_add_on_sections_are_named():
    found = extra(DRAFT)
    assert len(found) == 1 and "Background; Next steps" in found[0].hint and found[0].line == 7
    assert extra("# Title\n\n## Costs\n\nThe text is here.\n") == []


def test_the_title_is_never_judged_and_a_request_that_asks_for_everything_passes():
    asked = "the costs, the background, the next steps and the retention of the backup offer"
    assert extra(DRAFT, request=asked) == []
    assert extra("# Background\n\nThe text is here.\n") == []


def test_cli_write_check_takes_the_request_file(tmp_path, capsys):
    draft = tmp_path / "offer.md"
    draft.write_text(DRAFT, encoding="utf-8")
    req = tmp_path / "request.md"
    req.write_text("What does the backup cost per month and how long is the retention?\n", encoding="utf-8")
    assert cli.main(["write", "check", str(draft), "--request", str(req)]) == 0
    out = capsys.readouterr().out
    assert "extra-section  7  2 sections the request did not ask for: Background; Next steps" in out
    assert "(not blocking)" in out and "sections not asked for  2" in out
    assert cli.main(["write", "check", str(draft), "--request", str(tmp_path / "missing.md")]) == 2
