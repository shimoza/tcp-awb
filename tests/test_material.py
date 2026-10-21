"""awb material add: text the owner pasted into the project session, kept as project material (CS6).

The checks need no vault: the secret scan, the home-path rule, the owner blocklist and the name check through the
check socket. A hit refuses the whole text with its class and line and writes nothing. Every name is an invented
fixture name; the secrets are put together at run time.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import date

import pytest

from awb import cli, codes, config, ledger, material
from tests import fixtures as fx

CODE = "tcp-q7m4"
NOTE = "the load balancer in front of the two app clusters needs port 8443 open\nterraform 1.9 does the rollout\n"


def api_token() -> str:
    return "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"


@pytest.fixture
def project(home, monkeypatch):
    root = home.projects_root / CODE
    (root / "input").mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- customer: none\n" % (CODE, CODE), encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def run(monkeypatch, text, *args) -> int:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8"))
    return cli.main(["material", "add", *args])


def kept(root) -> dict[str, str]:
    return {f.name: f.read_text(encoding="utf-8") for f in sorted((root / "input").iterdir())}


def test_a_clean_note_is_kept_unchanged_and_the_file_name_is_all_it_prints(home, project, monkeypatch, capsys):
    assert run(monkeypatch, NOTE, "--title", "port plan") == 0
    out, err = capsys.readouterr()
    day = date.today().isoformat()
    assert out == "note-%s-1.md\n" % day and err == ""
    assert kept(project) == {"note-%s-1.md" % day: NOTE}
    assert run(monkeypatch, NOTE) == 0
    assert capsys.readouterr().out == "note-%s-2.md\n" % day
    lines = [json.loads(x) for f in home.ledger.glob("*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    assert [e["done"] for e in lines] == ["material added: note, port plan", "material added: note"]
    assert {e["project"] for e in lines} == {CODE}


@pytest.mark.parametrize("kind,name", [("mail", "mail-%s-1.md"), ("transcript", "transcript-%s-1.md"),
                                       ("table", "table-%s-1.csv"), ("note", "note-%s-1.md")])
def test_the_kind_picks_the_name_of_the_paste_door(home, project, monkeypatch, capsys, kind, name):
    assert run(monkeypatch, NOTE, "--kind", kind) == 0
    assert capsys.readouterr().out == (name % date.today().isoformat()) + "\n"


@pytest.mark.parametrize("form", [fx.CUSTOMER_FORMS[0], fx.PERSON_FORMS[0]])
def test_a_registered_name_refuses_the_whole_text_with_its_kind_and_line(home, project, monkeypatch, capsys, form):
    text = "first line\nthe offer for %s is late\nlast line\n" % form
    assert run(monkeypatch, text) == 2
    out, err = capsys.readouterr()
    assert out == "" and "registered name at line 2" in err and "nothing was written" in err
    assert form not in err and form.split()[0] not in err
    assert kept(project) == {} and not list(home.ledger.glob("*.jsonl"))


def test_a_secret_refuses_the_whole_text_with_its_class_and_line(home, project, monkeypatch, capsys):
    text = "build log\ntoken %s\nend\n" % api_token()
    assert run(monkeypatch, text) == 2
    out, err = capsys.readouterr()
    assert out == "" and "token at line 2" in err and api_token() not in err
    assert kept(project) == {}


def test_a_home_path_and_the_owner_blocklist_refuse_the_text(home, project, monkeypatch, capsys):
    assert run(monkeypatch, "see /home/%s/notes/plan.md\n" % "qvortek") == 2
    assert "homepath at line 1" in capsys.readouterr().err
    from awb import gate

    monkeypatch.setattr(gate, "blocklist_re", lambda: __import__("re").compile("zorblax"))
    assert run(monkeypatch, "fine\nthe zorblax folder\n") == 2
    assert "blocklist at line 2" in capsys.readouterr().err
    assert kept(project) == {}


def test_a_title_with_a_name_refuses_too(home, project, monkeypatch, capsys):
    assert run(monkeypatch, NOTE, "--title", "notes of %s" % fx.PERSON_FORMS[0]) == 2
    assert fx.PERSON_FORMS[0] not in capsys.readouterr().err
    assert kept(project) == {}


def test_empty_text_is_refused(home, project, monkeypatch, capsys):
    assert run(monkeypatch, "  \n") == 2
    assert "empty" in capsys.readouterr().err and kept(project) == {}


def test_outside_a_project_folder_it_refuses(home, tmp_path, monkeypatch, capsys):
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert run(monkeypatch, NOTE) == 2
    out, err = capsys.readouterr()
    assert out == "" and "run it in a project folder" in err
    assert list(bare.iterdir()) == []


def test_it_runs_inside_an_assistant_session(home, project, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(monkeypatch, NOTE) == 0
    assert capsys.readouterr().out.startswith("note-")


def test_when_the_name_check_cannot_run_nothing_is_written(home, project, monkeypatch, capsys):
    from awb import hooks

    def down(*a, **k):
        raise hooks.Unavailable("no vault daemon")

    monkeypatch.setattr(hooks, "name_hits", down)
    assert run(monkeypatch, NOTE) == 2
    out, err = capsys.readouterr()
    assert out == "" and "name check did not run" in err and kept(project) == {}


def test_a_failed_ledger_keeps_the_file_and_says_so(home, project, monkeypatch, capsys):
    def broken(*a, **k):
        raise ledger.LedgerError("no")

    monkeypatch.setattr(ledger, "add", broken)
    assert run(monkeypatch, NOTE) == 1
    out, err = capsys.readouterr()
    assert out.startswith("note-") and "ledger line was not added" in err and len(kept(project)) == 1


def test_the_owner_side_keeps_the_paste_door_as_it_is(home, project, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("never read"))
    assert cli.main(["import", CODE, "--paste"]) == 2
    assert capsys.readouterr().err.strip() == codes.PASTE_HOLD_LINE % CODE
    assert kept(project) == {}


def test_the_work_rules_carry_the_material_rule():
    rules = (config.Path(material.__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md")
    text = rules.read_text(encoding="utf-8")
    assert "After `material:` pipe the text unchanged to `awb material add`; report its one line." in " ".join(text.split())
