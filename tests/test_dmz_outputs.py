"""DMZ 5 (B-089, the owner's word of 2026-11-25): the documentation pages, the knowledge base, the review, the lab logs and
the files the sessions write run no content check inside.

The checks stay at the entry of customer material (DMZ 1) and at the exit (the public push, the dataset, the solutions
packages, the public page). A fact with a registered name is added and shown as written; the dataset build leaves it out
with a count. A page of a public host with a sample password is shown whole. A status mail with an IP address, a name or a
key shaped value is sent as written. Every name is an invented fixture name; the values are put together at run time.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from awb import cli, config, kb, ownerwait, projects
from awb.tcp import dataset, doc
from tests import fixtures as fx
from tests.test_dataset import TODAY, facts, live, new, snapshot   # noqa: F401  (the fixtures of the dataset tests)
from tests.test_doc import HOSTS, PROJECT, Getter, html_page

NAME = fx.CUSTOMER_FORMS[0]
IP = "203.0.113.45"
SECRET = "db_pass" + "word = " + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"


# --------------------------------------------------------------------------- the knowledge base


def test_a_kb_add_with_a_fixture_name_passes_and_find_and_show_print_it_as_written(home, capsys):
    statement = "the clusters of %s run behind %s in eu-de" % (NAME, IP)
    e = new(statement, grade="docs", tags=["ecs"])
    assert e.statement == statement and kb.get(e.id).statement == statement
    assert cli.main(["kb", "find", "clusters", "behind"]) == 0
    out = capsys.readouterr().out
    assert e.id in out and NAME in out and "withheld" not in out
    assert cli.main(["kb", "show", e.id]) == 0
    assert NAME in capsys.readouterr().out
    assert NAME in (kb.root() / "INDEX.md").read_text(encoding="utf-8")


def test_a_kb_add_needs_no_register_and_takes_a_secret_shaped_text(home):
    home.register.unlink()
    e = new("the sample reads %s from the console" % SECRET, grade="docs")
    assert SECRET in kb.get(e.id).statement


def test_the_dataset_build_leaves_a_fact_with_a_name_out_and_counts_it(home, facts, tmp_path, capsys):
    named = new("the cluster of %s answers on eu-de" % NAME, grade="docs", tags=["ecs"])
    keyed = new("the sample reads %s in eu-de" % SECRET, grade="docs", tags=["obs"])
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08"]) == 0
    out = capsys.readouterr().out
    assert "withheld 2" in out
    root = tmp_path / "ds" / "tcp-facts-2026-10-08"
    rows = [json.loads(l) for l in (root / "facts.jsonl").read_text().splitlines()]
    ids = {r.get("id") for r in rows}
    assert named.id not in ids and keyed.id not in ids and facts.id in ids
    for path in root.rglob("*.md"):
        assert NAME not in path.read_text(), path.name


def test_the_fact_export_checks_each_fact_and_an_address_is_no_finding(home):
    plain = new("the endpoint answers on 203.0.113.45 and gw01.qvelmar-demo.net", grade="docs", tags=["ecs"])
    named = new("the cluster of %s answers" % NAME, grade="docs", tags=["obs"])
    records, left = kb.export(include_expired=True, today=TODAY)
    assert [r["id"] for r in records] == [plain.id]
    assert left["withheld"] == 1 and named.id not in {r["id"] for r in records}


# --------------------------------------------------------------------------- the documentation page


def test_a_doc_page_with_a_sample_password_is_shown_whole(home, tmp_path):
    body = "<p>Connect with the sample:</p><pre>%s</pre><p>Then %s answers.</p>" % (SECRET, NAME)
    answer, stream = doc.serve({"url": "https://example.com/x"}, PROJECT, doc.Limiter(lambda: None), None,
                               getter=Getter({("example.com", "/x"): html_page(body)}),
                               hosts=HOSTS, tmp_dir=tmp_path)
    assert answer["ok"], answer
    text = stream.read_text(encoding="utf-8")
    stream.unlink()
    assert SECRET in text and NAME in text and "withheld" not in text


# --------------------------------------------------------------------------- the owner status mail


def test_a_status_mail_with_an_ip_a_name_and_a_secret_is_sent_as_written(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    (Path(proj.path) / "OPEN.md").write_text(
        "## Waiting on the owner\n\n- Owner item 1: open %s for %s, key %s\n" % (IP, NAME, SECRET), encoding="utf-8")
    sent: list = []
    word = ownerwait.mail(home, lambda s, b: sent.append((s, b)) or "sent", tmp_path / "none.md", tmp_path / "runs")
    assert word == "sent" and len(sent) == 1
    assert IP in sent[0][1] and NAME in sent[0][1] and SECRET in sent[0][1]
    assert "withheld" not in sent[0][1]


def test_a_status_mail_is_sent_when_the_name_check_cannot_run(home, tmp_path, monkeypatch):
    from awb import ledger

    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    (Path(proj.path) / "OPEN.md").write_text("## Waiting on the owner\n\n- Owner item 1: open %s\n" % IP, encoding="utf-8")

    def unavailable(*a, **k):
        raise ledger.LedgerError("private detail")

    monkeypatch.setattr(ledger, "hit_indexes", unavailable)
    sent: list = []
    assert ownerwait.mail(home, lambda s, b: sent.append(b) or "sent", tmp_path / "none.md", tmp_path / "runs") == "sent"
    assert IP in sent[0]
