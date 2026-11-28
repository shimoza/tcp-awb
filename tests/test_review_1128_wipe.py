"""Findings G6, G7 and G8 of the attacker review of 2026-11-28: names that survived the wipe of an import.

G6: a folder import wrote directory and file names into the copy, wiped piece by piece; a name glued to its
neighbours in a piece stayed. G7: a sentence in a string of a code file counted as data when it opened like SQL or
carried a placeholder, and nothing guessed a name there. G8: a learned form matched whole words only, so a hyphen
compound, a host name and a CamelCase word kept the name. Every fix has its counter test: a technical word stays.
The names are invented (tests/fixtures.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import awb
from awb import bucket, importcmd, intake, projects, register
from awb.extract import text as extract_text
from awb.intake.wipe import runstate as rs
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
SURNAME = fx.SECOND_LAST                 # no list knows it
COMPANY = fx.MEETING_BRANDS[3]           # the word of an unregistered company
WORD_SURNAME = fx.WORD_SURNAME           # a surname that is a German word


# --------------------------------------------------------------------------- G8: learned forms inside compounds


def _run(*texts: str) -> tuple[rs.WipeState, list[list[str]]]:
    """Learn every text like one import run, then wipe each: the state and the wiped values of each text."""
    state = rs.WipeState()
    for t in texts:
        rs.learn(state, t, [], [])
    return state, [[t[w.start:w.end] for w in rs.collect(t, [], state)] for t in texts]


def test_g8_a_learned_company_is_wiped_in_a_hyphen_compound_and_in_a_host_name():
    _, (_, wiped) = _run("Kontakt: %s GmbH\n" % COMPANY,
                         "Die %s-Umgebung liegt auf srv-%s-01.\n" % (COMPANY, COMPANY.lower()))
    assert wiped == [COMPANY, COMPANY.lower()]


def test_g8_a_learned_company_is_wiped_next_to_a_digit_and_a_change_of_case():
    _, (_, wiped) = _run("Kontakt: %s GmbH\n" % COMPANY,
                         "Wir nutzen %sNet und den Host %s01 und das %sNetz.\n" % (COMPANY, COMPANY.lower(), COMPANY))
    assert wiped == [COMPANY, COMPANY.lower(), COMPANY]


def test_g8_a_learned_surname_matches_at_a_hyphen_a_digit_and_a_case_change_but_never_inside_a_plain_word():
    """Replaces test_a_learned_surname_still_matches_as_a_whole_word_only of tests/test_dmz_entry_de.py (G8): a
    strong form that no list knows is also found next to a hyphen, a digit or a change of case; with a letter of
    the same case next to it (NAMEstraße) it stays part of that word."""
    state = rs.WipeState()
    state.learned.learn("person", fx.PLANTED_PERSON, ("person", "1"))
    surname = fx.PLANTED_PERSON.split()[1]
    text = "Mail an %s und %sstraße, %s-Verfahren, %s2 und Ex%s und %s." % ((surname,) * 6)
    found = [(s.start, text[s.start:s.end]) for s in state.learned.find(text, lambda a, b: False)]
    assert [v for _, v in found] == [surname] * 5
    assert text.index(surname + "straße") not in [a for a, _ in found]


def test_g8_a_technical_word_and_a_known_word_surname_stay_in_compounds_and_identifiers():
    seeds = "Kontakt: %s GmbH\nFrau %s hat das Angebot geprüft.\n" % (COMPANY, WORD_SURNAME)
    tech = ("Der Kubernetes-Cluster nutzt den load-balancer lb-prod-01 und srv-backup-02, max_retries bleibt 3.\n"
            "Der %s-Rechner %s-01 und die %steile bleiben, backupServer01 auch.\n"
            % (WORD_SURNAME, WORD_SURNAME.lower(), WORD_SURNAME))
    state, (_, wiped) = _run(seeds, tech)
    assert COMPANY.casefold() in state.learned.words and WORD_SURNAME.casefold() in state.learned.words
    assert wiped == []


def test_g8_a_form_the_run_writes_as_a_lower_case_word_stays_a_whole_word():
    """A learned word that the run also writes as a lower-case word of its prose keeps the whole word rule of B-090:
    the learned forms do not find it glued."""
    word = fx.MEETING_BRANDS[2]
    state, _ = _run("Kontakt: %s GmbH\nWir nutzen %s für das Routing.\n" % (word, word.lower()))
    text = "Es laufen %s-proxy-01 und %s01 mit %s.\n" % (word.lower(), word.lower(), word)
    assert [text[s.start:s.end] for s in state.learned.find(text, lambda a, b: False)] == [word]


# --------------------------------------------------------------------------- G6: names in the path of a folder import

GOAL = "move two app clusters to managed k8s"
UNSEEN = fx.MEETING_LASTS[1]             # a surname no text of the import carries


@pytest.fixture
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


@pytest.fixture
def project(home, register_path, monkeypatch, owner_terminal):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def _import(home, project, root: Path, tree: dict[str, str], **kw) -> tuple[dict[str, str], str]:
    """Import an invented folder: the path line of every copy by the first line of its body, and every text a
    session can read (the copies and the public report) joined."""
    for rel, body in tree.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    files, paths = importcmd._expand_paths([str(root)])
    assert importcmd.run(home, project.code, files, paths=paths, **kw) == 0
    lines, every = {}, []
    for f in sorted((home.outbox / fx.CUSTOMER_CODE).glob("*.md")):
        text = f.read_text(encoding="utf-8")
        every.append(text)
        m = re.search(r"(?m)^- path: (.*)$", text)
        if m:
            lines[m.group(1)] = text
    return lines, "\n".join(every)


def test_g6_a_name_glued_into_a_file_or_folder_name_reaches_no_copy_and_no_public_report(home, project, tmp_path,
                                                                                         capsys):
    tree = {
        "a.md": "Herr %s prüft das Angebot der %s GmbH. Frau %s gibt es frei.\n" % (SURNAME, COMPANY, WORD_SURNAME),
        "offers/Angebot-%s-2026.md" % SURNAME: "Es gilt bis März.\n",
        "%sBackup/restore_%s.md" % (COMPANY, SURNAME.lower()): "Restore steps of the backup.\n",
        "offers/Angebot_%s_v2.md" % WORD_SURNAME: "Die zweite Fassung.\n",
        "%s/notes.md" % UNSEEN: "Notes on the build.\n",
    }
    lines, every = _import(home, project, tmp_path / "repo", tree)
    for name in (SURNAME, SURNAME.lower(), COMPANY, WORD_SURNAME, UNSEEN):
        assert name not in every, "a name of the tree reached a copy or the public report"
    paths = sorted(lines)
    assert len(paths) == 5, paths
    # the hyphen compound may go as a whole (the identifier rule of the content) or word by word
    assert any(re.fullmatch(r"offers/(?:Angebot-\[person \d+\]|\[name \d+\])-2026\.md", x) for x in paths), paths
    assert any(re.fullmatch(r"\[company \d+\]Backup/restore_\[person \d+\]\.md", x) for x in paths), paths
    assert any(re.fullmatch(r"offers/Angebot_\[person \d+\]_v2\.md", x) for x in paths), paths
    assert any(re.fullmatch(r"\[name \d+\]/notes\.md", x) for x in paths), paths
    out = capsys.readouterr()
    assert SURNAME not in out.out + out.err and UNSEEN not in out.out + out.err


def test_g6_the_technical_words_of_a_path_stay(home, project, tmp_path):
    tree = {
        "a.md": "Herr %s prüft das Angebot.\n" % SURNAME,
        "src/UserController/HttpClient-v2.md": "Notes on the client.\n",
        "docs/Netzplan-Backup_2026.md": "Notes on the plan.\n",
        "Terraform/modules/VPC-Peering/README.md": "How to run it.\n",
        "infra/k8s-cluster/load-balancer.md": "The load balancer.\n",
    }
    lines, _ = _import(home, project, tmp_path / "repo", tree)
    assert sorted(lines) == sorted(tree)


def test_g6_a_trusted_import_guesses_no_name_in_a_path(home, project, tmp_path):
    tree = {"%s/Notes.md" % UNSEEN: "Notes on the build.\n"}
    lines, _ = _import(home, project, tmp_path / "repo", tree, trusted=True)
    assert sorted(lines) == sorted(tree)


def test_g6_the_words_of_a_piece_are_cut_at_marks_digits_and_case_changes():
    piece = "XMLExport_%sBackup-2026.v2" % COMPANY
    assert [piece[a:b] for a, b in intake._path_words(piece)] == ["XML", "Export", COMPANY, "Backup", "v"]


# --------------------------------------------------------------------------- G7: sentences in the strings of code


def _engine(*seen: str):
    engine = intake._Engine([], entry=True)
    for t in seen:
        engine.detect("F-TEST detect", t)
    return engine


def _wipe_code(src: str, profile: str) -> str:
    engine = _engine(extract_text.mask_code(src, profile, prose=True))
    return intake._sanitize_code(engine, src, profile, "F-TEST output")[0]


def test_g7_a_sentence_in_a_json_value_that_opens_like_sql_is_wiped():
    src = '{"note": "Update for Herr %s of %s GmbH"}\n' % (SURNAME, COMPANY)
    out = _wipe_code(src, "json")
    assert SURNAME not in out and COMPANY not in out
    assert out.startswith('{"note": "Update for Herr ') and out.endswith('"}\n')


@pytest.mark.parametrize("profile,line", [
    ("yaml", "note: Use the VPN of Herr %s of %s GmbH\n"),
    ("sh", 'echo "Restore steps for Herr %s of %s GmbH"\n'),
    ("py", 'msg = "Set up by Herr %s of %s GmbH"\n'),
])
def test_g7_a_sentence_that_opens_with_a_sql_word_is_prose_in_every_profile(profile, line):
    out = _wipe_code(line % (SURNAME, COMPANY), profile)
    assert SURNAME not in out and COMPANY not in out


def test_g7_a_placeholder_cuts_a_literal_and_the_sentence_around_it_is_wiped():
    src = ('a = "Dear ${first}, the offer of Herr %s of %s GmbH is ready"\n'
           'b = "hello %%(name)s, Herr %s of %s GmbH called"\n' % (SURNAME, COMPANY, SURNAME, COMPANY))
    out = _wipe_code(src, "py")
    assert SURNAME not in out and COMPANY not in out
    assert "${first}" in out and "%(name)s" in out, "the placeholders stay as code"


def test_g7_a_quoted_value_of_a_sql_statement_is_wiped_and_the_statement_stays():
    src = 'q = "INSERT INTO kunden (name, ort) VALUES (\'%s\', 3)"\n' % fx.SECOND_PERSON
    out = _wipe_code(src, "py")
    assert fx.SECOND_FIRST not in out and SURNAME not in out
    assert out.startswith('q = "INSERT INTO kunden (name, ort) VALUES (\'') and out.endswith('\', 3)"\n')


def test_g7_sql_statements_keys_and_templates_stay_data_and_as_written():
    src = ('q = "SELECT name, ort FROM kunden WHERE id = ?"\n'
           'u = "UPDATE kunden SET ort = ? WHERE id = ?"\n'
           's = ["USE master", "SET NOCOUNT ON", "EXEC sp_who2", "DECLARE @n INT", "DBCC CHECKDB",\n'
           '     "RESTORE VERIFYONLY", "DROP TABLE kunden", "BACKUP DATABASE kunden TO DISK"]\n'
           'r = "(?P<doc_id>\\\\d+)/[^/]+"\n'
           'k = {"user": "svc_backup", "status": "Active"}\n')
    pieces = extract_text.literal_pieces(src, "py")
    literals = [(src[a:b], k) for a, b, k in pieces if k != "code"]
    assert literals and all(k == "data" for _, k in literals), [x for x in literals if x[1] != "data"]
    seen = "Herr %s prüft das Angebot der %s GmbH.\n" % (SURNAME, COMPANY)
    engine = _engine(seen)
    assert intake._sanitize_code(engine, src, "py", "F-TEST output")[0] == src


def test_g7_the_private_report_counts_what_the_wipe_left_in_data_literals(home, tmp_path):
    code = tmp_path / "export.py"
    code.write_text('q = "SELECT a FROM t WHERE c = 1"\n'
                    'v = "CREATE VIEW Herr %s AS SELECT a FROM t"\n' % SURNAME, encoding="utf-8")
    res = intake.run([code], fx.CUSTOMER_CODE, home)
    private = res.private_report.read_text(encoding="utf-8")
    assert "left in data: 1 guessed name(s)" in private
    public = (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")
    assert "left in data" not in public
