"""Tests of awb/hooks.py: the five client hooks with crafted hook input, against a throw-away Workbench.

The writing check, the review status, the knowledge base and the career log are other modules; the tests put
small fakes into sys.modules for them. None in their place shows that a missing module never blocks and never
crashes.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from awb import check, codes, config, hooks
from tests import fixtures
from tests.conftest import needs_hook_process

CODE = "tcp-q7m4"
GOAL = "move two app clusters to managed k8s"
REGISTER_CODES = (fixtures.CUSTOMER_CODE, fixtures.PERSON_CODE, fixtures.ORG_CODE, fixtures.LAWFIRM_CODE,
                  fixtures.PLACE_CODE)


def run_hook(name: str, payload, monkeypatch, capsys) -> tuple[int, str, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def assert_clean(text: str, where: str) -> None:
    """No fixture form and no code of the register in a hook message."""
    fixtures.assert_no_fixture_name(text, where)
    for c in REGISTER_CODES:
        assert c not in text, "a register code in %s" % where
    assert not codes.PLACEHOLDER_RE.search(text), "a code in %s" % where


def context_of(out: str, event: str) -> dict:
    data = json.loads(out)
    spec = data["hookSpecificOutput"]
    assert spec["hookEventName"] == event
    assert isinstance(spec["additionalContext"], str)
    return data


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    (root / "deliverables").mkdir(parents=True)
    (root / "SCOPE.md").write_text(
        "# Scope of %s\n\n- code: %s\n- kind: lab\n- customer: none\n- platform: tcp\n- goal: %s\n"
        "- mode: sealed\n" % (CODE, CODE, GOAL), encoding="utf-8")
    state = ["# State of %s" % CODE] + ["state line %d" % n for n in range(2, 81)]
    (root / "STATE.md").write_text("\n".join(state) + "\n", encoding="utf-8")
    (root / "OPEN.md").write_text("# Open items\n\n- size the backup, waits on the network team\n",
                                  encoding="utf-8")
    return root


@dataclass
class Tell:
    line: int
    cls: str
    blocking: bool
    hint: str


def fake_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType("awb." + name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, "awb." + name, mod)
    return mod


def missing_module(monkeypatch, name: str) -> None:
    monkeypatch.setitem(sys.modules, "awb." + name, None)


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("form", [fixtures.CUSTOMER_FORMS[0], fixtures.PERSON_FORMS[0], fixtures.ORG_FORMS[0],
                                  fixtures.CUSTOMER_FORMS[1].upper()])
def test_prompt_with_a_registered_form_is_blocked_without_echo(home, monkeypatch, capsys, form):
    prompt = "size the app clusters of %s for the next phase" % form
    code, out, err = run_hook("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": prompt},
                              monkeypatch, capsys)
    assert code == 2
    assert "the prompt carries a registered name (1 hits)" in err
    assert "awb register" in err
    assert out == ""
    assert_clean(err + out, "prompt hook output")


def test_prompt_counts_every_hit(home, monkeypatch, capsys):
    prompt = "call %s about %s" % (fixtures.PERSON_FORMS[0], fixtures.LAWFIRM_FORMS[0])
    code, out, err = run_hook("prompt", {"prompt": prompt}, monkeypatch, capsys)
    assert code == 2
    assert "(2 hits)" in err
    assert_clean(err, "prompt hook output")


def test_clean_prompt_passes_quietly(home, monkeypatch, capsys):
    code, out, err = run_hook("prompt", {"prompt": "size two app clusters with 10.0.0.0/16 for the next phase"},
                              monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")


def test_prompt_with_structured_data_is_blocked_by_class_only(home, monkeypatch, capsys):
    value = "ops@" + fixtures.CUSTOMER_DOMAIN.replace("zyxwo-logistik", "qrtvb")
    code, _, err = run_hook("prompt", {"prompt": "send the plan to %s today" % value}, monkeypatch, capsys)
    assert code == 2
    assert "structured data (mail 1)" in err
    assert value not in err
    assert "registered name" not in err


# --------------------------------------------------------------------------- T3: a locked vault blocks
# These replace the three tests of the hooks release of 2026-09-22 that pinned "a locked vault never blocks typing"
# (build/DECISIONS.md, 2026-10-07, D-T3).

SINCE = "2026-10-07T06:02:11Z"
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LOCKED_PROMPT = "size the app clusters of %s" % fixtures.PERSON_FORMS[0]


@contextlib.contextmanager
def stand_in(answers: dict):
    """A vault daemon stand-in on a short socket path: it answers each op with answers[op] (a dict or raw bytes)
    and keeps the ops it was asked, in order."""
    folder = Path(tempfile.mkdtemp(prefix="awb", dir="/tmp"))
    sock = folder / "check.sock"
    asked: list[str] = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(8)
    srv.settimeout(0.1)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                buf = b""
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    op = json.loads(buf.split(b"\n")[0]).get("op")
                except (OSError, ValueError):
                    continue
                asked.append(op)
                answer = answers.get(op, {"ok": False, "error": "unknown op"})
                conn.sendall(answer if isinstance(answer, bytes) else json.dumps(answer).encode() + b"\n")

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield sock, asked
    finally:
        stop.set()
        t.join(5)
        srv.close()
        shutil.rmtree(folder, ignore_errors=True)


def remote_side(home, tmp_path, monkeypatch, sock: Path) -> None:
    """The work side of a sealed host: the register lies encrypted in a vault folder, the check goes to `sock`."""
    work_vault = tmp_path / "work-vault"
    work_vault.mkdir()
    (work_vault / "register.tsv.gpg").write_bytes(b"sealed")
    monkeypatch.setenv("AWB_VAULT", str(work_vault))
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(sock))


def locked_answers(since) -> dict:
    check = {"ok": False, "error": "locked"}
    ping = {"ok": True, "state": "locked"}
    if since is not None:
        check["since"] = ping["since"] = since
    return {"check": check, "ping": ping}


def assert_locked_hint(err: str, since: str, reason: str) -> None:
    assert err.startswith("the Workbench is locked since %s (%s): " % (since, reason)), err
    assert "as the owner run awb vault unlock (and awb keys unlock), then send the prompt again" in err
    assert len(err.strip().splitlines()) == 1


def test_a_locked_vault_blocks_the_prompt_with_the_hint(home, monkeypatch, capsys):
    """Planted failure: the hook of 2026-09-22 (exit 0 with a warning); a hint that repeats a value of the prompt."""
    def locked(text, register_path):
        raise check.CheckUnavailable("name check unavailable: vault locked")

    monkeypatch.setattr(check, "check_text", locked)
    code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert_locked_hint(err, "unknown", "vault locked")
    assert_clean(out + err, "prompt hook output")


def test_no_daemon_blocks_the_prompt(home, monkeypatch, capsys):
    """Planted failure: a hook that treats "no vault daemon" as a warning."""
    shutil.rmtree(home.vault)
    code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert_locked_hint(err, "unknown", "no vault daemon")
    assert_clean(out + err, "prompt hook output")


@pytest.mark.parametrize("broken", ["missing", "no-check-text", "raises"])
def test_a_broken_check_module_blocks_the_prompt(home, monkeypatch, capsys, broken):
    """Planted failure: a check module without check_text, or one that raises another exception, lets the prompt
    through."""
    if broken == "missing":
        missing_module(monkeypatch, "check")
        reason = "the name check is not installed"
    elif broken == "no-check-text":
        fake_module(monkeypatch, "check", CheckUnavailable=check.CheckUnavailable)
        reason = "the name check is not installed"
    else:
        def fails(text, register_path):
            raise ZeroDivisionError(fixtures.PERSON_FORMS[0])

        monkeypatch.setattr(check, "check_text", fails)
        reason = "ZeroDivisionError in the name check"
    code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert_locked_hint(err, "unknown", reason)
    assert_clean(out + err, "prompt hook output")


@pytest.mark.parametrize("planted", [False, True])
def test_the_rate_limit_still_says_busy(home, monkeypatch, capsys, planted):
    """Planted failure: a hook that blocks the rate limit with the locked hint or lets it through. The planted run
    takes the rate branch away and must fail the same asserts."""
    def busy(text, register_path):
        raise check.CheckRateLimited("name check unavailable: rate limit of the vault daemon, try again in a minute")

    monkeypatch.setattr(check, "check_text", busy)
    monkeypatch.setattr(hooks, "HOOK_RATE_WAIT", 0.0)
    if planted:
        monkeypatch.setattr(hooks, "_is_rate", lambda mod, exc: False)
    code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    ok = code == 2 and out == "" and "busy" in err and "again in a minute" in err and "locked" not in err
    assert ok is not planted
    assert_clean(out + err, "prompt hook output")


def test_a_healthy_daemon_passes_quietly(home, tmp_path, monkeypatch, capsys):
    """Planted failure: a hook that blocks everything."""
    with stand_in({"check": {"ok": True, "hits": []}}) as (sock, asked):
        remote_side(home, tmp_path, monkeypatch, sock)
        assert run_hook("prompt", {"prompt": "size two app clusters"}, monkeypatch, capsys) == (0, "", "")
    assert asked == ["check"]


def test_a_locked_prompt_names_the_time(home, tmp_path, monkeypatch, capsys):
    """The time comes with the locked answer of the check: one request, no ping (the rate budget)."""
    with stand_in(locked_answers(SINCE)) as (sock, asked):
        remote_side(home, tmp_path, monkeypatch, sock)
        code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert_locked_hint(err, SINCE, "vault locked")
    assert asked == ["check"]
    assert_clean(err, "prompt hook output")


@pytest.mark.parametrize("since", [fixtures.CUSTOMER_FORMS[0], "2026-10-07T12:00:00+00:00", "2026-10-07 12:00:00Z",
                                   "2026-10-07T12:00:00Z\n" + fixtures.PERSON_FORMS[0], 1791382662, None],
                         ids=["fixture-name", "offset", "space", "second-line", "number", "absent"])
def test_since_is_a_timestamp_or_nothing(home, tmp_path, monkeypatch, capsys, since):
    """Planted failure: a client that passes the raw string of the daemon on to the session or the board."""
    from awb import vault
    from awb.tcp import board

    with stand_in(locked_answers(since)) as (sock, asked):
        remote_side(home, tmp_path, monkeypatch, sock)
        with pytest.raises(vault.VaultLocked) as err:
            vault.check_remote("a text", sock)
        assert err.value.since is None
        assert vault.ping_state(sock) == {"state": "locked", "since": None}
        code, out, hook_err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
        line = board.locked(config.paths())
    assert code == 2
    assert_locked_hint(hook_err, "unknown", "vault locked")
    assert line == "locked since unknown"
    assert_clean(out + hook_err + line, "hook output and board line")


def test_a_real_locked_daemon_blocks_end_to_end(home, tmp_path, monkeypatch, capsys):
    """The daemon of awb/vault.py with an encrypted register and no unlock. Planted failure: a _reason that maps the
    real VaultLocked text to a warning."""
    from awb import vault
    from tests.test_vault import serving, short_dir

    monkeypatch.setattr(vault, "S2K_COUNT", 65536)
    homedir = home.vault / ".gnupg"
    homedir.mkdir(mode=0o700, exist_ok=True)
    home.register_encrypted.write_bytes(vault.encrypt_bytes(home.register.read_bytes(), "a passphrase of 2 tests",
                                                            homedir))
    home.register.unlink()
    with short_dir() as d:
        with serving(home, admin_sock=d / "a.sock", check_sock=d / "c.sock") as daemon:
            assert daemon.state() == "locked"
            remote_side(home, tmp_path, monkeypatch, d / "c.sock")
            code, out, err = run_hook("prompt", {"prompt": LOCKED_PROMPT}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    since = err.split("locked since ", 1)[1].split(" ", 1)[0]
    assert SINCE_RE.fullmatch(since), err
    assert_locked_hint(err, since, "vault locked")
    assert_clean(err, "prompt hook output")


def test_unreadable_register_goes_through_the_check_module(home, monkeypatch, capsys):
    """When the register cannot be read here the hook still asks check.check_text (which asks the daemon)."""
    os.chmod(home.register, 0)
    if os.access(home.register, os.R_OK):
        pytest.skip("the file mode does not bind this user")
    monkeypatch.setattr(check, "CheckUnavailable", type("CheckUnavailable", (Exception,), {}), raising=False)
    seen = []

    def remote(text, register_path):
        seen.append(register_path)
        return [{"start": 4, "length": 6, "cls": "name"}]

    monkeypatch.setattr(check, "check_text", remote)
    code, _, err = run_hook("prompt", {"prompt": "for someone"}, monkeypatch, capsys)
    assert code == 2 and seen == [home.register]
    assert "(1 hits)" in err


def test_input_that_is_not_json_is_a_non_blocking_error(home, monkeypatch, capsys):
    raw = "not json " + fixtures.CUSTOMER_FORMS[0]
    code, out, err = run_hook("prompt", raw, monkeypatch, capsys)
    assert code == 1
    assert "not JSON" in err
    assert_clean(out + err, "error output")


def test_unknown_hook_name_is_a_non_blocking_usage_error(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hooks.main([fixtures.CUSTOMER_FORMS[1]]) == 1
    assert_clean(capsys.readouterr().err, "usage error")


@needs_hook_process
def test_the_command_line_runs_the_prompt_hook(home):
    payload = json.dumps({"prompt": "size the app clusters of %s" % fixtures.CUSTOMER_FORMS[0]})
    res = subprocess.run([sys.executable, "-m", "awb", "hook", "prompt"], input=payload, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 2
    assert "registered name" in res.stderr
    assert_clean(res.stdout + res.stderr, "hook output")


# --------------------------------------------------------------------------- post-write


def write_payload(path: Path, cwd: Path | None = None, tool: str = "Write") -> dict:
    data = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": str(path)}}
    if cwd is not None:
        data["cwd"] = str(cwd)
    return data


@pytest.fixture
def no_writing(monkeypatch):
    calls = []

    def check_file(path, mode="doc", scope="tcp"):
        calls.append((Path(path).name, mode, scope))
        return [], {}

    fake_module(monkeypatch, "writing", check_file=check_file)
    return calls


def test_post_write_blocks_a_file_with_a_name_by_class_and_line(project, monkeypatch, capsys, no_writing):
    f = project / "evidence-notes.md"
    f.write_text("# Notes\n\nfirst line\nthe offer for %s is late\n" % fixtures.CUSTOMER_FORMS[1], encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2
    assert "name on line 4" in err
    assert "1 hits of the name check" in err
    assert_clean(out + err, "post-write output")
    assert no_writing == []


def test_post_write_passes_a_clean_file_outside_deliverables(project, monkeypatch, capsys, no_writing):
    f = project / "STATE.md"
    code, out, err = run_hook("post-write", write_payload(f, tool="Edit"), monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")
    assert no_writing == []


def test_post_write_on_a_deliverable_with_an_em_dash_is_blocked(project, monkeypatch, capsys):
    calls = []

    def check_file(path, mode="doc", scope="tcp"):
        calls.append((mode, scope))
        text = Path(path).read_text(encoding="utf-8")
        tells = [Tell(n, "em-dash", True, "no em-dash") for n, line in enumerate(text.splitlines(), 1)
                 if "\u2014" in line]
        return tells + [Tell(1, "long-word", False, "words longer than nine letters")], {"sentences": 2}

    fake_module(monkeypatch, "writing", check_file=check_file)
    f = project / "deliverables" / (CODE + "-offer.md")
    f.write_text("# Offer\n\nThe network \u2014 two zones.\n", encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2
    assert "em-dash line 3" in err
    assert "long-word" not in err
    assert calls == [("doc", "tcp")]


def test_post_write_on_a_deliverable_with_soft_tells_only_passes(project, monkeypatch, capsys):
    fake_module(monkeypatch, "writing",
                check_file=lambda path, mode="doc", scope="tcp": ([Tell(1, "long-word", False, "")], {}))
    f = project / "deliverables" / (CODE + "-offer.md")
    f.write_text("# Offer\n\nTwo zones.\n", encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")


def test_post_write_reports_name_and_writing_findings_together(project, monkeypatch, capsys):
    fake_module(monkeypatch, "writing",
                check_file=lambda path, mode="doc", scope="tcp": ([Tell(2, "comma-and-or", True, "")], {}))
    f = project / "deliverables" / (CODE + "-mail.md")
    f.write_text("# Mail\nsizes and zones for %s\n" % fixtures.PERSON_FORMS[0], encoding="utf-8")
    code, _, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2
    assert "name on line 2" in err and "comma-and-or line 2" in err
    assert_clean(err, "post-write output")


def test_post_write_without_the_writing_module_warns_and_passes(project, monkeypatch, capsys):
    missing_module(monkeypatch, "writing")
    f = project / "deliverables" / (CODE + "-offer.md")
    f.write_text("# Offer\n\nTwo zones \u2014 one plan.\n", encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 0 and err == ""
    text = context_of(out, "PostToolUse")["hookSpecificOutput"]["additionalContext"]
    assert "writing check did not run" in text


def test_post_write_resolves_a_relative_path_against_the_session_folder(project, monkeypatch, capsys, no_writing):
    (project / "note.md").write_text("to %s\n" % fixtures.ORG_FORMS[0], encoding="utf-8")
    payload = write_payload(Path("note.md"), cwd=project)
    code, _, err = run_hook("post-write", payload, monkeypatch, capsys)
    assert code == 2 and "name on line 1" in err


def test_post_write_finds_a_name_in_the_file_name(project, monkeypatch, capsys, no_writing):
    f = project / ("notes-%s.md" % fixtures.CUSTOMER_FORMS[1].lower())
    f.write_text("nothing here\n", encoding="utf-8")
    code, _, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "path (whole file)" in err
    assert_clean(err, "post-write output")


def test_post_write_with_a_locked_vault_tells_the_session_the_file_is_unchecked(project, monkeypatch, capsys,
                                                                                no_writing):
    """Review of release 2: a file written while the name check cannot run is not passed as if it was checked.
    The tool ran already (PostToolUse), so exit 2 puts the message in front of the session as an error."""
    monkeypatch.setattr(check, "CheckUnavailable", type("CheckUnavailable", (Exception,), {}), raising=False)

    def locked(*args):
        raise check.CheckUnavailable("name check unavailable: vault locked")

    monkeypatch.setattr(check, "check_text", locked)
    monkeypatch.setattr(check, "check_file", locked)
    f = project / "note.md"
    f.write_text("to %s\n" % fixtures.ORG_FORMS[0], encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and out == ""
    assert "the written file is not checked" in err and "vault locked" in err
    assert_clean(out + err, "post-write output")


def test_post_write_while_locked_names_the_unlock(project, home, tmp_path, monkeypatch, capsys, no_writing):
    """T3. Planted failure: a message without the hint or the time."""
    f = project / "note.md"
    f.write_text("to %s\n" % fixtures.ORG_FORMS[0], encoding="utf-8")
    with stand_in(locked_answers(SINCE)) as (sock, asked):
        remote_side(home, tmp_path, monkeypatch, sock)
        code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert "the written file is not checked: the Workbench is locked since %s (vault locked): as the owner run awb " \
           "vault unlock (and awb keys unlock), then run `awb check` on it" % SINCE in err
    assert_clean(err, "post-write output")


def test_post_write_with_an_unreadable_register_asks_the_check_module(project, home, monkeypatch, capsys,
                                                                      no_writing):
    os.chmod(home.register, 0)
    if os.access(home.register, os.R_OK):
        pytest.skip("the file mode does not bind this user")
    monkeypatch.setattr(check, "CheckUnavailable", type("CheckUnavailable", (Exception,), {}), raising=False)
    seen = []

    def remote(path, register_path):
        seen.append((Path(path).name, register_path))
        return [{"start": 3, "length": 5, "cls": "name"}]

    monkeypatch.setattr(check, "check_file", remote)
    f = project / "note.md"
    f.write_text("to someone\n", encoding="utf-8")
    code, _, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "name on line 1" in err
    assert seen == [("note.md", home.register)]


def test_post_write_ignores_input_without_a_file(project, monkeypatch, capsys):
    assert run_hook("post-write", {"tool_input": {}}, monkeypatch, capsys) == (0, "", "")
    missing = write_payload(project / "gone.md")
    assert run_hook("post-write", missing, monkeypatch, capsys) == (0, "", "")


# --------------------------------------------------------------------------- stop


def fake_review(monkeypatch, rows):
    calls = []

    def status(project):
        calls.append(Path(project))
        return rows

    fake_module(monkeypatch, "review", status=status)
    return calls


def test_stop_with_a_stale_record_is_blocked(project, monkeypatch, capsys):
    calls = fake_review(monkeypatch, [
        {"file": "deliverables/%s-offer.md" % CODE, "state": "stale"},
        {"file": "deliverables/%s-sizing.md" % CODE, "state": "valid"},
        {"file": "deliverables/%s-mail.md" % CODE, "state": "missing"},
    ])
    code, out, err = run_hook("stop", {"hook_event_name": "Stop", "cwd": str(project / "deliverables"),
                                       "stop_hook_active": False}, monkeypatch, capsys)
    assert code == 2
    assert err.strip() == "run the review for: %s-offer.md, %s-mail.md" % (CODE, CODE)
    assert calls == [project]


def test_a_refused_ledger_draft_moves_no_marker_and_is_said_once(project, home, monkeypatch, capsys):
    """TM0 item 5: a commit subject the ledger refuses leaves the marker where it was, the stop hook says so once,
    and the next commit is drafted again."""
    import subprocess

    from awb import ledger

    fake_review(monkeypatch, [])
    monkeypatch.setattr(hooks, "DRAFT_HOURS", 0)
    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    (project / "notes.md").write_text("x\n", encoding="utf-8")
    subprocess.run(git + ["add", "notes.md"], check=True)
    subprocess.run(git + ["commit", "-qm", "call with %s" % fixtures.PERSON_FORMS[0]], check=True)
    marker = home.ledger / ".drafts" / (CODE + ".json")
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and "the ledger refused the draft" in err and "awb ledger add" in err
    assert_clean(err, "the stop message")
    assert not marker.exists() and not ledger.load(home)
    _, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert "refused" not in err
    (project / "notes.md").write_text("y\n", encoding="utf-8")
    subprocess.run(git + ["commit", "-qam", "Sizing of the two clusters"], check=True)
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and "refused" in err


def test_stop_hook_active_never_loops(project, monkeypatch, capsys):
    calls = fake_review(monkeypatch, [{"file": "deliverables/%s-offer.md" % CODE, "state": "stale"}])
    payload = {"cwd": str(project), "stop_hook_active": True}
    assert run_hook("stop", payload, monkeypatch, capsys) == (0, "", "")
    assert calls == []


def test_stop_with_valid_records_passes(project, monkeypatch, capsys):
    fake_review(monkeypatch, [{"file": "deliverables/%s-offer.md" % CODE, "state": "valid"}])
    assert run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys) == (0, "", "")


def test_stop_masks_a_file_name_that_carries_a_name(project, monkeypatch, capsys):
    fake_review(monkeypatch, [{"file": "deliverables/offer-%s.md" % fixtures.CUSTOMER_FORMS[1], "state": "stale"}])
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2
    assert err.strip() == "run the review for: file 1"


def test_stop_without_the_review_module_passes(project, monkeypatch, capsys):
    missing_module(monkeypatch, "review")
    assert run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys) == (0, "", "")


def test_stop_outside_a_project_passes(home, tmp_path, monkeypatch, capsys):
    calls = fake_review(monkeypatch, [{"file": "x.md", "state": "stale"}])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_hook("stop", {"cwd": str(elsewhere)}, monkeypatch, capsys) == (0, "", "")
    assert calls == []


# --------------------------------------------------------------------------- session-start


def test_session_start_prints_scope_state_and_open(project, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    code, out, err = run_hook("session-start", {"hook_event_name": "SessionStart", "source": "startup",
                                                "cwd": str(project)}, monkeypatch, capsys)
    assert code == 0 and err == ""
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "- goal: %s" % GOAL in text
    assert "- code: %s" % CODE in text
    assert "state line 60" in text and "state line 61" not in text
    assert "size the backup, waits on the network team" in text
    assert "career log" not in text and "knowledge base" not in text


def test_session_start_counts_expired_knowledge_entries(project, home, monkeypatch, capsys):
    missing_module(monkeypatch, "career")
    fake_module(monkeypatch, "kb", expired=lambda p: ["KB-AAAA", "KB-BBBB", "KB-CCCC"])
    _, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert "knowledge base: 3 expired entries" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_session_start_reads_the_entry_headers_without_the_kb_module(project, home, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    entries = home.kb / "entries"
    entries.mkdir(parents=True)
    old = (date.today() - timedelta(days=3)).isoformat()
    new = (date.today() + timedelta(days=30)).isoformat()
    for n, expires in enumerate((old, new, old)):
        (entries / ("KB-000%d.md" % n)).write_text(
            "id: KB-000%d\nscope: tcp\nclass: api\nexpires: %s\n---\nA statement.\n" % (n, expires),
            encoding="utf-8")
    # a retired entry is expired on paper and is not due again
    (entries / "KB-0009.md").write_text(
        "id: KB-0009\nscope: tcp\nclass: api\nexpires: %s\nretired: %s\nwhy: the fact was wrong\n---\nA statement.\n"
        % (old, old), encoding="utf-8")
    _, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert "knowledge base: 2 expired entries" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("days, shown", [(120, True), (91, True), (90, False), (10, False)])
def test_session_start_shows_the_career_update_when_over_90_days(project, home, monkeypatch, capsys, days, shown):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    (home.shared / "career-updated").write_text((date.today() - timedelta(days=days)).isoformat() + "\n",
                                                encoding="utf-8")
    _, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert (("career log: last update %d days ago" % days) in text) is shown


def test_session_start_asks_the_career_module_first(project, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    fake_module(monkeypatch, "career", days_since_update=lambda p: 95)
    _, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert "last update 95 days ago" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_session_start_withholds_a_file_with_a_name(project, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    (project / "STATE.md").write_text("# State\n\ncall %s on friday\n" % fixtures.PERSON_FORMS[0],
                                      encoding="utf-8")
    code, out, err = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 0
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "withheld" in text and "- goal: %s" % GOAL in text
    assert_clean(text + err, "session-start context")


def test_session_start_while_locked_gives_one_line_and_no_file_content(project, home, tmp_path, monkeypatch,
                                                                       capsys):
    """T3. Planted failure: any text of a file in the context or more than one section. The claim still runs."""
    from awb import sessions

    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    (project / "SCOPE.md").write_text("# Scope\n\n- goal: size the clusters of %s\n" % fixtures.CUSTOMER_FORMS[0],
                                      encoding="utf-8")
    with stand_in(locked_answers(SINCE)) as (sock, asked):
        remote_side(home, tmp_path, monkeypatch, sock)
        code, out, err = run_hook("session-start", {"cwd": str(project), "session_id": "a-session-of-3"},
                                  monkeypatch, capsys)
    assert (code, err) == (0, "")
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert text == hooks.LOCKED_START % SINCE and len(text.splitlines()) == 1
    assert "is locked since %s. Every prompt is refused" % SINCE in text and "files were not loaded" in text
    assert asked == ["check"]
    assert sessions.owner(home, CODE)["session"] == "a-session-of-3"
    assert_clean(out, "session-start context")


def test_session_start_outside_a_project_still_prints_json(home, tmp_path, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code, out, _ = run_hook("session-start", {"cwd": str(elsewhere)}, monkeypatch, capsys)
    assert code == 0
    assert "no SCOPE.md in this folder" in context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]


@needs_hook_process
def test_the_command_line_runs_the_session_start_hook(project):
    res = subprocess.run([sys.executable, "-m", "awb", "hook", "session-start"],
                         input=json.dumps({"cwd": str(project)}), capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[-300:]
    assert GOAL in json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


# --------------------------------------------------------------------------- settings


def test_command_prefix_keeps_home_paths_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    inside = tmp_path / "userhome" / "tcp-awb" / ".venv" / "bin" / "python"
    assert hooks.command_prefix(inside) == '"$HOME/tcp-awb/.venv/bin/python" -m awb hook'
    assert hooks.command_prefix("/opt/tcp-awb/venv/bin/python") == "/opt/tcp-awb/venv/bin/python -m awb hook"
    spaced = tmp_path / "with space" / "python"
    assert hooks.command_prefix(spaced) == "'%s' -m awb hook" % spaced


# --------------------------------------------------------------------------- with the real modules (Release 2)


def test_a_vault_folder_without_a_register_blocks_the_prompt(home, monkeypatch, capsys):
    """The check treats a readable vault folder without a register as an empty register; the hooks must not call a
    name clean on that ground. T3 replaced the warning of Release 2 with the block (build/DECISIONS.md, D-T3)."""
    home.register.unlink()
    prompt = "size the app clusters of %s" % fixtures.CUSTOMER_FORMS[0]
    code, out, err = run_hook("prompt", {"prompt": prompt}, monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert_locked_hint(err, "unknown", "no register found")
    assert_clean(out + err, "prompt hook output")


def test_post_write_runs_the_real_writing_check_on_a_deliverable(project, monkeypatch, capsys):
    from awb import writing

    assert hooks._module("writing") is writing
    target = project / "deliverables" / "offer.md"
    target.write_text("The landing zone is ready.\nThe backup plan — as agreed — is next.\n",
                      encoding="utf-8")
    code, _, err = run_hook("post-write", write_payload(target), monkeypatch, capsys)
    assert code == 2
    assert "em-dash line 2" in err
    target.write_text("The landing zone is ready.\nThe backup plan is next.\n", encoding="utf-8")
    assert run_hook("post-write", write_payload(target), monkeypatch, capsys) == (0, "", "")


def test_stop_reads_the_real_review_status(project, tmp_path, monkeypatch, capsys):
    from awb import review

    assert hooks._module("review") is review
    target = project / "deliverables" / "offer.md"
    target.write_text("The landing zone is ready.\n", encoding="utf-8")
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and err.strip() == "run the review for: offer.md"
    # review of release 2: a record written by hand with the hash alone is no longer a valid record
    record = review.review_dir(project, "offer.md") / review.RECORD
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"sha256": review.sha256_file(target), "tier": 0}), encoding="utf-8")
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and err.strip() == "run the review for: offer.md"
    # a real pass is
    request = tmp_path / "request.txt"
    request.write_text("Is the landing zone ready?\n", encoding="utf-8")
    review.init(target, request, 0, 50)
    assert review.run_pass(target).passed
    assert run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys) == (0, "", "")
    target.write_text("The landing zone is ready now.\n", encoding="utf-8")
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and "offer.md" in err


def test_session_start_counts_with_the_real_kb_and_career(project, home, monkeypatch, capsys):
    from awb import career, kb

    old = date.today() - timedelta(days=100)
    kb.add("Object storage buckets can be versioned per bucket.", scope="tcp", tags=["obs"], grade="docs",
           cls="availability", source="the public service documentation", checked=old.isoformat(), where=home)
    kb.add("Elastic volumes can grow while attached to a running server.", scope="tcp", tags=["evs"],
           grade="docs", cls="stable", source="the public service documentation", where=home)
    assert len(kb.expired(home)) == 1
    career.add("Landing zone automation", "designed and built", "Terraform, Ansible",
               "four environments from one module", "Built a landing zone module used for four environments.",
               day=old.isoformat(), p=home)
    assert career.due(home)[0] == 100
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 0
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "knowledge base: 1 expired entries" in text
    assert "career log: last update 100 days ago" in text


# --------------------------------------------------------------------------- pre-write: the knowledge base takes facts through awb kb only


def test_pre_write_blocks_a_write_into_the_knowledge_base(home, monkeypatch, capsys):
    target = home.kb / "entries" / "KB-AAAA.md"
    code, out, err = run_hook("pre-write", {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
                              monkeypatch, capsys)
    assert code == hooks.BLOCK
    assert "awb kb add" in err


def test_pre_write_lets_every_other_write_pass(home, tmp_path, monkeypatch, capsys):
    other = tmp_path / "projects" / "tcp-q7m4" / "notes.md"
    assert run_hook("pre-write", {"tool_input": {"file_path": str(other)}}, monkeypatch, capsys)[0] == hooks.OK
    assert run_hook("pre-write", {"tool_input": {}}, monkeypatch, capsys)[0] == hooks.OK
    near = home.kb.parent / (home.kb.name + "-notes") / "x.md"      # a sibling with the same prefix
    assert run_hook("pre-write", {"tool_input": {"file_path": str(near)}}, monkeypatch, capsys)[0] == hooks.OK


def test_pre_write_resolves_relative_paths_and_links(home, tmp_path, monkeypatch, capsys):
    home.kb.mkdir(parents=True, exist_ok=True)
    rel = {"cwd": str(home.kb), "tool_input": {"file_path": "entries/KB-BBBB.md"}}
    assert run_hook("pre-write", rel, monkeypatch, capsys)[0] == hooks.BLOCK
    link = tmp_path / "shortcut"
    link.symlink_to(home.kb)
    via = {"tool_input": {"file_path": str(link / "INDEX.md")}}
    assert run_hook("pre-write", via, monkeypatch, capsys)[0] == hooks.BLOCK


# --------------------------------------------------------------------------- T8: pasted output and agent results
# Structured data inside a pasted block or a background agent's result no longer blocks (build/DECISIONS.md, D-T8).

THREE_URLS = ("Error: Post \"https://ecs.eu-de.qrtvb.net/v1/servers\": timeout\n"
              "  see https://build.qrtvb.io/run/42 and https://docs.qrtvb.net/errors/ecs.0001\n")


def pasted(body: str, typed: str = "what does this error mean?") -> str:
    return '%s\n<pasted_content id="p7k2">\n%s</pasted_content id="p7k2">' % (typed, body)


def notification(result: str, task_id: str = "a3f9c21") -> str:
    return ("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n<summary>finished</summary>\n"
            "<result>%s</result>\n</task-notification>" % (task_id, result))


def test_t8_a_pasted_block_with_three_urls_passes_with_a_context_line(home, monkeypatch, capsys):
    code, out, err = run_hook("prompt", {"prompt": pasted(THREE_URLS)}, monkeypatch, capsys)
    assert (code, err) == (0, "")
    line = context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
    assert line == "The paste carries 3 data items (url 3); never repeat them, refer to them by line."
    assert "qrtvb" not in out
    # the same text typed, without the paste tag, keeps the strict rule
    code, out, err = run_hook("prompt", {"prompt": "what does this mean?\n" + THREE_URLS}, monkeypatch, capsys)
    assert code == 2 and "structured data (url 3)" in err and out == ""
    # a typed address next to a clean paste blocks too: only the pasted block is softened
    code, _, err = run_hook("prompt", {"prompt": pasted(THREE_URLS, "and call 198.51.100.23 now")},
                            monkeypatch, capsys)
    assert code == 2 and "structured data" in err


@pytest.mark.parametrize("form", [fixtures.CUSTOMER_FORMS[0], fixtures.PERSON_FORMS[0]])
def test_t8_a_pasted_block_with_a_registered_form_blocks(home, monkeypatch, capsys, form):
    body = "From: the project lead\nDear team, the offer for %s is late.\n%s" % (form, THREE_URLS)
    code, out, err = run_hook("prompt", {"prompt": pasted(body)}, monkeypatch, capsys)
    assert code == 2 and "registered name" in err and out == ""
    assert_clean(out + err, "prompt hook output")


def test_t8_a_pasted_block_with_a_secret_or_a_mail_blocks(home, monkeypatch, capsys):
    word = "".join(["K7mQ2v", "X9pL4n", "R8sT"])
    code, out, err = run_hook("prompt", {"prompt": pasted(THREE_URLS + "password=%s\n" % word)}, monkeypatch, capsys)
    assert code == 2 and "carries a secret" in err and word[:6] not in err + out
    code, _, err = run_hook("prompt", {"prompt": pasted(THREE_URLS + "ops@qrtvb.net\n")}, monkeypatch, capsys)
    assert code == 2 and "mail 1" in err


def test_t8_a_task_notification_with_addresses_reaches_the_session(home, monkeypatch, capsys):
    result = "checked 198.51.100.23, 198.51.100.24 and https://build.qrtvb.io/run/42: all three answer"
    code, out, err = run_hook("prompt", {"prompt": notification(result)}, monkeypatch, capsys)
    assert (code, err) == (0, "")
    line = context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
    assert line == "The agent result carries 3 data items (ip 2, url 1); never repeat them, refer to them by line."
    # the client may wrap it in a reminder; a CI event is treated the same way
    wrapped = "<system-reminder>\nA background task finished.\n%s\n</system-reminder>" % notification(result)
    assert run_hook("prompt", {"prompt": wrapped}, monkeypatch, capsys)[0] == 0
    ci = "<ci-monitor-event>\ncheck build failed on 198.51.100.23\n</ci-monitor-event>"
    code, out, _ = run_hook("prompt", {"prompt": ci}, monkeypatch, capsys)
    assert code == 0 and "The CI event carries 1 data items (ip 1)" in out


def test_t8_a_task_notification_with_a_registered_form_blocks_with_the_agent_named(home, monkeypatch, capsys):
    result = "the draft for %s is in deliverables/, see 198.51.100.23" % fixtures.CUSTOMER_FORMS[0]
    code, out, err = run_hook("prompt", {"prompt": notification(result, "a3f9c21")}, monkeypatch, capsys)
    assert code == 2 and out == ""
    assert err.startswith("the result of background agent a3f9c21 was not delivered: it carries registered name 1.")
    assert "codes only" in err
    assert_clean(out + err, "prompt hook output")
    # an id of another shape is not repeated
    code, _, err = run_hook("prompt", {"prompt": notification(result, fixtures.PERSON_FORMS[1])}, monkeypatch, capsys)
    assert code == 2 and "the result of a background agent was not delivered" in err
    assert_clean(err, "prompt hook output")


def test_t8_the_tag_in_the_middle_of_a_typed_prompt_is_still_his_prompt(home, monkeypatch, capsys):
    text = "look at this: %s and call 198.51.100.23" % notification("ok")
    code, out, err = run_hook("prompt", {"prompt": text}, monkeypatch, capsys)
    assert code == 2 and "the prompt carries structured data (ip 1)" in err


def test_t8_awb_paste_masks_the_soft_classes_and_refuses_a_name(project, monkeypatch, capsys):
    from awb import paste

    listing = ("-rw-r----- 1 awb awb     0 2026-10-07 06:02 0001-req.txt\n"
               "last login 2026-10-07 06:02:11 from 198.51.100.23\n" + THREE_URLS +
               "eth0 ether 52:54:00:12:34:56 and call +49 30 1234567\n")
    path, counts, outside = paste.paste(listing, project, today=date(2026, 10, 8))
    assert path == project / "notes" / "pastes" / "2026-10-08-1.txt"
    assert counts == {"ip": 1, "url": 3, "mac": 1, "phone": 1} and outside == 0
    text = path.read_text(encoding="utf-8")
    assert "[ip]" in text and text.count("[url]") == 3 and "[phone]" in text and "[mac]" in text
    for value in ("198.51.100.23", "qrtvb", "52:54:00", "1234567"):
        assert value not in text
    assert "0001-req.txt" in text and "06:02:11" in text
    second, _, _ = paste.paste("plain text\n", project, today=date(2026, 10, 8))
    assert second.name == "2026-10-08-2.txt"
    named, _, _ = paste.paste("plain text\n", project, name="tf-error", today=date(2026, 10, 8))
    assert named.name == "2026-10-08-tf-error.txt"
    with pytest.raises(paste.PasteError):
        paste.paste("plain text\n", project, name="tf-error", today=date(2026, 10, 8))
    before = sorted(p.name for p in path.parent.iterdir())
    with pytest.raises(paste.PasteError) as err:
        paste.paste("the offer for %s\n%s" % (fixtures.CUSTOMER_FORMS[0], THREE_URLS), project,
                    today=date(2026, 10, 8))
    assert "registered name 1" in str(err.value)
    assert_clean(str(err.value), "paste message")
    with pytest.raises(paste.PasteError):
        paste.paste("write to ops@qrtvb.net\n", project, today=date(2026, 10, 8))
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_t8_awb_paste_command_line(project, monkeypatch, capsys):
    from awb import paste

    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", io.StringIO(THREE_URLS))
    assert paste.main(["--as", "build-log"]) == 0
    out = capsys.readouterr().out
    assert "notes/pastes/" in out and "build-log.txt" in out and "masked url 3" in out
    monkeypatch.setattr(sys, "stdin", io.StringIO("call %s\n" % fixtures.PERSON_FORMS[0]))
    assert paste.main([]) == 2
    cap = capsys.readouterr()
    assert "nothing was written" in cap.err
    assert_clean(cap.out + cap.err, "paste output")
    monkeypatch.chdir(project.parent)
    monkeypatch.setattr(sys, "stdin", io.StringIO("x\n"))
    assert paste.main([]) == 2


# --------------------------------------------------------------------------- the build user (self-build SB1)


@pytest.fixture
def build_root(tmp_path, monkeypatch):
    """A throw-away build root outside /tmp's pass: <tmp>/build with two clones and the venv; the own clone is where
    the client started."""
    base = Path(os.path.realpath(tmp_path))
    root = base / "build"
    for d in ("wt/B-001-1/tests", "wt/B-002-1", "venv/bin", "runs/B-001/1"):
        (root / d).mkdir(parents=True)
    (root / "wt" / "B-001-1" / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    (root / "wt" / "B-002-1" / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "wt" / "B-001-1"))
    return root


def _build(root: Path, tool: str, tool_input: dict, cwd: Path | None = None) -> str | None:
    own = root / "wt" / "B-001-1"
    return hooks.build_guard({"tool_name": tool, "cwd": str(cwd or own), "tool_input": tool_input}, root,
                             root.parent / "home", root.parent / "tmp")


def test_the_build_guard_keeps_a_task_in_its_clone(build_root, tmp_path):
    """SB1 item 3, THREAT-MODEL N5. Planted refusals: a Read of a project path, of the owner's home and of another
    clone, a Write into the venv, a Read of the runs, a shell command that names another clone or climbs out of the
    clone, a link from the clone to another clone. Passes: a Read and a Write in the own clone, a Read of the venv,
    the venv's interpreter in a shell command with 2>/dev/null, /tmp."""
    root = build_root
    own, other = root / "wt" / "B-001-1", root / "wt" / "B-002-1"
    project = tmp_path / "work" / "tcp-aaaa"
    project.mkdir(parents=True)
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    refused = [
        ("Read", {"file_path": str(project / "STATE.md")}, hooks.OUTSIDE_BUILD),
        ("Read", {"file_path": str(owner_home / ".ssh" / "id_ed25519")}, hooks.OUTSIDE_BUILD),
        ("Read", {"file_path": str(other / "CLAUDE.md")}, hooks.ANOTHER_CLONE),
        ("Write", {"file_path": str(root / "venv" / "bin" / "python"), "content": "x"}, hooks.BUILD_VENV),
        ("Read", {"file_path": str(root / "runs" / "B-001" / "1" / "result.md")}, hooks.OUTSIDE_BUILD),
        ("Glob", {"pattern": "*.md", "path": str(root / "wt")}, hooks.ANOTHER_CLONE),
        ("Bash", {"command": "cat ../B-002-1/CLAUDE.md"}, hooks.ANOTHER_CLONE),
        ("Bash", {"command": "cd .. && ls"}, hooks.ANOTHER_CLONE),
        ("Bash", {"command": "ls /etc/awb"}, hooks.OUTSIDE_BUILD),
        ("Grep", {"pattern": "x", "path": "/"}, hooks.ANOTHER_CLONE),
    ]
    for tool, tool_input, want in refused:
        assert _build(root, tool, tool_input) == want, (tool, tool_input)
    (own / "peek").symlink_to(other)
    assert _build(root, "Read", {"file_path": str(own / "peek" / "CLAUDE.md")}) == hooks.ANOTHER_CLONE
    passes = [
        ("Read", {"file_path": str(own / "CLAUDE.md")}),
        ("Write", {"file_path": "tests/test_new.py", "content": "x"}),
        ("Read", {"file_path": str(root / "venv" / "bin" / "python")}),
        ("Bash", {"command": "%s/bin/python -I -B -m pytest -q tests 2>/dev/null" % (root / "venv")}),
        ("Bash", {"command": "git log --oneline -3 && git status --porcelain"}),
        ("Grep", {"pattern": "def main", "path": "awb"}),
        ("Read", {"file_path": str(root.parent / "tmp" / "scratch.txt")}),
    ]
    for tool, tool_input in passes:
        assert _build(root, tool, tool_input) is None, (tool, tool_input)
    # a refusal names the class, never the path
    line = hooks.build_refusal(hooks.ANOTHER_CLONE)
    assert "another build clone" in line and str(root) not in line


