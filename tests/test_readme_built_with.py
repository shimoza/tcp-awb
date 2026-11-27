"""B-113: the README says in the open how the code is made; CLAUDE.md states a neutral commit rule; the commit-msg
hook still refuses a co-author line that names an assistant."""
from __future__ import annotations

import re
from pathlib import Path

from awb import gate

ROOT = Path(__file__).resolve().parent.parent


# the heading in lower case: a capitalised run in a test must come from the fixtures (tests/test_self_clean.py)
HEADING = "built with claude code"


def _section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    found = [p for p in re.split(r"(?m)^## ", text) if p.lower().startswith(HEADING)]
    assert found, "README.md has no section " + HEADING
    return found[0]


def test_the_readme_has_the_built_with_section_and_names_the_checks():
    section = _section()
    assert len(section.strip().splitlines()) < 25
    low = section.lower()
    assert "claude code" in low and "codex" in low
    assert "build queue" in low and "without a model" in low
    assert "tests" in low and "gate" in low and "paths" in low
    assert "responsible" in low


def test_the_rules_file_has_the_neutral_commit_rule_and_no_attribution_wording():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "co-author" not in low and "generated with" not in low
    assert ("commits carry the owner as author; the readme section %s says how the code is made." % HEADING
            ) in " ".join(low.split())


def test_the_commit_msg_hook_still_refuses_a_co_author_line_naming_an_assistant(tmp_path, register_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("A change\n\nThe body.\n\nCo-Authored-By: Clau" + "de <noreply@anthropic.com>\n", encoding="utf-8")
    assert gate.main(["--message", str(msg), "--inside"]) == 1
    assert "--message" in (ROOT / "hooks" / "commit-msg").read_text(encoding="utf-8")
