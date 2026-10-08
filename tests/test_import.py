"""awb import CODE: customer material into a project in one command (T4, owner side).

The files come from the command line, the bucket folder in/ of the project (tests/obs_fake.py) or the vault inbox;
the intake runs in wipe mode for the project's customer; the output is counts only and the sessions of the project
hear of the copies at their next prompt (awb/rulesync.py). Every name is an invented fixture name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import awb
from awb import bucket, config, importcmd, intake, projects, register, rulesync
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
FIRST, LAST = fx.PLANTED_PERSON.split()
TEXT = "Termin mit Herrn %s am Montag, das Angebot der %s liegt vor.\n" % (fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE)


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    """The owner's own terminal: no assistant session in the environment, no bucket unless a test gives one."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


@pytest.fixture
def lab(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "lab", GOAL, None, register_path)


def _files(folder: Path, n: int = 2) -> list[Path]:
    out = []
    for i in range(n):
        f = folder / ("note-%s-%d.txt" % (LAST.lower(), i))
        f.write_text(TEXT, encoding="utf-8")
        out.append(f)
    return out


def _copies(home, customer=fx.CUSTOMER_CODE) -> dict[str, str]:
    return {f.stem: f.read_text(encoding="utf-8") for f in sorted((home.outbox / customer).glob("F-*.md"))}


def _lines(capsys) -> str:
    out, err = capsys.readouterr()
    return out + err


def _no_name(text: str) -> None:
    fx.assert_no_fixture_name(text, "the output of awb import")
    for word in (FIRST, LAST, fx.PLANTED_CANDIDATE.split()[0], "note-"):
        assert word.lower() not in text.lower(), "awb import printed a name or a file name"


# --------------------------------------------------------------------------- where the files come from


def test_the_vault_inbox_is_taken_and_only_counts_are_printed(home, project, capsys):
    _files(home.inbox)
    assert importcmd.run(home, project.code, []) == 0
    out = _lines(capsys)
    _no_name(out)
    assert "%s: 2 file(s) from the vault inbox" % project.code in out
    for line in ("files taken: 2", "copies written: 2", "pictures held: 0", "files withheld: 0"):
        assert line in out
    assert re.search(r"wiped: person \d+, company \d+", out)
    copies = _copies(home)
    assert len(copies) == 2 and all(fx.PLANTED_CANDIDATE not in t and "[person 1]" in t for t in copies.values())
    assert list(home.inbox.iterdir()) == []


def test_a_file_is_taken_once(home, project, capsys):
    _files(home.inbox)
    importcmd.run(home, project.code, [])
    capsys.readouterr()
    assert importcmd.run(home, project.code, []) == 0
    assert "no new file" in _lines(capsys)
    assert len(_copies(home)) == 2


def test_files_on_the_command_line(home, project, tmp_path, capsys):
    files = _files(tmp_path, 1)
    assert importcmd.run(home, project.code, files) == 0
    out = _lines(capsys)
    assert "1 file(s) from the command line" in out and "copies written: 1" in out
    assert not files[0].exists(), "the original moved into the vault"


def test_the_bucket_folder_in_comes_before_the_vault_inbox(home, project, monkeypatch, capsys):
    with FakeOBS() as fake:
        monkeypatch.setattr(bucket, "client", lambda: fake.client())
        folder, _ = bucket.ensure_folder(fake.client(), project)
        fake.objects[folder + "in/brief %s.txt" % fx.CUSTOMER_FORMS[1]] = TEXT.encode("utf-8")
        _files(home.inbox, 1)
        assert importcmd.run(home, project.code, []) == 0
        out = _lines(capsys)
        assert "1 file(s) from the bucket folder in/ of %s" % project.code in out
        _no_name(out)
        assert len(list(home.inbox.iterdir())) == 1, "the vault inbox waits for the next import"
        assert importcmd.run(home, project.code, []) == 0
        assert "from the vault inbox" in _lines(capsys)
    assert len(_copies(home)) == 2


# --------------------------------------------------------------------------- the customer


def test_customer_new_issues_the_projects_own_four_characters(home, lab, capsys):
    _files(home.inbox, 1)
    typed = iter(["%s Spedition GmbH\n" % fx.SECOND_LAST, "%s\n" % fx.SECOND_LAST, "\n"])
    assert importcmd.run(home, lab.code, [], customer="new", read=lambda: next(typed), tty=True) == 0
    want = "CUST-" + lab.code.split("-")[1].upper()
    out = _lines(capsys)
    assert "new customer %s" % want in out and "registered 2 form(s) of %s" % want in out
    assert fx.SECOND_LAST not in out
    forms = [e.form for e in register.load(home.register) if e.code == want]
    assert len(forms) == 2
    assert _copies(home, want)


def test_customer_new_is_random_when_the_projects_code_is_taken(home, lab, capsys):
    taken = "CUST-" + lab.code.split("-")[1].upper()
    config.make_dir(home.outbox / taken, 0o750, shared=True)
    code, own = importcmd.new_customer(home, lab.code)
    assert not own and code != taken and code.startswith("CUST-")


def test_the_forms_prompt_needs_a_terminal(home, lab, capsys):
    _files(home.inbox, 1)
    with pytest.raises(importcmd.ImportRefused, match="own terminal"):
        importcmd.run(home, lab.code, [], customer="new", read=lambda: "", tty=False)
    with pytest.raises(importcmd.ImportRefused, match="no form was typed"):
        importcmd.run(home, lab.code, [], customer="new", read=lambda: "\n", tty=True)
    assert len(list(home.inbox.iterdir())) == 1, "nothing was imported"