def test_the_build_guard_refuses_every_call_without_a_clone(build_root, monkeypatch):
    """SB1 item 3: no clone folder means refused, fail closed. Planted: the client started in the build root, in wt/
    itself, outside the build root, and a clone that is a link; an input that is not an object; a broken root."""
    root = build_root
    own = root / "wt" / "B-001-1"
    read = {"file_path": str(own / "CLAUDE.md")}
    for started in (root, root / "wt", root.parent):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(started))
        assert _build(root, "Read", read, cwd=started) == hooks.NO_CLONE, started
    (root / "wt" / "B-003-1").symlink_to(own)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "wt" / "B-003-1"))
    assert _build(root, "Read", read, cwd=root / "wt" / "B-003-1") == hooks.NO_CLONE
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(own))
    assert hooks.build_guard({"tool_name": "Read", "cwd": str(own), "tool_input": "x"}, root) == hooks.NO_CLONE
    assert hooks.build_guard({"tool_name": "Read", "cwd": str(own), "tool_input": read}, object()) == hooks.NO_CLONE
    # a tool the guard does not see is not its business (the build settings deny the rest)
    assert _build(root, "TodoWrite", {"todos": []}) is None
    assert "no clone" in hooks.build_refusal(hooks.NO_CLONE)


def test_the_build_selftest_holds_the_n5_cases_and_fails_without_its_guard(monkeypatch):
    """SB1 item 3: `awb hook selftest` plants the three refusals of N5 as the build user. Planted: the place check
    removed, every planted refusal passes and the selftest reports each."""
    assert hooks.build_selftest() == []
    assert hooks.selftest() == []
    monkeypatch.setattr(hooks, "build_place_class", lambda *a, **k: None)
    problems = hooks.build_selftest()
    assert len(problems) == 5 and all(p.startswith("build case") for p in problems)
    assert hooks.selftest()[-5:] == problems


