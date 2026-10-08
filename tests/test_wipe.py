"""Wipe mode (T4): the candidate rules of awb/wipe.py, the forms a run learns, the tokens and the final check.

Every rule of DESIGN.md section 2 (presentations/names) has rows here, one written shape per row, with the invented
names of tests/fixtures.py; the shapes come from the case modules of the red team of 2026-10-07 (the pack in
calibration/redteam/wipe/ holds all 405 of them, test_redteam_pack.py runs a sample). Capitalised runs of words that
are no fixture name are written as tuples and joined here, so that tests/test_self_clean.py reads them as words.
Nothing here prints a planted value.
"""
from __future__ import annotations

import re
from collections import Counter

import pytest

from awb import intake, patterns, planted, register, wipe
from awb.matcher import Span
from tests import fixtures as fx

FIRST, LAST = fx.PLANTED_PERSON.split()
BRAND = fx.PLANTED_CANDIDATE.split()[0]
S1, S2 = fx.SECOND_FIRST, fx.SECOND_LAST
CYR_FIRST, CYR_LAST = fx.CYRILLIC_PERSON.split()
TOWN = FIRST + "hausen"


def _j(*words_: str) -> str:
    return " ".join(words_)


def _spans(text: str, known=()) -> list[tuple[str, str, str]]:
    """(value, class, rule) of every candidate of `text`."""
    return [(text[s.start:s.end], s.cls, s.rule) for s in intake._collect(text, list(known))]


def _wiped(text: str, entries=None) -> str:
    """One text through an engine of its own: detection (the forms it teaches), then the sanitising."""
    engine = intake._Engine(entries if entries is not None else [])
    engine.detect("t detect", text)
    out, _ = engine.sanitize(text, "t output")
    return out


# --------------------------------------------------------------------------- one row per rule and shape

