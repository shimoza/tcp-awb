"""B-110: the README of the dataset TCP Facts shows a reader what it is on its first screen. Built from a small
invented knowledge base: no line that is only a fact id and no table of ids, the three ways and "What this proves"
in the first 40 lines, only files of the export or named release assets, a contract fact of the current revision of
the service description is not due for its date, no source note says "old setup", the MCP server carries an
Apache-2.0 header and the export a LICENSE-CODE."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from awb import kb, offered
from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, live, new  # noqa: F401  (fixtures)

OLD = TODAY - timedelta(days=400)                    # past the check period of every class
S_CURRENT = "ECS offers the flavor family s3 in eu-de per the service description"
S_OLDER = "OBS offers the storage class cold in eu-de per the service description"
S_DOCS_OLD = "ECS flavor s2.medium.1 boots from an EVS disk of at least 40 GB"
S_CERT = "TCP lists an ISO 27001 certificate for the data centres of eu-de on its public trust page"
RELEASE_ASSETS = ("MANIFEST.json", "HOW-TO.pdf", "tcp-facts.mcpb", "tcp-facts-<date>.mcpb")


@pytest.fixture
def small(home, facts):
    rev = dataset._catalog().revision
    current = new(S_CURRENT, grade="contract", cls="availability", tags=["ecs"], checked=OLD,
                  source="service description revision %s, section 3.1.1" % offered._dmy(rev))
    older = new(S_OLDER, grade="contract", cls="availability", tags=["obs"], checked=OLD,
                source="service description revision 01.03.2025, section 3.4.1")
    docs = new(S_DOCS_OLD, grade="docs", cls="api", tags=["ecs"], checked=OLD,
               source="docs mirror, user guide (old setup, imported 2026-10-20)")
    new(S_CERT, grade="docs", cls="stable", tags=["tender"], source="docs mirror, trust page")
    built = dataset.build(home, out=home.shared / "ds-b110", today=TODAY)
    return built, current, older, docs


def _readme(built) -> str:
    return (built.folder / "README.md").read_text(encoding="utf-8")


def test_the_readme_has_no_line_that_is_only_a_fact_id_and_no_table_of_ids(small):
    built, *_ = small
    lines = _readme(built).splitlines()
    assert not [x for x in lines if re.fullmatch(r"[\s|*`-]*%s[\s|*`.-]*" % kb.ID_RE.pattern.strip("^$"), x)]
    assert not [x for x in lines if x.lstrip().startswith("|") and re.search(kb.ID_RE.pattern.strip("^$"), x)]
    assert "Facts with a recheck due" not in _readme(built)


def test_the_first_40_lines_hold_the_three_ways_what_this_proves_the_licence_and_the_date(small):
    built, *_ = small
    text = _readme(built)
    head = "\n".join(text.splitlines()[:40])
    assert dataset.public_statements_missing(text) == []
    ways = [head.index(x) for x in ("1. Attach facts.md", "2. One click", "3. %s" % " ".join(("Claude", "Code")))]
    assert ways == sorted(ways) and dataset.ONE_LINER in head and dataset.MCPB_LATEST_URL in head
    assert dataset.CLAUDE_CODE_ADD in head
    assert "## " + dataset.PROVES_HEADING in head and ways[-1] < head.index("## " + dataset.PROVES_HEADING)
    proves = head[head.index("## " + dataset.PROVES_HEADING):]
    for command in (dataset.JQ_FACTS, dataset.JQ_LIVE, dataset.JQ_SERVICES, dataset.GH_RELEASES):
        assert "`%s`" % command in proves, command
    assert "**%d facts**" % built.facts in proves and "**%d facts tested live**" % built.grades["live"] in proves
    assert "**%d services**" % built.services in proves
    assert proves.index("Example fact") < proves.index("Freshness:") < proves.index("Released %s." % built.day)
    assert dataset.CODE_LICENCE_LINE in head
    assert "PROMPT.md" not in text


def test_every_file_the_readme_names_is_in_the_export_or_a_named_release_asset(small):
    built, *_ = small
    text = _readme(built)
    links = re.findall(r"\]\(([^)]+)\)", text) + re.findall(r'href="([^"]+)"', text)
    for target in links:
        if not re.match(r"https?://|#", target):
            assert (built.folder / target).exists(), target
    plain = re.sub(r"(?:https?://|www\.|github\.com/)\S+", "", text)
    named = set(re.findall(r"(?<![\w.<>-])((?:topics/)?[\w<>-]+\.(?:md|jsonl|json|pdf|py|mcpb|zip))\b", plain))
    named |= set(re.findall(r"\b(LICENSE(?:-CODE)?)\b", plain))
    clients = {"claude_desktop_config.json", ".vscode/mcp.json", "mcp.json"}
    for name in sorted(named - clients):
        if name in RELEASE_ASSETS:
            assert re.search(r"(?i)release assets?\b[^\n]*%s|%s[^\n]*release asset" % (re.escape(name), re.escape(name)),
                             text), name
        elif name == "LICENSE":
            assert "LICENSE at github.com/%s" % dataset.GITHUB_REPO in text     # the repository's own, hand written
        elif name != "topics/<topic>.md":
            assert (built.folder / name).is_file(), name
    assert set(dataset.GITHUB_FILES) >= {"LICENSE-CODE", dataset.MCP_FILE}


def test_a_contract_fact_of_the_current_revision_is_not_counted_as_recheck_due(small):
    built, current, older, docs = small
    rows = {r["id"]: r for r in dataset.read_facts(built.folder / "facts.jsonl")}
    assert rows[current.id]["recheck_due"] is False and rows[current.id]["level"] == "V2"
    assert rows[older.id]["recheck_due"] is True and rows[docs.id]["recheck_due"] is True
    manifest = json.loads((built.folder / "MANIFEST.json").read_text())
    assert manifest["recheck_due"] == 2
    text = _readme(built)
    assert "Freshness: 2 of the %d facts wait for a recheck" % built.facts in text and "`%s`" % dataset.JQ_DUE in text
    assert "rechecked weekly" not in text and "weekly recheck" not in text and "re-checked every" not in text
    assert "%d of the %d facts have a weekly automated check" % (manifest["levels"]["V4"], built.facts) in text


def test_a_fact_is_of_the_current_revision_by_the_revision_it_cites_or_else_by_its_check_date():
    rev = "2026-11-18"
    fact = {"grade": "contract", "checked": "2026-11-20", "source": "service description section 3.1.1"}
    assert dataset.of_revision(fact, rev)
    assert not dataset.of_revision(dict(fact, checked="2026-11-01"), rev)
    assert dataset.of_revision(dict(fact, checked="2026-01-01", source="service description revision 18.11.2026"), rev)
    assert not dataset.of_revision(dict(fact, source="service description revision 01.03.2025"), rev)
    assert not dataset.of_revision(dict(fact, grade="docs"), rev)


def test_no_source_note_says_old_setup(small):
    built, _, _, docs = small
    for path in built.folder.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".jsonl", ".json", ".py"):
            assert "old setup" not in path.read_text(encoding="utf-8").lower(), path.name
    row = {r["id"]: r for r in dataset.read_facts(built.folder / "facts.jsonl")}[docs.id]
    assert row["source"] == "docs mirror, user guide (earlier private knowledge base, imported 2026-10-20)"


def test_the_mcp_file_starts_with_an_apache_header_and_the_export_carries_license_code(small):
    built, *_ = small
    head = (built.folder / dataset.MCP_FILE).read_text(encoding="utf-8").split('"""')[0].splitlines()
    assert head[0] == "#!/usr/bin/env python3" and head[1] == "# SPDX-License-Identifier: Apache-2.0"
    assert all(x.startswith("#") or not x for x in head)
    apache = " ".join(("Apache", "License"))           # words apart: the run check of the tests
    assert any("Licensed under the %s, Version 2.0" % apache in x for x in head)
    code = (built.folder / "LICENSE-CODE").read_text(encoding="utf-8")
    assert code.lstrip().startswith(apache) and "Version 2.0, January 2004" in code
    manifest = json.loads((built.folder / "MANIFEST.json").read_text())
    assert "LICENSE-CODE" in manifest["files"] and manifest["licence"] == "CC BY 4.0"
    assert manifest["code_licence"] == {dataset.MCP_FILE: "Apache-2.0"}


def test_the_readme_says_which_facts_name_a_certificate(small):
    built, *_ = small
    text = _readme(built)
    assert "name a certificate or an attestation as a public source lists it, 1 of them" in text and "topics/tender.md" in text
    assert (built.folder / "topics" / "tender.md").is_file()
