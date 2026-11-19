"""Tests of awb/tcp/solutions.py: build, status, the release screen and the release of a package, with invented
projects, a stand-in price API and a temporary knowledge base."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from awb import cli, kb, kbpattern
from awb.tcp import solutions
from tests import test_kbpattern as tk

SOLUTIONS = "tcp-r2nz"
TODAY = date.today()


@dataclass
class Rec:
    flavor: str
    os: str
    amount: Decimal
    unit: str = "Hour"
    currency: str = "EUR"

    def price(self, term):
        return self.amount if term == "PAYG" else None


class Got:
    def __init__(self, records):
        self.records = records


def fetcher(amount="1.2345"):
    calls = []

    def fetch(service, region):
        calls.append((service, region))
        return Got([Rec("g7v.2xlarge.8", "Windows", Decimal(amount)), Rec("g7v.2xlarge.8", "Linux", Decimal("0.9")),
                    Rec("s3.large.2", "Linux", Decimal("0.08"))])
    fetch.calls = calls
    return fetch


def git(d: Path, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.org", "PATH": "/usr/bin:/bin", "HOME": str(d)})


def solutions_project(home, monkeypatch, *, offer=None, kind_card="sizing-rule", services="Elastic Cloud Server"):
    """A source project with a fact and a card, and the solutions project with one package that names both."""
    fact = tk.fact()
    card = tk.add(home, kind=kind_card, facts=[fact.id])
    d = home.projects_root / SOLUTIONS
    pkg = d / "packages" / "cad-on-tcp"
    pkg.mkdir(parents=True)
    (d / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- kind: query\n- tags: vdi\n- goal: [service "
                                "project] Customer-free solution packages for TCP\n" % (SOLUTIONS, SOLUTIONS))
    (pkg / "PACKAGE.md").write_text("slug: cad-on-tcp\nkind: offer\naudience: sales and partners\ntags: gpu, vdi\n"
                                    "parts: offer.md, sizing.md\nservices: %s\n---\nnotes\n" % services)
    (pkg / "offer.md").write_text(offer or "# CAD on TCP\n\nA seat on g7v.2xlarge.8 costs "
                                  "{{price ecs g7v.2xlarge.8 os=Windows}}.\n")
    (pkg / "sizing.md").write_text("## Sizing\n\n%s (%s, %s).\n" % (tk.RULE, fact.id, card.id))
    (pkg / "risks.md").write_text("Seen once, stays here.\n")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "start")
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.org"}.items():
        monkeypatch.setenv(k, v)
    return d, pkg, fact, card


def test_build_fills_prices_live_and_writes_sources_and_out(home, monkeypatch):
    d, pkg, fact, card = solutions_project(home, monkeypatch)
    f = fetcher()
    version, out, n = solutions.build("cad-on-tcp", project=d, fetcher=f, today=TODAY)
    assert version == "v1" and out == pkg / "out" / "v1" and n == {"sources": 2, "prices": 1}
    text = (out / "cad-on-tcp.md").read_text()
    assert "{{price" not in text and "1.2345 EUR per hour (PAYG, eu-de, live price API of %s)" % TODAY in text
    assert "Seen once" not in text
    assert (out / "cad-on-tcp.html").read_text().startswith("<!doctype html>")
    assert (out / "cad-on-tcp.pdf").read_bytes().startswith(b"%PDF")
    rows = solutions.read_tsv(pkg / "SOURCES.tsv")
    assert {r["id"]: r["type"] for r in rows} == {fact.id: "fact", card.id: "card"}
    assert solutions.read_tsv(out / "PRICES.tsv")[0]["amount"] == "1.2345"
    assert f.calls == [("ecs", "eu-de")]


@pytest.mark.parametrize("offer, why", [
    ("Rests on KB-ZZZZ.\n", "not in the knowledge base"),
    ("A seat costs {{price ecs g7v.2xlarge.8}}.\n", "2 records match"),
    ("A seat costs {{price ecs nosuch.flavor}}.\n", "0 records match"),
], ids=["unknown-id", "two-records", "no-record"])
def test_build_refuses_a_missing_source_and_an_unclear_price(home, monkeypatch, offer, why):
    d, pkg, _, _ = solutions_project(home, monkeypatch, offer=offer)
    with pytest.raises(solutions.SolutionsError) as ex:
        solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)
    assert why in str(ex.value)
    assert not (pkg / "out").exists()


def test_build_refuses_a_service_that_is_not_offered_and_risks_as_a_part(home, monkeypatch):
    d, pkg, _, _ = solutions_project(home, monkeypatch, services="Elastic Cloud Server, quantum annealer")
    with pytest.raises(solutions.SolutionsError) as ex:
        solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)
    assert "service 2" in str(ex.value)
    (pkg / "PACKAGE.md").write_text((pkg / "PACKAGE.md").read_text().replace(
        ", quantum annealer", "").replace("parts: offer.md", "parts: risks.md, offer.md"))
    with pytest.raises(solutions.SolutionsError) as ex:
        solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)
    assert "risks.md" in str(ex.value)


def test_release_screens_commits_tags_and_the_next_build_is_a_minor(home, monkeypatch, capsys):
    d, pkg, _, _ = solutions_project(home, monkeypatch)
    solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), today=TODAY)
    version, found = solutions.release("cad-on-tcp", project=d, today=TODAY)
    assert found == [] and version == "v1"
    assert subprocess.run(["git", "-C", str(d), "tag"], capture_output=True, text=True).stdout.split() == [
        "cad-on-tcp/v1"]
    assert solutions.releases(pkg)[0][:2] == ("v1", TODAY.isoformat())
    assert solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)[0] == "v1.1"
    assert solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False, major=True)[0] == "v2"
    st = solutions.status("cad-on-tcp", project=d, fetcher=fetcher(), today=TODAY)
    assert st.state == "released" and st.version == "v1" and not any(st.counts.values())


def test_status_counts_each_staleness_reason(home, monkeypatch, capsys):
    d, pkg, fact, card = solutions_project(home, monkeypatch)
    solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), today=TODAY - timedelta(days=1), pdf=False)
    solutions.release("cad-on-tcp", project=d, today=TODAY - timedelta(days=1))
    kb.amend(fact.id, tk.FACT + " and eu-nl", checked=TODAY)
    other = tk.project(home, tk.OTHER, resource="vtrqz-host-02", goal="plorvex", total="912.30")
    tk.add(home, "Customers ask for a two-week PoC with one session host", kind="signal", proj=other)
    st = solutions.status("cad-on-tcp", project=d, fetcher=fetcher("2.0"), today=TODAY)
    assert st.state == "stale"
    assert st.counts == {"changed": 1, "retired": 0, "expired": 0, "new": 1, "price": 1, "service": 0, "age": 0}
    kbpattern.retire(card.id, "replaced by a finer rule")
    late = solutions.status("cad-on-tcp", project=d, offline=True, today=TODAY + timedelta(days=100))
    assert late.counts["retired"] == 1 and late.counts["age"] == 1 and "price" not in late.counts
    code = cli.main(["solutions", "status", "--offline", "--project", str(d)])
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("cad-on-tcp  v1  stale  changed 1, retired 1")


def _built(home, monkeypatch, offer):
    d, pkg, _, card = solutions_project(home, monkeypatch, offer=offer)
    solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)
    return d, pkg, card


@pytest.mark.parametrize("planted, cls", [
    ("Name the host %s in the kit.\n" % tk.RESOURCE, "fingerprint (resource)"),
    ("The base tier comes to 6,185.46 a month.\n", "fingerprint (total)"),
    ("The PoC ran on 2026-11-03.\n", "fingerprint (date)"),
    ("Taken from tcp-" "k2xa.\n", "code (project)"),
    ("See K-" "4 of the calc.\n", "code (K number)"),
], ids=["resource", "total", "date", "project-code", "k-number"])
def test_the_release_screen_finds_fingerprints_and_codes_without_the_value(home, monkeypatch, capsys, planted, cls):
    d, pkg, _ = _built(home, monkeypatch, "# CAD on TCP\n\n" + planted)
    version, found = solutions.release("cad-on-tcp", project=d)
    assert version == "v1" and cls in [f.cls for f in found]
    assert {(f.file, f.line) for f in found} == {("out/v1/cad-on-tcp.md", 3)}
    assert {f.cls for f in found} <= {cls, "export (name, secret or candidate)"}
    assert solutions.releases(pkg) == []
    code = cli.main(["solutions", "screen", "cad-on-tcp", "--project", str(d)])
    out = capsys.readouterr().out
    assert code == 1 and cls in out
    for value in (tk.RESOURCE, "6,185.46", "2026-11-03", "k2xa", tk.CODE):
        assert value not in out


def test_the_release_screen_holds_a_customer_card_until_support_two(home, monkeypatch):
    d, pkg, fact, card = solutions_project(home, monkeypatch, kind_card="objection")
    solutions.build("cad-on-tcp", project=d, fetcher=fetcher(), pdf=False)
    _, found = solutions.release("cad-on-tcp", project=d)
    assert [f.cls.split(" ")[0] for f in found] == ["support"]
    other = tk.project(home, tk.OTHER, resource="vtrqz-host-02", goal="plorvex", total="912.30")
    kbpattern.support(card.id, project=other)
    version, found = solutions.release("cad-on-tcp", project=d)
    assert found == [] and version == "v1"


def test_the_release_screen_runs_the_export_check(home, monkeypatch):
    from tests import fixtures

    d, pkg, _ = _built(home, monkeypatch, "# CAD on TCP\n\nThe kit came from %s.\n" % fixtures.CUSTOMER_FORMS[1])
    _, found = solutions.screen("cad-on-tcp", project=d)
    assert [(f.line, f.cls) for f in found] == [(3, "export (name, secret or candidate)")]


def test_the_selftest_passes_and_fails_when_the_screen_is_blind(monkeypatch, capsys):
    assert solutions.selftest() == []
    assert cli.main(["solutions", "screen", "--selftest"]) == 0
    monkeypatch.setattr(kbpattern, "hashed", lambda value, key: "0" * 40)
    problems = solutions.selftest()
    assert "the planted fingerprint (resource) is not found" in problems
