"""The check blocks of kb-check-blocks/FK10b-2026-10.md (task fk10b-check-blocks): each section parses with the
knowledge base parser, stands under its rules and passes verify once it is written to an entry with `kb.how`."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from awb import kb

FILE = Path(__file__).resolve().parent.parent / "kb-check-blocks" / "FK10b-2026-10.md"
IDS = ("KB-MQND", "KB-SRKV", "KB-2NF7", "KB-6SR2", "KB-FBFG", "KB-HVJS", "KB-J2C5", "KB-MC6B", "KB-TMQ5", "KB-YHSH")
STATEMENT = "ECS flavor s3.large.2 is offered in eu-de"


def sections() -> dict[str, str]:
    if not FILE.is_file():
        pytest.skip("the block file is applied on the host and is not part of main")
    parts = re.split(r"^## (KB-[A-Z0-9]{4})\n", FILE.read_text(encoding="utf-8"), flags=re.M)
    return {parts[i]: parts[i + 1].strip("\n") + "\n" for i in range(1, len(parts), 2)}


def test_the_file_has_one_section_per_entry():
    assert sorted(sections()) == sorted(IDS)


@pytest.mark.parametrize("kb_id", IDS)
def test_a_block_parses_and_stands_under_the_rules(kb_id):
    check = kb.parse_block(sections()[kb_id].splitlines(), kb_id)
    assert kb.block_problems(check) == []
    assert "control" in dict(check)


@pytest.mark.parametrize("kb_id", IDS)
def test_a_block_is_taken_by_how_and_the_entry_verifies(home, kb_id):
    block = sections()[kb_id]
    e = kb.add(STATEMENT, scope="tcp", tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide")
    got = kb.how(e.id, block, register_path=home.register)
    assert dict(got.check) == dict(kb.parse_block(block.splitlines(), kb_id))
    assert kb.verify([got.path]) == []
