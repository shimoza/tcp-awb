"""Offered services: the latest service description of TCP and nothing else (awb/tcp/services.py).

A service is offered when chapter 3 of the service description lists it and no "No longer available from" mark has
passed; a service the documentation or the price API still knows is not offered for that. The check takes the names
people write (short names, -ing for -er, "and" left out). The chats get the list in their instructions and a note
under any answer that names a service which is not offered; the review refuses such a deliverable.
"""
from __future__ import annotations

import pytest

from awb import cli, review
from awb.tcp import ask, services
from tests.test_review import project, start, writing  # noqa: F401  (fixtures of the review tests)

SD = """Contents
3   Services                                                                 12
     3.1   Computing                                                         12
     3.1.1   Elastic Cloud Server                                            12
     3.1.2   Dedicated host                                                  13
     3.3   Storage                                                           20
     3.3.4   Cloud Server Backup Service                                     20
     3.4   Network                                                           25
     3.4.3   Elastic Load Balancer                                           26
     3.9   Security                                                          40
     3.9.3   Web Application Firewall (Classic Mode)                         40
     3.9.5   Web Application Firewall (Dedicated)                            41
     3.10   Rights of use, licenses                                          50
     3.10.1   General provisions                                             50
     3.11   Optional services                                                60
     3.11.8   Marketplace                                                    60
     3.12   Preview and beta versions                                        61
     3.12.1   Special conditions for preview and beta versions               61
     3.12.3   Cloud Container Instance                                       62

Elastic Cloud Server (ECS) offers virtual machines; a Web Application Firewall (Dedicated) protects web sites.
No longer available from 01.01.2026: 3.3.4 Cloud Server Backup Service, backups of servers.
No longer bookable from 01.06.2026: 3.9.3 Web Application Firewall (Classic Mode), a filter for traffic.
"""
TODAY = "2026-10-04"


@pytest.fixture
def catalog():
    return services.Catalog("2026-08-17", services.parse(SD), [["Bare Metal Server", "BMS"]])


def test_chapter_three_gives_the_services_their_chapter_and_their_end(catalog):
    by = {s.number: s for s in catalog.services}
    assert sorted(by) == ["3.1.1", "3.1.2", "3.11.8", "3.12.3", "3.3.4", "3.4.3", "3.9.3", "3.9.5"]
    assert (by["3.11.8"].status, by["3.12.3"].status, by["3.1.1"].status) == ("optional", "preview", "offered")
    assert "ECS" in by["3.1.1"].aliases and "Dedicated" not in by["3.9.3"].aliases
    assert (by["3.3.4"].ends, by["3.3.4"].end_kind) == ("2026-01-01", "available")
    assert (by["3.9.3"].ends, by["3.9.3"].end_kind) == ("2026-06-01", "bookable")


def test_the_check_takes_the_names_people_write(catalog):
    for name in ("Elastic Cloud Server", "ECS", "elastic cloud servers", "Elastic Load Balancing", "ELB", "DeH"):
        assert services.check(name, catalog, TODAY).offered, name
    waf = services.check("WAF", catalog, TODAY)
    assert waf.offered and len(waf.services) == 2 and "no longer bookable since 01.06.2026" in waf.note
    preview = services.check("Cloud Container Instance", catalog, TODAY)
    assert preview.offered and "as a preview" in preview.note


def test_a_service_the_text_no_longer_offers_is_not_offered_after_its_date(catalog):
    before = services.check("Cloud Server Backup Service", catalog, "2025-12-31")
    after = services.check("CSBS", catalog, TODAY)
    assert before.offered and "no longer available from 01.01.2026" in before.note
    assert not after.offered and "since 01.01.2026" in after.note and "new work" in after.note


def test_a_service_outside_the_service_description_is_not_offered(catalog):
    for name in ("Bare Metal Server", "BMS", "LLM Hub"):
        verdict = services.check(name, catalog, TODAY)
        assert not verdict.offered and "Do not propose it" in verdict.note, name
    assert "documentation may still describe it" in services.check("BMS", catalog, TODAY).note
    assert "documentation" not in services.check("LLM Hub", catalog, TODAY).note


def test_mentions_name_what_is_not_offered_and_leave_the_offered_alone(catalog):
    text = "We propose a Bare Metal Server, CSBS backups and a WAF in front of the ECS fleet."
    assert services.mentions(text, catalog, TODAY) == ["Bare Metal Server", "Cloud Server Backup Service"]
    assert services.mentions("two BMS nodes", catalog, TODAY) == ["Bare Metal Server"]
    assert services.mentions("the bms flag of a log line", catalog, TODAY) == []
    assert services.mentions("an ECS behind an ELB", catalog, TODAY) == []
    assert services.mentions("CSBS for now", catalog, "2025-06-01") == []
    note = services.note_for(text, catalog, TODAY)
    assert note.startswith("Not offered on TCP: Bare Metal Server, Cloud Server Backup Service")


