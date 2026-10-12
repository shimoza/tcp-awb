"""awb import CODE --paste: pasted text through the wipe-mode intake (owner side).

The text comes on standard input and is never echoed. A secret refuses the whole paste with its class and line, the
rest runs through the engine of the intake: the customer forms become the code, every other person, company, place,
mail address and phone a token. A mail loses its signature and its quoted chain headers as a whole, a transcript
keeps its turns as [person 1], [person 2] and its timestamps, a table keeps its columns and loses the columns of
people and mail addresses. Every name is an invented fixture name; the secrets are put together at run time.
"""
from __future__ import annotations

import io
import re
import stat
import sys
from datetime import date
from pathlib import Path

import pytest

import awb
from awb import cli, codes, config, importcmd, pastein, projects, rulesync
from tests import fixtures as fx
from tests.test_intake import sealed_vault  # noqa: F401  (the fixture of the encrypted vault)

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
DAY = date.today().isoformat()

SIGNER = fx.PLANTED_PERSON
SIGNER_FIRST, SIGNER_LAST = SIGNER.split()
OTHER = fx.SECOND_PERSON
OTHER_FIRST, OTHER_LAST = OTHER.split()
THIRD = fx.FIRST_LAST
REGISTERED_PERSON = fx.PERSON_FORMS[0]
PHONE = "+49 170 1234567"
OTHER_PHONE = "+49 30 5550 4321"
SIGNER_MAIL = "xqarv.pomblet@nrgtz-beratung.example"
OTHER_MAIL = "ulvrad.qesmotz@grvetz-systemhaus.example"
SIGNER_URL = "https://www.nrgtz-beratung.example/team"
TITLE = "senior platform architect"
OTHER_TITLE = "head of platform"
TECHNICAL = "the load balancer in front of the two app clusters needs port 8443 open and terraform 1.9 does the rollout"


def private_key() -> str:
    return "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----\n"


def api_token() -> str:
    return "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"


def mail_text(extra: str = "") -> str:
    """A mail with a signature, a quoted answer with its own signature, a quoted header block and the key `extra`."""
    sep = "-----Original " + "Message-----"
    return "\n".join([
        "Hi %s," % OTHER_FIRST,
        "",
        TECHNICAL + ".",
        "We spoke with %s about the timeline and I asked %s for the sizing." % (fx.CUSTOMER_FORMS[0], REGISTERED_PERSON),
        extra.rstrip("\n"),
        "Best regards,",
        SIGNER,
        TITLE,
        fx.PLANTED_CANDIDATE,
        fx.PLANTED_STREET,
        "12345 " + fx.PLACE_FORMS[0],
        "Tel " + PHONE,
        SIGNER_MAIL,
        SIGNER_URL,
        "",
        "On 5 Oct 2026 at 09:14, %s <%s> wrote:" % (OTHER, OTHER_MAIL),
        "> Hello %s," % SIGNER_FIRST,
        "> can you confirm the zone layout for %s?" % fx.CUSTOMER_FORMS[1],
        ">",
        "> Thanks,",
        "> " + OTHER_FIRST,
        "> %s | %s" % (fx.SECOND_COMPANY, OTHER_TITLE),
        "> " + OTHER_PHONE,
        "",
        sep,
        "From: %s <jonas.vrelkam@%s>" % (THIRD, fx.CUSTOMER_DOMAIN),
        "Sent: Friday, 2 October 2026 11:00",
        "To: %s; %s" % (SIGNER, OTHER),
        "Subject: Re: landing zone sizing",
        "",
        "Please see the numbers below.",
        "",
    ])


PIECES_OF_THE_SIGNATURE = (SIGNER_FIRST, SIGNER_LAST, TITLE, fx.PLANTED_CANDIDATE.split()[0], fx.PLANTED_STREET.split()[0],
                           "1234567", SIGNER_MAIL, "nrgtz-beratung", OTHER_FIRST, OTHER_LAST, OTHER_TITLE, "5550",
                           fx.SECOND_COMPANY.split()[0], "grvetz", THIRD.split()[1], "landing zone sizing",
                           "09:14", "2 October")


