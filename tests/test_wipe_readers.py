"""The readers before detection (T4, the build items of the red team of 2026-10-07): a folded calendar line is
unfolded, a flavor head is no host, a host number before an IP is no phone. Invented names of tests/fixtures.py."""
from __future__ import annotations

from awb import patterns
from tests import fixtures as fx

FIRST, LAST = fx.PLANTED_PERSON.split()


def _j(*words_: str) -> str:
    return " ".join(words_)


def test_a_folded_calendar_line_is_unfolded_before_detection(tmp_path):
    from awb import extract

    f = tmp_path / "invite.ics"
    f.write_text("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDESCRIPTION:Termin mit %s\r\n %s %s bestätigen.\r\nEND:VEVENT\r\n"
                 "END:VCALENDAR\r\n" % (FIRST[:3], FIRST[3:], LAST), encoding="utf-8")
    ex = extract.extract(f)
    assert fx.PLANTED_PERSON in ex.text
    assert extract.text.unfold_calendar("A\n B\n\tC\nD") == "ABC\nD"


def test_a_flavor_id_is_no_host_and_a_host_number_before_an_ip_is_no_phone():
    for text in ("| %s | rds.pg.c6.large.4 |" % _j("Relational", "Database", "Service"), "rds.pg.c2.large"):
        assert not [s for s in patterns.find_structured(text) if s.cls == "url"], text
    for text in ("| srv-db-01 | 10.0.0.5 | DB |", "| srv-web-02 | 192.0.2.10 | Web |"):
        assert not [s for s in patterns.find_structured(text) if s.cls == "phone"], text
    assert [s.cls for s in patterns.find_structured("call 030 1234567 today")] == ["phone"]
    assert [s.cls for s in patterns.find_structured("see nrgtz.example.\nNext")] == ["url"]