def test_the_build_selftest_fails_when_the_venv_interpreter_is_refused(monkeypatch):
    """B-001 follow-up 1: the host's venv python is a link to /usr/bin/python3.12 and the guard refused it as outside
    the clone. Planted: the old rule (no link may leave the venv); the selftest names the interpreter case."""
    monkeypatch.setattr(hooks, "_venv_interpreter", lambda *a: False)
    problems = hooks.build_selftest()
    assert len(problems) == 1 and "(Bash)" in problems[0] and "outside the build clone" in problems[0]


def test_the_build_guard_passes_the_venv_interpreter_link_and_no_other_link_out(build_root, tmp_path):
    """B-001 follow-up 1: the venv's python, python3 and python3.12 are links to the base interpreter pyvenv.cfg names;
    a Read and a run of them pass. Planted refusals: a Write through the link, the base interpreter by its own path, a
    link in the venv's bin to the owner's home, a link named python that leads elsewhere, the same link with no
    pyvenv.cfg, a Read of the runs and of main.git under the build root."""
    root = build_root
    venv, usr = root / "venv", tmp_path / "usr" / "bin"
    usr.mkdir(parents=True)
    (usr / "python3.12").write_bytes(b"")
    (usr / "python3").symlink_to("python3.12")
    (venv / "pyvenv.cfg").write_text("home = %s\ninclude-system-site-packages = true\nversion = 3.12.3\n"
                                     "executable = %s\n" % (usr, usr / "python3.12"), encoding="utf-8")
    (venv / "bin" / "python3").symlink_to(usr / "python3")
    (venv / "bin" / "python").symlink_to("python3")
    (venv / "bin" / "python3.12").symlink_to("python3")
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    (venv / "bin" / "notes").symlink_to(owner_home / "notes.md")
    (root / "main.git").mkdir()
    passes = [
        ("Bash", {"command": "%s/bin/python -I -B -m pytest -q tests/test_x.py" % venv}),
        ("Bash", {"command": "%s/bin/python3.12 -m pytest -q" % venv}),
        ("Read", {"file_path": str(venv / "bin" / "python3")}),
        ("Read", {"file_path": str(venv / "pyvenv.cfg")}),
    ]
    for tool, tool_input in passes:
        assert _build(root, tool, tool_input) is None, (tool, tool_input)
    refused = [
        ("Write", {"file_path": str(venv / "bin" / "python"), "content": "x"}, hooks.BUILD_VENV),
        ("Bash", {"command": "%s -c 1" % (usr / "python3.12")}, hooks.OUTSIDE_BUILD),
        ("Read", {"file_path": str(venv / "bin" / "notes")}, hooks.OUTSIDE_BUILD),
        ("Read", {"file_path": str(root / "runs" / "B-001" / "1" / "result.md")}, hooks.OUTSIDE_BUILD),
        ("Read", {"file_path": str(root / "main.git" / "HEAD")}, hooks.OUTSIDE_BUILD),
    ]
    for tool, tool_input, want in refused:
        assert _build(root, tool, tool_input) == want, (tool, tool_input)
    # a link named like the interpreter that leads elsewhere is refused
    (venv / "bin" / "python3").unlink()
    (venv / "bin" / "python3").symlink_to(owner_home / "notes.md")
    assert _build(root, "Read", {"file_path": str(venv / "bin" / "python")}) == hooks.OUTSIDE_BUILD
    # without its pyvenv.cfg no link leaves the venv
    (venv / "bin" / "python3").unlink()
    (venv / "bin" / "python3").symlink_to(usr / "python3")
    (venv / "pyvenv.cfg").unlink()
    assert _build(root, "Bash", {"command": "%s/bin/python -m pytest" % venv}) == hooks.OUTSIDE_BUILD


