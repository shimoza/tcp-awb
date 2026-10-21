"""`awb kb import`: the facts.jsonl of a TCP Facts release (or its zip) become entries of a knowledge base."""
from __future__ import annotations

import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from awb import cli, kb, kbimport
from tests import fixtures

DAY = date.today()
OLD = (DAY - timedelta(days=40)).isoformat()
NEW = (DAY - timedelta(days=2)).isoformat()


def record(kb_id, statement, **kw):
    r = {"id": kb_id, "scope": "tcp", "statement": statement, "grade": "docs", "checked": NEW, "class": "api",
         "expires": (DAY + timedelta(days=100)).isoformat(), "expired": False, "tags": ["ecs"],
         "source": "docs mirror, user guide", "negative": False, "tried": []}
    r.update(kw)
    return r


def release(tmp_path, records, name="facts.jsonl", meta=True):
    lines = [{"dataset": "TCP Facts", "date": "2026-10-19", "count": len(records)}] if meta else []
    text = "".join(json.dumps(x, sort_keys=True) + "\n" for x in lines + records)
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def run(argv, capsys):
    code = cli.main(["kb", *argv])
    out, err = capsys.readouterr()
    return code, out, err


FACTS = [
    record("KB-AAAA", "Flavor s3.large.2 is offered in region eu-de with two vCPUs."),
    record("KB-BBBB", "The volume backup quota is fifty per project.", tags=["evs"], **{"class": "stable"}),
    record("KB-CCCC", "Shared storage cannot be mounted on a bare metal server.", grade="live",
           tried=["mounted the share on a bare metal host", "read the service limits page"], negative=True),
]


def test_import_writes_entries_with_the_release_values(home, tmp_path, capsys):
    code, out, err = run(["import", str(release(tmp_path, FACTS))], capsys)
    assert code == 0, err
    assert "imported 3, updated 0, skipped 0 (id taken), refused 0" in out
    e = kb.get("KB-CCCC")
    assert (e.scope, e.grade, e.cls, e.checked) == ("tcp", "live", "api", NEW)
    assert e.expires == FACTS[2]["expires"] and e.tags == ("ecs",) and len(e.tried) == 2
    assert e.statement == FACTS[2]["statement"]
    assert e.source == "imported: tcp-facts 2026-10-19; docs mirror, user guide"
    assert kb.get("KB-BBBB").cls == "stable" and kb.get("KB-BBBB").tags == ("evs",)
    assert "KB-AAAA" in (home.kb / "INDEX.md").read_text(encoding="utf-8")


def test_a_refused_entry_is_counted_by_class_and_not_written(home, tmp_path, capsys):
    bad = [record("KB-DDDD", "The runbook of %s lists the flavor." % fixtures.CUSTOMER_FORMS[0]),
           record("KB-EEEE", "The flavor costs 12 EUR per hour."),
           record("KB-FFFF", "Gateways are offered.", tags=["nosuchtag"]),
           record("KB-GGGG", "The bucket cannot be renamed after creation.")]
    code, out, err = run(["import", str(release(tmp_path, FACTS[:1] + bad))], capsys)
    assert code == 1
    assert "imported 1, updated 0, skipped 0 (id taken), refused 4" in out
    assert "name 1" in err and "price 1" in err and "tag 1" in err and "negative 1" in err
    assert fixtures.CUSTOMER_FORMS[0] not in out + err
    assert [p.stem for p in (home.kb / "entries").glob("KB-*.md")] == ["KB-AAAA"]


def test_a_taken_id_is_skipped_and_never_overwritten(home, tmp_path, capsys):
    kb.add("Flavor s3.large.2 is offered in region eu-de with two vCPUs.", scope="tcp", tags=["ecs"], grade="docs",
           cls="api", source="docs mirror, user guide", checked=OLD)
    (taken,) = kb.load()
    run(["import", str(release(tmp_path, [record(taken.id, "A different statement about the same id.")]))], capsys)
    code, out, _ = run(["import", str(release(tmp_path, [record(taken.id, "A different statement about the same id.")]))],
                       capsys)
    assert code == 0 and "skipped 1" in out and "imported 0" in out
    assert kb.get(taken.id).statement.startswith("Flavor s3.large.2")


