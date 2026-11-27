"""Over-scrub of code and of SQL Server words (asks through development, 2026-11-27: tcp-7c7b and tcp-j5sy import 1).

The import of a code tree replaced column names, attribute names, f-string fields, dict keys and CSS property names
by tokens, because a learned form (a name of the prose) matched them inside string literals: nine Python files no
longer compiled. In a code file a comment, a docstring and a literal that reads like text are wiped as before; a key,
an identifier, a SQL statement or a regular expression gets the registered forms, the structured data and a learned
form standing as a capitalised word of its own only. SQL Server maintenance words (DBCC CHECKDB, RESTORE VERIFYONLY,
RDS) are technical terms. Every name here is invented (tests/fixtures.py).
"""
from __future__ import annotations

import py_compile
from pathlib import Path

from awb import intake
from awb.extract import text as extract_text
from awb.intake.engine import _Engine
from tests import fixtures as fx

PERSON_FIRST, PERSON_LAST = fx.MEETING_FIRSTS[3], fx.MEETING_LASTS[3]
CONTACT_FIRST, CONTACT_LAST = fx.MEETING_FIRSTS[9], fx.MEETING_LASTS[9]
BRAND = fx.MEETING_BRANDS[3]
COMPANY = fx.MEETING_COMPANIES[3]
PLACE = fx.MEETING_PLACES[0]

NOTE = ("Termin mit Herrn %s von der %s. Ansprechpartner: %s %s. Vergabestelle %s.\n"
        % (PERSON_LAST, COMPANY, CONTACT_FIRST, CONTACT_LAST, PLACE))

CODE = '''"""Tender radar of the %(brand)s portal, written by %(cf)s %(cl)s."""
import getpass
import re

# Herr %(pl)s wants the %(brand)s export every Monday
PORTALS = {"%(b)s": {"id": "%(b)s", "adapter": "%(brand)sAdapter", "name": "%(brand)s"}}
%(B)s_DATE = "2026-11-01"
DOC_RE = re.compile(r"(?P<doc_id>\\d+)/(?P<%(brand)s>[^/]+)\\.pdf")


def row(r, conn):
    q = conn.execute("SELECT %(pll)s, %(b)s_type, %(b)s_city FROM tenders WHERE id = ?", (1,))
    s = f"{r.%(b)s} {r.%(brand)s_city} {%(B)s_DATE} {r['%(pll)s']} ready"
    css = "--%(pll)s-blue: #fff; --%(b)s-grey: #eee;"
    log = "%(cf)s %(cl)s started the run for %(company)s"
    pw = getpass.getpass("pin: ")
    return q, s, css, log, pw
''' % {"brand": BRAND, "b": BRAND.lower(), "B": BRAND.upper(), "cf": CONTACT_FIRST, "cl": CONTACT_LAST,
       "pl": PERSON_LAST, "pll": PERSON_LAST.lower(), "company": COMPANY}


def _copy_body(text: str) -> str:
    return text.split("- notes:", 1)[1].split("\n", 2)[2]


def test_a_code_tree_with_names_in_its_prose_still_compiles_and_the_prose_is_wiped(home, tmp_path):
    note = tmp_path / "meeting-notes.md"
    note.write_text(NOTE, encoding="utf-8")
    code = tmp_path / "radar.py"
    code.write_text(CODE, encoding="utf-8")
    res = intake.run([note, code], fx.CUSTOMER_CODE, home)
    copies = {o.name: o.read_text(encoding="utf-8") for o in res.outputs if o.name != "intake-report.md"}
    body = next(_copy_body(t) for t in copies.values() if "import getpass" in t)
    compiled = tmp_path / "copy.py"
    compiled.write_text(body, encoding="utf-8")
    py_compile.compile(str(compiled), doraise=True)
    first_line, comment = body.splitlines()[0], next(l for l in body.splitlines() if l.startswith("# "))
    log = next(l for l in body.splitlines() if l.lstrip().startswith("log = "))
    for name in (CONTACT_FIRST, CONTACT_LAST):
        assert name not in first_line and name not in log, "a name in the docstring or a log text stayed"
    assert PERSON_LAST not in comment, "a learned surname in a comment stayed"
    # the identifiers, keys, columns and property names of the code stay as written
    for kept in ("%s_type" % BRAND.lower(), "r.%s}" % BRAND.lower(), "%s_DATE" % BRAND.upper(),
                 "r['%s']" % PERSON_LAST.lower(), "--%s-blue" % PERSON_LAST.lower(), "?P<doc_id>"):
        assert kept in body, kept


def test_a_literal_is_prose_only_when_it_reads_like_text():
    src = ('x = {"user": "tndrsvc", "msg": "the run of %s %s ended"}\n'
           'q = "SELECT a, b FROM t WHERE c = 1"\n' % (CONTACT_FIRST, CONTACT_LAST))
    kinds = {src[a:b]: k for a, b, k in extract_text.literal_pieces(src, "py")}
    assert kinds['"tndrsvc"'] == "data" and kinds['"user"'] == "data"
    assert kinds['"SELECT a, b FROM t WHERE c = 1"'] == "data"
    assert kinds['"the run of %s %s ended"' % (CONTACT_FIRST, CONTACT_LAST)] == "prose"


def test_sql_server_maintenance_words_and_rds_stay():
    # the runs of capitalised words are joined here, so the self-clean check of the test files reads no run
    words = {k: " ".join(v) for k, v in (("test", ("Restore", "Test")), ("edition", ("Standard", "Edition")),
                                         ("server", ("RDS", "for", "SQL", "Server")), ("der", ("Der", "Restore")))}
    table = ("| Aufgabe | Befehl | Intervall |\n| --- | --- | --- |\n| Konsistenzprüfung | DBCC CHECKDB | wöchentlich |\n"
             "| %(test)s | RESTORE VERIFYONLY | monatlich |\n| Edition | %(edition)s | |\n" % words)
    prose = ("Firma: RDS\nDie Datenbank läuft auf %(server)s. %(der)s lief online, tempdb war voll.\n" % words)
    engine = _Engine([], entry=True)
    for t in (table, prose):
        engine.detect("F-TEST detect", t)
    assert engine.sanitize(table, "F-TEST output")[0] == table
    assert engine.sanitize(prose, "F-TEST output")[0] == prose