def _host_file(tmp_path, monkeypatch, text: str | None, mode: int = 0o644) -> Path:
    f = tmp_path / "etc-awb" / "paths.conf"
    f.parent.mkdir(exist_ok=True)
    if text is not None:
        f.write_text(text, encoding="utf-8")
        f.chmod(mode)
    monkeypatch.setattr(config, "HOST_CONF", str(f))
    monkeypatch.setattr(hooks, "HOST_FILE", f)
    return f


def test_the_build_user_is_known_by_the_host_file_or_by_its_home(tmp_path, monkeypatch):
    """SB1: the build user cannot read the host file (640 root:awb) and its task unit hides /etc/awb. Planted: a
    readable host file that names this user (build user) or another (not); a closed host file with this user's home
    inside the build root (build user) or elsewhere (not); no host file (not); root (never)."""
    if os.geteuid() == 0:
        pytest.skip("a closed file is open to root")
    import pwd

    real = pwd.getpwuid(os.geteuid())
    me = real.pw_name

    def home(path: str):
        entry = pwd.struct_passwd((me, "x", real.pw_uid, real.pw_gid, "", path, "/bin/bash"))
        monkeypatch.setattr(config.pwd, "getpwuid", lambda uid: entry)

    f = _host_file(tmp_path, monkeypatch, "work_user = awbwork\nbuild_user = %s\nbuild_root = /srv/x\n" % me)
    assert config.is_build_user() and config.build_user() == me and config.build_root() == Path("/srv/x")
    f.write_text("work_user = awbwork\nbuild_user = someone-else\n", encoding="utf-8")
    assert not config.is_build_user() and config.build_root() == Path(config.BUILD_ROOT)
    f.chmod(0)
    home(config.BUILD_ROOT + "/home")
    assert config.is_build_user() and config.build_user() is None and config.build_root() == Path(config.BUILD_ROOT)
    home(config.BUILD_ROOT)
    assert not config.is_build_user()
    home("/var/lib/elsewhere")
    assert not config.is_build_user()
    f.chmod(0o644)
    f.unlink()
    home(config.BUILD_ROOT + "/home")
    assert not config.is_build_user()
    monkeypatch.setattr(config.os, "geteuid", lambda: 0)
    assert not config.is_build_user()


