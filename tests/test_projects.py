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
    goal = "review the network plan for " + form
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, goal=goal)
    msg = str(ei.value)
    assert goal not in msg
    assert form.lower() not in msg.lower()
    fixtures.assert_no_fixture_name(msg, "error message")
    assert "name" in msg
    _nothing_created(home)


def test_goal_with_structured_data_is_refused(home, register_path):
    goal = "open the firewall for 203.0.113.7 today"
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, goal=goal)
    msg = str(ei.value)
    assert "203.0.113.7" not in msg
    assert "ip" in msg
    _nothing_created(home)


def test_tag_with_a_registered_name_is_refused(home, register_path):
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, tags=["network", fixtures.CUSTOMER_FORMS[1].lower()])
    msg = str(ei.value)
    fixtures.assert_no_fixture_name(msg, "error message")
    assert "tag 2" in msg
    _nothing_created(home)


def test_a_blocked_tag_is_refused(home, register_path):
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, tags=[OLD_TAG])
    msg = str(ei.value)
    assert "blocklist" in msg
    assert OLD_TAG not in msg
    _nothing_created(home)


@pytest.mark.parametrize("tag", ["TIC" + "KT7", "x_" + OLD_TAG])
def test_a_blocked_tag_in_another_spelling_is_refused(home, register_path, tag):
    with pytest.raises(projects.ProjectError, match="blocklist"):
        _spawn(home, register_path, tags=[tag])
    _nothing_created(home)


def test_a_blocked_word_in_the_goal_is_refused(home, register_path):
    goal = "follow up the notes of " + OLD_GOAL_WORD
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, goal=goal)
    assert "blocklist" in str(ei.value)
    assert OLD_GOAL_WORD not in str(ei.value)
    _nothing_created(home)


def test_a_blocked_path_in_the_goal_is_refused(home, register_path):
    with pytest.raises(projects.ProjectError, match="blocklist"):
        _spawn(home, register_path, goal="port the tooling of " + OLD_PATH_WORD)
    _nothing_created(home)


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
    monkeypatch.delattr(awb, "check", raising=False)
    monkeypatch.setitem(sys.modules, "awb.check", None)
    with pytest.raises(projects.ProjectError, match="name check is not available"):
        _spawn(home, register_path)
    _nothing_created(home)


def test_final_check_refuses_a_generated_file_with_a_name(home, register_path, tmp_path, monkeypatch):
    rules = tmp_path / fixtures.CUSTOMER_FORMS[1].lower() / "CLAUDE.md"
    rules.parent.mkdir()
    rules.write_text("rules\n", encoding="utf-8")
    monkeypatch.setattr(projects, "RULES_FILE", rules)
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path)
    assert "CLAUDE.md" in str(ei.value)
    fixtures.assert_no_fixture_name(str(ei.value), "error message")
    _nothing_created(home)


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
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, goal=goal)
    fixtures.assert_no_fixture_name(str(ei.value), "error message")
    assert "name check" in str(ei.value)
    _nothing_created(home)


@pytest.mark.parametrize("goal", [
    "migration for the " + fixtures.PLANTED_CANDIDATE,
    "migration for " + fixtures.PLANTED_CANDIDATE.upper(),
    "call " + "Herr " + fixtures.PLANTED_PERSON.split()[1] + " about the network",
])
def test_goal_with_an_unregistered_name_is_refused(home, register_path, goal):
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, register_path, goal=goal)
    msg = str(ei.value)
    for word in fixtures.PLANTED_CANDIDATE.split()[:1] + fixtures.PLANTED_PERSON.split():
        assert word.lower() not in msg.lower()
    assert "not in the register" in msg
    _nothing_created(home)


def test_spawn_without_a_register_is_refused(home, tmp_path):
    missing = tmp_path / "no-vault" / "register.tsv"
    with pytest.raises(projects.ProjectError) as ei:
        _spawn(home, missing, customer=None, goal="migration for " + fixtures.CUSTOMER_FORMS[0])
    assert "register is missing" in str(ei.value)
    _nothing_created(home)


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


def test_spawn_writes_client_settings_with_the_five_hooks(home, register_path):
    pr = _spawn(home, register_path)
    settings = _settings(pr)
    assert set(settings) == {"hooks"}
    assert set(settings["hooks"]) == set(HOOK_EVENTS)
    for event, name in HOOK_EVENTS.items():
        entries = settings["hooks"][event]
        assert len(entries) == 1
        commands = [h["command"] for h in entries[0]["hooks"]]
        assert [h["type"] for h in entries[0]["hooks"]] == ["command"]
        assert len(commands) == 1 and commands[0].endswith(" -m awb hook " + name)
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    # committed with the skeleton
    assert ".claude/settings.json" in _git(Path(pr.path), "ls-files").split()


def test_client_settings_use_this_interpreter_without_a_home_path(home, register_path):
    from awb import gate
    pr = _spawn(home, register_path)
    text = (Path(pr.path) / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert gate.DETECTORS["homepath"](text) == []
    exe = Path(sys.executable)
    try:
        shown = '"$HOME/%s"' % exe.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = str(exe)
    assert _settings(pr)["hooks"]["Stop"][0]["hooks"][0]["command"] == shown + " -m awb hook stop"


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
    with pytest.raises(projects.ProjectError, match="name check") as ei:
        _spawn(p, p.register, goal="migration for " + fixtures.CUSTOMER_FORMS[0])
    fixtures.assert_no_fixture_name(str(ei.value), "error message")
    assert [r.code for r in projects.load(p)] == [pr.code]


def test_spawn_on_the_work_side_checks_names_over_the_check_socket(daemon, tmp_path):
    p = daemon
    work_register = tmp_path / "not-here" / "register.tsv"   # the work user cannot see the vault at all
    with pytest.raises(projects.ProjectError, match="no intake issued") as ei:
        _spawn(p, work_register)
    assert fixtures.CUSTOMER_CODE in str(ei.value)
    (p.outbox / fixtures.CUSTOMER_CODE).mkdir(parents=True)
    with pytest.raises(projects.ProjectError, match="name check") as ei:
        _spawn(p, work_register, goal="migration for " + fixtures.CUSTOMER_FORMS[1])
    fixtures.assert_no_fixture_name(str(ei.value), "error message")
    _nothing_created(p)
    pr = _spawn(p, work_register)
    assert pr.customer == fixtures.CUSTOMER_CODE
    assert (Path(pr.path) / ".claude" / "settings.json").is_file()


def test_spawn_on_the_work_side_refuses_while_the_vault_is_locked(daemon, tmp_path):
    p = daemon
    _encrypt(p, unlock=False)
    (p.outbox / fixtures.CUSTOMER_CODE).mkdir(parents=True)
    for reg in (tmp_path / "not-here" / "register.tsv", p.register):
        with pytest.raises(projects.ProjectError, match="vault locked") as ei:
            _spawn(p, reg)
        assert "nothing was created" in str(ei.value)
        _nothing_created(p)


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
    refused = _commit(folder)
    assert refused.returncode != 0
    assert "token" in refused.stdout + refused.stderr
    assert _git(folder, "rev-parse", "HEAD").strip() == head
    (folder / "evidence" / "notes.md").write_text("two app clusters, one per zone\n", encoding="utf-8")
    _git(folder, "add", "-A")
    passed = _commit(folder)
    assert passed.returncode == 0, passed.stdout + passed.stderr
