"""Q-0015 (UXR7, finding 7 of the UX review 2026-11-18): the edit mode of the drafting skill.

Each correction round shows the whole Russian text again with no change list (owner word 2026-11-19), the master
in the target language is updated in the same pass, several fixes go in one pass, the review runs once at the end
and the length does not grow with the edits. The rules are pinned in both shipped copies of the skill; the rules of the
work user send every draft through it.
"""
import re
from pathlib import Path

from awb import writing

REPO = Path(__file__).resolve().parent.parent
SKILLS = (REPO / "plugin" / "skills" / "drafting" / "SKILL.md",
          REPO / "seal" / "work-claude" / "skills" / "drafting" / "SKILL.md")
WORK_RULES = REPO / "seal" / "work-claude" / "CLAUDE.md"


def _flat(path):
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(text):
    m = re.search(r"## Edit rounds (.+?) ## ", text)
    assert m, "the drafting skill has no section Edit rounds"
    return m.group(1)


def test_both_shipped_copies_of_the_drafting_skill_are_the_same():
    assert SKILLS[0].read_text(encoding="utf-8") == SKILLS[1].read_text(encoding="utf-8")


def test_each_round_shows_the_whole_russian_text_and_no_change_list():
    for path in SKILLS:
        part = _section(_flat(path))
        assert "Show the whole Russian text again in the reply, from the first line to the last, unasked." in part
        assert "No list of changes, no diff and no summary of the edit." in part
        assert "ends with the whole text" in part


def test_the_master_in_the_target_language_is_updated_in_the_same_pass():
    for path in SKILLS:
        part = _section(_flat(path))
        assert "Apply the fixes to the master first." in part
        assert "usually English or German, saved in its file" in part
        assert "make the Russian reading copy again from the corrected master with `awb write translate FILE`" in part
        assert "never by editing the copy alone" in part
        assert "carry the change back into the master" in part
        assert "Name the files of the master and the copy" in part


def test_several_fixes_in_one_message_are_one_pass():
    for path in SKILLS:
        part = _section(_flat(path))
        assert "Take every fix of his message in one pass, also when he sends several fixes at once." in part


def test_the_review_runs_once_at_the_end_and_a_small_edit_gets_the_write_check_only():
    for path in SKILLS:
        part = _section(_flat(path))
        assert "A small edit (a word, a phrase or a sentence) gets `awb write keep` and `awb write check` only." in part
        assert "the review run once, at the end, when he accepts the text" in part
        assert "An edit that adds a fact, a number or a price gets that fact checked" in part


def test_the_length_is_set_from_the_request_and_does_not_grow():
    for path in SKILLS:
        text = _flat(path)
        part = _section(text)
        assert "The length budget set from the request holds for every round." in part
        assert "the text does not grow with the edits" in part
        assert "Set the length budget now, from the request." in text


def test_facts_first_and_voice_rules_are_kept():
    for path in SKILLS:
        text = _flat(path)
        for must in ("facts first, voice second", "Write TCP always", "awb write keep BEFORE AFTER",
                     "Cut to the budget as a separate last step", "Keep his own choice of I or we in a draft",
                     "## How he writes", "## German and Russian", "awb voice learn"):
            assert must in text, (path, must)
        assert text.index("## How he writes") < text.index("## Edit rounds") < text.index("## After he sends it")


def test_the_edit_rounds_read_the_way_the_skill_asks():
    text = SKILLS[0].read_text(encoding="utf-8")
    tells, _ = writing.check_text(text, mode="doc")
    assert [t for t in tells if t.blocking] == []
    assert [t for t in tells if t.cls in ("verdict-heading", "fragment", "comma-splice", "connector")] == []



def test_the_work_rules_send_every_draft_through_the_drafting_skill():
    """The work rules sit at their line budget, so the edit rounds live in the skill they point to."""
    assert "Every text under his name goes through the drafting skill" in _flat(WORK_RULES)