def test_the_build_user_runs_three_hooks_and_never_the_name_check(build_root, tmp_path, monkeypatch, capsys):
    """SB1: the managed settings run all six hooks for every user; for the build user prompt, stop and session-start
    exit 0 without a word, pre-tool is the build guard and post-write checks secrets without the name check (the
    runner's gate does it with the register). Planted: a name check that fails if it is called; a written file with
    a secret blocks, a clean one passes; a Read of another clone through `awb hook pre-tool` is refused."""
    monkeypatch.setattr(config, "is_build_user", lambda: True)
    monkeypatch.setattr(config, "build_root", lambda: build_root)
    monkeypatch.setattr(hooks, "BUILD_TMP", str(tmp_path / "tmp"))
    monkeypatch.setattr(hooks, "file_hits", lambda *a, **k: pytest.fail("the name check ran for the build user"))
    monkeypatch.setattr(hooks, "name_hits", lambda *a, **k: pytest.fail("the name check ran for the build user"))
    own = build_root / "wt" / "B-001-1"
    for name in ("prompt", "stop", "session-start"):
        code, out, err = run_hook(name, {"prompt": "x", "cwd": str(own)}, monkeypatch, capsys)
        assert (code, out, err) == (0, "", ""), name
    code, _, err = run_hook("pre-tool", {"tool_name": "Read", "cwd": str(own),
                                         "tool_input": {"file_path": str(build_root / "wt" / "B-002-1" / "x")}},
                            monkeypatch, capsys)
    assert code == 2 and "another build clone" in err
    code, _, _ = run_hook("pre-tool", {"tool_name": "Read", "cwd": str(own),
                                       "tool_input": {"file_path": str(own / "CLAUDE.md")}}, monkeypatch, capsys)
    assert code == 0
    from awb import gate
    secret = own / "settings.py"
    secret.write_bytes(gate._selftest_cases()["secret"])
    code, _, err = run_hook("post-write", {"tool_name": "Write", "cwd": str(own),
                                          "tool_input": {"file_path": str(secret)}}, monkeypatch, capsys)
    assert code == 2 and "carries a secret" in err
    clean = own / "clean.py"
    clean.write_text("X = 1\n", encoding="utf-8")
    code, _, err = run_hook("post-write", {"tool_name": "Write", "cwd": str(own),
                                          "tool_input": {"file_path": str(clean)}}, monkeypatch, capsys)
    assert code == 0 and err == ""


