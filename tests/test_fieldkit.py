"""The light plugin in fieldkit/ and its entry in the marketplace at the repository root."""
from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "fieldkit"
SCRIPT = KIT / "skills" / "tcp-price" / "price_lookup.py"


def front(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---")[1]


def kit_text() -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(KIT.rglob("*")) if f.is_file() and "__pycache__" not in f.parts)


def test_the_marketplace_lists_the_kit_next_to_awb_and_says_nothing_of_a_release():
    manifest = json.loads((KIT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entries = {e["name"]: e for e in market["plugins"]}
    assert set(entries) == {"awb", "tcp-fieldkit"}
    assert manifest["name"] == entries["tcp-fieldkit"]["name"] == "tcp-fieldkit"
    assert (ROOT / entries["tcp-fieldkit"]["source"]).resolve() == KIT
    assert entries["tcp-fieldkit"]["version"] == manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert not re.search(r"(?i)releas|publish|launch", entries["tcp-fieldkit"]["description"])
    assert not (KIT / ".claude-plugin" / "marketplace.json").exists()
    assert not (ROOT / "plugin-lite").exists()


def test_the_kit_has_two_skills_and_no_hook_agent_or_command():
    assert sorted(d.name for d in (KIT / "skills").iterdir()) == ["tcp-facts", "tcp-price"]
    for sub in ("hooks", "agents", "commands", "bin"):
        assert not (KIT / sub).exists(), sub
    manifest = json.loads((KIT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert not {"hooks", "mcpServers", "commands", "agents"} & set(manifest)
    assert sorted(f.suffix for f in KIT.rglob("*") if f.is_file() and "__pycache__" not in f.parts) == [".json", ".md", ".md", ".md", ".py"]


def test_every_skill_names_itself_and_uses_no_awb_command():
    for name in ("tcp-facts", "tcp-price"):
        path = KIT / "skills" / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        head = front(path)
        assert re.search(r"(?m)^name: %s$" % re.escape(name), head), name
        assert re.search(r"(?m)^description: .{20,300}$", head), name
        assert not re.search(r"`awb\b|\bawb [a-z]|\bcurl\b", text), name
    assert re.search(r"(?m)^allowed-tools: Read, Grep, Glob$", front(KIT / "skills" / "tcp-facts" / "SKILL.md"))
    assert re.search(r"(?m)^allowed-tools: Read, Bash\(python3:\*\)$", front(KIT / "skills" / "tcp-price" / "SKILL.md"))


def test_the_facts_skill_answers_from_the_files_only():
    text = (KIT / "skills" / "tcp-facts" / "SKILL.md").read_text(encoding="utf-8")
    assert "github.com/shimoza/tcp-facts" in text and "latest release" in text
    assert "Answer only from the files" in text and "checked date" in text and "not in the facts" in text


def test_the_price_skill_looks_up_live_and_reads_no_csv_or_price_folder():
    text = (KIT / "skills" / "tcp-price" / "SKILL.md").read_text(encoding="utf-8")
    assert "price_lookup.py" in text and "live" in text and "not found" in text
    assert "never stated from memory" in text and "date of the lookup" in text
    assert "region" in text and "flavor" in text and "unit" in text
    assert not re.search(r"(?i)\.csv|prices/", kit_text())


def test_the_price_script_uses_the_standard_library_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in getattr(n, "names", [None])}
    assert mods <= set(sys.stdlib_module_names), mods
    assert "awb" not in mods


def load_script():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("price_lookup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def serve(rows, count=None):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"response": {"code": "Success", "stats": {"count": count if count is not None else len(rows)},
                                            "result": {"ecs": rows}}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_port, seen


ROW = {"id": "r1", "productIdParameter": "ecs", "productName": "General Purpose", "opiFlavour": "s3.large.2",
       "osUnit": "Linux", "unit": "h", "region": "eu-de", "priceAmount": "0.5 EUR", "R12": "0.3 EUR"}


def test_the_price_script_prints_region_flavor_price_unit_and_the_date(capsys):
    srv, url, seen = serve([ROW, dict(ROW, id="r2", opiFlavour="s3.xlarge.2", priceAmount="1.0 EUR")])
    try:
        code = load_script().main(["ecs", "--region", "eu-de", "--flavor", "s3.large.2", "--api", url])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert code == 0 and "sn=ecs" in seen[0] and "rn=eu-de" in seen[0]
    for want in ("region: eu-de", "flavor: s3.large.2", "unit: h", "pay as you go: 0.5 EUR", "looked up: "):
        assert want in out
    assert "s3.xlarge.2" not in out


def test_the_price_script_says_not_found_and_gives_no_price(capsys):
    srv, url, _ = serve([ROW])
    try:
        code = load_script().main(["ecs", "--region", "eu-nl", "--flavor", "s9.huge.9", "--api", url])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert code == 1 and out.startswith("not found") and "EUR" not in out


def test_the_price_script_gives_no_price_when_the_api_does_not_answer(capsys):
    code = load_script().main(["ecs", "--api", "http://127.0.0.1:9/"])
    cap = capsys.readouterr()
    assert code == 2 and "no price can be given" in cap.err and "EUR" not in cap.out


def test_the_readme_installs_the_kit_and_disclaims_the_operator():
    text = (KIT / "README.md").read_text(encoding="utf-8")
    assert "claude plugin marketplace add shimoza/tcp-awb" in text
    assert "claude plugin install tcp-fieldkit@tcp-awb" in text
    assert ("An independent, community-made toolkit for T Cloud Public (TCP). Not a product of the operator; "
            "it speaks for T-Systems or Deutsche Telekom in no way.") in text
    assert "awb kb import facts.jsonl" in text


def test_nothing_of_the_kit_mentions_screenshots_a_console_mirror_or_a_walk():
    assert not re.search(r"(?i)screenshot|console mirror|\bwalk\b|lite", kit_text())


def test_the_import_does_not_expect_a_prices_folder(tmp_path):
    src = (ROOT / "awb" / "kbimport.py").read_text(encoding="utf-8")
    assert "prices" not in src
