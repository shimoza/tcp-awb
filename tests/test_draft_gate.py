"""The draft gate of the stop hook: a reply that shows him a draft under deliverables/ or a mail draft is held
until `awb write check` of that file ran clean in the same turn. One line names the file. A draft in Russian is
exempt. The gate fails closed.

Every planted text is invented. The transcripts are built in the shape the client writes (JSON lines with user and
assistant entries, tool_use and tool_result blocks).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import hooks
from tests import fixtures
from tests.test_hooks import CODE, assert_clean, fake_review, missing_module, project, run_hook  # noqa: F401

CLEAN = "# Offer\n\nI checked the quota in eu-de. The backup costs 120 EUR per month.\n"
DIRTY = "# Offer\n\nWe checked the quota in eu-de. The backup is a game changer.\n"
RUSSIAN = "# Предложение\n\nЯ проверил квоту в eu-de. Мы считаем, что бэкап стоит 120 EUR в месяц.\n"
MAIL = "Hello,\n\nI checked the quota in eu-de. It is 20 vCPU.\n\nBest regards\n"
CLEAN_OUT = "metrics:\n  sentences  2\nresult: 0 blocking, 1 reported\n"
DIRTY_OUT = "we-in-doc  3  a text in his name speaks as I\nresult: 2 blocking, 0 reported\n"


class Transcript:
    """A transcript of one session: his prompt, then the assistant's tool calls and reply."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = []
        self.n = 0

    def prompt(self, text: str) -> "Transcript":
        self.entries.append({"type": "user", "message": {"role": "user", "content": text}})
        return self

    def _tool(self, name: str, args: dict, out: str, error: bool = False) -> "Transcript":
        self.n += 1
        tid = "toolu_%d" % self.n
        self.entries.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": args}]}})
        self.entries.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": error}]}})
        return self

    def write(self, path: Path, text: str) -> "Transcript":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self._tool("Write", {"file_path": str(path), "content": text}, "File created")

    def bash(self, command: str, out: str = "", error: bool = False) -> "Transcript":
        return self._tool("Bash", {"command": command}, out, error)

    def check(self, rel: str, clean: bool = True, mode: str = "") -> "Transcript":
        cmd = "awb write check %s%s" % (rel, " --mode %s" % mode if mode else "")
        return self.bash(cmd, CLEAN_OUT if clean else DIRTY_OUT, error=not clean)

    def reply(self, text: str) -> Path:
        self.entries.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}})
        self.path.write_text("".join(json.dumps(e) + "\n" for e in self.entries), encoding="utf-8")
        return self.path


@pytest.fixture
def turn(project, tmp_path, monkeypatch):
    fake_review(monkeypatch, [])
    return Transcript(tmp_path / "session.jsonl").prompt("draft the offer for the backup")


def stop(project, transcript: Path, monkeypatch, capsys, **more):
    payload = {"cwd": str(project), "transcript_path": str(transcript), **more}
    return run_hook("stop", payload, monkeypatch, capsys)


