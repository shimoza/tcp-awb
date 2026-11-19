"""The pre-tool hook knows the project of a call that cds into it, also when the call carries long text.

Defect of 2026-11-19: a Bash call `cd <project> && awb kb add "<long statement>"` was refused with "the project
folder of this session cannot be determined". The guard reads a quoted word with a blank and a / in it as one
whole path as well; a statement of more than 255 bytes before or between its slashes is a file name the kernel
refuses with ENAMETOOLONG, and that error was taken for a project the guard cannot find. A name that long cannot
exist, so it is a missing place now and classed as written. A call outside every project still refuses.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from awb import hooks, toolguard
from tests import fixtures

OWN = "tcp-q7m4"
OTHER = "tcp-z3k5"


def run(payload: dict, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main(["pre-tool"])
    return code, capsys.readouterr().err


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = tmp_path / "userhome"
    for folder in (".ssh", ".config", ".claude"):
        (user / folder).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    own, other = home.projects_root / OWN, home.projects_root / OTHER
    for root in (own, other):
        (root / "notes").mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
    (home.kb / "entries").mkdir(parents=True, exist_ok=True)
    return {"p": home, "own": own, "other": other, "user": user, "tmp": tmp_path}


def bash(command: str, cwd) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd), "session_id": "s-1"}


def statement() -> str:
    """A long statement for the knowledge base: quotes, newlines, slashes and invented names only."""
    head = ("%s runs its %s on two ECS of flavor s3.large.2 behind an ELB; the storage team asked whether "
            "the EVS disks of type SSD keep their IOPS after a resize, and the answer from the console "
            "and the API was the same on both tenants. " % (fixtures.CUSTOMER_CODE, fixtures.KEEP_WORDS[3]))
    return (head * 3 + "A resize/extend of 100 GB to 200 GB kept 'the same' IOPS class.\n"
            "Second line: the \"ultra-high I/O\" type was not offered in eu-nl/AZ2 on 2026-11-19.\n" + head)


def test_a_long_kb_add_after_a_cd_into_the_project_passes(world, monkeypatch, capsys):
    text = statement().replace('"', '\\"')
    command = 'cd %s && awb kb add --tag storage "%s"' % (world["own"], text)
    assert len(text.split("/")[0].encode()) > 255
    code, err = run(bash(command, world["own"]), monkeypatch, capsys)
    assert (code, err) == (hooks.OK, "")


def test_the_long_statement_passes_from_a_subfolder_and_in_single_quotes(world, monkeypatch, capsys):
    text = statement().replace("'", "")
    for cwd in (world["own"], world["own"] / "notes"):
        command = "cd %s\nawb kb add '%s'" % (world["own"] / "notes", text)
        code, err = run(bash(command, cwd), monkeypatch, capsys)
        assert (code, err) == (hooks.OK, "")


def test_a_long_word_without_a_cd_passes_from_the_hook_cwd(world, monkeypatch, capsys):
    command = 'awb kb add "%s"' % statement().replace('"', "'")
    code, err = run(bash(command, world["own"]), monkeypatch, capsys)
    assert (code, err) == (hooks.OK, "")


def test_a_short_call_after_a_cd_passes(world, monkeypatch, capsys):
    command = "cd %s && cat notes/x.md" % world["own"]
    assert run(bash(command, world["own"]), monkeypatch, capsys) == (hooks.OK, "")


def test_a_long_word_naming_another_project_still_refuses(world, monkeypatch, capsys):
    long_name = "a" * 300
    command = 'cd %s && cat "%s/%s x/y"' % (world["own"], world["other"], long_name)
    code, err = run(bash(command, world["own"]), monkeypatch, capsys)
    assert code == hooks.BLOCK
    assert err.strip() == hooks.refusal(hooks.ANOTHER_PROJECT)


def test_a_long_plain_path_naming_another_project_still_refuses(world, monkeypatch, capsys):
    command = "cat %s/%s" % (world["other"], "b" * 300)
    code, err = run(bash(command, world["own"]), monkeypatch, capsys)
    assert code == hooks.BLOCK
    assert err.strip() == hooks.refusal(hooks.ANOTHER_PROJECT)


def test_a_call_outside_every_project_still_refuses_on_a_sealed_host(world, monkeypatch, capsys):
    outside = world["tmp"] / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(toolguard, "_sealed", lambda: True)
    command = 'cd %s && awb kb add "%s"' % (outside, statement().replace('"', "'"))
    code, err = run(bash(command, outside), monkeypatch, capsys)
    assert code == hooks.BLOCK
    assert err.strip() == hooks.refusal(hooks.UNKNOWN_PROJECT)