def test_update_replaces_only_older_entries_of_the_same_dataset(home, tmp_path, capsys):
    older = [record("KB-AAAA", "Flavor s3.large.2 is offered in region eu-de.", checked=OLD),
             record("KB-BBBB", "The volume backup quota is fifty per project.", tags=["evs"], checked=OLD)]
    run(["import", str(release(tmp_path, older))], capsys)
    own = kb.add("The own fact stays where it is.", scope="tcp", tags=["ecs"], grade="docs", cls="api",
                 source="docs mirror, user guide", checked=OLD)
    fresh = [record("KB-AAAA", "Flavor s3.large.2 is offered in region eu-de and eu-nl.", checked=NEW),
             record("KB-BBBB", "The volume backup quota is fifty per project.", tags=["evs"], checked=OLD),
             record(own.id, "A release statement for a hand-written id.", checked=NEW)]
    f = release(tmp_path, fresh, name="again.jsonl")
    code, out, _ = run(["import", str(f)], capsys)
    assert "imported 0, updated 0, skipped 3" in out and kb.get("KB-AAAA").checked == OLD
    code, out, _ = run(["import", "--update", str(f)], capsys)
    assert "imported 0, updated 1, skipped 2" in out
    assert kb.get("KB-AAAA").checked == NEW and "eu-nl" in kb.get("KB-AAAA").statement
    assert kb.get(own.id).statement == "The own fact stays where it is."


def test_a_zip_and_a_dry_run(home, tmp_path, capsys):
    jsonl = release(tmp_path, FACTS)
    z = tmp_path / "tcp-facts-2026-10-19.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(jsonl, "tcp-facts-2026-10-19/facts.jsonl")
    code, out, _ = run(["import", "--dry-run", str(z)], capsys)
    assert code == 0 and "imported 3" in out and "dry run" in out
    assert kb.load() == []
    code, out, _ = run(["import", "--release", "r2", str(z)], capsys)
    assert code == 0 and kb.get("KB-AAAA").source.startswith("imported: tcp-facts r2; ")


def test_bad_lines_and_bad_input_are_counted_without_their_content(home, tmp_path, capsys):
    f = release(tmp_path, FACTS[:1])
    f.write_text(f.read_text(encoding="utf-8") + "not json at all\n" + json.dumps(record("KB-1", "x")) + "\n"
                 + json.dumps(record("KB-HHHH", "A fact with a bad date.", checked="2999-01-01")) + "\n",
                 encoding="utf-8")
    code, out, err = run(["import", str(f)], capsys)
    assert code == 1 and "imported 1" in out and "refused 3" in out
    assert "format 1" in err and "id 1" in err and "shape 1" in err
    assert "not json" not in out + err
    code, _, err = run(["import", str(tmp_path / "missing.jsonl")], capsys)
    assert code == 2


def test_it_needs_no_vault_daemon_or_work_user(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(tmp_path / "nowhere" / "check.sock"))
    home.register.write_text("code\tkind\tform\tadded\tstatus\n", encoding="utf-8")   # an empty register, as awb init
    code, out, err = run(["import", str(release(tmp_path, FACTS))], capsys)
    assert code == 0, err
    assert kbimport.MARK in kb.get("KB-AAAA").source


def test_a_scope_that_the_folder_does_not_hold_is_refused(home, tmp_path, capsys):
    home.kb.mkdir(parents=True, exist_ok=True)
    (home.kb / "SCOPE").write_text("hcs\n", encoding="utf-8")
    code, out, err = run(["import", str(release(tmp_path, FACTS[:1]))], capsys)
    assert code == 1 and "scope 1" in err
