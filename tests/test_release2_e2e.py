"""Release 2 end to end: the owner's side and the work side of the seal in one throw-away Workbench.

Temporary places and a vault daemon in threads of this process, its sockets in a short folder under /tmp that is
removed afterwards. The owner runs init, registers a fixture customer, encrypts the vault (the daemon is unlocked
with it) and takes a docx in: the register then goes through the admin socket. The work side cannot see the vault
at all (its register path lies in a folder that does not exist), so every name check it makes goes through the
check socket: it checks the intake output, spawns a project whose client settings carry the hooks, adds a
knowledge entry, adds to the ledger and reports by customer, checks a clean deliverable for writing and reviews it
up to a valid record. Then the owner locks the vault: `awb check` exits 2 and the prompt hook warns without
blocking.

Names come from tests/fixtures.py only. No command output may carry a fixture form or the passphrase.
"""
from __future__ import annotations

import csv
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import cli, codes, config, hooks, projects, register, review, vault
from tests import fixtures as fx
from tests.conftest import SEALED_FOR_ANOTHER_USER

PASS = "fixture passphrase of the end to end test 3"
FULL = fx.CUSTOMER_FORMS[0]
PERSON = fx.PERSON_FORMS[0]


class Console:
    """Runs `awb` commands in this process and keeps everything they printed for the final check."""

    def __init__(self, capsys):
        self.capsys = capsys
        self.seen: list[str] = []

    def run(self, *argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            code = cli.main([str(a) for a in argv])
        finally:
            sys.stdin = old
        out, err = self.capsys.readouterr()
        self.seen.append(out + err)
        return code, out, err

    def hook(self, name: str, payload: dict) -> tuple[int, str, str]:
        return self.run("hook", name, stdin=json.dumps(payload))

    def assert_clean(self) -> None:
        text = "\n".join(self.seen)
        fx.assert_no_fixture_name(text, "command output")
        assert PASS not in text, "the passphrase was printed"


def _vault_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("awb-vault")]


