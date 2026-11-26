"""Tests of awb/projects.py: spawn, load and close against a throw-away Workbench with the fixture register."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import awb
from awb import codes, projects, register
from tests import fixtures
from tests.conftest import needs_hook_process

GOAL = "move two app clusters to managed k8s now"
RULES = Path(awb.__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md"
"""The work rules: what a session in a project follows. Not the developer instructions of the repository."""
SKELETON_FILES = ("SCOPE.md", "STATE.md", "OPEN.md", "RESOURCES.md", "CLAUDE.md")
SKELETON_DIRS = ("input", "evidence", "deliverables", "reviews")

# words of the test blocklist (tests/blocklist.txt), put together at run time so this file passes the gate
OLD_TAG = "CLI" + "ENT3"
OLD_GOAL_WORD = "PART" + "NR12"
OLD_PATH_WORD = "legacy" + "-shared"


def _git(folder: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True)
    return res.stdout


def _scope(folder: Path) -> dict[str, str]:
    out = {}
    for line in (folder / "SCOPE.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("- ") and ": " in line:
            key, value = line[2:].split(": ", 1)
            out[key] = value
    return out


def _nothing_created(home) -> None:
    root = home.projects_root
    made = [d.name for d in root.iterdir()] if root.exists() else []
    assert made == [], "%d folders were created" % len(made)
    assert not home.projects_register.exists()


def _spawn(home, register_path, kind="engagement", goal=GOAL, customer=fixtures.CUSTOMER_CODE, tags=()):
    return projects.spawn(home, kind, goal, customer, register_path, tags=list(tags))


# --- a complete project -------------------------------------------------------------------------------------

def test_goal_is_forty_characters():
    assert len(GOAL) == 40


def test_spawn_creates_the_skeleton(home, register_path):
    pr = _spawn(home, register_path)
    folder = home.projects_root / pr.code
    assert codes.PROJECT_CODE_RE.fullmatch(folder.name)
    assert folder.name.startswith("tcp-")
    assert pr.path == str(folder)
    assert folder.is_dir()
    for name in SKELETON_FILES:
        assert (folder / name).is_file(), name
        assert (folder / name).read_text(encoding="utf-8").strip(), name
    for name in SKELETON_DIRS:
        assert (folder / name).is_dir(), name
    assert pr.code in (folder / "STATE.md").read_text(encoding="utf-8")


def test_scope_carries_kind_code_customer_goal_created_and_sealed(home, register_path):
    pr = _spawn(home, register_path)
    folder = Path(pr.path)
    scope = _scope(folder)
    assert scope["code"] == pr.code
    assert scope["kind"] == "query"          # engagement became query on 2026-10-03 (his two kinds)
    assert scope["customer"] == fixtures.CUSTOMER_CODE
    assert scope["goal"] == GOAL
    assert scope["created"] == date.today().isoformat()
    assert scope["tags"] == "none"
    assert "mode: sealed" in (folder / "SCOPE.md").read_text(encoding="utf-8")


def test_scope_lists_tags(home, register_path):
    pr = _spawn(home, register_path, tags=["cce", "migration", "cce"])
    assert _scope(Path(pr.path))["tags"] == "cce, migration"


def _imports(text: str) -> list[Path]:
    return [Path(os.path.expanduser(line[1:].strip())).resolve() for line in text.splitlines()
            if line.startswith("@")]


def test_claude_md_is_short_clean_and_points_at_the_workbench_rules(home, register_path):
    pr = _spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 30
    fixtures.assert_no_fixture_name(text, "CLAUDE.md")
    assert RULES.is_file()
    assert _imports(text) == [RULES]
    rules = RULES.read_text(encoding="utf-8")
    assert "working sessions" in rules and "Build state lives in build/" not in rules
    assert pr.code in text
    assert fixtures.CUSTOMER_CODE in text


def test_claude_md_uses_the_absolute_path_outside_home(home, register_path, tmp_path, monkeypatch):
    rules = tmp_path / "rules" / "CLAUDE.md"
    rules.parent.mkdir()
    rules.write_text("rules\n", encoding="utf-8")
    monkeypatch.setattr(projects, "RULES_FILE", rules)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    pr = _spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@" + str(rules) in text.splitlines()


def test_claude_md_under_home_carries_no_home_path(home, register_path, tmp_path, monkeypatch):
    rules = tmp_path / "userhome" / "tcp-awb" / "CLAUDE.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("rules\n", encoding="utf-8")
    monkeypatch.setattr(projects, "RULES_FILE", rules)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    pr = _spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@~/tcp-awb/CLAUDE.md" in text.splitlines()
    assert str(tmp_path) not in text


def test_spawned_project_passes_the_gate(home, register_path):
    from awb import gate
    pr = _spawn(home, register_path, tags=["network"])
    files = [f for f in Path(pr.path).rglob("*") if f.is_file() and ".git" not in f.relative_to(pr.path).parts]
    # Release 2 adds .claude/settings.json (the client hooks); the gate below covers it too
    assert len(files) == len(SKELETON_FILES) + len(SKELETON_DIRS) + 1
    assert Path(pr.path) / ".claude" / "settings.json" in files
    findings = gate.scan_files(files, register_path)
    assert [(Path(f.file).name, f.line, f.cls) for f in findings] == []


def test_no_generated_file_carries_a_fixture_name(home, register_path):
    pr = _spawn(home, register_path, tags=["network"])
    for f in Path(pr.path).rglob("*"):
        if f.is_file() and ".git" not in f.relative_to(pr.path).parts:
            fixtures.assert_no_fixture_name(f.read_text(encoding="utf-8"), f.name)


def test_git_repository_with_one_commit_holding_everything(home, register_path):
    pr = _spawn(home, register_path)
    folder = Path(pr.path)
    assert (folder / ".git").is_dir()
    assert _git(folder, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(folder, "status", "--porcelain").strip() == ""
    tracked = set(_git(folder, "ls-files").split())
    for name in SKELETON_FILES:
        assert name in tracked
    for name in SKELETON_DIRS:
        assert name + "/.gitkeep" in tracked
    log = _git(folder, "log", "--format=%an %ae %s")
    assert pr.code in log
    assert GOAL not in log
    fixtures.assert_no_fixture_name(log, "git log")


def test_project_is_registered_with_memory_key(home, register_path):
    pr = _spawn(home, register_path)
    rows = projects.load(home)
    assert rows == [pr]
    row = rows[0]
    folder = home.projects_root / pr.code
    assert row.path == str(folder)
    assert os.path.isabs(row.path)
    assert row.memory_key == str(folder).replace("/", "-")
    assert row.kind == "query"               # engagement became query on 2026-10-03 (his two kinds)
    assert row.customer == fixtures.CUSTOMER_CODE
    assert row.platform == "tcp"
    assert row.state == "active"
    assert row.created == date.today().isoformat()
    raw = home.projects_register.read_text(encoding="utf-8")
    assert raw.splitlines()[0] == "\t".join(projects.HEADER)
    assert GOAL not in raw
    fixtures.assert_no_fixture_name(raw, "projects.tsv")


def test_project_without_customer(home, register_path):
    pr = _spawn(home, register_path, kind="lab", customer=None)
    assert pr.customer == "none"
    assert _scope(Path(pr.path))["customer"] == "none"
    assert projects.load(home)[0].customer == "none"


@pytest.mark.parametrize("kind", projects.PROJECT_KINDS)
def test_every_kind_spawns(home, register_path, kind):
    pr = _spawn(home, register_path, kind=kind)
    assert pr.kind == kind
    assert _scope(Path(pr.path))["kind"] == kind


def test_two_spawns_give_two_codes(home, register_path):
    a = _spawn(home, register_path)
    b = _spawn(home, register_path, goal="size the backup of the app clusters")
    assert a.code != b.code
    assert Path(a.path).is_dir() and Path(b.path).is_dir()
    assert [r.code for r in projects.load(home)] == [a.code, b.code]


# --- refusals -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("form", [
    fixtures.CUSTOMER_FORMS[0],
    fixtures.CUSTOMER_FORMS[1],
    fixtures.PERSON_FORMS[0],
    fixtures.ORG_FORMS[0],
    fixtures.LAWFIRM_FORMS[0],
    fixtures.CUSTOMER_DOMAIN,
    fixtures.TENDER_ID,
    fixtures.CUSTOMER_FORMS[1].lower() + "prod01",
    fixtures.CUSTOMER_FORMS[1] + "s",
])
def test_goal_with_a_registered_name_is_refused_without_echo(home, register_path, form):
    """DMZ 4 (B-088): no content check inside, a registered name in the goal is spawned and kept as written."""
    goal = "review the network plan for " + form
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


def test_goal_with_structured_data_is_refused(home, register_path):
    goal = "open the firewall for 203.0.113.7 today"
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


def test_tag_with_a_registered_name_is_refused(home, register_path):
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, tags=["network", fixtures.CUSTOMER_FORMS[1].lower()])
    msg = str(ei.value)
    fixtures.assert_no_fixture_name(msg, "error message")
    assert "tag 2" in msg
    _nothing_created(home)


def test_a_blocked_tag_is_refused(home, register_path):
    """B-088: the blocklist is no check inside; a tag stays one of rules/tags.txt, so this one is refused as unknown."""
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, tags=[OLD_TAG])
    msg = str(ei.value)
    assert "rules/tags.txt" in msg
    assert OLD_TAG not in msg
    _nothing_created(home)


@pytest.mark.parametrize("tag", ["TIC" + "KT7", "x_" + OLD_TAG])
def test_a_blocked_tag_in_another_spelling_is_refused(home, register_path, tag):
    with pytest.raises(projects.ProjectError, match="rules/tags.txt"):
        _spawn(home, register_path, tags=[tag])
    _nothing_created(home)


def test_a_blocked_word_in_the_goal_is_refused(home, register_path):
    goal = "follow up the notes of " + OLD_GOAL_WORD
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


def test_a_blocked_path_in_the_goal_is_refused(home, register_path):
    goal = "port the tooling of " + OLD_PATH_WORD
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


@pytest.mark.parametrize("customer", [
    "CUST-ZZZZ",
    fixtures.PARTNER_CODE,
    fixtures.ORG_CODE,
    fixtures.PERSON_CODE,
    "PART-RET2",
    fixtures.CUSTOMER_CODE.lower(),
    fixtures.CUSTOMER_FORMS[1],
    fixtures.CUSTOMER_FORMS[0],
])
def test_customer_not_a_registered_cust_code_is_refused(home, register_path, customer):
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, customer=customer)
    fixtures.assert_no_fixture_name(str(ei.value), "error message")
    _nothing_created(home)


def test_retired_customer_is_refused(home, register_path):
    assert register.retire(register_path, fixtures.CUSTOMER_CODE) > 0
    with pytest.raises(projects.ProjectError, match="retired"):
        _spawn(home, register_path)
    _nothing_created(home)


@pytest.mark.parametrize("kind", ["poc", "Engagement", "", "tender"])
def test_unknown_kind_is_refused(home, register_path, kind):
    with pytest.raises(projects.ProjectError, match="unknown project kind"):
        _spawn(home, register_path, kind=kind)
    _nothing_created(home)


@pytest.mark.parametrize("goal", ["", "   ", "two\nlines of goal", "x" * 501, "zero\u200bwidth"])
def test_malformed_goal_is_refused(home, register_path, goal):
    with pytest.raises(projects.ProjectError):
        _spawn(home, register_path, goal=goal)
    _nothing_created(home)


@pytest.mark.parametrize("tags", [["two words"], [""], ["a,b"], ["x" * 49], "network"])
def test_malformed_tags_are_refused(home, register_path, tags):
    with pytest.raises(projects.ProjectError):
        projects.spawn(home, "lab", GOAL, None, register_path, tags=tags)
    _nothing_created(home)


def test_existing_folder_is_refused_and_left_alone(home, register_path, monkeypatch):
    monkeypatch.setattr(codes, "new_project_code", lambda platform, existing=frozenset(): "tcp-yyyy")
    folder = home.projects_root / "tcp-yyyy"
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(projects.ProjectError, match="already exists"):
        _spawn(home, register_path)
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert projects.load(home) == []


def test_second_project_with_the_same_code_is_refused(home, register_path, monkeypatch):
    monkeypatch.setattr(codes, "new_project_code", lambda platform, existing=frozenset(): "tcp-zzzz")
    first = _spawn(home, register_path)
    assert first.code == "tcp-zzzz"
    with pytest.raises(projects.ProjectError, match="already"):
        _spawn(home, register_path, goal="a second goal for the same code")
    assert [r.code for r in projects.load(home)] == ["tcp-zzzz"]
    assert _scope(Path(first.path))["goal"] == GOAL


def test_missing_name_check_refuses(home, register_path, monkeypatch):
    """B-088: a project needs no name check, so a missing check module stops nothing."""
    monkeypatch.delattr(awb, "check", raising=False)
    monkeypatch.setitem(sys.modules, "awb.check", None)
    pr = _spawn(home, register_path)
    assert pr.code.startswith("tcp-")


def test_final_check_refuses_a_generated_file_with_a_name(home, register_path, tmp_path, monkeypatch):
    """B-088: no final check of the generated files: a rules file under a registered name is copied as it is."""
    rules = tmp_path / fixtures.CUSTOMER_FORMS[1].lower() / "CLAUDE.md"
    rules.parent.mkdir()
    rules.write_text("rules\n", encoding="utf-8")
    monkeypatch.setattr(projects, "RULES_FILE", rules)
    pr = _spawn(home, register_path)
    assert (Path(pr.path) / "CLAUDE.md").is_file()


def test_git_failure_leaves_nothing_behind(home, register_path, tmp_path, monkeypatch):
    empty = tmp_path / "nobin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(projects.ProjectError, match="git"):
        _spawn(home, register_path)
    assert list(home.projects_root.iterdir()) == []
    assert projects.load(home) == []


# --- close --------------------------------------------------------------------------------------------------

def test_close_sets_state_closed(home, register_path):
    a = _spawn(home, register_path)
    b = _spawn(home, register_path, goal="size the backup of the app clusters")
    closed = projects.close(home, a.code)
    assert closed.state == "closed"
    assert closed.code == a.code
    states = {r.code: r.state for r in projects.load(home)}
    assert states == {a.code: "closed", b.code: "active"}
    again = projects.close(home, a.code)
    assert again.state == "closed"
    assert Path(a.path).is_dir()


def test_close_refuses_unknown_or_malformed_code(home, register_path):
    _spawn(home, register_path)
    with pytest.raises(projects.ProjectError, match="not registered"):
        projects.close(home, "tcp-zzzz")
    with pytest.raises(projects.ProjectError) as ei:
        projects.close(home, fixtures.CUSTOMER_FORMS[1])
    fixtures.assert_no_fixture_name(str(ei.value), "error message")



# --- open (T5) ----------------------------------------------------------------------------------------------

def _fake_sudo(tmp_path, monkeypatch, allowed=True) -> Path:
    """A sudo on PATH that writes its arguments, one call per line, and refuses `-n` when not allowed."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "sudo.log"
    sudo = bindir / "sudo"
    sudo.write_text("#!/bin/sh\nprintf '%%s|' \"$@\" >> %s\necho >> %s\nexit %d\n"
                    % (log, log, 0 if allowed else 1), encoding="utf-8")
    sudo.chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bindir, os.environ.get("PATH", "")))
    monkeypatch.setattr(awb.config, "work_user", lambda: "awbwork")
    monkeypatch.setattr(awb.config, "is_work_user", lambda: False)
    return log


