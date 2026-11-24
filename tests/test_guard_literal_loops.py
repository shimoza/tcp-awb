"""The command guard on `for VAR in LIT...; do CMD "$VAR"; done` (ask 2026-11-21-tcp-bytg-17): a loop over literal
relative paths of the project passes; a list with an absolute path, .., a substitution or a variable is refused.
A glob in the list is not refused: tests/test_guard_review.py::test_the_shell_reader_passes_the_commands_of_an_honest_session
needs `for f in deliverables/*.md` to pass."""
from __future__ import annotations

import pytest

from tests.test_guard_review import OTHER, bash, world  # noqa: F401

LIST = "ch1/en.md ch2/de.md ch3/fr.md"


@pytest.mark.parametrize("body", ['sed -n 1,5p "$f"', 'awb write check "$f"', "sed -n '2,3p' $f"])
def test_a_loop_over_literal_project_paths_passes(world, body):
    assert bash(world, "for f in %s; do %s; done" % (LIST, body)) is None


def test_a_loop_in_the_newline_form_passes(world):
    assert bash(world, 'for f in ch1/en.md "ch2/de.md"\ndo\n  sed -n 1p "$f"\ndone') is None


@pytest.mark.parametrize("item", ["/etc/passwd", "../x.md", "$(ls)", "$DIR/a.md", "$X", "`ls`", "~/a.md",
                                  "a/../../x.md"])
def test_a_loop_with_an_unsafe_list_item_is_refused(world, item):
    assert bash(world, 'for f in ch1/en.md %s; do sed -n 1p "$f"; done' % item) is not None
    assert bash(world, 'for f in %s\ndo sed -n 1p "$f"\ndone' % item) is not None


def test_a_loop_over_another_project_is_refused(world):
    assert bash(world, 'for f in ../%s/STATE.md; do sed -n 1p "$f"; done' % OTHER) is not None


@pytest.mark.parametrize("body", ["cat ../%s/STATE.md" % OTHER, 'sed -n 1p "$f" ../%s/STATE.md' % OTHER,
                                  'cd .. && cat "$f"', 'bash -c "cd; cat .ssh/id_ed25519"',
                                  'cat "$f" > ../%s/STATE.md' % OTHER])
def test_a_loop_whose_body_is_refused_for_one_path_is_refused(world, body):
    assert bash(world, "for f in %s; do %s; done" % (LIST, body)) is not None
