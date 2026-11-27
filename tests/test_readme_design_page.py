"""B-111: README.md is a short design page for a reader of the public repository; the operator manual lives in docs/.
The README stays under 300 lines, carries no internal task code and no project code, keeps the two public statements,
a quick start and "What this proves", and every relative link in it resolves. One hook count everywhere, taken from
the hooks the code installs, and one version for the package and the plugin."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import awb
from awb import codes, hooks
from awb.tcp import dataset

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12}
COUNT_RE = re.compile(r"(?i)\b(%s|[0-9]+) (hooks|checkpoints)\b" % "|".join(NUMBERS))
LINK_RE = re.compile(r'\]\(([^)\s]+)\)|href="([^"]+)"|src="([^"]+)"')
COUNTED = ("README.md", "INSTALL.md", "INTERFACES.md", "plugin/README.md", "seal/README.md",
           "plugin/.claude-plugin/plugin.json")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    parts = re.split(r"(?m)^## ", text)
    found = [p for p in parts if p.startswith(heading + "\n")]
    assert found, "no section %r" % heading
    return found[0]


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation other than - and _ dropped, spaces to -."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    out, fence = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            m = re.match(r"#{1,6} (.+)", line)
            if m:
                out.add(_slug(m.group(1)))
    return out


def _relative_links(path: Path) -> list[str]:
    """The relative targets of the links of a page; a link shown as code (a fenced block, `...`) is no link."""
    text = re.sub(r"(?ms)^\s*```.*?^\s*```", "", path.read_text(encoding="utf-8"))
    text = re.sub(r"`[^`\n]*`", "", text)
    out = []
    for m in LINK_RE.finditer(text):
        target = next(g for g in m.groups() if g)
        if "://" not in target and not target.startswith("mailto:"):
            out.append(target)
    return out


def _code_count() -> int:
    return sum(len(entry["hooks"]) for entries in hooks.client_settings("awb hook")["hooks"].values()
               for entry in entries)


def test_the_readme_is_under_300_lines():
    assert len(_readme().splitlines()) < 300


def test_the_readme_holds_no_task_code_and_no_project_code():
    text = _readme()
    for rx in (r"\bB-[0-9]{3}\b", r"\bQ-[0-9]{4}\b", r"\bDMZ [0-9]+\b", r"\b(?:tcp|hcs)-[a-z0-9]{4}\b",
               codes.PROJECT_RE.pattern, codes.TENANT_RE.pattern):
        found = re.findall(rx, text)
        assert not found, (rx, found)


def test_the_readme_keeps_the_two_public_statements():
    assert dataset.public_statements_missing(_readme()) == []


def test_the_readme_has_a_quick_start_without_the_sealed_host():
    quick = _section(_readme(), "Quick start")
    for command in ("pip install -e .", "awb kb import", "awb kb find", "awb calc", "awb service check"):
        assert command in quick, command
    for sealed in ("sudo", "awb unlock", "seal/setup.sh"):
        assert sealed not in quick, sealed


def test_what_this_proves_names_a_command_for_every_number():
    rows = [ln for ln in _section(_readme(), "What this proves").splitlines()
            if ln.startswith("|") and not ln.startswith("|---") and not ln.startswith("| what |")]
    assert len(rows) >= 5
    text = " ".join(rows)
    for subject in ("tests", "commits", "facts", "tested live", "lab recipes"):
        assert subject in text, subject
    for row in rows:
        check = row.rstrip(" |").rsplit(" | ", 1)[1]
        assert re.search(r"`(?:jq|gh|awb) ", check), row


def test_every_relative_link_of_the_readme_and_its_docs_pages_resolves():
    pages = [README] + [ROOT / t for t in _relative_links(README) if t.startswith("docs/") and t.endswith(".md")]
    assert len(pages) > 5
    for page in pages:
        for target in _relative_links(page):
            where, _, anchor = target.partition("#")
            dest = (page.parent / where).resolve() if where else page
            assert dest.exists(), "%s: %s" % (page.relative_to(ROOT), target)
            if anchor and dest.suffix == ".md":
                assert anchor in _anchors(dest), "%s: %s" % (page.relative_to(ROOT), target)


def test_one_hook_count_in_the_readme_and_the_docs_taken_from_the_code():
    want = _code_count()
    assert want == 7
    files = [ROOT / f for f in COUNTED] + sorted((ROOT / "docs").glob("*.md"))
    seen = 0
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for word, _ in COUNT_RE.findall(line):
                got = int(word) if word.isdigit() else NUMBERS[word.lower()]
                assert got == want, "%s:%d says %s" % (path.relative_to(ROOT), n, line.strip()[:120])
                seen += 1
    assert seen >= 5
    assert COUNT_RE.search(_readme())


def test_the_package_and_the_plugin_carry_one_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    listed = [p["version"] for p in market["plugins"] if p["name"] == "awb"]
    assert project == plugin == awb.__version__ == "0.3.0"
    assert listed == [plugin]
