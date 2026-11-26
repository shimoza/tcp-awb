"""B-096, lesson producers L2 L3 L4 (awb/lessons.py): the stop hook asks for lesson cards once per batch after a step
that failed and then worked, a reverted step or a new decision in STATE.md; a compaction makes the next prompt ask
once; `awb lessons distill CODE` reads the transcripts of its own project only, writes a digest that passes the
screens of `kb add` and starts the side session lessons; `awb close` runs the distill before the close. The
transcripts are stand-ins in a home of the test, the names come from tests/fixtures.py."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from awb import cli, codes, config, hooks, kb, lessons, projects, side
from tests import fixtures as fx
from tests.test_lifecycle import spawn


def tool(cmd: str, tid: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}})


def result(tid: str, text: str = "ok", error: bool = False) -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": text}]}})


def say(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def transcript(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def fail_then_work(prefix: str, error: str = "Error: the flavor is not offered in this zone") -> list[str]:
    return [tool("terraform apply -auto-approve", prefix + "1"), result(prefix + "1", "Exit code 1\n" + error, True),
            tool("terraform apply -auto-approve", prefix + "2"), result(prefix + "2", "Apply complete")]


def guarded(monkeypatch):
    monkeypatch.setattr(hooks, "for_this_user", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)


def run_hook(name, payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    code = hooks.main([name])
    out = capsys.readouterr()
    return code, out.out, out.err


# --------------------------------------------------------------------------- L2


def test_a_failure_then_a_success_makes_the_stop_hook_ask_once_per_batch(home, register_path, tmp_path,
                                                                          monkeypatch, capsys):
    guarded(monkeypatch)
    pr = spawn(home, register_path)
    t = transcript(tmp_path / "t.jsonl", *fail_then_work("a"), say("it works in the other zone"))
    payload = {"cwd": pr.path, "transcript_path": str(t), "session_id": "l2-s1"}
    code, _, err = run_hook("stop", payload, monkeypatch, capsys)
    assert code == hooks.BLOCK and "Lessons before you stop" in err and "fail and then work" in err
    assert "awb kb pattern add --kind lesson" in err and "nothing" in err
    code, _, err = run_hook("stop", payload, monkeypatch, capsys)
    assert code == hooks.OK and "Lessons" not in err                # once, never in a loop
    transcript(t, say("nothing to learn"))
    assert run_hook("stop", payload, monkeypatch, capsys)[0] == hooks.OK
    transcript(t, *fail_then_work("b"))
    code, _, err = run_hook("stop", payload, monkeypatch, capsys)
    assert code == hooks.BLOCK and "Lessons before you stop" in err  # a new batch asks again


def test_no_failure_no_question_and_a_lesson_card_answers(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    root = Path(pr.path)
    t = transcript(tmp_path / "t.jsonl", tool("terraform apply", "a1"), result("a1"),
                   tool("terraform plan", "a2"), result("a2", "Exit code 1\nError", True))
    assert lessons.due(home, t, "l2-s2", root) is None             # a failure alone or a success alone is no lesson
    t2 = transcript(tmp_path / "t2.jsonl", *fail_then_work("c"),
                    tool('awb kb pattern add --kind lesson --sub working-path --package lessons --tag ecs "..."', "c3"),
                    result("c3"))
    assert lessons.due(home, t2, "l2-s3", root) is None             # a lesson card after the step answers it


def test_a_reverted_step_and_a_new_decision_ask(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    root = Path(pr.path)
    t = transcript(tmp_path / "t.jsonl", tool("git revert --no-edit HEAD", "r1"), result("r1"))
    assert "reverted a step" in lessons.due(home, t, "l2-s4", root)
    t2 = transcript(tmp_path / "t2.jsonl", say("hello"))
    assert lessons.due(home, t2, "l2-s5", root) is None            # the decisions as they stand are taken in
    with open(root / "STATE.md", "a", encoding="utf-8") as f:
        f.write("- Use the managed database instead of a self-built cluster.\n")
    transcript(t2, say("decided"))
    assert "new decision" in lessons.due(home, t2, "l2-s5", root)
    transcript(t2, say("go on"))
    assert lessons.due(home, t2, "l2-s5", root) is None


def test_a_side_session_is_never_asked(home, register_path, tmp_path, monkeypatch, capsys):
    guarded(monkeypatch)
    pr = spawn(home, register_path)
    monkeypatch.setattr(side, "is_side_worktree", lambda root: True)
    t = transcript(tmp_path / "t.jsonl", *fail_then_work("d"))
    code, _, err = run_hook("stop", {"cwd": pr.path, "transcript_path": str(t), "session_id": "l2-s6"},
                            monkeypatch, capsys)
    assert "Lessons" not in err


# --------------------------------------------------------------------------- L3


def test_a_pre_compact_mark_makes_the_next_prompt_ask_once(home, register_path, monkeypatch, capsys):
    guarded(monkeypatch)
    pr = spawn(home, register_path)
    sid = "l3-s1"
    code, _, _ = run_hook("prompt", {"cwd": pr.path, "session_id": sid, "prompt": "go on"}, monkeypatch, capsys)
    assert code == hooks.OK
    assert run_hook("compact", {"cwd": pr.path, "session_id": sid}, monkeypatch, capsys)[0] == hooks.OK
    assert (home.shared / "sessions" / ("lessons-compact-%s" % sid)).exists()
    code, out, _ = run_hook("prompt", {"cwd": pr.path, "session_id": sid, "prompt": "go on"}, monkeypatch, capsys)
    assert code == hooks.OK and out.count("Lessons after the compaction") == 1
    assert "awb kb pattern add --kind lesson" in out
    code, out, _ = run_hook("prompt", {"cwd": pr.path, "session_id": sid, "prompt": "and then?"}, monkeypatch, capsys)
    assert code == hooks.OK and "Lessons after the compaction" not in out
    # another session of the project is not asked for this one's compaction
    run_hook("compact", {"cwd": pr.path, "session_id": sid}, monkeypatch, capsys)
    _, out, _ = run_hook("prompt", {"cwd": pr.path, "session_id": "l3-s2", "prompt": "go on"}, monkeypatch, capsys)
    assert "Lessons after the compaction" not in out


def test_a_notification_leaves_the_question_for_the_next_prompt(home, register_path, monkeypatch, capsys):
    guarded(monkeypatch)
    pr = spawn(home, register_path)
    run_hook("compact", {"cwd": pr.path, "session_id": "l3-s3"}, monkeypatch, capsys)
    _, out, _ = run_hook("prompt", {"cwd": pr.path, "session_id": "l3-s3",
                                    "prompt": "<task-notification>done</task-notification>"}, monkeypatch, capsys)
    assert "Lessons" not in out
    _, out, _ = run_hook("prompt", {"cwd": pr.path, "session_id": "l3-s3", "prompt": "go on"}, monkeypatch, capsys)
    assert "Lessons after the compaction" in out


# --------------------------------------------------------------------------- L4


def world(home, register_path, tmp_path, monkeypatch):
    """Two projects and a work user's home with transcripts of both, of a side session and of a subagent."""
    pr = spawn(home, register_path)
    other = spawn(home, register_path)
    work = tmp_path / "work-home"
    base = work / ".claude" / "projects"
    own = base / lessons.encoded(Path(pr.path))
    secret = "".join(("Zq8", "XwP4", "mN2r", "Lk7v", "Tb5y"))
    home_path = "/".join(("", "ho" + "me", "worker", "keys.txt"))
    transcript(own / "s1.jsonl",
               tool("awb cloud call GET ecs /v1/x --tenant test-1", "e1"),
               result("e1", "Exit code 1\nError 403: the project %s of %s may not call it, see %s, key %s"
                      % (other.code, fx.CUSTOMER_FORMS[0], home_path, secret), True),
               tool("awb cloud call GET ecs /v1/x --tenant test-1 --role lab", "e2"), result("e2"),
               say("The call needs the lab role. %s confirmed it for %s, billed 4200 EUR per month."
                   % (fx.PERSON_FORMS[0], pr.code)))
    transcript(Path(str(own) + "--side-report") / "s2.jsonl", *fail_then_work("f"),
               say("The second zone takes the flavor."))
    transcript(own / "s1" / "subagents" / "agent-1.jsonl", *fail_then_work("g"), say("subagent words"))
    transcript(base / lessons.encoded(Path(other.path)) / "s3.jsonl",
               tool("make build", "o1"), result("o1", "Exit code 2\nError: pineapple", True),
               tool("make build", "o2"), result("o2"), say("pineapple orchard"))
    with open(Path(pr.path) / "STATE.md", "a", encoding="utf-8") as f:
        f.write("- Keep the database managed, decided with %s.\n" % fx.PERSON_FORMS[1])
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setenv("HOME", str(work))
    started = []
    monkeypatch.setattr(side, "start", lambda name, brief, model, effort, cwd=None:
                        started.append((name, brief, model, effort, Path(cwd).name)) or "side session %s started" % name)
    opened = []
    real_open = lessons._open_transcript
    monkeypatch.setattr(lessons, "_open_transcript", lambda path: opened.append(Path(path)) or real_open(path))
    monkeypatch.chdir(pr.path)
    return pr, other, own, started, opened, (secret, home_path)


