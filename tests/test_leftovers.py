"""The leftovers of 2026-09-25: a check before every push, a key pair written without a variable name, a size limit
for what a session loads at start, 101 planted intake cases, and the fixes the run of that day found on the way
(a knowledge base that holds one scope, a folder name on the blocklist, flavor names
and masks in the claim list, an IBAN in lower case and a phone number with 00).

Every value is invented. The key pair is built from pieces so that this file does not carry one.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import awb
from awb import gate, hooks, intake, kb, patterns, planted, review
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
AK = "QW7X" + "ZV2P" + "RB4F" + "IR9M" + "A1C3"            # 20 capitals and digits
SK = "k8Hs2" + "Lq9Wz" + "4Rt7Y" + "u1Pn6" + "Xc3Vb" + "5Mj0D" + "g2Fh8" + "Ns4Qa"   # 40 letters and digits
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid")


# --------------------------------------------------------------------------- a key pair without a variable name


def test_a_bare_key_pair_is_a_secret_on_one_line_and_on_two():
    assert len(AK) == 20 and len(SK) == 40
    one = "the keys: %s %s\n" % (AK, SK)
    two = "%s\n%s\n" % (AK, SK)
    for text in (one, two):
        assert gate._detect_secret(text), text.replace(SK, "<sk>")
    # an id alone, a commit id and a key id far from anything are not secrets
    for text in ("the key id %s is listed\n" % AK,
                 "commit %s and %s\n" % (AK, "9f" * 20),
                 "%s\n\n\n%s\n" % (AK, SK)):
        assert not gate._detect_secret(text)


def test_the_gate_finds_the_bare_pair_in_a_file(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("first line\n%s %s\n" % (AK, SK), encoding="utf-8")
    assert [(x.cls, x.line) for x in gate.scan_files([f], None)] == [("secret", 2)]


# --------------------------------------------------------------------------- the check before every push


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=ENV, input=stdin)
    assert r.returncode == 0, r.stderr
    return r.stdout


def repo_with_history(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose second commit adds a secret and whose third removes it again."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("clean text\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "one")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.txt").write_text("keys %s %s\n" % (AK, SK), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "two")
    (repo / "b.txt").write_text("gone again\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "three")
    head = git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def test_a_push_is_scanned_commit_by_commit(tmp_path):
    repo, base, head = repo_with_history(tmp_path)
    zero = "0" * 40
    lines = "refs/heads/main %s refs/heads/main %s\n" % (head, base)
    updates = gate.parse_push_lines(lines)
    assert updates == [("refs/heads/main", head, "refs/heads/main", base)]
    assert len(gate.pushed_commits(repo, updates)) == 2
    found = gate._scan_pushed(repo, updates, None)
    assert [(f.cls, f.file.split(":", 1)[1], f.line) for f in found] == [("secret", "b.txt", 1)]
    # a new branch carries every commit no remote has; a deletion carries nothing
    assert len(gate.pushed_commits(repo, gate.parse_push_lines("refs/heads/x %s refs/heads/x %s\n" % (head, zero)))) == 3
    assert gate.pushed_commits(repo, gate.parse_push_lines("(delete) %s refs/heads/x %s\n" % (zero, head))) == []
    # the command line reads the lines from standard input
    r = subprocess.run([sys.executable, "-m", "awb.gate", "--pushed", "--no-register", "--repo", str(repo)],
                       input=lines, capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 1 and "secret" in r.stdout and SK not in r.stdout
    clean = "refs/heads/main %s refs/heads/main %s\n" % (base, zero)
    r = subprocess.run([sys.executable, "-m", "awb.gate", "--pushed", "--no-register", "--repo", str(repo)],
                       input=clean, capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_writes_a_push_hook_next_to_the_commit_hook(tmp_path):
    repo, _, _ = repo_with_history(tmp_path)
    hook = gate.install_hook(repo)
    push = hook.parent / "pre-push"
    assert hook.name == "pre-commit" and push.is_file() and os.access(push, os.X_OK)
    text = push.read_text(encoding="utf-8")
    assert gate.HOOK_MARK in text and "gate --pushed" in text and "gate --selftest" in text
    assert subprocess.run(["sh", "-n", str(push)]).returncode == 0
    push.write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="pre-push hook of another tool"):
        gate.install_hook(repo)


def test_the_workbench_has_its_own_push_hook():
    hook = ROOT / "hooks" / "pre-push"
    text = hook.read_text(encoding="utf-8")
    assert os.access(hook, os.X_OK) and "awb.gate --pushed" in text and "--selftest" in text
    assert subprocess.run(["sh", "-n", str(hook)]).returncode == 0


# --------------------------------------------------------------------------- what a session loads at start


def test_the_start_context_has_a_size_limit(home, register_path, monkeypatch, capsys):
    from awb import projects
    pr = projects.spawn(home, "lab", "size limit of the start", None, register_path)
    folder = Path(pr.path)
    (folder / "OPEN.md").write_text("# Open\n\n" + "- an open item of the invented test\n" * 900, encoding="utf-8")
    (folder / "STATE.md").write_text("# State\n\n" + ("a long state line of the invented test " * 4 + "\n") * 58,
                                     encoding="utf-8")
    (folder / "SCOPE.md").write_text((folder / "SCOPE.md").read_text(encoding="utf-8")
                                     + "\n" + "a long note line of the invented test\n" * 400, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(folder), "session_id": "s-1"})))
    assert hooks.main(["session-start"]) == 0
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    body, _, checks = text.partition("## Checks")
    assert len(body.rstrip("\n")) <= hooks.START_CHARS
    for name in ("SCOPE.md", "STATE.md", "OPEN.md"):
        assert "%s is longer than %d characters" % (name, hooks.SECTION_CHARS) in checks
    assert "cut at %d characters" % hooks.START_CHARS in checks


# --------------------------------------------------------------------------- the planted intake cases


def test_the_planted_set_has_over_ninety_cases_and_the_engine_passes_them():
    labels = [c[0] for c in planted.CASES]
    assert len(labels) == len(set(labels)) >= 90
    assert {c[2] for c in planted.CASES} >= {"name", "mail", "iban", "phone", "ip", "mac", "bic", "vat", "hrb", "url"}
    assert intake.selftest() == []


def test_an_iban_in_lower_case_and_a_phone_number_with_00_are_found():
    assert [s.cls for s in patterns.find_structured("iban de89 3704 0044 0532 0130 00 please")] == ["iban"]
    assert [s.cls for s in patterns.find_structured("call 0049 30 1234567 today")] == ["phone"]
    # the checksum still decides: a wrong IBAN in lower case is not one
    assert "iban" not in [s.cls for s in patterns.find_structured("iban de89 3704 0044 0532 0130 01 please")]


# --------------------------------------------------------------------------- fixes found on the way


def test_a_knowledge_base_that_holds_one_scope_refuses_the_other(home, register_path):
    (home.kb).mkdir(parents=True, exist_ok=True)
    (home.kb / kb.SCOPE_FILE).write_text("tcp\n", encoding="utf-8")
    assert kb.folder_scope(home) == "tcp"
    with pytest.raises(kb.KBError, match="holds tcp facts only"):
        kb.add("The invented stack runs two clusters in the test region.", scope="hcs", tags=["other"],
               grade="docs", cls="stable", source="an invented source", where=home, register_path=register_path)
    e = kb.add("The invented stack runs two clusters in the test region.", scope="tcp", tags=["other"],
               grade="docs", cls="stable", source="an invented source", where=home, register_path=register_path)
    assert e.scope == "tcp"


def test_a_blocked_folder_name_is_found_and_a_longer_name_is_not():
    assert gate._detect_blocklist("see the folder %s" % ("legacy" + "-kb"))
    assert not gate._detect_blocklist("the new base awb-kb-legacy")


def test_the_claim_list_takes_any_flavor_name_and_a_mask():
    kinds = {t: [(c.kind, c.risk) for c in review.find_claims(t)] for t in (
        "I propose two nodes of the flavor s9.huge.8 in eu-de.",
        "The flavor c7t.28xlarge.4.physical is offered in eu-de.",
        "A subnet accepts masks down to /29 today.",
        "We use v1.28.3 of the provider.")}
    assert list(kinds.values()) == [[("identifier", "high")], [("identifier", "high")], [("number", "medium")],
                                    [("version", "medium")]]
    assert fx.CUSTOMER_CODE not in str(kinds)


def test_only_in_a_region_is_a_negative_claim():
    kinds = [c.kind for c in review.find_claims("The flavor is offered only in eu-nl.")]
    assert kinds == ["negative"]
    assert review.find_claims("We use the cluster only for the test.") == []


def test_reserved_ranges_are_not_customer_addresses():
    for text in ("the system route 198.19.128.0/20 is on every route table",
                 "the range 224.0.0.0/3 is reserved"):
        assert [s.cls for s in patterns.find_structured(text)] == [], text
    assert [s.cls for s in patterns.find_structured("the server 93.184.216.34 answers")] == ["ip"]