def _open(code, capsys) -> tuple[int, list[str]]:
    from awb import cli

    rc = cli.main(["projects", "open", code])
    return rc, capsys.readouterr().err.strip().splitlines()


def test_open_runs_the_session_as_the_work_user(home, register_path, tmp_path, monkeypatch, capsys):
    pr = _spawn(home, register_path)
    log = _fake_sudo(tmp_path, monkeypatch)
    rc, err = _open(pr.code, capsys)
    assert (rc, err) == (0, [])
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == ["-n|-u|awbwork|true|",
                     "-n|-u|awbwork|-H|bash|-lc|cd %s && claude|" % pr.path]


@pytest.mark.parametrize("state", ["unknown", "malformed", "deleted"])
def test_open_refuses_an_unknown_or_deleted_code(home, register_path, tmp_path, monkeypatch, capsys, state):
    pr = _spawn(home, register_path)
    code = {"unknown": "tcp-zzzz", "malformed": "tcp-", "deleted": pr.code}[state]
    if state == "deleted":
        projects.close(home, pr.code)
        projects.delete(home, pr.code)
    log = _fake_sudo(tmp_path, monkeypatch)
    rc, err = _open(code, capsys)
    assert rc == 2 and len(err) == 1
    assert not log.exists()


def test_open_is_refused_for_the_work_user(home, register_path, tmp_path, monkeypatch, capsys):
    pr = _spawn(home, register_path)
    log = _fake_sudo(tmp_path, monkeypatch)
    monkeypatch.setattr(awb.config, "is_work_user", lambda: True)
    rc, err = _open(pr.code, capsys)
    assert rc == 2 and len(err) == 1 and "owner side" in err[0]
    assert not log.exists()