def transcript_text() -> str:
    return "\n".join([
        "WEBVTT",
        "",
        "Recording started",
        "%s joined the meeting" % OTHER,
        "%s joined the meeting" % SIGNER,
        "",
        "00:00:05 %s: Welcome everyone, today we look at the landing zone for %s." % (OTHER, fx.CUSTOMER_FORMS[0]),
        "00:00:21 %s: Thanks %s. I think two app clusters in one zone are enough." % (SIGNER, OTHER_FIRST),
        "00:00:40 %s: I disagree, %s. The terraform state needs a second region." % (THIRD, SIGNER_FIRST),
        "(Crosstalk)",
        "00:01:02 %s: OK, %s has a point. Let us ask %s." % (OTHER_FIRST, THIRD.split()[0], REGISTERED_PERSON),
        "00:01:30 %s: Fine." % SIGNER_LAST,
        "%s left the meeting" % THIRD,
        "Meeting ended after 00:02:10",
        "This transcript was generated automatically.",
        "",
    ])


def table_text() -> str:
    rows = [
        ("Name", "Rolle", "E-Mail", "Telefon", "System", "Zone"),
        (SIGNER, "platform lead", SIGNER_MAIL, PHONE, "load balancer", "az-1"),
        (OTHER, "architect", OTHER_MAIL, OTHER_PHONE, "terraform", "az-2"),
        (REGISTERED_PERSON, "owner", "tobias@" + fx.CUSTOMER_DOMAIN, "", "k8s", "az-1"),
    ]
    return "\n".join("\t".join(r) for r in rows) + "\n"


class Stdin(io.StringIO):
    """Standard input of a test: text, no terminal."""


