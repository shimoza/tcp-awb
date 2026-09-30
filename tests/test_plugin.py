"""The plugin for the client in plugin/ and the marketplace at the repository root stay in step with the Workbench."""
from __future__ import annotations

import json
import re
from pathlib import Path

from awb import cli, hooks

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"


def test_the_manifest_and_the_marketplace_name_the_same_plugin():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"] == "awb"
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert not (ROOT / ".claude-plugin" / "plugin.json").exists(), "the marketplace root is no plugin"


def test_the_hooks_are_the_hooks_of_the_seal():
    data = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data == hooks.client_settings("awb hook")


def test_the_drafting_skill_is_the_copy_of_the_seal():
    seal = ROOT / "seal" / "work-claude" / "skills" / "drafting" / "SKILL.md"
    assert (PLUGIN / "skills" / "drafting" / "SKILL.md").read_bytes() == seal.read_bytes()


def test_every_skill_names_itself_and_every_command_it_uses_exists():
    known = set(cli.DELEGATED) | {"init", "intake", "register", "spawn", "projects", "close"}
    skills = sorted((PLUGIN / "skills").iterdir())
    assert len(skills) == 6
    for d in skills:
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---")[1]
        assert re.search(r"(?m)^name: %s$" % re.escape(d.name), front), d.name
        assert re.search(r"(?m)^description: .{20,300}$", front), d.name
        for cmd in re.findall(r"`awb ([a-z]+)", text):
            assert cmd in known, "%s names awb %s" % (d.name, cmd)


def test_the_migration_workers_have_the_tools_of_their_tier():
    tools = {}
    for f in sorted((PLUGIN / "agents").glob("*.md")):
        front = f.read_text(encoding="utf-8").split("---")[1]
        name = re.search(r"(?m)^name: (.+)$", front).group(1)
        assert name == f.stem
        tools[name] = {t.strip() for t in re.search(r"(?m)^tools: (.+)$", front).group(1).split(",")}
    assert tools["migration-worker-files"] == {"Read", "Grep", "Glob", "Write", "Edit"}
    assert tools["migration-worker-shell"] == {"Read", "Grep", "Glob", "Write", "Edit", "Bash"}


def test_every_phase_has_its_file_and_its_worker():
    from awb.tcp import migrate

    for phase in migrate.PHASES:
        assert migrate.phase_file(phase.name).is_file(), phase.name
        worker = migrate.WORKERS[phase.worker]
        if worker:
            assert (PLUGIN / "agents" / (worker.split(":")[1] + ".md")).is_file()