def test_open_is_refused_where_sudo_n_is_not_allowed(home, register_path, tmp_path, monkeypatch, capsys):
    pr = _spawn(home, register_path)
    log = _fake_sudo(tmp_path, monkeypatch, allowed=False)
    rc, err = _open(pr.code, capsys)
    assert rc == 2 and len(err) == 1 and "sudo -n" in err[0]
    assert log.read_text(encoding="utf-8").splitlines() == ["-n|-u|awbwork|true|"]


# --- load ---------------------------------------------------------------------------------------------------

GOOD = ["tcp-abcd", "engagement", fixtures.CUSTOMER_CODE, "tcp", "/srv/p/tcp-abcd", "-srv-p-tcp-abcd", "active",
        fixtures.TODAY]
OTHER = ["tcp-efgh", "lab", "none", "tcp", "/srv/p/tcp-efgh", "-srv-p-tcp-efgh", "closed", fixtures.TODAY]
THIRD = ["tcp-wxyz", "engagement", fixtures.CUSTOMER_CODE, "tcp", "/srv/p/tcp-wxyz", "-srv-p-tcp-wxyz", "active",
         fixtures.TODAY]
NAME = fixtures.CUSTOMER_FORMS[0]


def _row(**change) -> str:
    """A valid third row with one or more fields changed; its code differs from the two good rows."""
    fields = dict(zip(projects.HEADER, THIRD))
    fields.update(change)
    return "\t".join(fields[h] for h in projects.HEADER)


def _write(home, lines: list[str]) -> None:
    home.projects_register.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_missing_file_is_empty(home):
    assert projects.load(home) == []


def test_load_reads_valid_rows(home):
    _write(home, ["\t".join(projects.HEADER), "\t".join(GOOD), "\t".join(THIRD), "\t".join(OTHER)])
    rows = projects.load(home)
    assert rows == [projects.Project(*GOOD), projects.Project(*THIRD), projects.Project(*OTHER)]