def test_the_prompt_lists_the_offered_the_ended_and_the_rule(catalog):
    text = services.prompt_lines(catalog, TODAY)
    assert "service description of 17.08.2026" in text and "Elastic Cloud Server (ECS)" in text
    assert "Previews: Cloud Container Instance" in text
    assert "Cloud Server Backup Service (no longer available since 01.01.2026)" in text
    assert "Not offered at all: Bare Metal Server" in text and "Never recommend or propose" in text


def test_the_catalog_of_the_repository_knows_the_current_revision():
    catalog = services.load()
    assert catalog.revision and len(catalog.services) >= 70
    assert not services.check("Bare Metal Server", catalog, TODAY).offered
    assert not services.check("BMS", catalog, TODAY).offered
    assert services.check("ECS", catalog, TODAY).offered and services.check("OBS", catalog, TODAY).offered
    assert not services.check("CSBS", catalog, TODAY).offered
    assert services.mentions("a Bare Metal Server cluster", catalog, TODAY) == ["Bare Metal Server"]


def test_the_command_says_offered_or_not(capsys):
    assert cli.main(["service", "check", "ECS"]) == 0
    assert "offered, section 3.1.1" in capsys.readouterr().out
    assert cli.main(["service", "check", "ECS", "BMS"]) == 1
    assert "BMS: not offered" in capsys.readouterr().out
    assert cli.main(["service", "list"]) == 0
    assert "Elastic Cloud Server" in capsys.readouterr().out


def test_the_ask_page_gets_the_list_and_a_note_under_an_answer_that_offers_too_much(home, monkeypatch, tmp_path):
    monkeypatch.setattr(ask, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(ask, "screen", lambda text, p: text)
    seen = []

    def sender(body, key):
        seen.append(body)
        return {"content": [{"type": "text", "text": "Take a Bare Metal Server for the database."}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}
    result = ask.ask("Which server type fits a database?", home, sender=sender)
    assert "Offered on TCP is exactly what the service description" in seen[0]["system"]
    assert result["answer"].endswith("Not offered on TCP: Bare Metal Server (the service description of %s)."
                                     % services.revision_label(services.load()))


def test_the_project_chat_puts_the_note_under_the_answer():
    from tests.test_web_chat_service import Tests

    t = Tests()
    t.setUp()
    try:
        t.engine.sender = lambda body, key: (t.calls.append(body) or
                                             {"content": [{"type": "text", "text": "Two BMS nodes would do."}],
                                              "stop_reason": "end_turn",
                                              "usage": {"input_tokens": 20, "output_tokens": 5}})
        turn = t.submit()
        assert "Not offered on TCP: Bare Metal Server" in turn["answer"]
        assert "Never recommend or propose" in t.calls[0]["system"]
    finally:
        t.tearDown()


def test_the_review_refuses_a_deliverable_that_proposes_a_service_not_offered(project, tmp_path, writing):  # noqa: F811
    d = start(project, tmp_path, text="# Offer\n\nWe size two Bare Metal Server nodes for the database tier.\n")
    res = review.l0(d)
    assert res.blocked and any(p.startswith("service: Bare Metal Server is not offered on TCP") for p in res.problems)


def _mirror(base, revision):
    sd = base / "service-description"
    (sd / revision).mkdir(parents=True)
    (sd / revision / "service-description.txt").write_text("Last revised: %s\n" % revision, encoding="utf-8")
    (sd / "CURRENT").write_text(revision + "\n", encoding="utf-8")
    return base


def test_the_refresh_says_when_the_lists_are_older_than_the_mirror(home, tmp_path):
    from awb.tcp import refresh

    revision = services.load().revision
    stale = refresh.part_defaults(home, _mirror(tmp_path / "new", "2099-01-01"))
    assert "rules/services.tsv is of the revision %s, the mirror holds 2099-01-01: run awb service update" % revision \
        in stale.lines
    same = refresh.part_defaults(home, _mirror(tmp_path / "same", revision))
    assert "rules/services.tsv: the current revision %s" % revision in same.lines
    assert stale.overdue == same.overdue + 1


def test_the_check_says_when_a_newer_service_description_waits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AWB_MIRRORS", str(_mirror(tmp_path / "m", "2099-01-01")))
    assert cli.main(["service", "check", "ECS"]) == 0
    assert "the mirror holds the service description of 01.01.2099" in capsys.readouterr().out
    monkeypatch.setenv("AWB_MIRRORS", str(_mirror(tmp_path / "n", services.load().revision)))
    assert cli.main(["service", "check", "ECS"]) == 0
    assert "note:" not in capsys.readouterr().out
