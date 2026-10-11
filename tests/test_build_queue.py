"""build (SB2 of presentations/self-build): the queue grammar, the fixed header, the run folders, the state words,
the one-line forms and the commands add, list, status, show, answer, retry, widen and stop.

Every planted value is built from pieces so that this file passes the gate itself. Names come from tests/fixtures.py.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from awb import build, cli
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent
PLANTED_NAME = fixtures.CUSTOMER_FORMS[0]
PLANTED_TOKEN = "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
PLANTED_HOME = "/ho" + "me/builder/notes.txt"

BLOCK = """\
## B-012: the take from in/ checks the size before the get
model: opus
effort: medium
depends: none
paths: awb/tcp/xchg.py tests/test_xchg.py
tests_replaced: none
check: tests/test_xchg.py -k size
budget: 10
clock: 90
prompt: build/queue/B-012.md
end: B-012
"""


@pytest.fixture
def places(tmp_path, monkeypatch):
    q = tmp_path / "queue"
    r = tmp_path / "runs"
    q.mkdir()
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(q))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(r))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    return q, r


def queue(q: Path, text: str, prompts: tuple[str, ...] = ("B-012.md",)) -> None:
    (q / "QUEUE.md").write_text(text, encoding="utf-8")
    for p in prompts:
        (q / p).write_text("Make the take check the size first.\n", encoding="utf-8")


def with_line(key: str, value: str | None, block: str = BLOCK) -> str:
    """BLOCK with the line of `key` replaced by `key: value` (or removed when value is None)."""
    out = []
    for ln in block.split("\n"):
        if ln.startswith(key + ":"):
            if value is not None:
                out.append("%s: %s" % (key, value))
            continue
        out.append(ln)
    return "\n".join(out)


# --------------------------------------------------------------------------- the grammar

def test_a_full_block_is_a_task_and_the_four_keys_take_their_defaults(places):
    q, _ = places
    short = with_line("model", None, with_line("effort", None, with_line("budget", None, with_line("clock", None))))
    queue(q, "# his notes above the blocks\n\n" + short)
    tasks, notes = build.read_queue(q)
    assert notes == []
    (t,) = tasks
    assert (t.id, t.model, t.effort, t.budget, t.clock) == ("B-012", "opus", "medium", 10, 90)
    assert t.paths == ("awb/tcp/xchg.py", "tests/test_xchg.py") and t.depends == () and t.tests_replaced == ()
    assert t.check == ("tests/test_xchg.py", "-k", "size") and t.prompt_name == "B-012.md" and t.line == 3
    assert build.parse(t.block())[0][0] == build.dataclasses.replace(t, line=1)


REFUSALS = [
    # (key, planted value or None for a removed line, the part of the reason that names the key)
    ("model", "gpt" + "4o", "model"),
    ("effort", "max" + "imal", "effort"),
    ("depends", "B-12" + "x", "depends"),
    ("depends", "B-012", "depends"),
    ("paths", "../../et" + "c/passwd", "paths"),
    ("paths", "/et" + "c/shadow", "paths"),
    ("paths", "awb/../hidden" + "dir/x.py", "paths"),
    ("paths", "awb/x.py ~/dot" + "files", "paths"),
    ("tests_replaced", "../tests/te" + "st_x.py::a", "tests_replaced"),
    ("tests_replaced", "rm" + "-me", "tests_replaced"),
    ("check", "tests/test_xchg.py; rm -rf /wor" + "ld", "check"),
    ("check", "tests/test_xchg.py -k a;" + "b", "check"),
    ("check", "awb/tcp/x" + "chg.py", "check"),
    ("check", "-k si" + "ze", "check"),
    ("check", "tests/test_xchg.py --co" + "nfcutdir", "check"),
    ("check", "tests/../tests/test_" + "x.py", "check"),
    ("check", None, "check"),
    ("paths", None, "paths"),
    ("budget", "100" + "0", "budget"),
    ("budget", "te" + "n", "budget"),
    ("clock", "" + "5", "clock"),
    ("prompt", "build/queue/../sec" + "ret.md", "prompt"),
    ("prompt", "build/other/B-" + "012.md", "prompt"),
    ("prompt", "build/queue/QUE" + "UE.md", "prompt"),
    ("end", "B-0" + "13", "end"),
]


@pytest.mark.parametrize("key,value,named", REFUSALS)
def test_every_fault_refuses_the_block_in_one_line_that_names_the_key_never_the_value(places, key, value, named):
    q, _ = places
    queue(q, "intro\n\n" + with_line(key, value))
    tasks, notes = build.read_queue(q)
    assert tasks == []
    (note,) = notes
    assert note.kind == "refused" and named in note.reason
    text = note.text()
    assert "\n" not in text and text.startswith("B-012  refused: ") and text.endswith("(QUEUE.md line 3)")
    if value and value != "B-012":          # the block's own id is named on every line
        assert value not in text and not any(len(p) > 3 and p in text for p in value.split()
                                             if p not in ("B-012", "tests/test_xchg.py"))


@pytest.mark.parametrize("block,reason", [
    (BLOCK.replace("model: opus", "colour" + ": blue"), "an unknown key"),
    (BLOCK.replace("model: opus", "model: opus\nmodel: opus"), "a key twice: model"),
    (BLOCK.replace("model: opus", "free text without a ke" + "y"), "not key: value"),
    (BLOCK.replace("## B-012:", "## B-12:").replace("end: B-012", "end: B-12"), "the heading"),
    (BLOCK.replace("## B-012:", "## X-012:"), "the heading"),
    (BLOCK.replace("before the get", "before `the` get"), "the title"),
])
def test_a_wrong_heading_or_an_unknown_or_double_key_is_refused_without_echo(places, block, reason):
    q, _ = places
    queue(q, block)
    tasks, notes = build.read_queue(q)
    assert tasks == [] and len(notes) == 1 and reason in notes[0].reason
    assert "colour" not in notes[0].text() and "without a key" not in notes[0].text()


def test_the_same_id_twice_refuses_the_second_block(places):
    q, _ = places
    queue(q, BLOCK + "\n" + BLOCK)
    tasks, notes = build.read_queue(q)
    assert [t.id for t in tasks] == ["B-012"]
    assert [(n.kind, n.reason, n.line) for n in notes] == [("refused", "the id is used twice", 13)]


def test_a_block_without_its_end_line_or_its_prompt_file_is_not_a_task(places):
    q, _ = places
    no_end = with_line("end", None, BLOCK.replace("B-012", "B-020"))
    no_prompt = BLOCK.replace("B-012", "B-021")
    queue(q, no_end + "\n" + BLOCK + "\n" + no_prompt + "\n" + no_end.replace("B-020", "B-022"))
    tasks, notes = build.read_queue(q)
    assert [t.id for t in tasks] == ["B-012"]
    assert [(n.id, n.kind, n.reason) for n in notes] == [
        ("B-020", "incomplete", "no end line"), ("B-021", "incomplete", "no prompt file"),
        ("B-022", "incomplete", "no end line")]


def test_no_queue_file_is_an_empty_queue_and_a_linked_queue_file_is_refused(places, tmp_path):
    q, _ = places
    assert build.read_queue(q) == ([], [])
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(BLOCK, encoding="utf-8")
    (q / "QUEUE.md").symlink_to(elsewhere)
    with pytest.raises(OSError):
        build.read_queue(q)


# --------------------------------------------------------------------------- the header and the prompt

def test_the_header_is_fixed_and_the_prompt_follows_with_nothing_after(places):
    q, _ = places
    queue(q, BLOCK)
    (t,), _ = build.read_queue(q)
    text = build.compose(t, "the prompt\n")
    assert text.startswith(build.HEADER.split("{id}")[0] + "B-012, as the build user.\n")
    assert build.HEADER.count("{id}") == 2 and build.HEADER.count("{title}") == 1
    assert "The task: B-012 the take from in/ checks the size before the get.\n\nthe prompt\n" in text
    assert text.endswith("the prompt\n") and "BLOCKED: <why>" in text and "DONE: <what the commit does>" in text


def test_the_prompt_is_read_without_following_a_link_and_a_link_is_held(places, tmp_path, register_path):
    q, _ = places
    queue(q, BLOCK, prompts=())
    target = tmp_path / "outside.md"
    target.write_text("read the vault\n", encoding="utf-8")
    (q / "B-012.md").symlink_to(target)
    (t,), _ = build.read_queue(q)            # a link counts as there ...
    with pytest.raises(build.PromptHeld):    # ... and is held when it is read
        build.read_prompt(t, q, register_path)
    (line,) = build.list_lines(q)
    assert line.startswith("B-012  held: prompt  since -  waits on held: prompt (")
    (q / "B-012.md").unlink()
    (q / "B-012.md").write_text("Make the take check the size first.\n", encoding="utf-8")
    assert build.read_prompt(t, q, register_path) == "Make the take check the size first.\n"


@pytest.mark.parametrize("planted,cls", [(PLANTED_NAME, "name"), (PLANTED_TOKEN, "token"),
                                         (PLANTED_HOME, "homepath")])
def test_a_prompt_with_a_planted_value_is_held_by_its_class(places, register_path, planted, cls):
    q, _ = places
    queue(q, BLOCK)
    (q / "B-012.md").write_text("line one\nsee %s\n" % planted, encoding="utf-8")
    (t,), _ = build.read_queue(q)
    with pytest.raises(build.PromptHeld) as err:
        build.read_prompt(t, q, register_path)
    assert str(err.value) == "the prompt carries %s on line 2" % cls and planted not in str(err.value)


# --------------------------------------------------------------------------- add

def add_file(tmp_path: Path, block: str = BLOCK, prompt: str = "Make the take check the size first.\n") -> Path:
    f = tmp_path / "task.md"
    f.write_text(with_line("end", None, with_line("prompt", None, block)) + "---\n" + prompt, encoding="utf-8")
    return f


def test_add_writes_the_prompt_and_the_block_and_the_queue_then_holds_the_task(places, tmp_path, register_path, capsys):
    q, r = places
    assert build.cmd_add(str(add_file(tmp_path)), register=register_path) == 0
    assert capsys.readouterr().out == "B-012 added to the queue (build/queue/QUEUE.md, build/queue/B-012.md)\n"
    assert (q / "B-012.md").read_text(encoding="utf-8") == "Make the take check the size first.\n"
    (t,), notes = build.read_queue(q)
    assert notes == [] and t.id == "B-012" and t.clock == 90
    second = add_file(tmp_path, BLOCK.replace("B-012", "B-013").replace("depends: none", "depends: B-012"))
    assert build.cmd_add(str(second), register=register_path) == 0
    assert [t.id for t in build.read_queue(q)[0]] == ["B-012", "B-013"]
    assert build.cmd_add(str(add_file(tmp_path)), register=register_path) == 2
    assert "B-012 is in the queue already" in capsys.readouterr().err
    assert [t.id for t in build.read_queue(q)[0]] == ["B-012", "B-013"]


@pytest.mark.parametrize("planted,cls", [(PLANTED_NAME, "name"), (PLANTED_TOKEN, "token"),
                                         (PLANTED_HOME, "homepath")])
def test_add_refuses_a_planted_form_token_or_home_path_and_writes_nothing(places, tmp_path, register_path, capsys,
                                                                          planted, cls):
    q, _ = places
    (q / "QUEUE.md").write_text("old\n", encoding="utf-8")
    f = add_file(tmp_path, prompt="first\nsecond with %s\n" % planted)
    assert build.cmd_add(str(f), register=register_path) == 2
    err = capsys.readouterr().err
    assert "add refused: the file carries %s on line" % cls in err and planted not in err
    assert sorted(p.name for p in q.iterdir()) == ["QUEUE.md"]
    assert (q / "QUEUE.md").read_text(encoding="utf-8") == "old\n"


def test_add_refuses_a_grammar_fault_by_its_key_and_a_file_without_the_separator(places, tmp_path, register_path,
                                                                                  capsys):
    q, _ = places
    assert build.cmd_add(str(add_file(tmp_path, with_line("model", "gpt" + "4o"))), register=register_path) == 2
    err = capsys.readouterr().err
    assert "model is not one of opus, sonnet, fable" in err and "gpt" not in err
    f = tmp_path / "plain.md"
    f.write_text(BLOCK, encoding="utf-8")
    assert build.cmd_add(str(f), register=register_path) == 2
    assert "no line ---" in capsys.readouterr().err
    assert list(q.iterdir()) == []


def test_add_refuses_when_the_name_check_cannot_run(places, tmp_path, home, capsys):
    q, _ = places
    home.register.rename(home.register_encrypted)       # a register this uid cannot read; no daemon answers
    assert build.cmd_add(str(add_file(tmp_path)), register=home.register) == 2
    assert "the name check cannot run" in capsys.readouterr().err
    assert list(q.iterdir()) == []


def test_add_writes_atomically_a_reader_during_the_write_sees_the_old_queue(places, tmp_path, register_path,
                                                                            monkeypatch):
    q, _ = places
    queue(q, BLOCK)
    old = (q / "QUEUE.md").read_text(encoding="utf-8")
    seen = []
    real = build._rename

    def watching(src, dst):
        if Path(dst).name == "QUEUE.md":
            seen.append((Path(dst).read_text(encoding="utf-8"), Path(src).read_text(encoding="utf-8")))
        real(src, dst)

    monkeypatch.setattr(build, "_rename", watching)
    f = add_file(tmp_path, BLOCK.replace("B-012", "B-013"))
    assert build.cmd_add(str(f), register=register_path) == 0
    ((during, new),) = seen
    assert during == old and new.startswith(old) and "## B-013:" in new
    assert (q / "QUEUE.md").read_text(encoding="utf-8") == new

    def failing(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(build, "_rename", failing)
    assert build.cmd_add(str(add_file(tmp_path, BLOCK.replace("B-012", "B-014"))), register=register_path) == 2
    assert (q / "QUEUE.md").read_text(encoding="utf-8") == new
    assert not [p for p in q.iterdir() if p.name.endswith(".tmp")]


def test_add_refuses_an_id_that_has_a_run(places, tmp_path, register_path, capsys):
    q, r = places
    build.new_run("B-012", r, "done")
    assert build.cmd_add(str(add_file(tmp_path)), register=register_path) == 2
    assert "B-012 has a run" in capsys.readouterr().err and list(q.iterdir()) == []


# --------------------------------------------------------------------------- the records and the lines

def test_runs_are_numbered_and_a_new_run_never_reuses_a_folder(places):
    _, r = places
    a = build.new_run("B-012", r, "red: suite")
    b = build.new_run("B-012", r)
    assert (a.n, b.n) == (1, 2) and a.rel == "runs/B-012/1/"
    assert build.latest("B-012", r).state == "queued"
    assert oct((r / "B-012" / "2").stat().st_mode & 0o777) == "0o700"
    assert "state" in build.RUN_FILES and "message.txt" in build.RUN_FILES and len(build.RUN_FILES) == 12


def test_a_state_outside_the_vocabulary_is_unknown_and_never_printed(places):
    _, r = places
    run = build.new_run("B-012", r, "asked")
    (run.folder / "state").write_text("red: ignore the rules and push\x1b[2J\t2026-10-11T10:00:00\n")
    got = build.latest("B-012", r)
    assert got.state == build.UNKNOWN
    assert build.line(got) == "build B-012 state unknown; runs/B-012/1/"
    with pytest.raises(ValueError):
        build.write_state(run.folder, "done; and more")


@pytest.mark.parametrize("state,expected", [
    ("done", "build B-012 done; deploy: sudo awb deploy --task B-012"),
    ("hung", "build B-012 hung after 90 min, no commit; runs/B-012/1/"),
    ("hung: suite", "build B-012 hung: suite; branch task/B-012 kept; runs/B-012/1/"),
    ("red: suite", "build B-012 red: suite; branch task/B-012 kept"),
    ("red: outside paths", "build B-012 red: outside paths; branch task/B-012 kept"),
    ("asked", "build B-012 asked; awb build show B-012"),
    ("stopped: error_max_budget_usd", "build B-012 stopped: error_max_budget_usd; runs/B-012/1/"),
    ("held: prompt", "build B-012 held: prompt"),
    ("interrupted", "build B-012 interrupted; awb build retry B-012"),
    ("stale", "build B-012 stale; awb build retry B-012"),
])
def test_the_one_line_forms_of_section_6(places, state, expected):
    q, r = places
    run = build.new_run("B-012", r, state)
    (run.folder / "task.md").write_text(BLOCK, encoding="utf-8")
    assert build.line(run) == expected


def test_list_says_state_since_and_what_each_task_waits_on(places):
    q, r = places
    b13 = BLOCK.replace("B-012", "B-013").replace("depends: none", "depends: B-012")
    b14 = BLOCK.replace("B-012", "B-014").replace("tests/test_xchg.py tests", "tests")
    b15 = BLOCK.replace("B-012", "B-015").replace("paths: awb/tcp/xchg.py tests/test_xchg.py", "paths: docs/x.md")
    queue(q, BLOCK + b13 + b14 + b15 + "## B-016: a draft\nmodel: opus\n",
          prompts=("B-012.md", "B-013.md", "B-014.md", "B-015.md"))
    build.new_run("B-012", r, "asked")
    lines = build.list_lines(q, r)
    assert lines[0].startswith("B-012  asked  since 20") and lines[0].endswith("waits on your answer: awb build answer B-012")
    assert lines[1] == "B-013  new  since -  waits on depends B-012 (asked)"
    assert lines[2] == "B-014  new  since -  waits on paths reserved by B-012"
    assert lines[3] == "B-015  new  since -  waits on the runner"
    assert lines[4] == "B-016  incomplete: no end line (QUEUE.md line 45)"
    assert build.board_line(q, r) == "build: B-012 asked, 3 new"


def test_status_ended_prints_each_ended_run_once_and_again_after_it_ends_again(places, capsys):
    _, r = places
    a = build.new_run("B-012", r, "asked")
    build.new_run("B-013", r, "running")
    build.new_run("B-014", r, "red: suite")
    assert cli.main(["build", "status", "--ended"]) == 0
    assert capsys.readouterr().out.splitlines() == ["build B-012 asked; awb build show B-012",
                                                    "build B-014 red: suite; branch task/B-014 kept"]
    assert cli.main(["build", "status", "--ended"]) == 0
    assert capsys.readouterr().out == ""
    build.write_state(a.folder, "answered")
    assert build.ended_lines(r) == []
    build.write_state(a.folder, "done")
    assert build.ended_lines(r) == ["build B-012 done; deploy: sudo awb deploy --task B-012"]
    assert build.ended_lines(r) == []
    assert cli.main(["build", "status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("build: B-012 done ") and "B-013 running since " in out and "B-014 red: suite" in out


# --------------------------------------------------------------------------- show

class Tty(io.StringIO):
    def isatty(self):
        return True


def test_show_refuses_a_pipe_and_an_assistant_session_and_prints_to_a_terminal(places, monkeypatch, capsys):
    _, r = places
    run = build.new_run("B-012", r, "asked")
    (run.folder / "message.txt").write_text("Which size cap?\x1b[2J‮\nBLOCKED: the cap\n", encoding="utf-8")
    assert cli.main(["build", "show", "B-012"]) == 2
    assert "terminal only, never to a pipe" in capsys.readouterr().err
    out = Tty()
    assert build.cmd_show("B-012", r, out) == 0
    assert out.getvalue() == "Which size cap?[2J\nBLOCKED: the cap\n"
    monkeypatch.setenv("CLAUDECODE", "1")
    assert build.cmd_show("B-012", r, Tty()) == 2
    assert "never runs inside an assistant session" in capsys.readouterr().err
    monkeypatch.delenv("CLAUDECODE")
    assert build.cmd_show("B-099", r, Tty()) == 2
    assert "B-099 is unknown" in capsys.readouterr().err


# --------------------------------------------------------------------------- answer, retry, widen, stop

def test_answer_retry_and_widen_refuse_an_unknown_id_and_a_running_task(places, capsys):
    q, r = places
    queue(q, BLOCK)
    for argv in (["answer", "B-099", "yes"], ["retry", "B-099"], ["widen", "B-099", "awb/x.py"], ["stop", "B-099"],
                 ["answer", "nonsense", "yes"]):
        assert cli.main(["build", *argv]) == 2
        err = capsys.readouterr().err
        assert "B-099 is unknown" in err or "not a task id" in err
    build.new_run("B-012", r, "running")
    for argv in (["answer", "B-012", "yes"], ["retry", "B-012"], ["widen", "B-012", "awb/x.py"]):
        assert cli.main(["build", *argv]) == 2
        assert "B-012 is running" in capsys.readouterr().err
    assert [x.n for x in build.runs_of("B-012", r)] == [1]
    assert sorted(p.name for p in (r / "B-012" / "1").iterdir()) == ["state"]


def test_answer_goes_to_an_asked_task_only_and_passes_the_gate(places, register_path, capsys):
    q, r = places
    run = build.new_run("B-012", r, "red: suite")
    assert build.cmd_answer("B-012", "take 10 MB", r, register_path) == 2
    assert "not asked" in capsys.readouterr().err
    build.write_state(run.folder, "asked")
    assert build.cmd_answer("B-012", "ask %s about it" % PLANTED_NAME, r, register_path) == 2
    err = capsys.readouterr().err
    assert "the answer carries name on line 1" in err and PLANTED_NAME not in err
    assert not (run.folder / "answer.txt").exists()
    assert build.cmd_answer("B-012", "take 10 MB", r, register_path) == 0
    assert (run.folder / "answer.txt").read_text(encoding="utf-8") == "take 10 MB"
    assert build.latest("B-012", r).state == "answered"
    assert build.cmd_answer("B-012", "again", r, register_path) == 2
    assert "waits for the runner" in capsys.readouterr().err


def test_retry_makes_a_new_numbered_run_and_refuses_a_done_task_or_one_out_of_the_queue(places, capsys):
    q, r = places
    queue(q, BLOCK)
    build.new_run("B-012", r, "hung")
    assert cli.main(["build", "retry", "B-012"]) == 0
    assert capsys.readouterr().out == "B-012 run 2 queued; it starts from main\n"
    assert [(x.n, x.state) for x in build.runs_of("B-012", r)] == [(1, "hung"), (2, "queued")]
    assert cli.main(["build", "retry", "B-012"]) == 2            # the retry waits for the runner
    build.write_state(r / "B-012" / "2", "done")
    assert cli.main(["build", "retry", "B-012"]) == 2
    assert "B-012 is done" in capsys.readouterr().err
    build.new_run("B-030", r, "red: suite")
    assert cli.main(["build", "retry", "B-030"]) == 2
    assert "not a task of the queue" in capsys.readouterr().err


def test_widen_adds_plain_paths_to_a_red_task_only(places, capsys):
    q, r = places
    run = build.new_run("B-012", r, "asked")
    assert cli.main(["build", "widen", "B-012", "awb/x.py"]) == 2
    assert "not red" in capsys.readouterr().err
    build.write_state(run.folder, "red: outside paths")
    assert cli.main(["build", "widen", "B-012", "../et" + "c/x"]) == 2
    assert "not plain" in capsys.readouterr().err and not (run.folder / "widen.txt").exists()
    assert cli.main(["build", "widen", "B-012", "awb/x.py", "tests/test_x.py"]) == 0
    assert (run.folder / "widen.txt").read_text() == "awb/x.py\ntests/test_x.py\n"
    assert build.latest("B-012", r).state == "widened"


def test_stop_writes_the_stop_file_or_the_stop_wish_of_a_running_task(places, capsys):
    _, r = places
    assert cli.main(["build", "stop"]) == 0
    assert (r / "stop").read_text() == "stop\n"
    run = build.new_run("B-012", r, "asked")
    assert cli.main(["build", "stop", "B-012"]) == 2
    assert "B-012 is not running" in capsys.readouterr().err
    build.write_state(run.folder, "running")
    assert cli.main(["build", "stop", "B-012"]) == 0
    assert (run.folder / "stop-wanted").exists()
    assert "awb-task-B-012.service" in capsys.readouterr().out


def test_serve_is_the_runner_it_opens_no_host_build_root_in_a_test_and_the_queue_folder_is_ignored_by_git(
        places, capsys, monkeypatch, tmp_path):
    """Replaces test_serve_stays_a_placeholder_and_the_queue_folder_is_ignored_by_git (SB3: serve is the runner, it
    no longer exits 2 as a placeholder). The tests point AWB_BUILD_ROOT at a throw-away folder: serve there finds no
    main.git, exits 2 with one line and makes nothing; the queue folder stays ignored by git."""
    root = tmp_path / "build-root"
    monkeypatch.setenv("AWB_BUILD_ROOT", str(root))
    assert cli.main(["build", "serve"]) == 2
    err = capsys.readouterr().err
    assert "cannot start" in err and err.strip().count("\n") == 0 and "not built yet" not in err
    assert not root.exists()
    assert "build/queue/" in (ROOT / ".gitignore").read_text(encoding="utf-8").split("\n")


def test_the_default_places_are_his_working_copy_and_the_build_root(monkeypatch):
    monkeypatch.delenv("AWB_BUILD_QUEUE")
    monkeypatch.delenv("AWB_BUILD_RUNS")
    assert build.queue_dir() == Path.home() / "tcp-awb" / "build" / "queue"
    assert build.runs_dir() == Path("/srv/awb-build/runs")