@pytest.mark.parametrize("bad", [
    "\t".join(THIRD) + "\t" + NAME,
    "\t".join(THIRD[:-1]),
    _row(kind=NAME),
    _row(kind="poc"),
    _row(code=NAME),
    _row(code="tcp-abc1"),
    _row(customer=NAME),
    _row(customer=fixtures.PERSON_CODE),
    _row(platform="hcs"),
    _row(path="srv/p/tcp-wxyz", memory_key="srv-p-tcp-wxyz"),
    _row(path="/srv/%s/tcp-wxyz" % NAME),
    _row(memory_key="-srv-%s" % NAME),
    _row(state="open"),
    _row(created="22.09.2026"),
    _row(created="2026-02-30"),
    "# " + NAME,
    "",
    "\t".join(THIRD) + "\r",
    _row(path="/srv/p/tcp-efgh", memory_key="-srv-p-tcp-efgh"),
    _row(path="/srv/p/../p/tcp-wxyz", memory_key="-srv-p-..-p-tcp-wxyz"),
    _row(path="/srv/p//tcp-wxyz", memory_key="-srv-p--tcp-wxyz"),
    "\t".join(GOOD),
])
def test_load_refuses_malformed_line_naming_the_line_only(home, bad):
    _write(home, ["\t".join(projects.HEADER), "\t".join(GOOD), bad, "\t".join(OTHER)])
    with pytest.raises(projects.ProjectError) as ei:
        projects.load(home)
    msg = str(ei.value)
    assert "line 3" in msg
    fixtures.assert_no_fixture_name(msg, "error message")
    for field in bad.split("\t"):
        if len(field.strip()) >= 5:
            assert field.strip() not in msg


@pytest.mark.parametrize("header", [
    "\t".join(projects.HEADER[:-1]),
    "\t".join(projects.HEADER).upper(),
    "\ufeff" + "\t".join(projects.HEADER),
    NAME,
])
def test_load_refuses_bad_header(home, header):
    _write(home, [header, "\t".join(GOOD)])
    with pytest.raises(projects.ProjectError) as ei:
        projects.load(home)
    assert "line 1" in str(ei.value)
    fixtures.assert_no_fixture_name(str(ei.value), "error message")


def test_load_refuses_invalid_utf8(home):
    home.projects_register.write_bytes(("\t".join(projects.HEADER) + "\n").encode() + b"\xff\xfe\n")
    with pytest.raises(projects.ProjectError, match="UTF-8"):
        projects.load(home)


def test_spawn_refuses_to_add_to_a_malformed_register(home, register_path):
    _write(home, ["\t".join(projects.HEADER), "\t".join(GOOD), "# " + NAME])
    with pytest.raises(projects.ProjectError, match="line 3"):
        _spawn(home, register_path)
    assert [d.name for d in home.projects_root.iterdir()] == []


# --- review findings (2026-09-22) -------------------------------------------------------------------------

@pytest.mark.parametrize("goal", [
    "migration for " + " ".join(fixtures.CUSTOMER_FORMS[1]),
    "migration for " + fixtures.ORG_FORMS[1].replace("ö", "%F6"),
    "migration for " + fixtures.CUSTOMER_FORMS[1].replace("o", "о"),
])
def test_goal_with_a_disguised_registered_name_is_refused(home, register_path, goal):
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


