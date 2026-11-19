"""Tests of awb/kbpattern.py: pattern cards in a temporary knowledge base, with an invented project folder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import cli, kb, kbpattern
from tests import fixtures

CODE = "tcp-q7m4"
OTHER = "tcp-k2xa"
RULE = "An inventory line with an 8 GB professional card and up to 64 GB RAM maps to flavor g7v.2xlarge.8"
FACT = "ECS flavor g7v.2xlarge.8 carries a vGPU of 8 GB in eu-de"
RESOURCE = "qxcad-sh-07"           # invented resource name of the planted project
GOAL_WORD = "vrmplex"              # invented goal word, no English word, no TCP word
TOTAL = "6185.46"                  # a computed total of the planted project


def project(home, code=CODE, *, resource=RESOURCE, goal=GOAL_WORD, total=TOTAL, day="2026-11-03") -> Path:
    d = home.projects_root / code
    (d / "calc").mkdir(parents=True, exist_ok=True)
    (d / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- kind: query\n- tags: vdi, gpu, %s\n- goal: CAD "
                                "seats on GPU for the %s office\n- created: %s\n" % (code, code, goal, goal, day),
                                encoding="utf-8")
    (d / "STATE.md").write_text("# State\n\nStatus: running since %s.\n" % day, encoding="utf-8")
    (d / "OPEN.md").write_text("# Open\n", encoding="utf-8")
    (d / "RESOURCES.md").write_text("| handle | kind |\n|---|---|\n| %s | ecs |\n" % resource, encoding="utf-8")
    (d / "calc" / "calc.tsv").write_text("id\ttime\tlabel\texpression\tlets\tresult\n"
                                         "K-1\t2026-11-03T10:00\ttotal\t1+1\t\t%s\n" % total, encoding="utf-8")
    return d


def fact(**kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["gpu"], grade="docs", cls="api", source="docs mirror, user guide")
    args.update(kw)
    return kb.add(FACT, **args)


def add(home, statement=RULE, *, where=None, proj=None, **kw):
    args = dict(packages=["cad-on-tcp"], kind="sizing-rule", tags=["gpu", "vdi"], project=proj or project(home))
    args.update(kw)
    return kbpattern.add(statement, **args)


def run(argv, capsys):
    code = cli.main(["kb", "pattern", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_add_a_card_find_it_and_show_it(home, capsys):
    e = fact()
    card = add(home, facts=[e.id])
    assert kbpattern.ID_RE.fullmatch(card.id) and card.support == 1 and card.facts == (e.id,)
    text = (home.kb / "patterns" / (card.id + ".md")).read_text(encoding="utf-8")
    assert text.startswith("id: %s\nscope: tcp\npackages: cad-on-tcp\nkind: sizing-rule\n" % card.id)
    assert kbpattern.parse(text, card.id) == card
    code, out, _ = run(["find", "professional", "card", "RAM"], capsys)
    assert code == 0 and card.id in out and "support 1" in out
    code, out, _ = run(["show", card.id], capsys)
    assert code == 0 and RULE in out


def test_add_on_the_command_line_from_the_project_folder(home, capsys, monkeypatch):
    d = project(home)
    monkeypatch.chdir(d)
    code, out, err = run(["add", "--package", "cad-on-tcp", "--kind", "step", "--tag", "vdi",
                          "A PoC moves the PDM archive, its SQL database and one session host by lift and shift"],
                         capsys)
    assert code == 0, err
    assert out.startswith("added PAT-") and "support 1" in out


def test_outside_a_project_the_card_is_refused(home, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = run(["add", "--package", "cad-on-tcp", "--kind", "step", "--tag", "vdi", "A step"], capsys)
    assert code == 2 and "source project" in err


@pytest.mark.parametrize("statement, why", [
    ("A seat costs 12.50 EUR per month on g7v.2xlarge.8", "price"),
    ("The office of %s runs eight CAD seats" % fixtures.CUSTOMER_FORMS[0], "name check"),
    ("The pattern came from %s" % OTHER, "code"),
    ("Pilot von zwei Wochen mit der Datenbank und dem Archiv auf dem Host", "English"),
], ids=["price", "name", "code", "language"])
def test_the_screens_of_kb_add_refuse_without_the_value(home, statement, why):
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, statement)
    text = " ".join(ex.value.reasons)
    assert why in text
    for part in (fixtures.CUSTOMER_FORMS[0], OTHER, "12.50"):
        assert part not in text
    assert kbpattern.load() == []


def test_unknown_kind_tag_and_fact_are_refused(home):
    with pytest.raises(kbpattern.PatternError):
        add(home, kind="anecdote")
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, tags=["nosuchtag"], facts=["KB-ZZZZ"])
    assert any("tags.txt" in r for r in ex.value.reasons)
    assert any("not in the knowledge base" in r for r in ex.value.reasons)
    e = fact()
    kb.retire(e.id, "superseded by a newer check")
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, facts=[e.id])
    assert any("retired" in r for r in ex.value.reasons)


@pytest.mark.parametrize("planted, cls", [
    ("Name the session host %s and join it to the domain" % RESOURCE, "resource"),
    ("Name the session host %s.corp and join it to the domain" % RESOURCE, "resource"),
    ("The %s office starts with eight seats" % GOAL_WORD, "scope word"),
    ("The tier lands at 6,185.46 a month for the base load", "total"),
    ("The tier lands at 6.185,46 a month for the base load", "total"),
    ("The PoC ran on 2026-11-03 with one session host", "date"),
    ("The PoC ran on 3.11.2026 with one session host", "date"),
    ("Ask [person 2] for the archive size before the PoC", "intake"),
], ids=["resource", "resource-inside", "scope-word", "total", "total-decimal-comma", "date-iso", "date-dotted",
        "intake-token"])
def test_the_fingerprint_check_refuses_the_projects_own_strings(home, planted, cls):
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, planted, kind="step")
    text = " ".join(ex.value.reasons)
    assert cls in text
    assert RESOURCE not in text and GOAL_WORD not in text and "6185" not in text and "2026-11-03" not in text


def test_a_word_a_kb_fact_names_is_no_fingerprint(home):
    """A rare tool in one inventory is a fingerprint; the same word in a KB fact is not (5.3)."""
    project(home, resource="flexlmd")
    with pytest.raises(kbpattern.Refused):
        add(home, "Keep the flexlmd licence server next to the session hosts", kind="lesson",
            proj=home.projects_root / CODE)
    kb.add("ECS runs a flexlmd licence server on a Windows image without extra licence terms", scope="tcp",
           tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide")
    card = add(home, "Keep the flexlmd licence server next to the session hosts", kind="lesson",
               proj=home.projects_root / CODE)
    assert card.id


def test_fingerprints_are_written_hashed_and_never_as_strings(home):
    add(home)
    files = list((home.shared / "patterns" / "fingerprints").glob("*.tsv"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert CODE not in files[0].name
    for s in (RESOURCE, GOAL_WORD, "618546", "2026-11-03"):
        assert s not in text
    assert {"resource", "scope word", "total", "date"} <= {l.split("\t")[0] for l in text.splitlines()[1:]}
    support = (home.shared / "patterns" / "support.tsv").read_text(encoding="utf-8")
    assert CODE not in support


def test_a_near_duplicate_is_refused_and_support_counts_projects_not_runs(home, capsys):
    card = add(home)
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, RULE + " in eu-de")
    assert "kb pattern support" in " ".join(ex.value.reasons)
    assert [c.id for _, c in ex.value.similar] == [card.id]
    again, rose = kbpattern.support(card.id, project=home.projects_root / CODE)
    assert again.support == 1 and not rose
    second = project(home, OTHER, resource="vtrqz-host-02", goal="plorvex", total="912.30")
    code, out, _ = run(["support", "--project", str(second), card.id], capsys)
    assert code == 0 and "support 2" in out
    assert kbpattern.get(card.id).support == 2
    assert kbpattern.support_of(card.id) == 2
    assert add(home, RULE + " in eu-de", force_new=True).id != card.id


def test_a_script_card_carries_its_checked_file(home, tmp_path):
    good = tmp_path / "setup-host.ps1"
    good.write_text("# install the vGPU driver\nStart-Process -Wait driver.exe\n", encoding="utf-8")
    card = add(home, "Install the vGPU driver before the session host role", kind="script", file=good)
    assert (home.kb / "patterns" / "files" / card.id / "setup-host.ps1").read_text(encoding="utf-8").startswith("#")
    bad = tmp_path / "join.ps1"
    bad.write_text("Rename-Computer -NewName %s\n" % RESOURCE, encoding="utf-8")
    with pytest.raises(kbpattern.Refused) as ex:
        add(home, "Rename the host and join the domain in one script", kind="script", file=bad)
    assert any("the file" in r and "resource" in r for r in ex.value.reasons)
    with pytest.raises(kbpattern.PatternError):
        add(home, "A sizing rule with a file", file=good)


def test_retire_keeps_the_file_and_find_skips_it(home, capsys):
    card = add(home)
    code, out, _ = run(["retire", "--why", "the flavor is no longer offered", card.id], capsys)
    assert code == 0 and "retired" in out
    assert kbpattern.find("professional card") == []
    code, out, _ = run(["show", card.id], capsys)
    assert code == 0 and "RETIRED" in out


def test_cards_never_reach_the_dataset_export(home):
    e = fact()
    add(home, facts=[e.id])
    records, _ = kb.export(include_expired=True)
    assert [r["id"] for r in records] == [e.id]
    assert all("PAT-" not in json.dumps(r) for r in records)
    assert [x.id for x in kb.load()] == [e.id]