class NoRead:
    """Standard input that must not be read: the checks of who may run the command come first."""

    def isatty(self):
        return False

    def read(self, *a):
        raise AssertionError("the paste was read before the checks")


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    """The owner's own terminal: no assistant session in the environment."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


@pytest.fixture
def lab(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "lab", GOAL, None, register_path)


def take(home, code, text, kind=None) -> int:
    return importcmd.run_paste(home, code, kind, stdin=Stdin(text))


def copies(home, folder=fx.CUSTOMER_CODE) -> dict[str, str]:
    box = home.outbox / folder
    return {f.name: f.read_text(encoding="utf-8") for f in sorted(box.iterdir()) if f.name != "intake-report.md"} \
        if box.is_dir() else {}


def said(capsys) -> str:
    out, err = capsys.readouterr()
    return out + err


def never_echoed(shown: str, text: str) -> None:
    """Nothing of the paste, no line of it and no name, is in what the command printed."""
    for line in text.splitlines():
        if len(line.strip()) >= 8:
            assert line.strip() not in shown, "the command printed a line of the paste"
    for word in (SIGNER_FIRST, SIGNER_LAST, OTHER_FIRST, OTHER_LAST, fx.PLANTED_CANDIDATE.split()[0], "nrgtz-beratung"):
        assert word.lower() not in shown.lower(), "the command printed a name"
    fx.assert_no_fixture_name(shown, "the output of awb import --paste")


# --------------------------------------------------------------------------- a mail


def test_a_secret_refuses_the_whole_paste_with_its_class_and_line_and_writes_nothing(home, project, capsys):
    text = mail_text(extra=private_key() + "token " + api_token())
    lines = text.splitlines()
    key_line = next(i for i, x in enumerate(lines, start=1) if "BEGIN RSA" in x)
    token_line = next(i for i, x in enumerate(lines, start=1) if x.startswith("token "))
    with pytest.raises(importcmd.ImportRefused) as err:
        take(home, project.code, text, "mail")
    msg = str(err.value)
    assert "private-key at line %d" % key_line in msg and "token at line %d" % token_line in msg
    assert "nothing was written" in msg
    never_echoed(msg + said(capsys), text)
    assert api_token() not in msg and "MIIEow" not in msg
    assert copies(home) == {}, "no copy"
    assert not (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").exists(), "no public report"
    assert not list(home.private_reports.rglob("*.md*")), "no private report"
    assert not list(rulesync.notices_dir(home).glob("input-*.json")), "no notice for the sessions"


def test_a_password_line_refuses_the_paste_too(home, project, capsys):
    text = "Hi,\n\nthe login is below.\npassword: %s\n\n%s\n" % ("hunter2" + "Qx9Tz4" + "LmVb7Rn2Kd", TECHNICAL)
    with pytest.raises(importcmd.ImportRefused, match=r"secret at line 4"):
        take(home, project.code, text, "mail")
    assert copies(home) == {}


def test_the_same_mail_without_the_key_is_wiped_and_nothing_of_the_signature_is_left(home, project, capsys):
    text = mail_text()
    assert take(home, project.code, text, "mail") == 0
    shown = said(capsys)
    never_echoed(shown, text)
    assert "copies written: 1 (mail-%s-1.md)" % DAY in shown
    assert re.search(r"wiped: person \d+", shown)
    (name, copy), = copies(home).items()
    assert name == "mail-%s-1.md" % DAY
    low = copy.lower()
    for piece in PIECES_OF_THE_SIGNATURE:
        assert piece.lower() not in low, "a piece of the signature or the header is left"
    fx.assert_no_fixture_name(copy, "the copy of a mail")
    assert fx.CUSTOMER_CODE in copy, "the customer forms are the code"
    assert fx.CUSTOMER_FORMS[0] not in copy and fx.CUSTOMER_FORMS[1] not in copy
    assert copy.count("[signature]") == 2, "the signature and the quoted signature, each as a whole"
    assert copy.count("[mail header]") == 2, "the reply line and the block of the quoted mail"
    assert re.search(r"\[mail header\] from \[person \d+\] to \[person \d+\], \[person \d+\]", copy)
    for kept in ("load balancer", "8443", "terraform 1.9", "Please see the numbers below.", "zone layout"):
        assert kept in copy, "the technical text stays"
    assert not re.search(r"(?:MAIL|PHONE|URL)-[A-Z2-7]{4}", copy), "the structured data went with the blocks"


def test_the_copy_and_the_reports_are_where_an_imported_file_goes(home, project, capsys):
    take(home, project.code, mail_text(), "mail")
    capsys.readouterr()
    copy = home.outbox / fx.CUSTOMER_CODE / ("mail-%s-1.md" % DAY)
    assert stat.S_IMODE(copy.stat().st_mode) == 0o640
    report = (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")
    assert "mail-%s-1.md" % DAY in report and "signature block(s) wiped as a whole" in report
    fx.assert_no_fixture_name(report, "the public report")
    for piece in PIECES_OF_THE_SIGNATURE[:6]:
        assert piece.lower() not in report.lower()
    (private,) = list((home.private_reports / fx.CUSTOMER_CODE).glob("*.md*"))
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert SIGNER_LAST in private.read_text(encoding="utf-8"), "the private report holds the wiped values"
    line = rulesync.notice(home, "s-paste", Path(project.path)) or ""
    assert "new input: 1 copies in the outbox of %s" % fx.CUSTOMER_CODE in line
    assert not (home.originals / fx.CUSTOMER_CODE).exists(), "a paste is no file: no original in the vault"


def test_the_number_counts_up_over_the_outbox_and_the_input_folder(home, project, capsys):
    take(home, project.code, mail_text(), "mail")
    take(home, project.code, mail_text(), "mail")
    (Path(project.path) / "input" / ("mail-%s-3.md" % DAY)).write_text("a mail the session took in earlier\n")
    take(home, project.code, mail_text(), "mail")
    assert sorted(copies(home)) == ["mail-%s-%d.md" % (DAY, n) for n in (1, 2, 4)]
    take(home, project.code, table_text(), "table")
    assert "table-%s-1.csv" % DAY in copies(home)


def test_a_paste_that_only_holds_technical_text_passes_unchanged(home, project, capsys):
    text = ("%s.\n\nThe zone layout is az-1 and az-2, the subnet is 10.20.0.0/24 and the health check runs on port 8443.\n"
            "Steps: 1. build the image, 2. run terraform plan, 3. apply.\n" % TECHNICAL)
    for kind in (None, "mail"):
        assert take(home, project.code, text, kind) == 0
        assert re.search(r"wiped: nothing", said(capsys))
    first, second = (copies(home)[n] for n in sorted(copies(home)))
    assert first == text and second == text
    assert sorted(copies(home)) == ["mail-%s-1.md" % DAY, "paste-%s-1.md" % DAY]


def test_a_heading_or_a_thanks_before_text_opens_no_signature_block(home, project):
    """The rules of wipe mode read a heading of two capitalised words that opens with Best as a sign-off and wipe the
    second word; the block of the signature needs more than that, so the lines after it stay."""
    text = "best practices".title() + "\nthe zone layout is az-1 and az-2\nthe next line is still text\n"
    take(home, project.code, text, "mail")
    thanks = "Thanks\n\nthe deployment starts at nine and the check runs after it\nthe rollback needs no approval\n"
    take(home, project.code, thanks, "mail")
    first, second = (copies(home)[n] for n in sorted(copies(home)))
    assert "[signature]" not in first and first.endswith("the zone layout is az-1 and az-2\nthe next line is still text\n")
    assert second == thanks


def test_a_signature_after_a_delimiter_line_is_wiped_as_a_whole(home, project):
    text = "%s\n\n-- \n%s\n%s\nTel %s\n" % (TECHNICAL, SIGNER, TITLE, PHONE)
    take(home, project.code, text, "mail")
    (copy,) = copies(home).values()
    assert copy.count("[signature]") == 1 and TITLE not in copy and "1234567" not in copy and SIGNER_LAST not in copy
    assert TECHNICAL in copy


def test_a_paste_without_a_kind_is_plain_text(home, project):
    take(home, project.code, mail_text())
    (name, copy), = copies(home).items()
    assert name == "paste-%s-1.md" % DAY and copy.count("[signature]") == 2


# --------------------------------------------------------------------------- a transcript


def test_a_transcript_keeps_its_turns_readable_as_numbered_persons(home, project, capsys):
    text = transcript_text()
    assert take(home, project.code, text, "transcript") == 0
    shown = said(capsys)
    never_echoed(shown, text)
    (name, copy), = copies(home).items()
    assert name == "transcript-%s-1.md" % DAY
    turns = [line for line in copy.splitlines() if line.strip()]
    assert [re.match(r"(\d\d:\d\d:\d\d) (\[person \d\]|CUST-Q7M4-PERS-1):", t).group(1) for t in turns] == [
        "00:00:05", "00:00:21", "00:00:40", "00:01:02", "00:01:30"], "every turn keeps its timestamp"
    who = [re.match(r"\d\d:\d\d:\d\d (\[person \d\]):", t).group(1) for t in turns]
    assert who == ["[person 1]", "[person 2]", "[person 3]", "[person 1]", "[person 2]"], \
        "numbered by the first turn, one token per speaker"
    assert "Thanks [person 1]." in copy and "I disagree, [person 2]." in copy, "the name in the speech is the same token"
    assert "[person 3] has a point" in copy
    assert fx.PERSON_CODE in copy, "a registered person keeps the code"
    for name_piece in (SIGNER_FIRST, SIGNER_LAST, OTHER_FIRST, OTHER_LAST, THIRD.split()[0], THIRD.split()[1]):
        assert name_piece not in copy
    for filler in ("WEBVTT", "Recording", "joined", "left the meeting", "Crosstalk", "Meeting ended", "generated"):
        assert filler not in copy, "the filler lines of the meeting tool are dropped"
    assert "landing zone for %s" % fx.CUSTOMER_CODE in copy
    assert "filler line(s) dropped" in (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")


def test_the_speaker_of_a_single_turn_is_a_person_too(home, project):
    guest = fx.SELFTEST_FORM.split()[0]
    text = "00:00:01 %s: We start now.\n00:00:09 %s: One question about the zone.\n" % (OTHER, guest)
    take(home, project.code, text, "transcript")
    (copy,) = copies(home).values()
    assert "00:00:01 [person 1]: We start now." in copy and "00:00:09 [person 2]: One question" in copy


def test_a_line_of_speech_with_a_colon_and_the_word_joined_is_no_filler(home, project):
    text = ("00:00:01 %s: the new node joined the cluster after the meeting.\n"
            "note that the zone layout needs a review before the call ended with the vendor\n"
            "NOTE a comment of the caption file\n" % OTHER)
    take(home, project.code, text, "transcript")
    (copy,) = copies(home).values()
    assert "joined the cluster" in copy and copy.startswith("00:00:01 [person 1]:")
    assert "note that the zone layout" in copy, "a spoken line is no filler"
    assert "caption file" not in copy, "the NOTE of a caption file is"


# --------------------------------------------------------------------------- a table


def test_a_table_keeps_its_columns_and_loses_the_columns_of_people_and_mail_addresses(home, project, capsys):
    text = table_text()
    assert take(home, project.code, text, "table") == 0
    shown = said(capsys)
    never_echoed(shown, text)
    (name, copy), = copies(home).items()
    assert name == "table-%s-1.csv" % DAY
    import csv

    rows = list(csv.reader(io.StringIO(copy)))
    assert len(rows) == 4 and {len(r) for r in rows} == {6}, "the rows and columns stay"
    assert rows[0] == ["Name", "Rolle", "E-Mail", "Telefon", "System", "Zone"]
    names, roles, mails, phones, systems, zones = zip(*rows[1:])
    assert names[0] == "[person 1]" and names[1] == "[person 2]" and names[2] == fx.PERSON_CODE
    assert all(re.fullmatch(r"MAIL-[A-Z2-7]{4}", m) for m in mails), "every mail address is a token"
    assert phones[2] == "" and all(re.fullmatch(r"PHONE-[A-Z2-7]{4}", x) for x in phones[:2])
    assert roles == ("platform lead", "architect", "owner")
    assert systems == ("load balancer", "terraform", "k8s") and zones == ("az-1", "az-2", "az-1")
    for piece in (SIGNER_FIRST, SIGNER_LAST, OTHER_LAST, "1234567", "5550", "nrgtz", "grvetz", fx.CUSTOMER_DOMAIN):
        assert piece not in copy
    assert "column(s) wiped as a whole" in (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")


def test_a_name_column_of_a_technical_table_stays_and_one_value_gets_one_token(home, project):
    text = "Name\tTyp\tIP\tVersion\nweb-1\tvm\t10.0.0.5\t1.9\nweb-2\tvm\t10.0.0.6\t1.9\n"
    take(home, project.code, text, "table")
    (copy,) = copies(home).values()
    assert "web-1,vm" in copy and "web-2,vm" in copy
    again = "Ansprechpartner\tSystem\n%s\tk8s\n%s\tvm\n" % (SIGNER, SIGNER)
    take(home, project.code, again, "table")
    csv_text = copies(home)["table-%s-2.csv" % DAY]
    assert csv_text.splitlines()[1:] == ["[person 1],k8s", "[person 1],vm"]


def test_a_company_cell_with_more_than_the_name_is_one_token(home, project):
    text = "Firma\tOrt\n%s (head office)\t%s\n" % (fx.PLANTED_CANDIDATE, fx.PLANTED_STREET)
    take(home, project.code, text, "table")
    (copy,) = copies(home).values()
    assert copy.splitlines()[1] == "[company 1],[place 1]" and "head office" not in copy


def test_text_without_tabs_is_no_table(home, project):
    with pytest.raises(importcmd.ImportRefused, match="tab-separated"):
        take(home, project.code, "a,b,c\n1,2,3\n", "table")
    assert copies(home) == {}


# --------------------------------------------------------------------------- the customer


def test_a_project_without_a_customer_gets_tokens_and_the_copy_goes_under_its_code(home, lab, capsys):
    assert take(home, lab.code, mail_text(), "mail") == 0
    (name, copy), = copies(home, lab.code).items()
    assert name == "mail-%s-1.md" % DAY
    assert fx.CUSTOMER_FORMS[0] not in copy, "a registered form is a code in every project"
    assert not [d for d in home.outbox.iterdir() if d.name.startswith("CUST-")], "no customer code was issued"
    report = (home.outbox / lab.code / "intake-report.md").read_text(encoding="utf-8")
    assert "- customer: none (project %s)" % lab.code in report
    assert "new input: 1 copies in the outbox of %s" % lab.code in (rulesync.notice(home, "s-lab", Path(lab.path)) or "")


def test_a_customer_without_a_form_is_asked_for_by_the_import_not_by_the_paste(home, project, capsys):
    lines = [x for x in home.register.read_text(encoding="utf-8").splitlines()
             if not (x.split("\t")[0] == fx.CUSTOMER_CODE and x.split("\t")[1] == "CUST")]
    home.register.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(importcmd.ImportRefused, match="no form in the register: run awb import"):
        take(home, project.code, mail_text(), "mail")
    assert copies(home) == {}


# --------------------------------------------------------------------------- who may run it


def test_refused_in_a_session_and_for_the_work_user_before_the_paste_is_read(home, project, monkeypatch, capsys):
    line = codes.PASTE_HOLD_LINE % project.code
    assert line == ("held: a pasted mail or transcript goes in through awb import %s --paste in your own terminal"
                    % project.code)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(sys, "stdin", NoRead())
    assert cli.main(["import", project.code, "--paste"]) == 2
    assert said(capsys).strip() == line
    assert cli.main(["import", project.code, "--paste", "--kind", "transcript"]) == 2
    assert said(capsys).strip() == line
    with pytest.raises(importcmd.ImportRefused) as err:
        importcmd.run_paste(home, project.code, "table", stdin=NoRead())
    assert str(err.value) == line
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert cli.main(["import", project.code, "--paste"]) == 2
    assert said(capsys).strip() == line
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT")
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["import", project.code, "--paste"]) == 2
    assert said(capsys).strip() == line
    assert copies(home) == {}


def test_the_hold_line_without_a_code_names_the_pattern(home, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert importcmd.paste_hold_line("") == codes.PASTE_HOLD_LINE % "tcp-xxxx"
    with pytest.raises(importcmd.ImportRefused) as err:
        importcmd.run_paste(home, "not-a-code", None, stdin=NoRead())
    assert str(err.value) == codes.PASTE_HOLD_LINE % "tcp-xxxx"


def test_a_project_code_is_checked_before_the_paste_is_read(home):
    with pytest.raises(importcmd.ImportRefused, match="tcp-xxxx"):
        importcmd.run_paste(home, "not-a-code", None, stdin=NoRead())
    with pytest.raises(importcmd.ImportRefused, match="not an active project"):
        importcmd.run_paste(home, "tcp-zzzz", None, stdin=NoRead())


# --------------------------------------------------------------------------- the command line


def test_the_command_reads_standard_input_and_the_arguments_are_checked(home, project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin(mail_text()))
    assert cli.main(["import", project.code, "--paste", "--kind", "mail"]) == 0
    assert "copies written: 1 (mail-%s-1.md)" % DAY in said(capsys)
    for argv, why in ((["--kind", "mail"], "--kind goes with --paste"),
                      (["--paste", "somefile.txt"], "--paste takes no FILE"),
                      (["--paste", "--redo"], "--paste takes no FILE"),
                      (["--paste", "--review"], "--paste takes no FILE"),
                      (["--paste", "--customer", "new"], "--paste takes no FILE")):
        monkeypatch.setattr(sys, "stdin", Stdin(mail_text()))
        assert cli.main(["import", project.code] + argv) == 2
        assert why in said(capsys)
    monkeypatch.setattr(sys, "stdin", Stdin(mail_text()))
    assert cli.main(["import", project.code, "--paste", "--kind", "letter"]) == 2
    assert len(copies(home)) == 1, "only the first paste was taken"


def test_an_empty_paste_and_text_that_is_not_utf8_are_refused(home, project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("  \n\n"))
    assert cli.main(["import", project.code, "--paste"]) == 2
    assert "the paste is empty" in said(capsys)
    with pytest.raises(pastein.PasteRefused, match="not UTF-8"):
        pastein.read_text(b"\xff\xfe\x00caf\xe9")
    with pytest.raises(pastein.PasteRefused, match="over 1 MB"):
        pastein.read_text(b"x " * (pastein.MAX_BYTES // 2 + 1))
    assert pastein.read_text(b"\xef\xbb\xbfa\r\nb\r") == "a\nb\n", "no byte order mark and newlines as \\n"
    assert copies(home) == {}


def test_the_help_names_the_clipboard_and_the_kinds(capsys):
    assert importcmd.main(["--help"]) == 0
    text = said(capsys)
    for must in ("--paste", "pbpaste", "wl-paste", "xclip", "--kind", "mail", "transcript", "table", "Ctrl-D"):
        assert must in text


def test_a_withheld_paste_writes_nothing_and_is_a_hold(home, project, monkeypatch, capsys):
    from awb import intake

    monkeypatch.setattr(intake, "_body_hits", lambda engine, text: [pastein.Span(0, 1, "unknown")])
    assert take(home, project.code, mail_text(), "mail") == 1
    shown = said(capsys)
    assert "the final check withheld the paste" in shown and "nothing was written" in shown
    never_echoed(shown, mail_text())
    assert copies(home) == {} and not list(home.private_reports.rglob("*.md*"))


def test_a_notice_takes_the_names_of_pastes_and_no_other_name(home, project):
    path = rulesync.post_input(home, fx.CUSTOMER_CODE, project.code, ["mail-2026-10-12-1.md", "table-2026-10-12-2.csv",
                                                                      "mail 2026.md", "F-ABCD"])
    text = path.read_text(encoding="utf-8")
    assert "mail-2026-10-12-1.md" in text and "table-2026-10-12-2.csv" in text and "F-ABCD" in text
    assert "mail 2026" not in text
    assert pastein.NAME_RE.fullmatch("transcript-2026-10-12-3.md") and not pastein.NAME_RE.fullmatch("note-2026-10-12-3.md")


def test_from_a_terminal_the_text_is_read_to_ctrl_d_without_an_echo(capsys):
    """The terminal does not print the paste back: the echo is off while the text is read and on again after."""
    import os
    import select
    import threading
    import time

    termios = pytest.importorskip("termios")
    master, slave = os.openpty()
    stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(slave, closefd=False)))
    typed = "a line of the paste that must not come back %s" % SIGNER_LAST

    def typist():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and termios.tcgetattr(slave)[3] & termios.ECHO:
            time.sleep(0.01)
        os.write(master, (typed + "\n").encode("utf-8") + b"\x04")

    thread = threading.Thread(target=typist)
    thread.start()
    try:
        data = importcmd.read_stdin(stdin)
        thread.join()
        assert data == (typed + "\n").encode("utf-8")
        assert termios.tcgetattr(slave)[3] & termios.ECHO, "the echo is on again"
        ready, _, _ = select.select([master], [], [], 0.2)
        echoed = os.read(master, 4096) if ready else b""
        assert SIGNER_LAST.encode() not in echoed, "the terminal printed the paste back"
    finally:
        thread.join()
        os.close(master)
        os.close(slave)
    err = capsys.readouterr().err
    assert "Ctrl-D" in err and SIGNER_LAST not in err


def test_the_prompt_hook_points_a_blocked_paste_at_the_door(home, project, monkeypatch, capsys):
    """A prompt with a pasted block that the hook stops tells where a pasted mail or transcript goes in; a typed
    prompt that is stopped does not (nothing was pasted)."""
    import json

    from awb import hooks

    def prompt(text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": text, "cwd": project.path})))
        code = hooks.main(["prompt"])
        return code, capsys.readouterr().err

    door = "A pasted mail or transcript goes in through `awb import %s --paste` in your own terminal." % project.code
    body = "%s\n\nBest regards,\n%s\n" % (TECHNICAL, fx.CUSTOMER_FORMS[0])
    code, err = prompt('what does this say?\n<pasted_content id="p7k2">\n%s</pasted_content id="p7k2">' % body)
    assert code == 2 and "the prompt carries a registered name (1 hits)" in err and door in err
    fx.assert_no_fixture_name(err, "the message of the prompt hook")
    code, err = prompt("size the app clusters of %s" % fx.CUSTOMER_FORMS[0])
    assert code == 2 and "registered name" in err and "--paste" not in err


def test_the_rows_of_this_file_are_in_the_test_list():
    import json

    rows = json.loads((ROOT / "build" / "tests.json").read_text(encoding="utf-8"))
    mine = [t for t in rows["tests"] if t["id"].startswith("PI-")]
    assert len(mine) >= 12 and all("tests/test_paste_import.py" in t["run"] for t in mine)
    assert rows["total_tests"] == len(rows["tests"])
    names = set(re.findall(r"^def (test_\w+)", Path(__file__).read_text(encoding="utf-8"), re.M))
    for row in mine:
        for ref in re.findall(r"::(test_\w+)", row["run"]):
            assert ref in names, "%s names a test that is not in the file" % row["id"]


def test_in_an_encrypted_vault_the_private_report_is_sealed_and_the_copy_is_written(sealed_vault):
    """The register is read through the vault daemon and the private report is sealed by it; the paste keeps no
    original, so nothing else of it lies in the vault."""
    import base64

    from awb import intake, vault

    p = sealed_vault
    assert intake.vault_encrypted(p)
    res = pastein.run(p, fx.CUSTOMER_CODE, mail_text(), "mail")
    assert res.unsealed == 0
    (copy,) = res.outputs
    text = copy.read_text(encoding="utf-8")
    assert fx.CUSTOMER_CODE in text and SIGNER_LAST not in text and "[signature]" in text
    assert res.private_report.name.endswith(".md.gpg") and res.private_report.is_file()
    names = sorted(f.name for f in p.private_reports.rglob("*") if f.is_file())
    assert names == [res.private_report.name], "no plaintext private report"
    opened = base64.b64decode(vault.admin_call("open_file", p.admin_sock, path=str(res.private_report))["data"])
    assert copy.name in opened.decode("utf-8") and SIGNER_LAST in opened.decode("utf-8")
    assert not (p.originals / fx.CUSTOMER_CODE).exists()
    fx.assert_no_fixture_name(res.public_report.read_text(encoding="utf-8"), "the public report")
