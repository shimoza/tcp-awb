"""Tests of awb/hooks.py: the five client hooks with crafted hook input, against a throw-away Workbench.

The writing check, the review status, the knowledge base and the career log are other modules; the tests put
small fakes into sys.modules for them. None in their place shows that a missing module never blocks and never
crashes.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from awb import check, codes, hooks
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


def test_missing_vault_never_blocks_the_prompt(home, monkeypatch, capsys):
    shutil.rmtree(home.vault)
    prompt = "size the app clusters of %s" % fixtures.CUSTOMER_FORMS[0]
    code, out, err = run_hook("prompt", {"prompt": prompt}, monkeypatch, capsys)
    assert code == 0
    data = context_of(out, "UserPromptSubmit")
    assert "did not run" in data["hookSpecificOutput"]["additionalContext"]
    assert "not checked" in data["systemMessage"]
    assert_clean(out + err, "prompt hook output")


def test_locked_vault_never_blocks_the_prompt(home, monkeypatch, capsys):
    unavailable = getattr(check, "CheckUnavailable", None) or type("CheckUnavailable", (Exception,), {})
    monkeypatch.setattr(check, "CheckUnavailable", unavailable, raising=False)

    def locked(text, register_path):
        raise unavailable("name check unavailable: vault locked")

    monkeypatch.setattr(check, "check_text", locked)
    code, out, err = run_hook("prompt", {"prompt": "call " + fixtures.PERSON_FORMS[0]}, monkeypatch, capsys)
    assert code == 0
    assert "vault locked" in context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
    assert_clean(out + err, "prompt hook output")


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


def test_missing_check_module_never_blocks_the_prompt(home, monkeypatch, capsys):
    missing_module(monkeypatch, "check")
    code, out, _ = run_hook("prompt", {"prompt": "call " + fixtures.PERSON_FORMS[0]}, monkeypatch, capsys)
    assert code == 0
    assert "not installed" in context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]


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


def test_client_settings_carry_the_five_hooks():
    s = hooks.client_settings("/usr/local/bin/awb hook")
    assert set(s) == {"hooks"}
    got = {event: [h["command"] for e in entries for h in e["hooks"]] for event, entries in s["hooks"].items()}
    assert got == {
        "UserPromptSubmit": ["/usr/local/bin/awb hook prompt"],
        "PreToolUse": ["/usr/local/bin/awb hook pre-write"],
        "PostToolUse": ["/usr/local/bin/awb hook post-write"],
        "Stop": ["/usr/local/bin/awb hook stop"],
        "SessionStart": ["/usr/local/bin/awb hook session-start"],
    }
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert s["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"


def test_command_prefix_keeps_home_paths_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    inside = tmp_path / "userhome" / "tcp-awb" / ".venv" / "bin" / "python"
    assert hooks.command_prefix(inside) == '"$HOME/tcp-awb/.venv/bin/python" -m awb hook'
    assert hooks.command_prefix("/opt/tcp-awb/venv/bin/python") == "/opt/tcp-awb/venv/bin/python -m awb hook"
    spaced = tmp_path / "with space" / "python"
    assert hooks.command_prefix(spaced) == "'%s' -m awb hook" % spaced


# --------------------------------------------------------------------------- with the real modules (Release 2)


def test_a_vault_folder_without_a_register_warns_and_never_passes_names_quietly(home, monkeypatch, capsys):
    """The check treats a readable vault folder without a register as an empty register; the hooks must not
    call a name clean on that ground."""
    home.register.unlink()
    prompt = "size the app clusters of %s" % fixtures.CUSTOMER_FORMS[0]
    code, out, err = run_hook("prompt", {"prompt": prompt}, monkeypatch, capsys)
    assert code == 0
    assert "no register found" in context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
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