def test_the_build_user_leaves_the_host_blocklist_to_the_runner(monkeypatch, tmp_path):
    """SB1: the host blocklist is 640 root:awb; the build user's gate reads none but the selftest's own (a read it
    may not do would refuse every commit of a task). Planted: the owner's view lists the host file."""
    from awb import gate
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    assert gate.BLOCKLIST_FILES[0] in gate.blocklist_files()
    monkeypatch.setattr(config, "is_build_user", lambda: True)
    assert gate.blocklist_files() == list(gate._SELFTEST_FILES)


def test_the_old_module_is_the_door_to_its_parts(monkeypatch):
    """F1 step 6: awb/hooks.py went into hooks, toolguard and receipt and awb/buildrun.py into five modules; the old
    module stays the door to its parts (awb/parts.py). Every old name is the part's object, a name set on the old
    module reaches the part that holds it (a test's stand-in is what the part calls) and the undo puts it back, also
    in a part loaded after the join. Planted: a part's name that the old module binds to another object of its own
    is left alone."""
    from awb import build_checks, build_selftest, buildrun, parts, toolguard
    from awb import receipt as receipt_part

    assert hooks.guard is toolguard.guard and hooks.receipt is receipt_part.receipt
    assert hooks.keys_call is receipt_part.keys_call and hooks.HOST_FILE is toolguard.HOST_FILE
    assert buildrun.World is build_selftest.World and buildrun.check_gate is build_checks.check_gate
    real, cases = toolguard.place_class, build_selftest.SELFTEST_CASES
    with monkeypatch.context() as m:
        m.setattr(hooks, "place_class", lambda *a: "planted")
        m.setattr(buildrun, "SELFTEST_CASES", [])
        assert toolguard.place_class() == "planted" and build_selftest.SELFTEST_CASES == []
    assert toolguard.place_class is real is hooks.place_class and build_selftest.SELFTEST_CASES is cases
    door, part, later = (types.ModuleType("awb_zz_door"), types.ModuleType("awb_zz_part"),
                         types.ModuleType("awb_zz_later"))
    shared, own, other = object(), object(), object()
    part.shared, part.own, door.own = shared, other, own
    for module in (door, part):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    parts.join("awb_zz_door", "awb_zz_part", "awb_zz_later")
    assert door.shared is shared and door.own is own
    later.shared = shared
    monkeypatch.setitem(sys.modules, "awb_zz_later", later)
    new = object()
    door.shared, door.own = new, new
    assert part.shared is new and later.shared is new and part.own is other