def test_a_project_without_a_customer_needs_one(home, lab):
    _files(home.inbox, 1)
    with pytest.raises(importcmd.ImportRefused, match="no customer"):
        importcmd.run(home, lab.code, [])
    with pytest.raises(importcmd.ImportRefused, match="CUST-XXXX or new"):
        importcmd.run(home, lab.code, [], customer=fx.ORG_CODE)
    assert len(list(home.inbox.iterdir())) == 1


def test_the_customer_of_a_project_cannot_be_changed(home, project):
    with pytest.raises(importcmd.ImportRefused, match="not the customer"):
        importcmd.run(home, project.code, [], customer="CUST-AAAA")
    with pytest.raises(importcmd.ImportRefused, match="has the customer"):
        importcmd.run(home, project.code, [], customer="new")


# --------------------------------------------------------------------------- --redo and --review


def test_redo_writes_the_copies_again_under_the_same_file_ids_with_the_keep_list_of_now(home, project, capsys):
    _files(home.inbox)
    importcmd.run(home, project.code, [])
    before = _copies(home)
    assert all(fx.PLANTED_CANDIDATE not in t for t in before.values())
    intake.keep_phrase(home, fx.PLANTED_CANDIDATE)
    capsys.readouterr()
    assert importcmd.run(home, project.code, [], redo=True) == 0
    out = _lines(capsys)
    assert "2 file(s) from the vault (the last import of %s)" % fx.CUSTOMER_CODE in out
    assert "copies written: 2 (replacing the earlier ones)" in out
    after = _copies(home)
    assert set(after) == set(before)
    assert all(fx.PLANTED_CANDIDATE in t for t in after.values())
    assert not list((home.vault / "tmp").glob("redo-*")), "the plaintext copies out of the vault are gone"
    assert len(list((home.originals / fx.CUSTOMER_CODE).iterdir())) == 2


def test_review_stops_once_registers_the_marks_and_wipes_what_is_left_for_later(home, project, capsys):
    _files(home.inbox, 1)

    def edit(path):
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#") and fx.PLANTED_PERSON in line:
                line = "p " + line.strip()
            elif not line.startswith("#") and fx.PLANTED_CANDIDATE.split()[0] in line:
                line = "- " + line.strip()
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    before = {e.code for e in register.load(home.register)}
    assert importcmd.run(home, project.code, [], review=True, edit=edit, confirm=lambda q: True, tty=True) == 0
    new = {e.code for e in register.load(home.register)} - before
    assert len(new) == 1 and new.pop().startswith(fx.CUSTOMER_CODE + "-PERS-")
    (copy,) = _copies(home).values()
    pers = next(e.code for e in register.load(home.register) if e.form == fx.PLANTED_PERSON)
    assert pers in copy and fx.PLANTED_CANDIDATE not in copy and "[company 1]" in copy
    _no_name(_lines(capsys))


# --------------------------------------------------------------------------- who may run it


def test_refused_for_the_work_user_and_inside_an_assistant_session(home, project, monkeypatch, capsys):
    from awb import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["import", project.code]) == 2
    assert "assistant session" in _lines(capsys)
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["import", project.code]) == 2
    assert "owner" in _lines(capsys)


def test_a_project_code_is_checked_before_anything_is_read(home, capsys):
    with pytest.raises(importcmd.ImportRefused, match="tcp-xxxx"):
        importcmd.run(home, "not-a-code", [])
    with pytest.raises(importcmd.ImportRefused, match="not an active project"):
        importcmd.run(home, "tcp-zzzz", [])


# --------------------------------------------------------------------------- the notice of the sessions


def test_the_next_prompt_of_a_session_of_the_project_hears_of_the_copies_once(home, project, lab, capsys):
    _files(home.inbox)
    importcmd.run(home, project.code, [])
    line = rulesync.notice(home, "s-one", Path(project.path))
    assert line and "new input: 2 copies in the outbox of %s, report intake-report.md" % fx.CUSTOMER_CODE in line
    again = rulesync.notice(home, "s-one", Path(project.path)) or ""
    assert "new input" not in again, "told once"
    assert "new input" in (rulesync.notice(home, "s-two", Path(project.path)) or ""), "every session once"
    assert "new input" not in (rulesync.notice(home, "s-lab", Path(lab.path)) or ""), "not another customer"
    for copy in (home.outbox / fx.CUSTOMER_CODE).glob("F-*.md"):
        copy.unlink()
    assert "new input" not in (rulesync.notice(home, "s-three", Path(project.path)) or ""), "nothing waits"


def test_a_redo_is_told_as_replaced(home, project):
    _files(home.inbox, 1)
    importcmd.run(home, project.code, [])
    rulesync.notice(home, "s-one", Path(project.path))
    importcmd.run(home, project.code, [], redo=True)
    line = rulesync.notice(home, "s-one", Path(project.path)) or ""
    assert "replaced: 1 copies in the outbox of %s" % fx.CUSTOMER_CODE in line


def test_a_notice_carries_codes_and_file_ids_only(home, project):
    path = rulesync.post_input(home, fx.CUSTOMER_CODE, project.code, ["F-ABCD", "not an id"], replaced=False)
    text = path.read_text(encoding="utf-8")
    assert "not an id" not in text and "F-ABCD" in text
    with pytest.raises(ValueError):
        rulesync.post_input(home, fx.CUSTOMER_FORMS[1], project.code, [])
