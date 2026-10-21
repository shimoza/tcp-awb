"""The light plugin in plugin-lite/ and its entry in the marketplace at the repository root."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LITE = ROOT / "plugin-lite"


def front(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---")[1]


def test_the_marketplace_lists_the_lite_plugin_next_to_awb():
    manifest = json.loads((LITE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entries = {e["name"]: e for e in market["plugins"]}
    assert set(entries) == {"awb", "tcp-facts-lite"}
    assert manifest["name"] == entries["tcp-facts-lite"]["name"] == "tcp-facts-lite"
    assert (ROOT / entries["tcp-facts-lite"]["source"]).resolve() == LITE
    assert entries["tcp-facts-lite"]["version"] == manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert len(manifest["description"]) > 20
    assert not (LITE / ".claude-plugin" / "marketplace.json").exists()


def test_the_lite_plugin_has_two_skills_and_nothing_that_runs():
    names = sorted(d.name for d in (LITE / "skills").iterdir())
    assert names == ["tcp-facts", "tcp-price"]
    for sub in ("hooks", "agents", "commands", "bin", "scripts"):
        assert not (LITE / sub).exists(), sub
    manifest = json.loads((LITE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert not {"hooks", "mcpServers", "commands", "agents"} & set(manifest)
    for f in LITE.rglob("*"):
        assert not f.is_file() or f.suffix in (".md", ".json"), f.name


def test_every_skill_names_itself_reads_only_and_uses_no_awb_command():
    for name in ("tcp-facts", "tcp-price"):
        path = LITE / "skills" / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        head = front(path)
        assert re.search(r"(?m)^name: %s$" % re.escape(name), head), name
        assert re.search(r"(?m)^description: .{20,300}$", head), name
        assert re.search(r"(?m)^allowed-tools: Read, Grep, Glob$", head), name
        assert not re.search(r"(?i)`awb\b|\bawb [a-z]|\bcurl\b|\bBash\b", text), name


def test_the_skills_carry_the_rules_of_the_task():
    facts = (LITE / "skills" / "tcp-facts" / "SKILL.md").read_text(encoding="utf-8")
    price = (LITE / "skills" / "tcp-price" / "SKILL.md").read_text(encoding="utf-8")
    for text in (facts, price):
        assert "github.com/shimoza/tcp-facts" in text and "latest release" in text
        assert "not in the facts" in text
    assert "Answer only from the files" in facts and "id" in facts and "checked date" in facts
    assert "never stated from memory" in price and "prices/<region>.csv" in price
    assert "region" in price and "fetched date" in price


def test_the_readme_gives_the_two_install_commands():
    text = (LITE / "README.md").read_text(encoding="utf-8")
    assert "claude plugin marketplace add shimoza/tcp-awb" in text
    assert "claude plugin install tcp-facts-lite@tcp-awb" in text
    assert "awb kb import facts.jsonl" in text