RULES = [
    # (label, text, the value that must be a candidate, its class)
    ("legal form", "das Angebot der %s liegt vor." % fx.PLANTED_CANDIDATE, fx.PLANTED_CANDIDATE, "company"),
    ("legal form after lower-case words", "wir reden mit %s gmbh morgen" % BRAND.lower(), BRAND.lower(), "unknown"),
    ("company label", "Auftraggeber: %s Systemhaus\n" % BRAND, BRAND + " Systemhaus", "company"),
    ("company column", "| Firma | Rolle |\n| --- | --- |\n| %s Systemhaus | Lieferant |\n" % BRAND,
     BRAND + " Systemhaus", "company"),
    ("legal form alone in a cell", "| %s | GmbH |\n| --- | --- |\n" % BRAND, BRAND, "company"),
    ("Firma word", "Wir sprechen morgen mit Firma %s." % BRAND, BRAND, "company"),
    ("Russian legal form", "ООО «%s» поставляет оборудование." % CYR_LAST, CYR_LAST, "company"),
    ("title", "Termin mit Herrn %s am Montag." % LAST, LAST, "person"),
    ("title with a role after it", "%s Projektleiterin %s schreibt." % ("Frau", LAST), LAST, "person"),
    ("title chain", "Prof. Dr. med. %s %s hat unterschrieben." % (FIRST, LAST), _j(FIRST, LAST), "person"),
    ("Russian title", "Встреча с г-ном %s завтра." % CYR_LAST, CYR_LAST, "person"),
    ("particle", "Gestern kam %s von %s ins Büro." % (FIRST, LAST), "%s von %s" % (FIRST, LAST), "person"),
    ("known noun von one unknown word", "die Firewall von %s läuft stabil." % BRAND, BRAND, "unknown"),
    ("vocative", "Danke %s, dann teste ich das." % S1, S1, "person"),
    ("by-line", "Erstellt von %s %s\n" % (FIRST, LAST), _j(FIRST, LAST), "person"),
    ("Last, First", "Teilnehmer heute: %s, %s\n" % (LAST, FIRST), "%s, %s" % (LAST, FIRST), "person"),
    ("initial", "das Konzept hat %s. %s geschrieben." % (FIRST[0], LAST), "%s. %s" % (FIRST[0], LAST), "person"),
    ("name and initial", "das Konzept hat %s %s. geschrieben." % (LAST, FIRST[0]), "%s %s." % (LAST, FIRST[0]),
     "person"),
    ("salutation", "Hallo %s,\n\nder Server steht.\n" % S1, S1, "person"),
    ("salutation after a comma", "Guten Tag, Hallo %s,\nwie besprochen.\n" % S1, S1, "person"),
    ("sign-off", "Wie besprochen.\n\nViele Grüße\n%s %s\n" % (S1, S2), _j(S1, S2), "person"),
    ("sign-off on the closing's line", "Wie besprochen.\n\nLG %s\n" % S1, S1, "person"),
    ("mail header", "Von: %s %s <u.%s@grvetz.example>\nBetreff: Termin\n" % (S1, S2, S2.lower()), _j(S1, S2),
     "person"),
    ("reply line", "Am 5. Oktober 2026 schrieb %s %s:\n> alt\n" % (S1, S2), _j(S1, S2), "person"),
    ("English reply line", "%s Mon, 5 Oct 2026 at 10:03, %s %s wrote:\n> old\n" % ("On", S1, S2), _j(S1, S2), "person"),
    ("speaker label", "%s: Das passt so.\nModerator: Gut.\n%s: Dann machen wir das so.\n" % (S2, S2), S2, "person"),
    ("speaker after a time stamp", "[10:03] %s %s: Ich teile den Bildschirm.\n" % (S1, S2), _j(S1, S2), "person"),
    ("person label", "Ansprechpartner: %s %s\n" % (S1, S2), _j(S1, S2), "person"),
    ("person column", "| Vorname | Nachname |\n| --- | --- |\n| %s | %s |\n" % (S1, S2), S1, "person"),
    ("login", 'owner = "%s%s"\n' % (S1[0].lower(), S2.lower()), S1[0].lower() + S2.lower(), "person"),
    ("ssh login", "ssh %s@bastion01 -p 22\n" % S2.lower(), S2.lower(), "person"),
    ("place label", "Standort: %s\n" % TOWN, TOWN, "place"),
    ("street and number", "die Lieferung geht in die %s." % fx.PLANTED_STREET, fx.PLANTED_STREET, "place"),
    ("postcode and town", "Anschrift: 12345 %s\n" % TOWN, TOWN, "place"),
    ("subject", "Betreff: %s\n\nwie besprochen.\n" % LAST, LAST, "unknown"),
    ("run of two unknown words", "Gestern haben wir %s %s informiert." % (S1, S2), _j(S1, S2), "unknown"),
    ("run that a first name opens", "Gestern hat Tobias %s angerufen." % S2, "Tobias " + S2, "person"),
    ("identifier", "Anbei Angebot_%s_Beratung als Entwurf." % BRAND, BRAND, "unknown"),
    ("hyphen compound", "Die %s-Lösung ist teuer." % BRAND, BRAND, "unknown"),
    ("names over two lines", "Organigramm\n%s\n%s\nProjektleiter\n" % (S1, S2), S1, "person"),
    ("lone first name", "Gestern hat Tobias die Firewall gebaut.", "Tobias", "person"),
]


@pytest.mark.parametrize("label,text,want,cls", RULES, ids=[r[0] for r in RULES])
def test_every_rule_finds_its_shape(label, text, want, cls):
    got = _spans(text)
    assert any(v == want and c == cls for v, c, _ in got), (label, [(c, r) for _, c, r in got])


@pytest.mark.parametrize("label,text,want,cls", RULES, ids=[r[0] for r in RULES])
def test_every_shape_comes_out_as_a_token_of_its_class(label, text, want, cls):
    out = _wiped(text)
    assert not re.search(r"(?<![^\W\d_])%s(?![^\W\d_])" % re.escape(want), out), label
    word = wipe.TOKEN_TEXT[cls]
    assert re.search(r"\[%s \d+\]" % word, out), label


def test_the_rule_of_a_span_is_named_in_words():
    labels = {r for _, _, r in _spans("Termin mit Herrn %s. %s GmbH liefert." % (LAST, BRAND))}
    assert labels <= set(wipe.RULE_LABELS.values()) | {wipe.LEARNED}
    assert {"title", "legal form"} <= labels


# --------------------------------------------------------------------------- what must survive