def test_distill_reads_its_own_project_only_and_the_digest_passes_kb_add(home, register_path, tmp_path,
                                                                          monkeypatch, capsys):
    pr, other, own, started, opened, (secret, home_path) = world(home, register_path, tmp_path, monkeypatch)
    assert cli.main(["lessons", "distill", pr.code]) == 0
    out = capsys.readouterr().out
    assert "digest of" in out and "side session lessons started" in out
    assert started == [("lessons", lessons.DIGEST, lessons.SIDE_MODEL, lessons.SIDE_EFFORT, pr.code)]
    assert opened and all(f.parent.name.startswith(own.name) and f.parent.parent == own.parent for f in opened)
    assert not any("subagents" in f.parts for f in opened)
    digest = (Path(pr.path) / lessons.DIGEST).read_text(encoding="utf-8")
    assert "failed, then worked" in digest and "lab role" in digest and "second zone" in digest
    assert "decision" in digest.lower() and "managed" in digest
    assert "pineapple" not in digest and "subagent" not in digest           # P6: no other transcript
    for name in fx.ALL_REGISTERED:
        assert name not in digest
    assert other.code not in digest and pr.code not in digest and fx.CUSTOMER_CODE not in digest
    assert sum(codes.count_codes(digest)) == 0
    assert secret not in digest and home_path not in digest and "4200" not in digest and "EUR" not in digest
    assert kb.text_problems("the digest", digest, register_path) == []
    for line in digest.splitlines():
        assert kb.text_problems("a line", line, register_path) == []
    # nothing new: no digest run and no side session
    assert cli.main(["lessons", "distill", pr.code]) == 0
    assert "nothing new" in capsys.readouterr().out and len(started) == 1
    transcript(own / "s1.jsonl", *fail_then_work("h"))
    assert cli.main(["lessons", "distill", pr.code]) == 0
    assert len(started) == 2


