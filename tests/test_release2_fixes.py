"""Tests of the fixes after the two reviews of release 2 (red team and code review), one or more per finding.

Every test here fails on the code before its fix. Sockets live under a short folder from
tempfile.mkdtemp(prefix="awb", dir="/tmp") and are removed afterwards; daemon threads stop at the end of each test.
Names are the invented ones of tests/fixtures.py only.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import career, check, codes, config, hooks, intake, kb, ledger, patterns, review, vault, writing
from awb.matcher import Matcher
from awb import register as register_mod
from tests import fixtures
from tests.test_vault import PASS, WRONG, serving, short_dir

REPO = Path(__file__).resolve().parent.parent
SEAL = REPO / "seal"
CODE = "tcp-q7m4"
FORM = fixtures.CUSTOMER_FORMS[0]
SHORT = fixtures.CUSTOMER_FORMS[1]


def run_hook(name: str, payload: dict, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def blocking(text: str, **kw) -> list[str]:
    tells, _ = writing.check_text(text, **kw)
    return [t.cls for t in tells if t.blocking]


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    for sub in ("deliverables", "reviews", "evidence"):
        (root / sub).mkdir(parents=True)
    (root / "deliverables" / ".gitkeep").write_bytes(b"")
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- platform: tcp\n- goal: size two app clusters\n"
                                   % (CODE, CODE), encoding="utf-8")
    (root / "STATE.md").write_text("# State\n\nstate line\n", encoding="utf-8")
    (root / "OPEN.md").write_text("# Open items\n\n- size the backup\n", encoding="utf-8")
    return root


def reviewed(project: Path, text: str, tmp_path: Path, name: str = "offer.md", tier: int = 0) -> Path:
    """A deliverable with a contract, ready for a pass."""
    d = project / "deliverables" / name
    d.write_text(text, encoding="utf-8")
    request = tmp_path / ("request-%s.txt" % name)
    request.write_text("Is the landing zone ready?\n", encoding="utf-8")
    review.init(d, request, tier, 200)
    return d


# --------------------------------------------------------------------------- red team G6: the writing checker


@pytest.mark.parametrize("text, cls", [
    ("We le*ver*age it.", "banned-word"),
    ("We lev**erage** it.", "banned-word"),
    ("A sea*ml*ess move.", "banned-word"),
    ("We ens*ur*e it.", "banned-word"),
    ("We lev<b>er</b>age it.", "banned-word"),
    ("We lev&#101;rage it.", "banned-word"),
    ("We lev\u200berage it.", "banned-word"),
    ("We l\u0435verage it.", "banned-word"),            # a Cyrillic look-alike letter
    ("We \uff4c\uff45verage it.", "banned-word"),         # fullwidth letters
    ("Shipped \u2015 done.", "em-dash"),
    ("Shipped \u2e3a done.", "em-dash"),
    ("Shipped \ufe58 done.", "em-dash"),
    ("Shipped \uff0d done.", "em-dash"),
    ("Shipped \u2212 done.", "em-dash"),
    ("Shipped &mdash; done.", "em-dash"),
    ("Fast\uff0c and cheap.", "comma-and-or"),
    ("Fast\u060c and cheap.", "comma-and-or"),
    ("Fast\u3001 or cheap.", "comma-and-or"),
])
def test_writing_sees_what_markup_and_look_alikes_hide(text, cls):
    assert cls in blocking(text)


def test_writing_sees_a_vendor_name_split_by_emphasis_and_in_a_file_name(tmp_path):
    vendor = writing.load_rules().vendor_names[0]
    split = vendor[:3] + "*" + vendor[3:5] + "*" + vendor[5:]
    assert "vendor-name" in blocking("It runs on %s hosts." % split)
    assert "vendor-name" not in blocking("It runs on %s hosts." % split, scope="hcs")
    f = tmp_path / ("offer-%s.md" % vendor.replace("a", "\u0430"))    # a look-alike letter in the file name
    f.write_text("Two zones.\n", encoding="utf-8")
    tells, _ = writing.check_file(f)
    assert [(t.line, t.cls) for t in tells if t.blocking] == [(0, "vendor-name")]


def test_writing_counts_a_hit_found_both_ways_once():
    tells, metrics = writing.check_text("We leverage it and we lev*er*age it.\n")
    assert [t.cls for t in tells if t.blocking] == ["banned-word", "banned-word"]
    tells, _ = writing.check_text("We leverage it.\n")
    assert [t.cls for t in tells if t.blocking] == ["banned-word"]


def test_c5_as_an_instance_family_is_no_certification_claim():
    assert "certification" not in blocking("We size the web tier on C5 instances.")
    assert "certification" not in blocking("The C5 instance family fits.")
    assert "certification" in blocking("The platform is C5 attested.")


def test_a_scope_line_cannot_switch_the_vendor_rule_off(project, monkeypatch, capsys):
    """A session writes SCOPE.md; the platform comes from the project code (the folder name)."""
    (project / "SCOPE.md").write_text("# Scope\n\n- platform: hcs\n", encoding="utf-8")
    vendor = writing.load_rules().vendor_names[0]
    d = project / "deliverables" / "offer.md"
    d.write_text("It runs on %s hosts.\n" % vendor, encoding="utf-8")
    code, _, err = run_hook("post-write", {"tool_input": {"file_path": str(d)}}, monkeypatch, capsys)
    assert code == 2 and "vendor-name line 1" in err
    assert codes.project_platform(project) == "tcp"
    assert codes.project_platform(project.parent / "hcs-q7m4") == "hcs"
    assert codes.project_platform(project.parent / "elsewhere") == "tcp"


# --------------------------------------------------------------------------- red team G4: codes and prices


@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2012", "\u2013", "\u2212", "\ufe63", "\uff0d"])
def test_a_code_with_a_look_alike_dash_is_still_a_code(home, dash):
    written = fixtures.CUSTOMER_CODE.replace("-", dash)
    assert codes.count_codes("the landing zone of %s" % written) == (1, 0)
    assert codes.count_codes("project %s" % CODE.replace("-", dash)) == (0, 1)
    with pytest.raises(kb.Refused) as err:
        kb.add("Object storage for %s keeps versions per bucket." % written, scope="tcp", tags=["obs"],
               grade="docs", cls="stable", source="the public service documentation", where=home)
    assert any("register or project code" in r for r in err.value.reasons)
    assert not list((home.kb / "entries").glob("KB-*.md")) if (home.kb / "entries").exists() else True
    with pytest.raises(career.Refused):
        career.add("Landing zone", "designed and built", "Terraform", "done for %s" % written,
                   "Built a landing zone module.", p=home)
    assert ledger.screen("for %s" % written, home.register, codes_too=True).get("code") == 1


def test_fullwidth_and_invisible_characters_do_not_hide_a_code():
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in fixtures.CUSTOMER_CODE)
    assert codes.count_codes("for %s" % fullwidth) == (1, 0)
    assert codes.count_codes("for CUST-\u2060Q7M4") == (1, 0)


@pytest.mark.parametrize("statement", [
    "Dedicated hosts are billed at 4200 per month in eu-de.",
    "Dedicated hosts cost USD 5000 in eu-de.",
    "Dedicated hosts cost $5000 in eu-de.",
    "Dedicated hosts cost 4200 CHF in eu-de.",
    "The monthly fee is 4200 for a dedicated host.",
])
def test_a_price_in_any_currency_or_with_a_price_word_is_refused(home, statement):
    with pytest.raises(kb.Refused) as err:
        kb.add(statement, scope="tcp", tags=["deh"], grade="docs", cls="stable", source="the price list page",
               where=home)
    assert any("carries a price" in r for r in err.value.reasons)
    # a billing fact without a price stays allowed
    e = kb.add("Elastic servers are billed per second with a minimum of one minute.", scope="tcp", tags=["ecs"],
               grade="docs", cls="stable", source="the public service documentation", where=home)
    assert e.id.startswith("KB-")


# --------------------------------------------------------------------------- code review 11: negative statements


@pytest.mark.parametrize("statement", [
    "Cross-region replication isn't supported for this volume type.",
    "The old flavor won't work with the new image.",
    "Shared volumes aren't available in eu-nl.",
    "The legacy gateway is not offered in eu-de.",
    "The classic load balancer is no longer sold.",
])
def test_contracted_and_other_negatives_need_two_tries(home, statement):
    assert kb.is_negative(statement)
    with pytest.raises(kb.Refused) as err:
        kb.add(statement, scope="tcp", tags=["evs"], grade="docs", cls="api", source="the public service documentation",
               where=home)
    assert any("negative" in r for r in err.value.reasons)


# --------------------------------------------------------------------------- red team G3 and G2: session start


def test_session_start_without_a_register_loads_no_file(project, home, monkeypatch, capsys):
    """Red team G3: a start that cannot check loads nothing. T3 replaced the three withheld sections with one line
    (build/DECISIONS.md, 2026-10-07, D-T3)."""
    (project / "SCOPE.md").write_text("# Scope\n\n- goal: size the clusters of %s\n" % FORM, encoding="utf-8")
    home.register.unlink()      # a readable vault folder without a register: the check cannot run
    code, out, err = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert text == hooks.LOCKED_START % "unknown" and len(text.splitlines()) == 1
    assert "is locked since %s. Every prompt is refused" % "unknown" in text and "files were not loaded" in text
    fixtures.assert_no_fixture_name(out + err, "session-start context")


def test_session_start_withholds_a_section_with_structured_data(project, monkeypatch, capsys):
    value = "ops@" + fixtures.CUSTOMER_DOMAIN.replace("zyxwo-logistik", "qrtvb")
    (project / "OPEN.md").write_text("# Open items\n\n- mail the plan to %s\n" % value, encoding="utf-8")
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and value not in text and "withheld: it carries structured data (mail 1)" in text
    assert "state line" in text


# --------------------------------------------------------------------------- code review 5: the rate limit


def test_prompt_hook_under_the_rate_limit_blocks_instead_of_passing(home, tmp_path, monkeypatch, capsys):
    with short_dir() as d:
        check_sock, admin_sock = d / "c.sock", d / "a.sock"
        with serving(home, admin_sock=admin_sock, check_sock=check_sock, rate_limit=1):
            # the work side: the register lies encrypted in a vault folder it cannot read, the check is remote
            work_vault = tmp_path / "work-vault"
            work_vault.mkdir()
            (work_vault / "register.tsv.gpg").write_bytes(b"sealed")
            monkeypatch.setenv("AWB_VAULT", str(work_vault))
            monkeypatch.setenv("AWB_CHECK_SOCKET", str(check_sock))
            monkeypatch.setattr(hooks, "HOOK_RATE_WAIT", 0.0)
            assert vault.check_remote("a first text", check_sock) == []    # the whole budget of this minute
            code, out, err = run_hook("prompt", {"prompt": "size the clusters of %s" % FORM}, monkeypatch, capsys)
            assert code == 2 and out == ""
            assert "rate limit" in err and "again in a minute" in err
            fixtures.assert_no_fixture_name(err, "prompt hook output")
            with pytest.raises(check.CheckRateLimited):
                check.check_text("a text", config.paths().register)
    assert hooks._reason(check.CheckRateLimited("x")) == "rate limit of the vault daemon"


def test_the_hooks_of_a_project_are_the_work_users_own_command_on_a_sealed_host(tmp_path, monkeypatch):
    """Identical hook commands run once in the client, so a sealed project does not pay the rate budget twice."""
    installed = Path(sys.executable).parent / "awb"
    if not installed.exists():
        pytest.skip("the virtual environment has no awb entry point")
    link = tmp_path / "bin" / "awb"
    link.parent.mkdir()
    link.symlink_to(installed)
    monkeypatch.setattr(hooks, "INSTALLED_AWB", link)
    assert hooks.command_prefix() == "%s hook" % link
    assert hooks.command_prefix("/opt/tcp-awb/venv/bin/python") == "/opt/tcp-awb/venv/bin/python -m awb hook"
    monkeypatch.setattr(hooks, "INSTALLED_AWB", tmp_path / "none" / "awb")
    assert hooks.command_prefix().endswith("-m awb hook")


# --------------------------------------------------------------------------- red team G8, code review 3: the seal


def test_the_hooks_leave_every_user_but_the_work_user_alone(home, tmp_path, monkeypatch, capsys):
    host = tmp_path / "paths.conf"
    monkeypatch.setattr(hooks, "HOST_FILE", host)
    prompt = {"prompt": "size the clusters of %s" % FORM}
    host.write_text("owner = someone\nwork_user = awb-not-this-user\n", encoding="utf-8")
    assert run_hook("prompt", prompt, monkeypatch, capsys) == (0, "", "")
    import getpass

    host.write_text("work_user = %s\n" % getpass.getuser(), encoding="utf-8")
    assert run_hook("prompt", prompt, monkeypatch, capsys)[0] == 2
    # AWB_CONF never switches them off: only the host file counts
    other = tmp_path / "other.conf"
    other.write_text("work_user = awb-not-this-user\n", encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(other))
    assert run_hook("prompt", prompt, monkeypatch, capsys)[0] == 2


def test_managed_settings_carry_the_hooks_and_no_deny_rule():
    data = json.loads((SEAL / "work-claude" / "managed-settings.json").read_text(encoding="utf-8"))
    assert data == {"hooks": hooks.client_settings("/usr/local/bin/awb hook")["hooks"]}


def _dry_run(script: Path, tmp_path: Path) -> str:
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    marks = tmp_path / "marks"
    for name in ("chattr", "lsattr", "cmp", "install", "chmod", "chown", "mkdir", "systemctl", "useradd",
                 "groupadd", "usermod", "gpasswd", "runuser", "mv", "ln", "rm", "tar", "git", "python3", "mount"):
        f = fake / name
        f.write_text("#!/bin/sh\necho %s >> '%s'\nexit 0\n" % (name, marks), encoding="utf-8")
        f.chmod(0o755)
    import getpass

    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env["PATH"] = "%s:%s" % (fake, os.environ.get("PATH", "/usr/bin:/bin"))
    env["SUDO_USER"] = getpass.getuser()
    res = subprocess.run(["bash", str(script), "--dry-run"], capture_output=True, text=True, env=env, timeout=60,
                         cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert not marks.exists(), "a command ran in a dry run"
    return res.stdout


def test_setup_makes_the_client_files_immutable_and_installs_the_managed_hooks(tmp_path):
    lines = _dry_run(SEAL / "setup.sh", tmp_path).splitlines()
    for name in ("settings.json", "CLAUDE.md"):
        install = next(i for i, x in enumerate(lines) if x.startswith("+ install -o root -g awb -m 644")
                       and x.endswith("/.claude/%s" % name))
        assert lines[install - 1].startswith("+ chattr -i ") and lines[install - 1].endswith("  # if present")
        assert lines[install + 1] == "+ chattr +i %s" % lines[install].split()[-1]
    assert "+ install -o root -g root -m 644 %s /etc/claude-code/managed-settings.d/awb-workbench.json" \
        % (SEAL / "work-claude" / "managed-settings.json") in lines
    assert "    | work_user = awb" in lines


def test_verify_compares_the_client_files_and_checks_the_flag(tmp_path):
    checks = [x[6:] for x in _dry_run(SEAL / "verify.sh", tmp_path).splitlines() if x.startswith("CHECK ")]
    for name in ("settings.json", "CLAUDE.md"):
        assert "the client file %s of the work user is the one of the repository" % name in checks
        assert "the client file %s of the work user is immutable" % name in checks
    assert "the managed client settings carry the Workbench hooks" in checks
    assert any(c.startswith("the host file names the work user") for c in checks)


# --------------------------------------------------------------------------- red team G7, code review 4 and 12


def test_a_file_that_changes_while_it_is_checked_gets_no_record(project, tmp_path, monkeypatch):
    """dirty when the pass starts, clean while the checks read it, dirty again before the last hash: before the
    fix the record carried the hash of the dirty file that no check had read."""
    dirty = "The landing zone of %s is ready.\n" % SHORT
    clean = "The landing zone of %s is ready.\n" % fixtures.CUSTOMER_CODE
    d = reviewed(project, clean, tmp_path)
    d.write_text(dirty, encoding="utf-8")
    real_ensure, real_lens = review._ensure_checkable, review.lens_summary

    def ensure_checkable(reg):
        d.write_text(clean, encoding="utf-8")        # the checks read a clean file
        return real_ensure(reg)

    def lens_summary(*args, **kw):
        d.write_text(dirty, encoding="utf-8")        # and it is dirty again when the pass ends
        return real_lens(*args, **kw)

    monkeypatch.setattr(review, "_ensure_checkable", ensure_checkable)
    monkeypatch.setattr(review, "lens_summary", lens_summary)
    res = review.run_pass(d)
    assert not res.passed and "the deliverable changed while the pass ran" in res.problems
    assert [r["state"] for r in review.status(project)] == ["missing"]
    monkeypatch.undo()
    d.write_text(clean, encoding="utf-8")
    assert review.run_pass(d).passed
    record = json.loads((review.review_dir(project, "offer.md") / review.RECORD).read_text(encoding="utf-8"))
    assert record["sha256"] == review.sha256_file(d) == record["l0"]["sha256"]


def test_a_hand_written_record_is_not_valid(project, tmp_path):
    d = reviewed(project, "The landing zone is ready.\n", tmp_path)
    record = review.review_dir(project, "offer.md") / review.RECORD
    record.write_text(json.dumps({"sha256": review.sha256_file(d), "tier": 0}), encoding="utf-8")
    assert [r["state"] for r in review.status(project)] == ["stale"]
    assert review.run_pass(d).passed
    assert [r["state"] for r in review.status(project)] == ["valid"]


def test_a_record_goes_stale_when_the_contract_the_claims_or_a_lens_changes(project, tmp_path):
    d = reviewed(project, "The landing zone is ready.\n", tmp_path)
    folder = review.review_dir(project, "offer.md")

    def state() -> str:
        return review.status(project)[0]["state"]

    assert review.run_pass(d).passed and state() == "valid"
    contract = folder / review.CONTRACT
    contract.write_text(contract.read_text(encoding="utf-8").replace("- tier: 0", "- tier: 3"), encoding="utf-8")
    assert state() == "stale"
    contract.write_text(contract.read_text(encoding="utf-8").replace("- tier: 3", "- tier: 0"), encoding="utf-8")
    assert state() == "valid"
    (folder / review.LENSES).mkdir()
    (folder / review.LENSES / "partner.json").write_text(
        json.dumps({"findings": [{"id": "p-1", "severity": "blocking", "outcome": "open"}]}), encoding="utf-8")
    assert state() == "stale"
    (folder / review.LENSES / "partner.json").unlink()
    assert state() == "valid"
    (folder / review.CLAIMS).write_text("\t".join(review.CLAIM_FIELDS) + "\n", encoding="utf-8")
    assert state() == "stale"


def test_status_lists_hidden_deliverables_and_leaves_only_an_empty_gitkeep_out(project):
    (project / "deliverables" / ".plan.md").write_text("A plan.\n", encoding="utf-8")
    (project / "deliverables" / "sub").mkdir()
    (project / "deliverables" / "sub" / ".gitkeep").write_text("A text.\n", encoding="utf-8")
    rows = review.status(project)
    assert [(r["file"], r["state"]) for r in rows] == [("deliverables/.plan.md", "missing"),
                                                       ("deliverables/sub/.gitkeep", "missing")]


def test_an_image_is_unreadable_and_the_stop_hook_says_so(project, monkeypatch, capsys):
    (project / "deliverables" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert review.status(project) == [{"file": "deliverables/diagram.png", "state": "unreadable", "tier": None}]
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2
    assert "cannot be read as text and can never pass the review" in err and "diagram.png" in err
    assert "run the review for" not in err


# --------------------------------------------------------------------------- code review 1, 2, 6, 8, 9: the vault


@pytest.fixture
def vp(home, monkeypatch):
    monkeypatch.setattr(vault, "S2K_COUNT", 65536)
    with short_dir() as d:
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(d / "run" / "check.sock"))
        monkeypatch.setenv("AWB_ADMIN_SOCKET", str(d / "admin.sock"))
        yield config.paths()


def _connect(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))     # blocking: a full listen backlog waits for the accept loop instead of failing
    s.settimeout(5)
    return s


def _closed_by_peer(s: socket.socket, wait: float = 3.0) -> bool:
    s.settimeout(wait)
    try:
        return s.recv(1) == b""
    except (TimeoutError, OSError):
        return False


def test_a_full_check_socket_never_locks_the_owner_out(vp):
    with serving(vp) as d:
        held = [_connect(d.check_path) for _ in range(vault.MAX_CONNECTIONS)]
        try:
            time.sleep(0.5)
            assert vault.admin_call("status", d.admin_path)["state"] == "plain"
            assert _closed_by_peer(held[-1])     # past the cap of one peer uid: closed at once
        finally:
            for s in held:
                s.close()


def test_a_connection_has_a_lifetime_and_a_rate_answer_closes_it(vp, monkeypatch):
    monkeypatch.setattr(vault, "MAX_CONNECTION_SECONDS", 1.0)
    with serving(vp) as d:
        s = _connect(d.check_path)
        try:
            buf = b""
            closed = False
            for _ in range(12):
                try:
                    s.sendall(b'{"op":"ping"}\n')
                    buf = s.recv(4096)
                except OSError:
                    closed = True
                    break
                if buf == b"":
                    closed = True
                    break
                time.sleep(0.25)
            assert closed, "a connection that keeps talking stays open for ever"
        finally:
            s.close()
    with serving(vp, rate_limit=1) as d:
        s = _connect(d.check_path)
        try:
            s.sendall(b'{"op":"ping"}\n{"op":"ping"}\n')
            data = b""
            while data.count(b"\n") < 2:
                chunk = s.recv(4096)
                assert chunk, "closed before the rate answer"
                data += chunk
            assert json.loads(data.splitlines()[1]) == {"ok": False, "error": "rate"}
            assert _closed_by_peer(s)
        finally:
            s.close()


def test_the_daemon_forgets_the_plain_register_once_the_vault_is_encrypted(vp):
    with serving(vp) as d:
        assert vault.check_remote("met %s" % FORM, d.check_path)
        assert d._plain is not None
        vault.encrypt_vault(vp, PASS)
        assert d.state() == "locked" and d._plain is None
        vault.admin_call("unlock", d.admin_path, passphrase=PASS)
        vault.admin_call("lock", d.admin_path)
        assert d._plain is None and d._entries is None and d._matcher is None


def test_a_lone_surrogate_is_checked_not_crashed(vp):
    with serving(vp) as d:
        hits = vault.check_remote("met \ud800 %s today" % FORM, d.check_path)
    assert [h["cls"] for h in hits] == ["name"]


def test_a_text_larger_than_one_request_is_checked_in_chunks(vp, monkeypatch):
    monkeypatch.setattr(vault, "MAX_REQUEST", 30_000)       # the daemon of this process takes it too
    monkeypatch.setattr(vault, "CHECK_CHUNK", 20_000)
    monkeypatch.setattr(vault, "CHECK_OVERLAP", 1_000)
    lines = []
    for n in range(4000):
        if n % 97 == 0:
            lines.append("line %d names %s and %s" % (n, FORM, fixtures.PERSON_FORMS[0]))
        elif n % 131 == 0:
            lines.append("line %d mails ops@qrtvb.example.com" % n)
        else:
            lines.append("line %d of the sizing, two zones and one plan" % n)
    text = "\n".join(lines) + "\n"
    assert len(text) > 5 * 20_000
    local = check._scan(text, Matcher(register_mod.forms_for_matching(register_mod.load(vp.register))))
    with serving(vp) as d:
        remote = vault.check_remote(text, d.check_path)
    assert remote == local and len(remote) > 50


def test_encrypt_again_refuses_another_passphrase_and_waits_for_an_intake(vp, monkeypatch):
    original = vp.originals / fixtures.CUSTOMER_CODE / "F-AB2C.txt"
    original.parent.mkdir(parents=True)
    original.write_text("offer to %s\n" % FORM, encoding="utf-8")
    second = vp.originals / fixtures.CUSTOMER_CODE / "F-AB2D.txt"
    second.write_text("offer to %s\n" % FORM, encoding="utf-8")
    # a first run sealed one file and stopped
    sealed = vault.seal_path(original, PASS, vp.vault / ".gnupg")
    with pytest.raises(vault.VaultError, match="another passphrase"):
        vault.encrypt_vault(vp, WRONG)
    assert second.exists() and vp.register.exists() and not vp.register_encrypted.exists()
    # while an intake holds the vault, the encryption waits and then gives up
    monkeypatch.setattr(vault, "LOCK_WAIT", 0.2)
    with vault.vault_lock(vp):
        with pytest.raises(vault.VaultError, match="holds the vault"):
            vault.encrypt_vault(vp, PASS)
        planted = vp.inbox / "offer.txt"
        planted.write_text("two zones\n", encoding="utf-8")
        with pytest.raises(intake.IntakeError, match="holds the vault"):
            intake.run([planted], fixtures.CUSTOMER_CODE, vp)
    assert vault.encrypt_vault(vp, PASS) == 2     # the second original and the register
    assert vault.decrypt_bytes(sealed.read_bytes(), PASS, vp.vault / ".gnupg").startswith(b"offer")


# --------------------------------------------------------------------------- code review 7: the platform hosts


def test_the_public_hosts_of_the_platform_are_no_url_hits(project, monkeypatch, capsys):
    text = ("auth_url = \"https://iam.eu-de.otc.t-systems.com/v3\"\n"
            "# guide: https://docs.otc.t-systems.com/elastic-cloud-server/umn/\n"
            "# console: console.otc.t-systems.com and open-telekom-cloud.com\n")
    assert [s.cls for s in patterns.find_structured(text)] == []
    bucket = "https://qrtvbdata.obs.eu-de.otc.t-systems.com/backup.tar"
    assert [s.cls for s in patterns.find_structured(bucket)] == ["url"]
    f = project / "main.tf"
    f.write_text(text, encoding="utf-8")
    assert run_hook("post-write", {"tool_input": {"file_path": str(f)}}, monkeypatch, capsys) == (0, "", "")
