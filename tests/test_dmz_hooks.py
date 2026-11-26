"""DMZ 2 (B-086, ask 2026-11-25): the session hooks check no content inside.

The checks stay at the entry of customer material (DMZ 1) and at the exit of the public repositories. The prompt hook
refuses a private key block and an access key pair only, because they would leave the host for the model. A file
written inside a project is not checked for names or secrets and not masked; the start and the compaction show the
project's files as written; the ledger draft of the stop hook keeps the commit subjects. Every name is an invented
fixture name; the keys are put together at run time.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

from awb import gate, hooks, ledger, projects
from tests import fixtures as fx
from tests.test_hooks import CODE, project, run_hook  # noqa: F401

NAME = fx.CUSTOMER_FORMS[0]
PUBLIC_IP = "203.0.113.45"
MAIL = "anna.sample@" + "qvelmar-demo.net"
PASSWORD_LINE = "db_pass" + "word = " + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRI" + "VATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END " + "OPENSSH PRI" + "VATE KEY-----"
AK = "AKIA" + "Q7M4X2P9" + "B6L3C8D1"
SK = "wJalrXUt" + "nFEMI/K7M" + "DENG/bPxR" + "fiCYQ7m4Z2pK9x"
KEY_PAIR = "access key: %s\nsecret key: %s\n" % (AK, SK)


def prompt(text: str, monkeypatch, capsys, **extra):
    return run_hook("prompt", dict({"prompt": text}, **extra), monkeypatch, capsys)


# --------------------------------------------------------------------------- the prompt hook


def test_the_keys_of_the_tests_are_what_the_gate_calls_a_key():
    assert gate._PRIVATE_KEY_RE.search(PRIVATE_KEY)
    assert gate._detect_keypair(KEY_PAIR)


def test_a_prompt_with_a_registered_name_an_ip_and_a_mail_passes(home, monkeypatch, capsys):
    text = "size the clusters of %s behind %s, mail %s, password %s" % (NAME, PUBLIC_IP, MAIL, PASSWORD_LINE)
    assert prompt(text, monkeypatch, capsys) == (0, "", "")


def test_a_pasted_block_and_the_events_with_names_and_data_pass(home, monkeypatch, capsys):
    body = "%s on %s, %s, https://gw.qvelmar-demo.net/api" % (NAME, PUBLIC_IP, MAIL)
    for text in ("<pasted_content>%s</pasted_content>" % body,
                 "<task-notification><task-id>a1b2c3d4</task-id><result>%s</result></task-notification>" % body,
                 "<ci-monitor-event>%s</ci-monitor-event>" % body):
        assert prompt(text, monkeypatch, capsys) == (0, "", ""), text


def test_a_prompt_with_a_private_key_block_is_refused_without_an_echo(home, monkeypatch, capsys):
    code, out, err = prompt("use this key:\n%s\nfor the host" % PRIVATE_KEY, monkeypatch, capsys)
    assert code == 2 and "private-key" in err and out == ""
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in err


def test_a_prompt_with_an_access_key_pair_is_refused_without_an_echo(home, monkeypatch, capsys):
    code, out, err = prompt("my keys\n%s\nplease test" % KEY_PAIR, monkeypatch, capsys)
    assert code == 2 and "access-key" in err and out == ""
    assert AK not in err and SK not in err


def test_a_key_in_a_paste_or_an_agent_result_is_refused_too(home, monkeypatch, capsys):
    code, _, err = prompt("<pasted_content>%s</pasted_content>" % PRIVATE_KEY, monkeypatch, capsys)
    assert code == 2 and "private-key" in err
    event = "<task-notification><task-id>a1b2c3d4</task-id><result>%s</result></task-notification>" % KEY_PAIR
    code, _, err = prompt(event, monkeypatch, capsys)
    assert code == 2 and "background agent a1b2c3d4" in err and AK not in err


def test_a_key_id_alone_and_a_password_alone_pass(home, monkeypatch, capsys):
    assert prompt("the key id is %s, ask for the secret later" % AK, monkeypatch, capsys) == (0, "", "")
    assert prompt(PASSWORD_LINE, monkeypatch, capsys) == (0, "", "")


def test_the_prompt_hook_never_reads_the_vault(home, monkeypatch, capsys):
    """A locked vault, no register and a broken name check change nothing for a prompt."""
    home.register.unlink()
    monkeypatch.setattr(hooks, "name_hits", lambda *a, **k: pytest.fail("the prompt hook asked the name check"))
    assert prompt("size the clusters of %s" % NAME, monkeypatch, capsys) == (0, "", "")


def test_a_key_check_that_cannot_run_never_refuses(home, monkeypatch, capsys):
    def broken(text):
        raise RuntimeError("no check")

    monkeypatch.setattr(gate, "_detect_keypair", broken)
    monkeypatch.setattr(gate, "_PRIVATE_KEY_RE", None)
    assert prompt("%s\n%s" % (PRIVATE_KEY, KEY_PAIR), monkeypatch, capsys) == (0, "", "")


# --------------------------------------------------------------------------- the write hook


def written(path: Path, text: str, monkeypatch, capsys, root: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(root)}
    return run_hook("post-write", payload, monkeypatch, capsys)


def test_a_project_file_with_a_name_and_a_password_is_written_unchanged(home, project, monkeypatch, capsys):
    text = "# Notes\n\ncall %s on %s\n%s\nmail %s\n" % (NAME, PUBLIC_IP, PASSWORD_LINE, MAIL)
    path = project / "evidence" / "notes.md"
    assert written(path, text, monkeypatch, capsys, project) == (0, "", "")
    assert path.read_text(encoding="utf-8") == text


def test_a_project_file_with_a_key_is_written_unchanged(home, project, monkeypatch, capsys):
    text = "%s\n%s\n" % (PRIVATE_KEY, KEY_PAIR)
    path = project / "notes.md"
    assert written(path, text, monkeypatch, capsys, project) == (0, "", "")
    assert path.read_text(encoding="utf-8") == text


def test_a_folder_named_with_a_name_in_a_project_is_no_refusal(home, project, monkeypatch, capsys):
    path = project / ("notes-" + NAME.replace(" ", "-")) / "a.md"
    assert written(path, "text\n", monkeypatch, capsys, project) == (0, "", "")


def test_a_project_file_still_refuses_a_bidi_override(home, project, monkeypatch, capsys):
    code, _, err = written(project / "a.md", "evil \u202e text\n", monkeypatch, capsys, project)
    assert code == 2 and "bidi override" in err


def test_a_deliverable_keeps_the_writing_check(home, project, monkeypatch, capsys):
    from tests.test_hooks import Tell, fake_module

    fake_module(monkeypatch, "writing", check_file=lambda path, mode="doc", scope="tcp": [
        Tell(line=3, cls="banned-word", blocking=True, hint="leverage")])
    code, _, err = written(project / "deliverables" / "plan.md", "# Plan\n\nwe leverage it\n", monkeypatch, capsys,
                           project)
    assert code == 2 and "banned-word" in err


def test_a_file_outside_a_project_keeps_its_checks(home, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "loose"
    code, _, err = written(folder / "a.md", "call %s\n" % NAME, monkeypatch, capsys, folder)
    assert code == 2 and "name check" in err
    code, _, err = written(folder / "b.md", "%s\n" % PASSWORD_LINE, monkeypatch, capsys, folder)
    assert code == 2 and "secret" in err


# --------------------------------------------------------------------------- the start and the compaction


STATE_TEXT = ("# State of tcp-q7m4\n\nStatus: the gateway at %s runs for %s.\nNext: mail %s about it.\n\n"
              "## Pinned\n\n- Limit: never touch the host db-02.internal.qvelmar-demo.net, user %s.\n\n"
              "## Now\n\n- %s\n" % (PUBLIC_IP, NAME, MAIL, NAME, PASSWORD_LINE))


def started(project: Path, monkeypatch, capsys, source: str = "startup") -> str:
    code, out, _ = run_hook("session-start", {"cwd": str(project), "session_id": "s-dmz2", "source": source},
                            monkeypatch, capsys)
    assert code == 0
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_state_with_a_public_ip_and_a_name_is_shown_at_start(home, project, monkeypatch, capsys):
    (project / "STATE.md").write_text(STATE_TEXT, encoding="utf-8")
    (project / "OPEN.md").write_text("# Open\n\n- ask %s for %s\n" % (NAME, MAIL), encoding="utf-8")
    scope = project / "SCOPE.md"
    scope.write_text(scope.read_text(encoding="utf-8") + "- note: %s runs at %s\n" % (NAME, PUBLIC_IP),
                     encoding="utf-8")
    context = started(project, monkeypatch, capsys)
    assert STATE_TEXT.strip().splitlines()[2] in context and PASSWORD_LINE in context
    assert "ask %s for %s" % (NAME, MAIL) in context
    assert "%s runs at %s" % (NAME, PUBLIC_IP) in context
    assert "withheld" not in context


def test_the_start_shows_a_folder_named_with_a_name_and_works_without_the_vault(home, monkeypatch, capsys):
    root = home.projects_root / ("tcp-" + NAME.replace(" ", "-").lower())
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope\n\n- code: x\n- goal: %s\n" % NAME, encoding="utf-8")
    home.register.unlink()
    context = started(root, monkeypatch, capsys)
    assert "Workbench project %s." % root.name in context and "locked" not in context
    assert "goal: %s" % NAME in context


def test_the_start_after_a_compaction_and_the_compaction_show_the_pinned_block_as_written(
        home, project, monkeypatch, capsys):
    (project / "STATE.md").write_text(STATE_TEXT, encoding="utf-8")
    context = started(project, monkeypatch, capsys, "compact")
    assert context.index("Pinned state of this project") < context.index("## SCOPE.md")
    code, out, _ = run_hook("compact", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 0
    for part in (PUBLIC_IP, NAME, "db-02.internal.qvelmar-demo.net"):
        assert part in out and part in context


def test_a_parked_reason_is_shown_as_written(home, monkeypatch):
    pr = projects.spawn(home, "engagement", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register,
                        tags=["cce"])
    projects.park(home, pr.code, "waits for the network team")
    with projects._locked(home):        # the owner's own command refuses a name in a reason; the start must not read it
        projects._park_write(home, pr.code, ("2026-11-20", "waits for %s" % NAME))
    line = hooks._parked_line(home, Path(pr.path))
    assert line is not None and "(waits for %s)" % NAME in line


# --------------------------------------------------------------------------- the ledger draft of the stop hook

NOW = datetime.datetime(2026, 11, 26, 15, 0, tzinfo=datetime.timezone.utc)


def commit(folder: Path, name: str, subject: str) -> None:
    (folder / name).write_text(subject + "\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(folder), "commit", "-q", "--no-verify", "-m", subject], check=True,
                   capture_output=True, env=env)


def test_the_draft_keeps_the_commit_subjects_as_written(home, monkeypatch):
    monkeypatch.setattr(hooks, "DRAFT_HOURS", 0)
    pr = projects.spawn(home, "engagement", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register,
                        tags=["cce"])
    folder = Path(pr.path)
    commit(folder, "a.md", "Read the guide at %s" % fx.LEDGER_LINK)
    commit(folder, "b.md", "call with %s on %s" % (fx.PERSON_FORMS[0], PUBLIC_IP))
    assert hooks.draft_ledger(home, folder, now=NOW) is True
    [draft] = [e for e in ledger.load(home) if e.project == pr.code and e.outcome == hooks.DRAFT_OUTCOME]
    assert "Read the guide at %s" % fx.LEDGER_LINK in draft.done
    assert "call with %s on %s" % (fx.PERSON_FORMS[0], PUBLIC_IP) in draft.done
    assert "(url)" not in draft.done
