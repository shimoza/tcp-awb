"""Intake keeps code files whole and lists the tracked changes of a word file with anchors.

A script arrives as it is except for the string literals and comments. A reviewed word file gives its
insertions and deletions with an author number and the text before each.
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest

from awb import intake
from awb.extract import text as extract_text
from tests import fixtures as fx

FULL = fx.CUSTOMER_FORMS[0]
PERSON = fx.PERSON_FORMS[0]
SECRET_VALUE = "-".join(("Xk3", "planted", "value", "77"))

PS1 = (
    "function Get-BankReport {\n"
    "    param($Bic, $Iban)\n"
    "    $obj = Get-Thing\n"
    "    $Bic = $obj.Url\n"
    "    $Name = '%s'\n"
    "    Write-Output $obj.Server.Host $Bic\n"
    "}\n"
)


def _run(home, name: str, data: bytes):
    path = home.inbox / name
    path.write_bytes(data)
    res = intake.run([path], "new", home)
    assert len(res.outputs) == 1
    return res.outputs[0].read_text(encoding="utf-8")


def _body(output: str) -> str:
    return output.split("\n\n", 1)[1] if "\n\n" in output else output


def test_a_powershell_file_arrives_whole_except_a_name_in_a_string(home):
    out = _run(home, "script.ps1", (PS1 % FULL).encode("utf-8"))
    assert FULL not in out
    expected = PS1 % "CUST-"
    for line in expected.splitlines():
        if "CUST-" not in line:
            assert line in out
    for kept in ("param($Bic, $Iban)", "$Bic = $obj.Url", "Write-Output $obj.Server.Host $Bic",
                 "function Get-BankReport {"):
        assert kept in out
    assert re.search(r"\$Name = 'CUST-[A-Z0-9]{4}'", out)


def test_a_secret_in_a_comment_is_still_caught(home):
    text = "# service login\n# %s: %s\n$x = 1\n" % ("pass" + "word", SECRET_VALUE)
    out = _run(home, "login.ps1", text.encode("utf-8"))
    assert SECRET_VALUE not in out
    assert "$x = 1" in out


def test_an_encoded_run_in_a_string_of_a_code_file_is_still_decoded_and_checked(home):
    import base64
    blob = base64.b64encode(("contact %s today" % FULL).encode("utf-8")).decode("ascii")
    out = _run(home, "blob.py", ('data = "%s"\nprint(data)\n' % blob).encode("utf-8"))
    assert blob not in out
    assert FULL not in out


def test_a_hex_run_in_a_sql_file_is_still_decoded_and_checked(home):
    hexed = ("contact %s today" % FULL).encode("utf-8").hex()
    out = _run(home, "dump.sql", ("INSERT INTO t VALUES ('\\x%s');\n" % hexed).encode("utf-8"))
    assert hexed not in out
    assert FULL not in out


@pytest.mark.parametrize("suffix", sorted(extract_text.CODE_PROFILES))
def test_every_code_suffix_has_a_profile_that_covers_the_text(suffix):
    profile = extract_text.CODE_PROFILES[suffix]
    sample = "key: 'value' # note\nx = \"y\"\n"
    pieces = extract_text.code_segments(sample, profile)
    assert "".join(sample[a:b] for a, b, _ in pieces) == sample
    assert len(extract_text.mask_code(sample, profile)) == len(sample)


def test_literals_and_comments_of_the_profiles():
    def literals(text, profile):
        return [text[a:b] for a, b, lit in extract_text.code_segments(text, profile) if lit]

    assert literals("a = 'x' # c\n", "py") == ["'x'", "# c"]
    assert literals("$a = 'it''s' <# b #> # c\n", "ps") == ["'it''s'", "<# b #>", "# c"]
    assert literals("echo hi # not\ncat <<EOT\nbody\nEOT\nls\n", "sh") == ["# not", "body\n"]
    assert literals("name: web # c\nurl: http://h\n", "yaml") == [" web # c", " http://h"]
    assert literals("rem a note\nset A=b\ncall x.cmd\n", "bat") == ["rem a note", "b"]
    assert literals("select 1 -- c\n", "sql") == ["-- c"]


def _docx_with_changes() -> bytes:
    import docx
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("The delivery of the ordered goods is planned for the first week, ")
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "1")
    ins.set(qn("w:author"), PERSON)
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "and the second week"
    run.append(t)
    ins.append(run)
    p._p.append(ins)
    p.add_run(" as agreed ")
    dele = OxmlElement("w:del")
    dele.set(qn("w:id"), "2")
    dele.set(qn("w:author"), "second reviewer")
    drun = OxmlElement("w:r")
    dt = OxmlElement("w:delText")
    dt.text = "in writing"
    drun.append(dt)
    dele.append(drun)
    p._p.append(dele)
    p.add_run(" before the end.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_a_word_file_lists_its_insertion_and_its_deletion_with_anchors(home):
    out = _run(home, "reviewed.docx", _docx_with_changes())
    assert "### Tracked changes" in out
    lines = [line for line in out.splitlines() if line.startswith(("- insertion", "- deletion"))]
    assert len(lines) == 2
    ins, dele = lines
    assert re.fullmatch(r'- insertion by author-1 after "(.{0,40})": "and the second week"', ins)
    assert ins.split('after "')[1].split('"')[0].endswith("planned for the first week, ")
    assert re.fullmatch(r'- deletion by author-2 after "(.{0,40})": "in writing"', dele)
    assert dele.split('after "')[1].split('"')[0].endswith("and the second week as agreed ")
    assert len(dele.split('after "')[1].split('"')[0]) <= 40
    assert PERSON not in out
    zipfile.ZipFile(io.BytesIO(_docx_with_changes())).close()