def test_one_walk_finds_the_project_root_for_the_four_finders(tmp_path, monkeypatch):
    """F1 step 7: the hooks, awb review, awb pricebook and awb write translate find a project's root through one
    walk, config.scope_root, and keep their own edges: without a SCOPE.md awb review answers the folder it started
    in and the others None; the hooks read at most 40 folders above the start, the commands every one; translate
    starts at the folder of the file. Planted: a folder that cannot be read ends the walk of the hooks and of
    translate with None, while awb review and awb pricebook raise as before."""
    from awb import review, toolguard, translate
    from awb.tcp import pricebook

    root = tmp_path / "tcp-q7m4"
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (root / "SCOPE.md").write_text("scope\n", encoding="utf-8")
    draft = deep / "draft.md"
    draft.write_text("x\n", encoding="utf-8")
    assert hooks.project_root is toolguard.project_root
    assert hooks.project_root(str(draft)) == hooks.project_root(deep) == config.scope_root(deep) == root
    assert review.find_project(deep) == pricebook.find_project(deep) == translate._project_root(draft) == root
    bare = tmp_path / "bare"
    bare.mkdir()
    assert review.find_project(bare) == bare and pricebook.find_project(bare) is None
    assert translate._project_root(bare / "x.md") is None and hooks.project_root(str(bare)) is None
    far = root.joinpath(*["d"] * 41)
    far.mkdir(parents=True)
    assert hooks.project_root(str(far)) is None and config.scope_root(far, depth=40) is None
    assert review.find_project(far) == pricebook.find_project(far) == config.scope_root(far, depth=41) == root
    monkeypatch.chdir(root / "a")
    assert hooks.project_root("b/draft.md") == root and review.find_project() == root
    real = Path.is_file

    def is_file(self):
        if self.parent == deep:
            raise PermissionError(13, "planted")
        return real(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert hooks.project_root(str(draft)) is None and translate._project_root(draft) is None
    for finder in (review.find_project, pricebook.find_project):
        with pytest.raises(PermissionError):
            finder(deep)
