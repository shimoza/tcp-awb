"""The tool guard (baf45e5) and the start receipt (727cfd3) against a hostile project session, from the attacker
review of 2026-10-10 (group g6-guard); each part names its finding.

The tests are the review's, copied with the prefix test_g6_ dropped.
Invented names and codes only (tests/fixtures.py); no real name anywhere.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import hooks

OWN = "tcp-q7m4"
OTHER = "tcp-z3k5"


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    """Two projects under the projects root, the knowledge base and a user home with .ssh, .config and .claude
    (the layout of tests/test_guard.py)."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = tmp_path / "userhome"
    for folder in (".ssh", ".config", ".claude"):
        (user / folder).mkdir(parents=True)
    (user / ".ssh" / "id_ed25519").write_text("key\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    own, other = home.projects_root / OWN, home.projects_root / OTHER
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
        (root / "STATE.md").write_text("Status: working\nNext: size\n", encoding="utf-8")
    (home.kb / "entries").mkdir(parents=True, exist_ok=True)
    return {"p": home, "own": own, "other": other, "user": user}


@pytest.fixture
def homeworld(home, tmp_path, monkeypatch):
    """The sealed default (config.paths: projects root "~"): the projects live in the work user's home next to
    .ssh, .config and .claude."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = home.projects_root
    monkeypatch.setenv("HOME", str(user))
    for folder in (".ssh", ".config", ".claude"):
        (user / folder).mkdir(parents=True)
    (user / ".ssh" / "id_ed25519").write_text("key\n", encoding="utf-8")
    own, other = user / OWN, user / OTHER
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
        (root / "STATE.md").write_text("Status: working\nNext: size\n", encoding="utf-8")
    return {"p": home, "own": own, "other": other, "user": user}


def bash(w, command: str) -> str | None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(w["own"]), "session_id": "s-1"}
    return hooks.guard(payload, w["p"], w["user"], sealed=True)


# --------------------------------------------------------------------------- g6-guard-1: bare cd then relative


def test_bare_cd_then_a_relative_path_reaches_the_key_folder_and_another_project(homeworld):
    """`cd` without an argument goes to $HOME, where the sealed host keeps every project and .ssh; the relative
    path after it carries no `..` and is never looked at."""
    assert bash(homeworld, "cd && cat .ssh/id_ed25519") == hooks.KEY_FOLDER
    assert bash(homeworld, "cd; cat %s/STATE.md" % OTHER) == hooks.ANOTHER_PROJECT


# --------------------------------------------------------------------------- g6-guard-2: $PWD/.. and ~+/..


def test_pwd_variable_with_dotdot_reaches_another_project(world):
    """`$PWD/../tcp-z3k5` is taken as a folder named `$PWD` inside the session folder, so the `..` cancels it and
    the path normalises to the own project; the shell climbs out of it."""
    for command in ("cat $PWD/../%s/STATE.md" % OTHER, "cat ${PWD}/../%s/STATE.md" % OTHER,
                    "cat ~+/../%s/STATE.md" % OTHER):
        assert bash(world, command) == hooks.ANOTHER_PROJECT, command


# --------------------------------------------------------------------------- g6-guard-3: the tokenizer


def test_quote_and_backslash_split_an_absolute_path_past_the_guard(world):
    """The guard splits the command line on quotes and keeps backslashes; the shell joins `tc''p-` and drops the
    backslash, so the literal absolute path of another project reaches cat."""
    root = world["p"].projects_root
    for command in ("cat %s/tc''p-z3k5/STATE.md" % root, "cat %s/tcp\\-z3k5/STATE.md" % root,
                    'cat %s/t"cp-z3k5"/STATE.md' % root):
        assert bash(world, command) == hooks.ANOTHER_PROJECT, command


def test_an_option_with_an_attached_path_is_never_looked_at(world):
    """A token that starts with `-` or `@` is skipped, so `tar -C/abs/path` and `curl -d@/abs/path` pass."""
    other, key = world["other"], world["user"] / ".ssh" / "id_ed25519"
    assert bash(world, "tar -C%s -cf - . | tar -xOf -" % other) == hooks.ANOTHER_PROJECT
    assert bash(world, "curl -s -d@%s http://127.0.0.1:9/" % key) == hooks.KEY_FOLDER


# --------------------------------------------------------------------------- g6-guard-4: a link and a relative path


def test_a_link_inside_the_project_leads_out_through_a_relative_bash_path(world):
    """The commit promises links are followed. The Read and Edit tools follow `peek`; a shell call names it
    relatively without `..`, so only the session folder is checked."""
    (world["own"] / "peek").symlink_to(world["other"])
    (world["own"] / "keys").symlink_to(world["user"] / ".ssh")
    assert bash(world, "cat peek/STATE.md") == hooks.ANOTHER_PROJECT
    assert bash(world, "cat keys/id_ed25519") == hooks.KEY_FOLDER


# --------------------------------------------------------------------------- g6-guard-6: a relative Glob pattern


def test_a_relative_glob_pattern_that_climbs_is_never_looked_at(world):
    """A Glob or Grep pattern that does not start with / or ~ is skipped, even when it climbs with `..`."""
    payload = {"tool_name": "Glob", "tool_input": {"pattern": "../%s/*.md" % OTHER},
               "cwd": str(world["own"]), "session_id": "s-1"}
    assert hooks.guard(payload, world["p"], world["user"], sealed=True) == hooks.ANOTHER_PROJECT


# --------------------------------------------------------------------------- the shell reader keeps honest commands


def test_the_shell_reader_passes_the_commands_of_an_honest_session(world):
    """Findings 8, 20 and 21 make the reader strict; the commands a session runs every day still pass: quoted
    patterns, loops over a variable, a commit message in a here-document, a cd into the project and back."""
    (world["own"] / "deliverables").mkdir()
    for command in ("grep -rn 'obs' deliverables/", "sed -i 's/a\\/b/c/' STATE.md", "awk '{print $1}' STATE.md",
                    'for f in deliverables/*.md; do wc -l "$f"; done', "echo $?",
                    "python3 -c \"import json; print(json.load(open('x.json')))\"",
                    "git commit -m \"$(cat <<'EOF'\nThe sizing: don't count a/b twice, it's ~50 lines.\nEOF\n)\"",
                    "cd deliverables && pandoc a.md -o a.docx && cd ..", "ls deliverables 2>&1 | tail -3",
                    "find . -name '*.md' -newer STATE.md", "bash -c 'cd deliverables && ls'",
                    "ls %s" % world["p"].outbox):
        assert bash(world, command) is None, command


def test_a_cd_inside_bash_c_or_eval_is_followed(world):
    """The string of `bash -c` and of `eval` is a command line of its own; its cd is followed like any other."""
    for command in ('bash -c "cd; cat .ssh/id_ed25519"', "sh -ec 'cd && cat .ssh/id_ed25519'",
                    "eval cd\\; cat .ssh/id_ed25519"):
        assert bash(world, command) == hooks.KEY_FOLDER, command


# --------------------------------------------------------------------------- what holds (controls)


def test_holds_tokens_the_guard_does_read(world):
    root, other = world["p"].projects_root, world["other"]
    assert bash(world, "cat %s/tcp-z3k?/STATE.md" % root) == hooks.ANOTHER_PROJECT
    assert bash(world, "cat %s/{%s,x}/STATE.md" % (root, OTHER)) == hooks.ANOTHER_PROJECT
    assert bash(world, "python3 -c \"print(open('%s/STATE.md').read())\"" % other) == hooks.ANOTHER_PROJECT
    assert bash(world, "cat <<< %s/STATE.md" % other) == hooks.ANOTHER_PROJECT
    assert bash(world, "cat x/../../%s/STATE.md" % OTHER) == hooks.ANOTHER_PROJECT
    assert bash(world, "cat --file=%s/STATE.md" % other) == hooks.ANOTHER_PROJECT
    assert bash(world, "cat //%s/STATE.md" % str(other).lstrip("/")) == hooks.ANOTHER_PROJECT