TERMS = [
    ("Elastic", "Cloud", "Servers"), ("Virtual", "Private", "Clouds"), ("Load", "Balancers"), ("Elastic", "IPs"),
    ("Web", "Application", "Firewall"), ("NAT", "Gateway"), ("VPC", "Endpoint"), ("Availability", "Zones"),
    ("Availability", "Zone", "1"), ("Enterprise", "Project"), ("Dedicated", "Host"), ("General", "Purpose"),
    ("Memory-optimized",), ("s3.large.2",), ("c7n.2xlarge.2",), ("Standard_D4s_v3",), ("m6.xlarge",),
    ("Windows", "Server", "2022"), ("Windows", "Server", "2019", "Datacenter"), ("SUSE", "Linux", "Enterprise", "Server"),
    ("Red", "Hat", "Enterprise", "Linux", "9"), ("Rocky", "Linux", "9"), ("Oracle", "Linux", "8"),
    ("Microsoft", "SQL", "Server", "2019"), ("Oracle", "Database", "19c"), ("SAP", "HANA"), ("PostgreSQL", "15"),
    ("Cisco", "Meraki", "vMX"), ("Fortinet", "FortiGate"), ("SonicWall", "NSv"), ("Palo", "Alto", "Networks"),
    ("Check", "Point"), ("Pure", "Storage"), ("Juniper", "Networks"), ("Hewlett", "Packard", "Enterprise"),
    ("VMware", "vSphere"), ("VMware", "Cloud", "Foundation"), ("Azure", "Virtual", "Desktop"), ("Blob", "Storage"),
    ("Azure", "Site", "Recovery"), ("Log", "Analytics"), ("Key", "Vault"), ("Virtual", "Network"),
    ("Resource", "Group"), ("Network", "Security", "Group"), ("Application", "Gateway"), ("Front", "Door"),
    ("Traffic", "Manager"), ("Elastic", "Beanstalk"), ("Transit", "Gateway"), ("Simple", "Storage", "Service"),
    ("Route", "53"), ("Veeam", "Backup", "&", "Replication"), ("LEFT", "OUTER", "JOIN"), ("DEFAULT", "NULL"),
    ("UNIQUE", "INDEX"), ("PRIMARY", "KEY"), ("ORDER", "BY"), ("404", "Not", "Found"),
    ("Internal", "Server", "Error"), ("Bad", "Gateway"), ("Service", "Unavailable"),
    ("WARNING", "Connection", "refused"), ("ERROR", "Access", "denied"), ("Executive", "Summary"),
    ("Technical", "Requirements"), ("Network", "Architecture", "Overview"), ("Landing", "Zone"),
    ("Hub", "and", "Spoke"), ("Table", "of", "Contents"), ("Technische", "Anforderungen"),
    ("Allgemeine", "Geschäftsbedingungen"), ("Kosten", "Übersicht"), ("Netzwerk", "Architektur"),
    ("ISO", "27001"), ("BSI", "C5"), ("GDPR", "Article", "28"), ("DSGVO", "Art.", "28"),
    ("Senior", "Cloud", "Architect"), ("Key", "Account", "Manager"), ("Service", "Level", "Agreement"),
    ("Service", "Description"), ("Terms", "and", "Conditions"), ("Additional", "Terms"),
    ("Deutsche", "Telekom"), ("Open", "Telekom", "Cloud"), ("T", "Cloud", "Public"),
    ("T-Systems", "International", "GmbH"), ("Telekom", "Deutschland", "GmbH"), ("Deutsche", "Telekom", "AG"),
]


@pytest.mark.parametrize("term", [_j(*t) for t in TERMS])
def test_the_terms_a_session_needs_survive(term):
    for text in ("Wir planen %s im Projekt." % term, "| %s | 2 |\n" % term, "## %s\n" % term):
        assert term in _wiped(text), text


@pytest.mark.parametrize("text", [
    _j("Wir", "nutzen", "Grace", "Period", "und", "Max", "Pods."),
    "Die %s steht im Anhang." % _j("Bill", "of", "Materials"),
    "Wir schreiben ein %s und nutzen %s." % (_j("DR", "Konzept"), _j("MS", "Teams")),
    "A. Einleitung\nB. Betrieb\nC. Kosten\n",
    "Wir empfehlen Szenario B. als Weg.\n",
    "Hersteller: Kemp\nDer Kemp LoadMaster steht auf kemp-lb-01.\n",
    "Zammad: Ticketsystem\nGitea: Repository\nZammad: Version 6\n",
    "%s\n%s\n" % (_j("Viele", "Grüße"), _j("Das", "Projektteam")),
    _j("Hallo", "Netzwerkteam") + ",\nbitte prüfen.\n",
    "Reviewed by %s\n" % _j("Architecture", "Board"),
    _j("Am", "Standort") + " 2 laufen 40 VMs.\n",
    "Wir migrieren rund 30000 Postfächer.\n",
    _j("Je", "Mandant") + " gibt es ein Projekt.\n",
    "Siehe https://docs.example.org/zammad/ dort.\nZammad liefert die Tickets.\n",
    "## %s\n" % _j("Eingesetzte", "Werkzeuge"),
    "| Welle | Kandidat |\n| --- | --- |\n| 1 | %s |\n" % _j("Rehosting", "Candidates"),
    "Mo bis Fr %s von 8 bis 18 Uhr.\n" % "Supportzeiten",
])
def test_the_false_positive_shapes_lose_nothing(text):
    assert _wiped(text) == text