def test_distill_runs_as_the_work_user_in_its_own_project(home, register_path, tmp_path, monkeypatch, capsys):
    pr, other, *_ = world(home, register_path, tmp_path, monkeypatch)
    assert cli.main(["lessons", "distill", other.code]) == 2                 # the folder of another project
    assert "in the folder of that project" in capsys.readouterr().err
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert cli.main(["lessons", "distill", pr.code]) == 2
    assert "as the work user" in capsys.readouterr().err
    assert not (Path(pr.path) / lessons.DIGEST).exists()


def test_close_calls_distill_before_the_close(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path)
    calls = []

    def distill(p, code, cwd=None, home=None):
        calls.append((code, Path(cwd).name, {r.code: r.state for r in projects.load(p)}[code]))
        return ["digest of 1 episode(s)"]

    monkeypatch.setattr(lessons, "distill", distill)
    assert cli.main(["close", pr.code]) == 0
    assert calls == [(pr.code, pr.code, "active")]                          # before the close
    assert "digest of 1 episode(s)" in capsys.readouterr().out
    assert cli.main(["close", pr.code]) == 0                                # closed already: not again
    assert len(calls) == 1


def test_a_distill_that_cannot_run_never_stops_the_close(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert cli.main(["close", pr.code]) == 0
    out = capsys.readouterr().out
    assert "lessons not distilled" in out and "closed %s" % pr.code in out
    assert {r.code: r.state for r in projects.load(home)}[pr.code] == "closed"