@pytest.fixture
def places(tmp_path, monkeypatch):
    """Every place of a Workbench under tmp_path, the sockets under a short folder in /tmp."""
    short = Path(tempfile.mkdtemp(prefix="awb", dir="/tmp"))
    owner_vault = tmp_path / "owner" / "tcp-vault"
    env = {
        "AWB_SHARED": tmp_path / "work" / "tcp-shared",
        "AWB_VAULT": owner_vault,
        "AWB_PROJECTS": tmp_path / "work",
        "AWB_KB": tmp_path / "work" / "tcp-kb",
        "AWB_CHECK_SOCKET": short / "run" / "check.sock",
        "AWB_ADMIN_SOCKET": short / "admin.sock",
        "AWB_CONF": tmp_path / "no-host-conf",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(vault, "S2K_COUNT", 65536)   # the lowest count gpg takes without an agent: fast tests
    try:
        yield SimpleNamespace(tmp=tmp_path, short=short, owner_vault=owner_vault,
                              work_vault=tmp_path / "owner-home-is-closed" / "tcp-vault")
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert not short.exists()


@pytest.fixture
def daemon(places):
    d = vault.Daemon(config.paths())
    d.start()
    try:
        yield d
    finally:
        d.stop()
        assert not _vault_threads(), "a thread of the vault daemon is still running"
        assert not d.admin_path.exists() and not d.check_path.exists()


def owner_side(monkeypatch, places) -> config.Paths:
    monkeypatch.setenv("AWB_VAULT", str(places.owner_vault))
    return config.paths()


def work_side(monkeypatch, places) -> config.Paths:
    """The work user's view: the vault lies behind a home folder it cannot enter. A register path in a folder
    that does not exist is what the check code sees there: not readable, so the check socket answers."""
    monkeypatch.setenv("AWB_VAULT", str(places.work_vault))
    p = config.paths()
    assert not p.register.parent.exists()
    return p


def _docx(paragraphs: list[str]) -> bytes:
    import docx

    d = docx.Document()
    for text in paragraphs:
        d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _check_log_lines(p: config.Paths) -> int:
    return sum(len(f.read_text(encoding="utf-8").splitlines()) - 1 for f in p.check_log.glob("checks-*.tsv"))


def _set_evidence(claims: Path, evidence: str) -> int:
    """Fill evidence, grade and verdict of every high-risk row of claims.tsv, the way he does it by hand."""
    rows = list(csv.reader(claims.read_text(encoding="utf-8").splitlines(), delimiter="\t"))
    head = rows[0]
    risk, ev, grade, verdict = (head.index(k) for k in ("risk", "evidence", "grade", "verdict"))
    n = 0
    for row in rows[1:]:
        if row[risk] == "high":
            row[ev], row[grade], row[verdict] = evidence, "docs", "supported"
            n += 1
    claims.write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    return n


def test_release2_end_to_end(places, daemon, monkeypatch, capsys):
    con = Console(capsys)
    cust, pers = fx.CUSTOMER_CODE, fx.PERSON_CODE

    # ------------------------------------------------------------------ the owner: init, register, encrypt
    p = owner_side(monkeypatch, places)
    assert con.run("init")[0] == 0
    for form in fx.CUSTOMER_FORMS:
        assert con.run("register", "add", cust, "CUST", "-", stdin=form + "\n")[0] == 0
    assert con.run("register", "add", pers, "PERS", "-", stdin=PERSON + "\n")[0] == 0
    assert vault.ping(p.check_socket) == "plain"

    code, out, _ = con.run("vault", "encrypt", "--stdin", stdin="%s\n%s\n" % (PASS, PASS))
    assert code == 0 and "vault unlocked" in out
    assert not p.register.exists() and p.register_encrypted.is_file()
    assert vault.ping(p.check_socket) == "unlocked"
    code, out, _ = con.run("vault", "status")
    assert code == 0 and "state: unlocked" in out and "codes: 2" in out
    code, out, _ = con.run("register", "list")   # the owner's register commands go through the admin socket
    assert code == 0 and cust in out and pers in out

    # ------------------------------------------------------------------ the owner: intake of a docx
    original = _docx(["das angebot fuer %s geht an %s." % (FULL, PERSON),
                      "die landing zone bekommt zwei app cluster."])
    inbox_file = p.inbox / "angebot.docx"
    inbox_file.write_bytes(original)
    code, out, _ = con.run("intake", "--customer", cust, inbox_file)
    assert code == 0, out
    assert not inbox_file.exists()
    (output,) = sorted((p.outbox / cust).glob("F-*.md"))
    text = output.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(text, "intake output")
    assert cust in text and pers in text
    (sealed,) = (p.originals / cust).iterdir()
    assert sealed.name == output.stem + ".docx.gpg"
    assert all(f.name.endswith(".gpg") for f in p.private_reports.rglob("*") if f.is_file())
    assert vault.admin_call("open_file", p.admin_sock, path=str(sealed))["data"]

    # ------------------------------------------------------------------ the work side: check over the socket
    w = work_side(monkeypatch, places)
    before = _check_log_lines(p)
    code, out, err = con.run("check", "--register", w.register, output)
    assert (code, out) == (0, ""), err
    assert _check_log_lines(p) > before, "the check went through the check socket of the daemon"
    planted = places.tmp / "planted.md"
    planted.write_text(text + "\nsend it to %s\n" % FULL, encoding="utf-8")
    code, out, _ = con.run("check", "--register", w.register, planted)
    assert code == 1
    assert [json.loads(line)["cls"] for line in out.splitlines()] == ["name"]

    # ------------------------------------------------------------------ the work side: spawn with the hooks
    code, out, _ = con.run("spawn", "engagement", "--goal", "landing zone offer with two app clusters",
                           "--customer", cust, "--tag", "cce")
    assert code == 0, out
    (pr,) = projects.load(w)
    project = Path(pr.path)
    assert pr.customer == cust and codes.is_project_code(pr.code)
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    events = {"UserPromptSubmit": "prompt", "PreToolUse": "pre-write", "PostToolUse": "post-write", "Stop": "stop",
              "SessionStart": "session-start"}
    assert set(settings["hooks"]) == set(events)
    for event, name in events.items():
        (command,) = [h["command"] for e in settings["hooks"][event] for h in e["hooks"]]
        assert command.endswith(" -m awb hook " + name)
    shutil.copy(output, project / "input" / output.name)

    # the prompt hook of the project, run the way the client runs it: a name is blocked over the check socket
    prompt_command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    def prompt_hook(prompt: str) -> subprocess.CompletedProcess:
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(project), "prompt": prompt})
        res = subprocess.run(["sh", "-c", prompt_command], input=payload, capture_output=True, text=True,
                             cwd=project, timeout=120)
        con.seen.append(res.stdout + res.stderr)
        return res

    if not SEALED_FOR_ANOTHER_USER:     # on a sealed host a hook process is silent for the owner by design
        res = prompt_hook("size the app clusters of %s" % FULL)
        assert res.returncode == 2 and "registered name (1 hits)" in res.stderr
        assert prompt_hook("size the app clusters for the next phase").returncode == 0

    # ------------------------------------------------------------------ the work side: knowledge and ledger
    code, out, err = con.run("kb", "add", "--scope", "tcp", "--tag", "obs", "--grade", "docs", "--class", "stable",
                             "--source", "the public service documentation",
                             "Object storage buckets can be versioned per bucket.")
    assert code == 0, err
    kb_id = out.split()[1]
    assert kb_id.startswith("KB-")
    code, out, _ = con.run("kb", "find", "versioned", "buckets")
    assert code == 0 and kb_id in out
    code, _, err = con.run("kb", "add", "--scope", "tcp", "--tag", "obs", "--grade", "docs", "--class", "stable",
                           "--source", "a call with " + PERSON, "Buckets can be replicated to a second region.")
    assert code == 1 and "name" in err

    code, _, err = con.run("ledger", "add", "--kind", "proof", "--done", "sized the landing zone and wrote the offer",
                           "--project", pr.code, "--customer", cust, "--tag", "cce", "--outcome",
                           "offer ready for review", "--deliverable", "deliverables/offer.md")
    assert code == 0, err
    code, _, err = con.run("ledger", "add", "--kind", "tooling", "--done", "built the review workflow")
    assert code == 0, err
    code, out, err = con.run("report", "--by", "customer")
    assert code == 0, err
    assert cust in out and "sized the landing zone and wrote the offer" in out
    assert out.index(cust) < out.index("sized the landing zone")

    # ------------------------------------------------------------------ the work side: writing and review
    deliverable = project / "deliverables" / "offer.md"
    deliverable.write_text(
        "# Landing zone offer for %s\n\n"
        "The landing zone uses two app clusters and one shared network.\n"
        "The object storage calls go to the path /v1/buckets of the service.\n"
        "The backup runs every night and keeps the data for a month.\n" % cust, encoding="utf-8")
    code, out, err = con.run("write", "check", deliverable)
    assert code == 0, out + err
    code, _, _ = con.hook("post-write", {"hook_event_name": "PostToolUse", "cwd": str(project),
                                         "tool_input": {"file_path": str(deliverable)}})
    assert code == 0
    code, _, err = con.hook("stop", {"hook_event_name": "Stop", "cwd": str(project)})
    assert code == 2 and "run the review for: offer.md" in err

    request = project / "input" / "request.md"
    request.write_text("Write a short offer for the landing zone. Which services does it use?\n", encoding="utf-8")
    code, _, err = con.run("review", "init", deliverable, "--request", request, "--tier", "1", "--budget", "200")
    assert code == 0, err
    code, out, err = con.run("review", "claims", deliverable)
    assert code == 0, err
    code, out, _ = con.run("review", "l0", deliverable)
    assert code == 1 and "no verdict" in out   # the API path is a high-risk claim without evidence yet
    evidence = project / "evidence" / "api-path.md"
    evidence.write_text("The path was read in the public API reference.\n", encoding="utf-8")
    assert _set_evidence(review.review_dir(project, "offer.md") / review.CLAIMS, "evidence/api-path.md") >= 1
    code, out, _ = con.run("review", "l0", deliverable)
    assert code == 0 and out.startswith("l0 passed"), out
    code, out, _ = con.run("review", "pass", deliverable)
    assert code == 0 and "record.json written" in out, out
    code, out, _ = con.run("review", "status", project)
    assert code == 0 and out.split() == ["valid", "deliverables/offer.md"]
    assert con.hook("stop", {"hook_event_name": "Stop", "cwd": str(project)})[0] == 0

    code, out, _ = con.hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(project)})
    assert code == 0
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "- customer: %s" % cust in context and "withheld" not in context

    # ------------------------------------------------------------------ the owner locks the vault
    p = owner_side(monkeypatch, places)
    code, out, _ = con.run("vault", "lock")
    assert code == 0 and "locked" in out
    assert vault.ping(p.check_socket) == "locked"
    code, out, err = con.run("check", output)          # the owner's side: fails closed too
    assert (code, out) == (2, "") and "vault locked" in err
    code, out, err = con.run("intake", "--customer", cust, planted)
    assert code == 2 and "locked" in err and planted.exists()

    w = work_side(monkeypatch, places)
    code, out, err = con.run("check", "--register", w.register, output)
    assert (code, out) == (2, "")
    assert "name check unavailable: vault locked" in err
    code, out, err = con.hook("prompt", {"hook_event_name": "UserPromptSubmit", "cwd": str(project),
                                         "prompt": "size the app clusters of %s" % FULL})
    # T3 (build/DECISIONS.md, 2026-10-07, D-T3): a locked vault blocks the prompt with the time and the unlock
    assert (code, out) == (2, "")
    assert re.search(r"^the Workbench is locked since \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \(vault locked\): as the "
                     r"owner run awb vault unlock", err)
    if not SEALED_FOR_ANOTHER_USER:     # on a sealed host a hook process is silent for the owner by design
        res = prompt_hook("size the app clusters of %s" % FULL)
        assert res.returncode == 2 and res.stdout == "" and "(vault locked): as the owner run" in res.stderr
    code, out, err = con.hook("post-write", {"hook_event_name": "PostToolUse", "cwd": str(project),
                                             "tool_input": {"file_path": str(deliverable)}})
    # review of release 2: a file written while the check cannot run is reported as unchecked (exit 2)
    assert code == 2 and "not checked" in err and "(vault locked): as the owner run awb vault unlock" in err

    # the owner unlocks again: the check comes back
    p = owner_side(monkeypatch, places)
    assert con.run("vault", "unlock", "--stdin", stdin=PASS + "\n")[0] == 0
    w = work_side(monkeypatch, places)
    assert con.run("check", "--register", w.register, planted)[0] == 1

    con.assert_clean()
    # the check log of the daemon holds lengths and counts, never a text
    log = "\n".join(f.read_text(encoding="utf-8") for f in p.check_log.glob("checks-*.tsv"))
    fx.assert_no_fixture_name(log, "check log")
    assert "locked" in log and "\tok" in log
    assert not [f for f in p.vault.rglob("*") if f.is_file() and f.suffix in (".md", ".docx", ".tsv")
                and "log" not in f.relative_to(p.vault).parts], "a plaintext file is left in the vault"