def test_the_run_rule_spares_known_phrases_and_headings():
    for text in ("## %s\n" % _j("Executive", "Summary"), "%s\n" % _j("EXECUTIVE", "SUMMARY"),
                 "Die %s gilt." % _j("Service", "Description")):
        assert not [s for s in _spans(text) if s[2] == "run of capitalised words"], text


def test_a_vendor_under_a_product_label_is_never_learned_or_wiped():
    text = "Hersteller: Kemp\n\nKemp hat geliefert, der Host kemp-lb-01 läuft.\n"
    assert _wiped(text) == text


# --------------------------------------------------------------------------- the forms a run learns


def _run(*texts: str, entries=None) -> tuple[intake._Engine, list[str]]:
    engine = intake._Engine(entries if entries is not None else [])
    for i, t in enumerate(texts):
        engine.detect("part %d detect" % i, t)
    return engine, [engine.sanitize(t, "part %d output" % i)[0] for i, t in enumerate(texts)]


def test_a_surname_wiped_once_is_wiped_in_every_later_shape():
    _, (one, two) = _run("Termin mit Herrn %s %s." % (FIRST, LAST),
                         "%s kommt. der Host srv-%s-02 und %ss Konzept." % (LAST, LAST.lower(), LAST))
    assert LAST not in one and LAST.lower() not in two.lower()
    assert re.search(r"\[person 1\] kommt", two), "the surname alone gets the number of its person"


def test_a_russian_surname_is_learned_with_its_case_endings():
    _, (_, two) = _run("Уважаемый %s %s, спасибо." % (CYR_FIRST, CYR_LAST), "Письмо от %s пришло." % fx.CYRILLIC_GENITIVE)
    assert CYR_LAST.lower() not in two.lower()


def test_a_word_of_a_run_that_stands_in_lower_case_in_the_prose_is_not_learned():
    engine, outs = _run("Gestern haben wir %s %s informiert." % (S1, S2),
                        "die %s heute ist ein wort der prosa." % S2.lower(), "%s kam." % S2)
    assert outs[2] == "%s kam." % S2


def test_a_person_word_is_learned_even_when_it_stands_in_lower_case():
    _, outs = _run("Ansprechpartner: %s %s\n" % (S1, S2), "# TODO(%s): fix\nask %s first\n" % (S2.lower(), S2.lower()))
    assert S2.lower() not in outs[1].lower()


def test_a_weak_learned_form_never_matches_spread_out_below_eight_letters():
    spread = " ".join(S2)
    _, outs = _run("Gestern haben wir %s %s informiert." % (S1, S2), "Kopf: %s\n" % spread)
    assert spread in outs[1]


def test_the_mail_domain_of_a_person_teaches_the_company_and_a_provider_teaches_nothing():
    engine, outs = _run("Von: %s <%s.%s@%s.example>\n" % (fx.PLANTED_PERSON, FIRST.lower(), LAST.lower(), BRAND.lower()),
                        "%s liefert morgen." % BRAND)
    assert BRAND not in outs[1] and re.search(r"\[company \d+\]", outs[1])
    engine, outs = _run("Von: %s <%s.%s@gmail.com>\n" % (fx.PLANTED_PERSON, FIRST.lower(), LAST.lower()),
                        "Gmail liefert morgen.", "support@zammad.example antwortet.\nZammad liefert.")
    assert outs[1] == "Gmail liefert morgen."
    assert "Zammad liefert." in outs[2]