@pytest.mark.parametrize("goal", [
    "migration for the " + fixtures.PLANTED_CANDIDATE,
    "migration for " + fixtures.PLANTED_CANDIDATE.upper(),
    "call " + "Herr " + fixtures.PLANTED_PERSON.split()[1] + " about the network",
])
def test_goal_with_an_unregistered_name_is_refused(home, register_path, goal):
    pr = _spawn(home, register_path, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


def test_spawn_without_a_register_is_refused(home, tmp_path):
    """B-088: a project without a customer needs no register at all."""
    missing = tmp_path / "no-vault" / "register.tsv"
    goal = "migration for " + fixtures.CUSTOMER_FORMS[0]
    pr = _spawn(home, missing, customer=None, goal=goal)
    assert _scope(Path(pr.path))["goal"] == goal


def test_ordinary_capitalised_goal_still_spawns(home, register_path):
    goal = " ".join(("Design", "the", "Landing", "Zone", "for", "the", "first", "Workload"))
    pr = _spawn(home, register_path, goal=goal)
    assert pr.code.startswith("tcp-")


# --- client hooks (Release 2) -------------------------------------------------------------------------------

HOOK_EVENTS = {"UserPromptSubmit": "prompt", "PreToolUse": "pre-write", "PostToolUse": "post-write",
               "Stop": "stop", "SessionStart": "session-start"}


def _settings(pr) -> dict:
    import json
    return json.loads((Path(pr.path) / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_spawn_writes_client_settings_whose_tool_guard_takes_the_workflow_tool_and_compact(home, register_path):
    """Replaces test_spawn_writes_client_settings_whose_tool_guard_takes_the_workflow_tool (Q-0006, 2026-11-20):
    the settings of a new project carry PreCompact as well, which runs `compact` (the pinned block of STATE.md).
    Kept from the replaced test: the place guard (pre-tool) takes every file tool, both shell tools and the
    Workflow tool."""
    pr = _spawn(home, register_path)
    settings = _settings(pr)
    assert set(settings) == {"hooks"}
    assert set(settings["hooks"]) == set(HOOK_EVENTS) | {"PreCompact"}
    got = {event: [(e.get("matcher"), h["type"], h["command"].rsplit(" -m awb hook ", 1)[-1])
                   for e in entries for h in e["hooks"]] for event, entries in settings["hooks"].items()}
    write = "Write|Edit|MultiEdit|NotebookEdit"
    assert got == {
        "UserPromptSubmit": [(None, "command", "prompt")],
        "PreToolUse": [(write, "command", "pre-write"),
                       ("Read|Edit|MultiEdit|NotebookEdit|Write|Glob|Grep|Bash|Monitor|Workflow", "command",
                        "pre-tool")],
        "PostToolUse": [(write, "command", "post-write")],
        "Stop": [(None, "command", "stop")],
        "SessionStart": [(None, "command", "session-start")],
        "PreCompact": [(None, "command", "compact")],
    }
    for entries in settings["hooks"].values():
        for e in entries:
            assert all(" -m awb hook " in h["command"] for h in e["hooks"])
    # committed with the skeleton
    assert ".claude/settings.json" in _git(Path(pr.path), "ls-files").split()


def test_client_settings_use_this_interpreter_and_its_isolation_without_a_home_path(home, register_path):
    """Replaces test_client_settings_use_this_interpreter_without_a_home_path (SB0): the suite of a build runs as
    `python -I`, and a spawn from an isolated interpreter writes `-I` into the hooks too (T3)."""
    from awb import gate
    pr = _spawn(home, register_path)
    text = (Path(pr.path) / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert gate.DETECTORS["homepath"](text) == []
    exe = Path(sys.executable)
    try:
        shown = '"$HOME/%s"' % exe.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = str(exe)
    isolated = " -I" if sys.flags.isolated else ""
    assert _settings(pr)["hooks"]["Stop"][0]["hooks"][0]["command"] == shown + isolated + " -m awb hook stop"


@needs_hook_process
@pytest.mark.parametrize("prompt, code", [
    ("size two app clusters for the next phase", 0),
    ("size the app clusters of " + fixtures.CUSTOMER_FORMS[0], 2),
])
def test_the_prompt_hook_of_a_spawned_project_runs_in_a_shell(home, register_path, prompt, code):
    import json
    pr = _spawn(home, register_path)
    command = _settings(pr)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": pr.path, "prompt": prompt})
    res = subprocess.run(["sh", "-c", command], input=payload, capture_output=True, text=True, cwd=pr.path,
                         timeout=60)
    assert res.returncode == code, res.stderr[-300:]
    fixtures.assert_no_fixture_name(res.stderr + res.stdout, "hook output")
    if code:
        assert "registered name" in res.stderr


# --- the seal: an encrypted vault and the work side (Release 2) ----------------------------------------------

VAULT_PASS = "fixture passphrase of the projects 5"


@pytest.fixture
def daemon(home, monkeypatch):
    """A vault daemon in threads of this process on short sockets under /tmp, stopped and removed at the end."""
    import shutil
    import tempfile
    import threading

    from awb import config, vault

    monkeypatch.setattr(vault, "S2K_COUNT", 65536)
    short = Path(tempfile.mkdtemp(prefix="awb", dir="/tmp"))
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(short / "run" / "check.sock"))
    monkeypatch.setenv("AWB_ADMIN_SOCKET", str(short / "admin.sock"))
    p = config.paths()
    d = vault.Daemon(p)
    try:
        d.start()
        yield p
    finally:
        d.stop()
        shutil.rmtree(short, ignore_errors=True)
    assert not [t for t in threading.enumerate() if t.name.startswith("awb-vault")]


def _encrypt(p, unlock: bool = True) -> None:
    from awb import vault

    vault.encrypt_vault(p, VAULT_PASS)
    if unlock:
        vault.admin_call("unlock", p.admin_sock, passphrase=VAULT_PASS)


def test_spawn_on_the_owner_side_of_an_encrypted_vault(daemon):
    p = daemon
    _encrypt(p)
    assert not p.register.exists()
    pr = _spawn(p, p.register)
    assert pr.customer == fixtures.CUSTOMER_CODE
    # B-088: the goal runs no name check, the daemon is asked for the customer code only
    named = _spawn(p, p.register, goal="migration for " + fixtures.CUSTOMER_FORMS[0])
    assert [r.code for r in projects.load(p)] == [pr.code, named.code]


def test_spawn_on_the_work_side_checks_names_over_the_check_socket(daemon, tmp_path):
    p = daemon
    work_register = tmp_path / "not-here" / "register.tsv"   # the work user cannot see the vault at all
    with pytest.raises(projects.ProjectError, match="no intake issued") as ei:
        _spawn(p, work_register)
    assert fixtures.CUSTOMER_CODE in str(ei.value)
    (p.outbox / fixtures.CUSTOMER_CODE).mkdir(parents=True)
    # B-088: the check socket is not asked for the goal, a name in it is kept as written
    named = _spawn(p, work_register, goal="migration for " + fixtures.CUSTOMER_FORMS[1])
    pr = _spawn(p, work_register)
    assert pr.customer == fixtures.CUSTOMER_CODE
    assert _scope(Path(named.path))["goal"] == "migration for " + fixtures.CUSTOMER_FORMS[1]
    assert (Path(pr.path) / ".claude" / "settings.json").is_file()


def test_spawn_on_the_work_side_refuses_while_the_vault_is_locked(daemon, tmp_path):
    p = daemon
    _encrypt(p, unlock=False)
    (p.outbox / fixtures.CUSTOMER_CODE).mkdir(parents=True)
    # B-088: no name check, so a locked vault stops a spawn only where the register itself must be read
    pr = _spawn(p, tmp_path / "not-here" / "register.tsv")
    assert pr.customer == fixtures.CUSTOMER_CODE
    with pytest.raises(projects.ProjectError, match="register cannot be read"):
        _spawn(p, p.register)
    assert [r.code for r in projects.load(p)] == [pr.code]


# --- the commit gate of a project -------------------------------------------------------------------------------

def _commit(folder: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), "-c", "user.name=awb-test", "-c", "user.email=awb@example.org",
                           "-c", "commit.gpgsign=false", "commit", "-q", "-m", "work"],
                          capture_output=True, text=True, check=False)


def test_a_spawned_project_gets_the_commit_gate(home, register_path, monkeypatch):
    from awb import gate
    monkeypatch.setenv("PYTHONPATH", str(Path(awb.__file__).resolve().parent.parent))
    pr = _spawn(home, register_path)
    folder = Path(pr.path)
    hook = folder / ".git" / "hooks" / "pre-commit"
    text = hook.read_text(encoding="utf-8")
    assert gate.HOOK_MARK in text and "kb verify" not in text
    assert hook.stat().st_mode & 0o100
    head = _git(folder, "rev-parse", "HEAD").strip()
    planted = "remote push uses " + "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi" + "\n"
    (folder / "evidence" / "notes.md").write_text(planted, encoding="utf-8")
    _git(folder, "add", "-A")
    # DMZ (B-087..B-089): a project commit is inside; the gate hook stays but checks no content, so a planted
    # token commits; the check of secrets runs at the entry and at the public exits
    taken = _commit(folder)
    assert taken.returncode == 0, taken.stdout + taken.stderr
    assert _git(folder, "rev-parse", "HEAD").strip() != head
    (folder / "evidence" / "notes.md").write_text("two app clusters, one per zone\n", encoding="utf-8")
    _git(folder, "add", "-A")
    passed = _commit(folder)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_spawn_takes_a_reserved_code_and_refuses_one_that_is_taken(home, register_path):
    pr = projects.spawn(home, "query", "Compare the storage classes of the region", "none", register_path,
                        code="tcp-q7m4")
    assert pr.code == "tcp-q7m4" and [p.code for p in projects.load(home)] == ["tcp-q7m4"]
    for code in ("tcp-q7m4", "tcp-q7m1", "hcs-q7m4", "tcp-Q7M4"):
        with pytest.raises(projects.ProjectError, match="reserved project code"):
            projects.spawn(home, "query", "Compare the storage classes again", "none", register_path, code=code)
    assert len(projects.load(home)) == 1


def test_a_spawned_project_opens_open_md_with_the_decisions_section(home, register_path):
    """T13: a question that does not stop the work goes into OPEN.md with its default, not into the chat."""
    pr = _spawn(home, register_path)
    folder = Path(pr.path)
    text = (folder / "OPEN.md").read_text(encoding="utf-8")
    assert "## Decisions for the owner, with defaults" in text
    assert projects.open_items(folder) == 0              # the section alone is no open item: close still works


# --- T11: the git identity of a project ---------------------------------------------------------------------

def test_a_spawned_project_carries_the_workbench_identity_and_a_plain_commit_works(home, register_path, monkeypatch,
                                                                                  tmp_path):
    monkeypatch.setenv("PYTHONPATH", str(Path(awb.__file__).resolve().parent.parent))
    pr = _spawn(home, register_path)
    folder = Path(pr.path)
    assert _git(folder, "config", "--local", "user.name").strip() == projects._GIT_NAME
    assert _git(folder, "config", "--local", "user.email").strip() == "awb@localhost"
    (folder / "evidence" / "notes.md").write_text("two app clusters, one per zone\n", encoding="utf-8")
    _git(folder, "add", "-A")
    env = dict(os.environ, HOME=str(tmp_path), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env.pop("GIT_AUTHOR_NAME", None)
    env.pop("GIT_AUTHOR_EMAIL", None)
    res = subprocess.run(["git", "-C", str(folder), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "work"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _git(folder, "log", "-1", "--format=%an <%ae>").strip() == "%s <awb@localhost>" % projects._GIT_NAME


def test_the_resources_template_puts_the_handle_first_and_asks_for_the_full_id(home, register_path):
    folder = Path(_spawn(home, register_path).path)
    text = (folder / "RESOURCES.md").read_text(encoding="utf-8")
    assert "| handle | id |" in text
    assert "in full" in text and "awb cloud sweep" in text


def test_live_resources_reads_handle_rows_and_rows_of_the_older_width(home, register_path):
    folder = Path(_spawn(home, register_path).path)
    with open(folder / "RESOURCES.md", "a", encoding="utf-8") as f:
        f.write("| ecs-3 | srv-id-0003 | ecs | eu-de | small | 2026-12-31 | live | |\n"
                "| evs-1 | vol-id-0001 | evs | eu-de | small | none | deleted | |\n"
                "| eip-id-0001 | eip | eu-de | small | none | kept | the address of the demo |\n"
                "| obs-id-0001 | obs | eu-de | small | none | live | |\n")
    assert projects.live_resources(folder) == 2


def test_spawn_from_the_outbox_commits_through_the_gate_hook(home, register_path):
    """TM0 item 10: the gate hook is there before the first git add, so a planted outbox copy the gate refuses
    (a private key) never enters history: nothing is created and the outbox keeps the file."""
    box = home.outbox / fixtures.CUSTOMER_CODE
    box.mkdir(parents=True, exist_ok=True)
    planted = box / "brief.md"
    planted.write_text("sizing\n-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----\n",
                       encoding="utf-8")
    # DMZ (B-087..B-089): the outbox copies passed the entry check already; the spawn checks them no second time
    pr = projects.spawn(home, "engagement", GOAL, fixtures.CUSTOMER_CODE, register_path, from_outbox=True)
    assert (Path(pr.path) / ".git" / "hooks" / "pre-commit").is_file() and not planted.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens every folder")
def test_spawn_without_the_check_module_names_no_traceback_for_a_vault_it_cannot_open(home, register_path,
                                                                                       monkeypatch):
    """A vault folder the work user may not open answers with the refusal, not with PermissionError (2026-10-12);
    B-088: without the check module the customer code is looked up in the register alone."""
    monkeypatch.delattr(awb, "check", raising=False)
    monkeypatch.setitem(sys.modules, "awb.check", None)
    os.chmod(home.vault, 0)
    try:
        with pytest.raises(projects.ProjectError, match="register"):
            projects.spawn(home, "engagement", GOAL, fixtures.CUSTOMER_CODE, home.register)
    finally:
        os.chmod(home.vault, 0o700)
    assert projects.load(home) == []


# --- project administration hardening -----------------------------------------------------------


def _admin_project(home, code="tcp-abcd", state="closed", folder=None):
    """A local project fixture without hooks, installed packages or a repository above the test."""
    folder = folder or home.projects_root / code
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "RESOURCES.md").write_text(projects._resources(code), encoding="utf-8")
    (folder / "SCOPE.md").write_text("- goal: compare storage classes\n", encoding="utf-8")
    row = projects.Project(code, "query", "none", "tcp", str(folder), projects.memory_key(folder), state,
                           fixtures.TODAY)
    projects._save(home, projects.load(home) + [row])
    return row


@pytest.mark.timeout(5)
@pytest.mark.parametrize("kind", ["link", "dangling", "directory", "fifo"])
def test_admin_load_refuses_nonregular_register(home, tmp_path, kind):
    target = tmp_path / "outside.tsv"
    if kind == "link":
        target.write_text("\t".join(projects.HEADER) + "\n", encoding="utf-8")
    if kind in ("link", "dangling"):
        home.projects_register.symlink_to(target)
    elif kind == "directory":
        home.projects_register.mkdir()
    else:
        os.mkfifo(home.projects_register)
    with pytest.raises(projects.ProjectError):
        projects.load(home)


@pytest.mark.parametrize("stage", ["mkdir", "mkstemp", "fchmod", "fsync", "replace"])
def test_admin_register_io_errors_hide_values_and_preserve_snapshot(home, monkeypatch, stage):
    row = _admin_project(home)
    before = home.projects_register.read_bytes()
    secret = "pass" + "word=fixture-admin-value"

    def refused(*args, **kwargs):
        raise PermissionError(secret)

    module = Path if stage == "mkdir" else projects.tempfile if stage == "mkstemp" else projects.os
    monkeypatch.setattr(module, stage, refused)
    with pytest.raises(projects.ProjectError) as error:
        projects._save(home, [])
    assert secret not in str(error.value)
    assert home.projects_register.read_bytes() == before
    assert not list(home.shared.glob(".projects.tsv.*.tmp"))
    assert projects.load(home) == [row]


@pytest.mark.parametrize("kind", ["link", "fifo", "directory"])
def test_admin_lock_refuses_nonregular_file(home, tmp_path, kind):
    lock = home.shared / ".projects.lock"
    target = tmp_path / "private"
    target.write_text("untouched", encoding="utf-8")
    if kind == "link":
        lock.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(lock)
    else:
        lock.mkdir()
    with pytest.raises(projects.ProjectError):
        with projects.locked(home):
            pytest.fail("unsafe lock accepted")
    assert target.read_text() == "untouched"


@pytest.mark.parametrize("char", ["\u202e", "\u2028", "\u2029", "\ud800"])
def test_admin_register_rejects_invisible_or_unencodable_paths(home, char):
    path = "/srv/" + char + "/tcp-abcd"
    row = projects.Project(*GOOD)
    row.path, row.memory_key = path, projects.memory_key(path)
    with pytest.raises(projects.ProjectError):
        projects._save(home, [row])
    assert not home.projects_register.exists()


@pytest.mark.timeout(5)
@pytest.mark.parametrize("kind", ["link", "directory", "fifo", "folder-link", "invalid"])
def test_admin_goal_does_not_read_redirected_or_invalid_files(home, tmp_path, kind):
    row = _admin_project(home)
    folder = Path(row.path)
    scope = folder / "SCOPE.md"
    scope.unlink()
    target = tmp_path / "private.md"
    target.write_text("- goal: outside-only-value\n", encoding="utf-8")
    if kind == "link":
        scope.symlink_to(target)
    elif kind == "directory":
        scope.mkdir()
    elif kind == "fifo":
        os.mkfifo(scope)
    elif kind == "folder-link":
        moved = tmp_path / "moved"
        folder.rename(moved)
        (moved / "SCOPE.md").write_text(target.read_text(), encoding="utf-8")
        folder.symlink_to(moved, target_is_directory=True)
    else:
        scope.write_bytes(b"- goal: \xff\n")
    assert projects.goal_of(folder) == ""


@pytest.mark.parametrize("value", ["pass" + "word=fixture-admin-value", "gh" + "p_" + "Q" * 36],
                         ids=["assignment", "token"])
def test_admin_list_withholds_secrets_in_changed_goals(home, capsys, value):
    from awb import cli

    row = _admin_project(home)
    (Path(row.path) / "SCOPE.md").write_text("- goal: " + value + "\n", encoding="utf-8")
    assert cli.main(["projects", "list"]) == 0
    output = capsys.readouterr()
    # B-088: a goal is listed as written
    assert value in output.out and output.err == ""
    assert row.code in output.out
    assert "withheld" not in output.out


def test_admin_goal_withholds_when_pattern_checks_fail(home, monkeypatch):
    row = _admin_project(home)
    secret = "pass" + "word=fixture-admin-value"
    (Path(row.path) / "SCOPE.md").write_text("- goal: " + secret, encoding="utf-8")
    # B-088: no pattern check inside, so there is nothing that can fail: the goal is read as written
    assert projects.goal_of(row.path) == secret


@pytest.mark.timeout(5)
@pytest.mark.parametrize("kind", ["missing", "link", "dangling", "directory", "fifo", "invalid", "malformed"])
def test_admin_delete_requires_readable_resource_inventory(home, tmp_path, kind):
    row = _admin_project(home)
    resource = Path(row.path) / "RESOURCES.md"
    resource.unlink()
    target = tmp_path / "outside.md"
    if kind == "link":
        target.write_text(projects._resources(row.code), encoding="utf-8")
    if kind in ("link", "dangling"):
        resource.symlink_to(target)
    elif kind == "directory":
        resource.mkdir()
    elif kind == "fifo":
        os.mkfifo(resource)
    elif kind == "invalid":
        resource.write_bytes(b"\xff")
    elif kind == "malformed":
        resource.write_text("inventory is unavailable", encoding="utf-8")
    with pytest.raises(projects.ProjectError, match="RESOURCES.md"):
        projects.delete(home, row.code)
    assert Path(row.path).is_dir()
    assert projects.load(home) == [row]


def test_admin_delete_unreadable_inventory_hides_os_error(home, monkeypatch):
    row = _admin_project(home)
    original = os.open
    secret = "pass" + "word=fixture-admin-value"

    def unreadable(path, *args, **kwargs):
        if Path(path).name == "RESOURCES.md":
            raise PermissionError(secret)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(projects.os, "open", unreadable)
    with pytest.raises(projects.ProjectError) as error:
        projects.delete(home, row.code)
    assert secret not in str(error.value)
    assert Path(row.path).is_dir() and projects.load(home) == [row]


def test_admin_delete_checks_holder_even_when_folder_is_missing(home):
    import shutil

    row = _admin_project(home)
    shutil.rmtree(row.path)
    with pytest.raises(projects.ProjectError, match="live session"):
        projects.delete(home, row.code, holder=lambda code: code == row.code)
    assert projects.load(home) == [row]
    assert projects.delete(home, row.code).state == "deleted"


@pytest.mark.parametrize("missing", [False, True])
def test_admin_delete_refuses_outside_paths_even_when_missing(home, tmp_path, missing):
    import shutil

    row = _admin_project(home, folder=tmp_path / "outside" / "tcp-abcd")
    if missing:
        shutil.rmtree(row.path)
    with pytest.raises(projects.ProjectError, match="projects root"):
        projects.delete(home, row.code)
    assert projects.load(home) == [row]
    assert Path(row.path).exists() != missing


def test_admin_delete_detects_folder_replacement_during_holder_check(home, tmp_path):
    row = _admin_project(home)
    folder = Path(row.path)
    moved = tmp_path / "original"

    def replace_folder(code):
        folder.rename(moved)
        folder.mkdir()
        (folder / "keep").write_text("replacement", encoding="utf-8")
        return False

    with pytest.raises(projects.ProjectError):
        projects.delete(home, row.code, holder=replace_folder)
    assert (folder / "keep").read_text() == "replacement"
    assert (moved / "SCOPE.md").is_file()
    assert projects.load(home) == [row]


def test_admin_delete_failure_hides_paths_and_keeps_closed_row(home, monkeypatch):
    import shutil

    row = _admin_project(home)
    secret = "pass" + "word=fixture-admin-value"

    def refused(*args, **kwargs):
        raise PermissionError(secret)

    monkeypatch.setattr(shutil, "rmtree", refused)
    with pytest.raises(projects.ProjectError) as error:
        projects.delete(home, row.code)
    assert secret not in str(error.value)
    assert Path(row.path).is_dir() and projects.load(home) == [row]


def test_admin_check_refuses_malformed_register_instead_of_reporting_orphans(home):
    _admin_project(home)
    home.projects_register.write_text("broken", encoding="utf-8")
    with pytest.raises(projects.ProjectError):
        projects.unregistered(home)


def test_admin_check_does_not_follow_directory_links_or_expose_names(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.projects_root / "tcp-abcd").symlink_to(outside, target_is_directory=True)
    (home.projects_root / ("tcp-" + NAME)).mkdir()
    assert projects.unregistered(home) == ["a folder"]


def test_admin_check_unreadable_root_is_not_a_clean_result(home, monkeypatch):
    original = os.scandir
    secret = "pass" + "word=fixture-admin-value"

    def refused(path):
        if Path(path) == home.projects_root:
            raise PermissionError(secret)
        return original(path)

    monkeypatch.setattr(projects.os, "scandir", refused)
    with pytest.raises(projects.ProjectError) as error:
        projects.unregistered(home)
    assert secret not in str(error.value)


def test_admin_concurrent_deletes_remove_once_and_preserve_other_rows(home, monkeypatch):
    import shutil
    import threading
    from concurrent.futures import ThreadPoolExecutor

    row = _admin_project(home)
    other = _admin_project(home, code="tcp-efgh")
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original = shutil.rmtree
    removed = []

    def slow_remove(*args, **kwargs):
        removed.append(args[0])
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def second_delete():
        attempted.set()
        return projects.delete(home, row.code)

    monkeypatch.setattr(shutil, "rmtree", slow_remove)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(projects.delete, home, row.code)
        try:
            assert entered.wait(5)
            second = pool.submit(second_delete)
            assert attempted.wait(5)
            # The first run owns the lock across the filesystem change and the register write.
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5).state == second.result(timeout=5).state == "deleted"
    assert len(removed) == 1
    assert projects.load(home)[1] == other and Path(other.path).is_dir()


def test_admin_list_rechecks_names_and_hides_unchecked_goals(home, monkeypatch, capsys):
    from awb import cli, ledger

    row = _admin_project(home)
    scope = Path(row.path) / "SCOPE.md"
    scope.write_text("- goal: compare storage for " + NAME, encoding="utf-8")
    assert cli.main(["projects", "list"]) == 0
    out = capsys.readouterr()
    # B-088: no name check inside: the goal is listed as written, whatever the register says
    assert NAME in out.out and "withheld" not in out.out and out.err == ""
    scope.write_text("- goal: compare storage classes", encoding="utf-8")

    def unavailable(*args):
        raise ledger.LedgerError(NAME)

    monkeypatch.setattr(ledger, "hit_indexes", unavailable)
    assert cli.main(["projects", "list"]) == 0
    out = capsys.readouterr()
    assert "compare storage classes" in out.out and "name check is unavailable" not in out.out


def test_admin_list_deleted_filter_and_missing_goal(home, capsys):
    from awb import cli

    active = _admin_project(home, state="active")
    deleted = _admin_project(home, code="tcp-efgh", state="deleted")
    (Path(active.path) / "SCOPE.md").unlink()
    assert cli.main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert active.code in out and deleted.code not in out
    assert cli.main(["projects", "list", "--all", "--paths"]) == 0
    out = capsys.readouterr().out
    assert active.path in out and deleted.path in out


def test_admin_sync_selection_is_idempotent_and_keeps_notes_and_modes(home):
    from awb import rulesync

    rows = [_admin_project(home, code=code, state=state) for code, state in
            [("tcp-abcd", "active"), ("tcp-efgh", "parked"), ("tcp-wxyz", "closed"), ("tcp-zz22", "deleted")]]
    old, new = rulesync.STATE_FIXES[0]
    before = "Status: Started.\nNext: Compare storage.\n" + old + "\nLocal notes stay here.\n"
    for row in rows:
        path = Path(row.path) / "STATE.md"
        path.write_text(before, encoding="utf-8")
        path.chmod(0o640)
    selected = rows[1]
    path = Path(selected.path) / "STATE.md"
    inode = path.stat().st_ino
    assert rulesync.sync(home, [selected.code]) == [(selected.code, ["STATE.md"])]
    assert path.read_text() == before.replace(old, new)
    assert path.stat().st_mode & 0o777 == 0o640 and path.stat().st_ino == inode
    assert rulesync.sync(home, [selected.code]) == [(selected.code, [])]
    assert rulesync.sync(home) == [(rows[0].code, ["STATE.md"]), (selected.code, [])]
    for row in rows[2:]:
        assert (Path(row.path) / "STATE.md").read_text() == before


@pytest.mark.parametrize("kind", ["link", "missing", "directory", "invalid", "unreadable"])
def test_admin_sync_ignores_unusable_files_without_touching_outside(home, tmp_path, monkeypatch, kind):
    from awb import rulesync

    row = _admin_project(home, state="active")
    path = Path(row.path) / "STATE.md"
    (Path(row.path) / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    outside = tmp_path / "private.md"
    before = rulesync.STATE_FIXES[0][0] + "\n" + NAME
    outside.write_text(before, encoding="utf-8")
    if kind == "link":
        path.symlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    elif kind == "invalid":
        path.write_bytes(b"\xff")
    elif kind == "unreadable":
        path.write_text(before, encoding="utf-8")
        path.chmod(0)
        try:
            with open(path, "r+b"):
                still_usable = True          # a privileged user (the build runner) reads it anyway
        except PermissionError:
            still_usable = False
        try:
            if still_usable:
                # the file is not unusable for this user, so it is synced like any plain file
                assert rulesync.sync(home) == [(row.code, ["STATE.md"])]
            else:
                assert rulesync.sync(home) == [(row.code, [])]
        finally:
            path.chmod(0o640)
        assert outside.read_text() == before
        return
    assert rulesync.sync(home) == [(row.code, [])]
    assert outside.read_text() == before


def test_admin_sync_unknown_code_does_not_echo_input_or_change_project(home, capsys):
    from awb import cli, rulesync

    row = _admin_project(home, state="active")
    path = Path(row.path) / "STATE.md"
    before = rulesync.STATE_FIXES[0][0]
    path.write_text(before, encoding="utf-8")
    assert cli.main(["projects", "sync", NAME]) == 1
    output = capsys.readouterr()
    assert NAME not in output.out + output.err
    assert path.read_text() == before


def test_admin_delete_root_replacement_cannot_redirect_removal(home, tmp_path):
    row = _admin_project(home)
    moved = tmp_path / "original-root"
    outside = tmp_path / "outside-root"
    replacement = outside / row.code
    replacement.mkdir(parents=True)
    (replacement / "keep").write_text("outside", encoding="utf-8")

    def replace_root(code):
        home.projects_root.rename(moved)
        home.projects_root.symlink_to(outside, target_is_directory=True)
        return False

    assert projects.delete(home, row.code, holder=replace_root).state == "deleted"
    assert (replacement / "keep").read_text() == "outside"
    assert not (moved / row.code).exists()
    assert projects.load(home)[0].state == "deleted"