def test_a_draft_checked_clean_after_its_last_write_passes(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.md", CLEAN).check("deliverables/offer.md")
    assert stop(project, t.reply("The offer is in deliverables/offer.md."), monkeypatch, capsys) == (0, "", "")


def test_a_draft_shown_without_the_check_is_held_with_one_line_naming_it(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.md", CLEAN)
    code, out, err = stop(project, t.reply("The offer is in deliverables/offer.md."), monkeypatch, capsys)
    assert code == 2 and out == ""
    assert err.strip() == "show deliverables/offer.md only after `awb write check deliverables/offer.md` ran clean " \
                          "in this turn"


def test_a_check_before_the_last_change_does_not_count(project, turn, monkeypatch, capsys):
    path = project / "deliverables" / "offer.md"
    t = turn.write(path, CLEAN).check("deliverables/offer.md").write(path, CLEAN + "\nThe retention is 30 days.\n")
    assert stop(project, t.reply("See offer.md."), monkeypatch, capsys)[0] == 2
    # a change through the shell counts as a change too
    t = Transcript(turn.path).prompt("again")
    t.write(path, CLEAN).check("deliverables/offer.md").bash("sed -i 's/30/31/' deliverables/offer.md")
    assert stop(project, t.reply("See deliverables/offer.md."), monkeypatch, capsys)[0] == 2


def test_a_check_that_found_tells_does_not_count(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.md", CLEAN).check("deliverables/offer.md", clean=False)
    assert stop(project, t.reply("See deliverables/offer.md."), monkeypatch, capsys)[0] == 2


def test_a_claimed_clean_run_on_a_dirty_draft_is_held_because_the_hook_checks_again(project, turn, monkeypatch,
                                                                                     capsys):
    t = turn.write(project / "deliverables" / "offer.md", DIRTY).check("deliverables/offer.md")
    code, _, err = stop(project, t.reply("See deliverables/offer.md."), monkeypatch, capsys)
    assert code == 2 and "deliverables/offer.md" in err


def test_a_draft_from_an_earlier_turn_passes_when_it_is_clean_and_is_held_when_not(project, turn, monkeypatch,
                                                                                   capsys):
    (project / "deliverables" / "offer.md").write_text(CLEAN, encoding="utf-8")
    assert stop(project, turn.reply("The offer in deliverables/offer.md is unchanged."), monkeypatch, capsys)[0] == 0
    (project / "deliverables" / "offer.md").write_text(DIRTY, encoding="utf-8")
    assert stop(project, turn.reply("The offer in deliverables/offer.md is unchanged."), monkeypatch, capsys)[0] == 2


def test_a_draft_in_russian_is_exempt(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.ru.md", RUSSIAN)
    assert stop(project, t.reply("The reading copy is deliverables/offer.ru.md."), monkeypatch, capsys) == (0, "", "")


def test_a_written_draft_the_reply_does_not_show_passes(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.md", DIRTY)
    assert stop(project, t.reply("I sized the backup."), monkeypatch, capsys) == (0, "", "")


def test_a_mail_draft_needs_the_check_in_mode_mail(project, turn, monkeypatch, capsys):
    t = turn.write(project / "mail-reply.md", MAIL).check("mail-reply.md")
    code, _, err = stop(project, t.reply("The mail is in mail-reply.md."), monkeypatch, capsys)
    assert code == 2
    assert err.strip() == "show mail-reply.md only after `awb write check mail-reply.md --mode mail` ran clean in " \
                          "this turn"
    t = Transcript(turn.path).prompt("again").write(project / "mail-reply.md", MAIL)
    t.check("mail-reply.md", mode="mail")
    assert stop(project, t.reply("The mail is in mail-reply.md."), monkeypatch, capsys) == (0, "", "")


def test_a_mail_written_into_the_reply_is_held_unless_it_is_russian(project, turn, monkeypatch, capsys):
    code, _, err = stop(project, turn.reply("Here is the draft.\n\nSubject: Quota\n\nHello,\n\nI checked the "
                                            "quota.\n\nBest regards\n"), monkeypatch, capsys)
    assert code == 2 and "a mail draft in the reply: write it into a file in the project" in err
    russian = "Черновик:\n\nТема: квота\n\nЗдравствуйте,\n\nя проверил квоту.\n\nBest regards\n"
    assert stop(project, turn.reply(russian), monkeypatch, capsys) == (0, "", "")


def test_intake_material_is_no_draft(project, turn, monkeypatch, capsys):
    (project / "input").mkdir()
    (project / "input" / "mail-from-them.md").write_text(DIRTY, encoding="utf-8")
    assert stop(project, turn.reply("I read input/mail-from-them.md."), monkeypatch, capsys) == (0, "", "")


def test_the_gate_fails_closed(project, turn, monkeypatch, capsys):
    code, _, err = stop(project, project / "no-such-transcript.jsonl", monkeypatch, capsys)
    assert code == 2 and "could not read this session's transcript" in err
    t = turn.write(project / "deliverables" / "offer.md", CLEAN).check("deliverables/offer.md")
    path = t.reply("See deliverables/offer.md.")
    missing_module(monkeypatch, "writing")
    code, _, err = stop(project, path, monkeypatch, capsys)
    assert code == 2 and "deliverables/offer.md is shown unchecked: the writing check is not installed" in err


def test_a_broken_gate_holds_the_reply(project, turn, monkeypatch, capsys):
    def boom(data, root):
        raise RuntimeError("planted")

    monkeypatch.setattr(hooks, "draft_gate", boom)
    code, _, err = stop(project, turn.reply("Done."), monkeypatch, capsys)
    assert code == 2 and "the draft check of the stop hook failed (RuntimeError)" in err


def test_stop_hook_active_lets_the_next_stop_through(project, turn, monkeypatch, capsys):
    t = turn.write(project / "deliverables" / "offer.md", DIRTY)
    assert stop(project, t.reply("See deliverables/offer.md."), monkeypatch, capsys,
                stop_hook_active=True) == (0, "", "")


def test_a_file_name_with_a_registered_name_is_never_echoed(project, turn, monkeypatch, capsys):
    rel = "deliverables/offer-%s.md" % fixtures.CUSTOMER_FORMS[1]
    t = turn.write(project / rel, CLEAN)
    code, _, err = stop(project, t.reply("See %s." % rel), monkeypatch, capsys)
    assert code == 2 and err.strip().startswith("show file 1 only after")
    assert_clean(err, "the stop message")


def test_his_earlier_prompt_starts_the_turn(project, turn, monkeypatch, capsys):
    """A check of an earlier turn does not count for a change in this one."""
    path = project / "deliverables" / "offer.md"
    t = turn.write(path, CLEAN).check("deliverables/offer.md")
    t.reply("See deliverables/offer.md.")
    t.prompt("add the retention").write(path, CLEAN + "\nThe retention is 30 days.\n")
    assert stop(project, t.reply("See deliverables/offer.md."), monkeypatch, capsys)[0] == 2


def test_a_change_of_an_earlier_turn_is_judged_by_the_hook_s_own_check(project, turn, monkeypatch, capsys):
    """Written in the turn before without a check and shown now unchanged: the hook's own clean run counts."""
    t = turn.write(project / "deliverables" / "offer.md", CLEAN)
    t.reply("I sized the backup.")
    t.prompt("show me the offer")
    assert stop(project, t.reply("The offer is deliverables/offer.md."), monkeypatch, capsys) == (0, "", "")
