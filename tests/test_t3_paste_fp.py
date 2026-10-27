"""T3: false positives of `awb paste` reported by a project session. A platform error code (Common.0018,
APIGW.0301, ECS-0001) is no phone number, and the short column names of a markdown table (c0, c1, c2) are no MAC
address. Real phone numbers and MAC addresses are still found. The paste door, `awb clean --kind logs` and the
host import share `patterns.find_structured`, so all three gain the fix."""
from __future__ import annotations

from datetime import date

import pytest

from awb import patterns
from tests.test_hooks import project  # noqa: F401  (the project folder of the paste tests)
from tests.test_import_trusted import owner_terminal  # noqa: F401  (the owner's terminal for the import test)

TABLE3 = "| c0 | c1 | c2 |\n|----|----|----|\n| a | b | c |\n"
TABLE6 = "| c0 | c1 | c2 | c3 | c4 | c5 |\n|---|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 | 6 |\n"
ERROR_LINE = "error_code: Common.0018, error_msg: the request is not valid"
ERROR_CELL = "| 400 | Common.0018 | the request is not valid |\n| 401 | APIGW.0301 | 12345 |\n"

FALSE_POSITIVES = [
    TABLE3,
    TABLE6,
    "| c0 | c1 | c2 | c3 | c4 | c5 | c6 | c7 |",
    ERROR_LINE,
    "APIGW.0301",
    "APIGW.0301 401",
    ERROR_CELL,
    "| Common.0018 | 400 | bad request |",
    "| APIGW.0301 | 12345 |",
    "error_code: Common.0018 400 bad request",
    "IAM.0001 403, Ecs.0114 404, VPC.0101 409",
    "Common.0018 2026-10-27 06:02",
    "Ecs.0114\n400",
    "ECS-0001 400",
    '{"error_code": "APIGW.0301", "error_msg": "x", "status": 401}',
]

REAL = [
    ("call +49 89 1234567 today", "phone", "+49 89 1234567"),
    ("call 089 1234567 today", "phone", "089 1234567"),
    ("call 0171-1234567 today", "phone", "0171-1234567"),
    ("port 00:1A:2B:3C:4D:5E is up", "mac", "00:1A:2B:3C:4D:5E"),
    ("port 001a.2b3c.4d5e is up", "mac", "001a.2b3c.4d5e"),
    ("Tel.030 5551234 today", "phone", "030 5551234"),
    ("Tel.0341 5551234 today", "phone", "0341 5551234"),
    ("Fax-0341 5551234 today", "phone", "0341 5551234"),
    ("| 030 | 5551234 |", "phone", "030 | 5551234"),
    ("Common.0018 then call 0171 1234567", "phone", "0171 1234567"),
    ("| Common.0018 | 0171 1234567 |", "phone", "0171 1234567"),
    ("mac 00 1a 2b 3c 4d 5e on eth0", "mac", "00 1a 2b 3c 4d 5e"),
]


def _soft(text: str) -> list[tuple[str, str]]:
    return [(s.cls, text[s.start:s.end]) for s in patterns.find_structured(text) if s.cls in ("phone", "mac")]


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_an_error_code_or_a_short_column_name_is_no_phone_and_no_mac(text):
    assert _soft(text) == [], text


@pytest.mark.parametrize("text,cls,value", REAL)
def test_a_real_phone_or_mac_is_still_found(text, cls, value):
    assert (cls, value) in _soft(text), _soft(text)


def test_awb_paste_keeps_the_table_and_the_error_codes(project):  # noqa: F811
    from awb import paste

    text = TABLE3 + TABLE6 + ERROR_LINE + "\nAPIGW.0301\n" + ERROR_CELL
    path, counts, outside = paste.paste(text, project, today=date(2026, 10, 27))
    out = path.read_text(encoding="utf-8")
    assert counts == {} and outside == 0
    assert out == text
    assert "[phone]" not in out and "[mac]" not in out


def test_awb_paste_still_masks_real_phones_and_macs(project):  # noqa: F811
    from awb import paste

    text = ("error_code: Common.0018, call +49 89 1234567 or 089 1234567 or 0171-1234567\n"
            "| c0 | c1 | c2 |\n|----|----|----|\n| 00:1A:2B:3C:4D:5E | 001a.2b3c.4d5e | APIGW.0301 |\n")
    path, counts, _ = paste.paste(text, project, today=date(2026, 10, 27))
    out = path.read_text(encoding="utf-8")
    assert counts == {"phone": 3, "mac": 2}
    for value in ("1234567", "00:1A:2B", "001a.2b3c"):
        assert value not in out, value
    for kept in ("Common.0018", "APIGW.0301", "| c0 | c1 | c2 |", "|----|----|----|"):
        assert kept in out, kept


def test_clean_logs_keeps_the_error_codes_and_wipes_a_phone(tmp_path, monkeypatch):
    from awb import clean, laptop
    from tests import test_clean

    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    test_clean._register()
    src = tmp_path / "api.log"
    src.write_text(ERROR_LINE + "\n" + ERROR_CELL + TABLE6 + "call 0171-1234567\n", encoding="utf-8")
    (out,) = clean.run([src], test_clean.PROJECT, "logs", tmp_path / "out").outputs
    text = out.read_text(encoding="utf-8")
    for kept in ("Common.0018, error_msg", "| 400 | Common.0018 |", "| 401 | APIGW.0301 | 12345 |",
                 "| c0 | c1 | c2 | c3 | c4 | c5 |"):
        assert kept in text, kept
    assert "1234567" not in text


def test_the_host_import_keeps_the_error_codes_and_wipes_a_phone(home, capsys, tmp_path):
    from awb import importcmd
    from tests.test_import_trusted import _copy, _project

    pr = _project(home, capsys)
    f = tmp_path / "api-errors.md"
    f.write_text(ERROR_LINE + "\n\n" + ERROR_CELL + "\n" + TABLE6 + "\ncall 0171-1234567\n", encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True) == 0
    copy = _copy(home, pr.customer)
    for kept in ("Common.0018", "APIGW.0301 | 12345", "| c0 | c1 | c2 | c3 | c4 | c5 |"):
        assert kept in copy, kept
    assert "1234567" not in copy
