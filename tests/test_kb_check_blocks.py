"""The check blocks staged under kb-check-blocks/ are taken by `awb kb how` as they stand.

Each file holds one section per entry, `## KB-XXXX` followed by the block lines as `awb kb how ID -` reads them.
Every block is parsed with the kb parser and written to an entry of a throw-away knowledge base through `kb.how`,
which runs `block_problems` (a tier, a control, a needs, a recipe that exists, a decision tier that names what it
needs) and the text checks of `kb add` (English, no name, no code, no price, no secret) against the fixture
register. The file of FK10 carries the 44 expired entries of its task and nothing else.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from awb import kb

REPO = Path(__file__).resolve().parent.parent
FOLDER = REPO / "kb-check-blocks"
FILES = sorted(FOLDER.glob("*.md"))
HEADING = re.compile(r"^## (%s)[ \t]*$" % kb.ID_RE.pattern, re.M)
STATEMENT = "ECS flavor s3.large.2 is offered in eu-de"
FK10_FILE = "FK10-2026-10.md"
FK10_ENTRIES = 44


def sections(text: str) -> list[tuple[str, str]]:
    """(id, block text) per `## KB-XXXX` heading, the block being everything up to the next heading."""
    parts = HEADING.split(text)
    return [(parts[i], parts[i + 1].strip("\n")) for i in range(1, len(parts), 2)]


def test_the_folder_holds_the_staged_files():
    assert FK10_FILE in [p.name for p in FILES]


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_every_block_of_the_file_is_taken_by_kb_how(home, path):
    entry = kb.add(STATEMENT, scope="tcp", tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide")
    found = sections(path.read_text(encoding="utf-8"))
    assert found, "%s has no section" % path.name
    seen: set[str] = set()
    for kb_id, block in found:
        assert kb_id not in seen, "%s: %s has two sections" % (path.name, kb_id)
        seen.add(kb_id)
        # the parser takes the lines as they stand, in any order, each key once
        check = kb.parse_block(block.split("\n"), kb_id)
        assert kb.block_problems(check) == [], "%s: %s" % (path.name, kb_id)
        # the whole of `awb kb how`: the block problems and the text checks of add, nothing written on a refusal
        try:
            got = kb.how(entry.id, block)
        except kb.Refused as refused:
            pytest.fail("%s: %s refused: %s" % (path.name, kb_id, "; ".join(refused.reasons)))
        assert dict(got.check) == dict(check)
        assert kb.verify([got.path]) == []
        tier, needs = got.block["tier"], got.block["needs"]
        # the rules of the task: a read or write tier needs nothing, a decision tier names what it needs
        assert (needs == "none") == (tier in ("read", "write")) or tier == "none", "%s: %s" % (path.name, kb_id)
        if "event" in got.block:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", got.block["event"])
    if path.name == FK10_FILE:
        assert len(seen) == FK10_ENTRIES