def test_a_file_name_teaches_nothing():
    engine = intake._Engine([])
    engine.detect("F-ABCD file name", "Bestellung %s.pdf" % LAST)
    assert engine.wipe.learned.words == {}


def test_the_learned_forms_are_named_in_the_private_report(home):
    f = home.inbox / "note.txt"
    f.write_text("Termin mit Herrn %s %s.\n%s kommt.\n" % (FIRST, LAST, LAST), encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    private = res.private_report.read_text(encoding="utf-8")
    learned = private.split("## Learned forms", 1)[1].split("\n## ", 1)[0]
    assert LAST in learned and "person" in learned
    wiped = private.split("## Wiped", 1)[1].split("\n## ", 1)[0]
    assert "| title |" in wiped and "| learned form |" in wiped


# --------------------------------------------------------------------------- tokens


def test_tokens_are_numbered_per_class_and_value_within_one_run():
    _, (out,) = _run("Von: %s <a@b.example>\nAn: %s <c@d.example>\n\n%s schreibt an %s.\n"
                     % (fx.PLANTED_PERSON, fx.SECOND_PERSON, FIRST, S1))
    assert "[person 1]" in out and "[person 2]" in out and "[person 3]" not in out


def test_a_token_before_a_bracket_never_reads_as_a_link():
    _, (out,) = _run("Herr %s (Projektleitung) kommt." % LAST)
    assert re.search(r"\[person 1\] \(", out)


def test_a_company_after_the_customer_code_merges_into_it_and_a_heading_does_not():
    out = _wiped("Die %s Logistik Services GmbH liefert." % fx.CUSTOMER_FORMS[1],
                 register.parse("\n".join(fx.register_lines()) + "\n"))
    assert fx.CUSTOMER_CODE + " liefert" in out and "[company" not in out
    out = _wiped("## %s %s\n" % (fx.CUSTOMER_FORMS[1], _j("Technische", "Zielarchitektur")),
                 register.parse("\n".join(fx.register_lines()) + "\n"))
    assert "%s %s" % (fx.CUSTOMER_CODE, _j("Technische", "Zielarchitektur")) in out


def test_codes_and_tokens_are_never_part_of_a_candidate():
    text = "%s Hamburg und [person 1] Smith und F-ABCD Report\n" % fx.CUSTOMER_CODE
    assert not [s for s in _spans(text) if any(t in s[0] for t in (fx.CUSTOMER_CODE, "[person 1]", "F-ABCD"))]


def test_a_kept_phrase_is_never_a_candidate_and_never_a_learned_form():
    engine = intake._Engine([])
    engine.keep = (fx.PLANTED_CANDIDATE, LAST)
    text = "Herr %s %s und die %s.\n%s kommt.\n" % (FIRST, LAST, fx.PLANTED_CANDIDATE, LAST)
    engine.detect("t detect", text)
    out, _ = engine.sanitize(text, "t output")
    assert fx.PLANTED_CANDIDATE in out


# --------------------------------------------------------------------------- the final check


def test_the_final_check_never_reads_the_intakes_own_header_lines():
    engine = intake._Engine([])
    text = "# F-ABCD\n\n- file id: F-ABCD\n- kind: text\n- state: ok\n- notes: none\n\nDer Server steht.\n"
    assert intake._body_view(text).strip() == "none\n\nDer Server steht.".strip() or "file id" not in intake._body_view(text)
    assert intake._body_hits(engine, text) == []


def test_a_candidate_left_after_the_passes_withholds_the_output(home, monkeypatch):
    f = home.inbox / "note.txt"
    f.write_text("Termin mit Herrn %s %s.\n" % (FIRST, LAST), encoding="utf-8")
    monkeypatch.setattr(intake, "selftest", lambda: [])
    monkeypatch.setattr(intake._Engine, "sanitize", lambda self, text, where: (text, Counter()))
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    (fid,) = res.states
    assert res.states[fid] == "failed" and res.outputs == [] and res.held == [fid]
    assert f.is_file(), "the original stays in the inbox"


def test_the_public_report_carries_counts_and_rules_and_never_a_value(home):
    f = home.inbox / "note.txt"
    f.write_text("Hallo %s,\nTermin mit Herrn %s %s bei der %s.\n" % (S1, FIRST, LAST, fx.PLANTED_CANDIDATE),
                 encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    public = res.public_report.read_text(encoding="utf-8")
    assert "## Wiped" in public and "salutation" in public and "legal form" in public
    for v in (S1, FIRST, LAST, BRAND):
        assert v not in public
    assert "candidates were reviewed" not in public
    assert res.wiped["person"] >= 2 and res.wiped["company"] == 1


def test_a_word_of_the_report_template_never_withholds_the_public_report(home):
    f = home.inbox / "plan.txt"
    f.write_text("| Welle | Kandidat |\n| --- | --- |\n| 1 | %s %s |\n" % (_j("Replatforming", "Candidates"), S2),
                 encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    assert res.public_report.is_file() and len(res.outputs) == 1 and not res.held


# --------------------------------------------------------------------------- the self-test


def test_the_self_test_carries_the_invented_names_of_the_fixtures():
    assert planted.CUSTOMER_CODE == fx.SELFTEST_CUSTOMER_CODE
    assert planted.CUSTOMER_SHORT == fx.SELFTEST_CUSTOMER_SHORT and planted.CUSTOMER_FORM == fx.SELFTEST_CUSTOMER_FORM
    names = {FIRST, LAST, S1, S2, BRAND, CYR_LAST}
    used = {w for _, _, ws, _ in planted.WIPE_CASES for w in ws}
    assert used - {"nrgtz"} <= names | {fx.PLANTED_STREET.split("straße")[0]}
    assert len(planted.WIPE_CASES) >= 10 and len(planted.CONTROL_TERMS) == 7


@pytest.mark.parametrize("broken", ["no candidates", "stop phrases wiped", "no merge", "no learned forms"])
def test_the_self_test_refuses_a_broken_engine(home, monkeypatch, broken):
    f = home.inbox / "note.txt"
    f.write_text("nichts besonderes\n", encoding="utf-8")
    if broken == "no candidates":
        monkeypatch.setattr(wipe, "candidate_spans", lambda state, text, known: [])
    elif broken == "stop phrases wiped":
        real = wipe.candidate_spans
        monkeypatch.setattr(wipe, "candidate_spans", lambda state, text, known: [
            s for s in real(state, text, known)] + [Span(s.start, s.end, "unknown", None, "broken")
                                                    for s in wipe._stop_spans(text)])
    elif broken == "no merge":
        monkeypatch.setattr(wipe, "merge_customer_code", lambda text: text)
    else:
        monkeypatch.setattr(wipe._Learned, "find", lambda self, text, taken, keep=(): [])
    failed = intake.selftest()
    assert failed
    with pytest.raises(intake.IntakeError, match="self-test"):
        intake.run([f], fx.CUSTOMER_CODE, home)
    assert f.is_file()


# --------------------------------------------------------------------------- the lists


def test_a_missing_list_gives_fewer_exemptions_never_more(monkeypatch):
    assert wipe._list("no-such-list.txt") == frozenset()
    term = _j("Azure", "Virtual", "Desktop")
    text = "Wir planen %s im Projekt." % term
    assert term in _wiped(text)
    monkeypatch.setattr(wipe, "_SOURCE_PLATFORM_WORDS", frozenset())
    monkeypatch.setattr(wipe, "_VOCAB", frozenset())
    monkeypatch.setattr(wipe, "_PHRASES", frozenset())
    monkeypatch.setattr(wipe, "_CORPUS", frozenset())
    assert term not in _wiped(text)


def test_the_lists_of_the_rules_are_loaded():
    assert len(wipe._CORPUS) > 1000 and len(wipe._PHRASES) > 1000 and len(wipe._FIRST_NAMES) > 500
    assert len(wipe._SOURCE_PLATFORM_WORDS) > 500 and len(wipe._GERMAN_KNOWN) > 500
    assert len(wipe._GERMAN_FUNCTION) > 200 and len(wipe._EXTRA_KNOWN) > 300


def test_the_partners_own_names_are_stop_words():
    stop = (patterns.RULES_DIR / "stop-words.txt").read_text(encoding="utf-8").splitlines()
    for name in (_j("T-Systems", "International", "GmbH"), _j("Telekom", "Deutschland", "GmbH"),
                 _j("Deutsche", "Telekom", "AG"), "T-Systems"):
        assert name in stop




def test_a_first_name_glued_into_an_identifier_keeps_the_surname_with_it():
    out = _wiped('job1 = "Max_%s_export"\n' % "Qwertzung")
    assert "Qwertzung" not in out and "_export" in out
