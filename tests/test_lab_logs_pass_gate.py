"""Q-0010 (finding 11 of the UX review of 2026-11-18): lab logs pass the commit gate on the first try. The lab log
writers write a home path as ~, the gate does not read a fact id (KB-) or a file id (F-) of the Workbench as a name
and the commit-msg hook of a project takes a co-author or generator line naming Claude out of the message instead of
refusing the commit. Every project, user, run and value here is invented."""
from __future__ import annotations

import subprocess
from pathlib import Path

from awb import codes, config, gate, lablog, labclient, winlabsession
from awb.intake import engine
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "tcp-ab2c"
RUN = "run-20261120-100000-ab2c"
SMALL = 4096
NOT_INSTALLED = Path("/nonexistent/usr/local/bin/awb")

# home paths of an invented user, split so that this file itself carries none
LINUX = "/ho" + "me/builder/lab/out.txt"
WINDOWS = "C:\\Us" + "ers\\builder\\Desktop\\setup.log"
WINDOWS_PROMPT = "PS C:\\Us" + "ers\\Administrator> dir"
MAC = "/Us" + "ers/builder/Library/x"
ROOT_HOME = "/ro" + "ot/.ssh/known_hosts"
ACRONYM = fixtures.CUSTOMER_FORMS[2]          # a registered short form of three letters
CO_AUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@" + "anthropic.com>"
GENERATED = "\U0001f916 Generated with [Claude " + "Code](https://claude.com/claude-code)"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "projects" / PROJECT
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# %s\n\n- code: %s\n- customer: %s\n" % (PROJECT, PROJECT,
                                                                           fixtures.CUSTOMER_CODE), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _home_lines() -> list[str]:
    return ["copied to %s" % LINUX, "log at %s" % WINDOWS, WINDOWS_PROMPT, "cache %s" % MAC, "read %s" % ROOT_HOME,
            '{"path": "\\/ho' + 'me\\/builder\\/x"}']


def _no_homepath(text: str) -> None:
    assert gate._detect_homepath(text) == [], text
    assert "builder" not in text and "Administrator" not in text


# --------------------------------------------------------------------------- (1) the writers mask home paths


def test_q10_mask_home_writes_the_home_part_as_a_tilde():
    assert lablog.mask_home("copied to " + LINUX) == "copied to ~/lab/out.txt"
    assert lablog.mask_home("log at " + WINDOWS) == "log at ~\\Desktop\\setup.log"
    assert lablog.mask_home(WINDOWS_PROMPT) == "PS ~> dir"
    assert lablog.mask_home(MAC) == "~/Library/x"
    assert lablog.mask_home(ROOT_HOME) == "~/.ssh/known_hosts"
    for line in _home_lines():
        _no_homepath(lablog.mask_home(line))
    # a generic user the gate lets pass and a text without a home path stay as they are
    generic = "/ho" + "me/user/x and /srv/awb/y and ~/z"
    assert lablog.mask_home(generic) == generic
    assert lablog.mask_home("nothing here\n") == "nothing here\n"


def test_q10_a_small_lab_run_log_carries_no_home_path_and_passes_the_gate(tmp_path, monkeypatch):
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    lablog.append(root, log, "\n".join(["## 2026-11-20T10:00:00Z GET ecs"] + _home_lines()) + "\n\n")
    text = log.read_text(encoding="utf-8")
    _no_homepath(text)
    assert "~/lab/out.txt" in text and "~\\Desktop\\setup.log" in text
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert gate.main(["--staged", "--no-register", "--repo", str(root)]) == 0


def test_q10_a_large_log_its_full_text_and_its_summary_carry_no_home_path(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    for n in range(20):
        lines = ["## 2026-11-20T10:00:%02dZ POST ecs /v1/{project_id}/cloudservers" % n, "answer: 200",
                 "x" * 300, "recorded: server-%d %s" % (n, LINUX)] + _home_lines()
        lablog.append(root, log, "\n".join(lines) + "\n\n")
    full = log.parent / lablog.FULL / log.name
    summary = log.parent / (RUN + lablog.SUMMARY_SUFFIX)
    assert full.is_file() and summary.is_file() and not log.exists()
    _no_homepath(full.read_text(encoding="utf-8"))
    text = summary.read_text(encoding="utf-8")
    _no_homepath(text)
    assert "- server-0 ~/lab/out.txt" in text
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert gate.main(["--staged", "--no-register", "--repo", str(root)]) == 0


def test_q10_the_lab_client_writes_its_run_log_without_a_home_path(home, tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    labclient.log(root, RUN, _home_lines())
    text = (root / labclient.LOG_FOLDER / (RUN + ".log")).read_text(encoding="utf-8")
    _no_homepath(text)
    assert "~/lab/out.txt" in text


def test_q10_a_windows_lab_log_carries_no_home_path_in_and_outside_a_project(tmp_path):
    root = _project(tmp_path)
    path = root / "notes" / "winlab" / "jobs" / "job-0002.log"
    winlabsession._append(path, ["## 2026-11-20T11:00:00Z console"] + _home_lines())
    text = path.read_text(encoding="utf-8")
    _no_homepath(text)
    assert "PS ~> dir" in text
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert gate.main(["--staged", "--no-register", "--repo", str(root)]) == 0
    loose = tmp_path / "elsewhere" / "notes" / "winlab" / "loose.log"
    winlabsession._append(loose, _home_lines())
    _no_homepath(loose.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- (2) the own ids are no names


def test_q10_the_own_id_pattern_is_spelled_in_codes():
    assert codes.OWN_ID_RE.fullmatch("KB-Q7M4") and codes.OWN_ID_RE.fullmatch("F-AB2C")
    assert codes.OWN_ID_RE.fullmatch("F-AB2C.1.2")
    for bad in ("KB-Q7M0", "KB-Q7M4X", "F-AB2", "F-ab2c", "XKB-Q7M4", "KB-Q7M4-PERS"):
        assert not codes.OWN_ID_RE.fullmatch(bad) and not codes.OWN_ID_RE.search(bad), bad


def test_q10_a_registered_acronym_inside_an_own_id_is_no_name_in_a_file(register_path, tmp_path):
    """Four random id characters can spell a registered acronym: before Q-0010 the gate refused them as a name."""
    matcher = gate._load_matcher(register_path)
    kb_id, file_id = "KB-%s2" % ACRONYM, "F-%sQ.1" % ACRONYM
    assert codes.OWN_ID_RE.fullmatch(kb_id) and codes.OWN_ID_RE.fullmatch(file_id)
    notes = tmp_path / "notes.md"
    notes.write_text("checked against %s and %s\n" % (kb_id, file_id), encoding="utf-8")
    assert gate._scan_paths([notes], matcher) == []
    # the same letters outside an id and a registered form beside an id are still found
    notes.write_text("ok %s\nsee %s with %s\nKB-%s27\n" % (kb_id, kb_id, ACRONYM, ACRONYM), encoding="utf-8")
    assert [(f.line, f.cls) for f in gate._scan_paths([notes], matcher)] == [(2, "name"), (3, "name")]
    notes.write_text("see %s for %s\n" % (kb_id, fixtures.CUSTOMER_FORMS[0]), encoding="utf-8")
    assert [(f.line, f.cls) for f in gate._scan_paths([notes], matcher)] == [(1, "name")]


def test_q10_own_ids_in_a_commit_message_are_neither_names_nor_candidates(register_path, tmp_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Facts checked\n\nKB-%s2, F-ABCD.1: ok\nKB-ABCD, KB-EFGH\n" % ACRONYM, encoding="utf-8")
    assert gate.scan_message(msg, gate._load_matcher(register_path)) == []
    assert gate.main(["--register", str(register_path), "--message", str(msg)]) == 0
    # words in the place of the ids are still candidates, so the rule itself is unchanged
    unregistered = fixtures.CONTROL_UNREGISTERED
    assert engine.short_text_candidates("%s, %s: ok" % (unregistered, unregistered)) == [("person", 1)]
    assert engine.short_text_candidates("F-ABCD, KB-EFGH: ok") == []
    capsys.readouterr()


# --------------------------------------------------------------------------- (3) the commit-msg hook strips


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=awb-test", "-c", "user.email=awb-test@example.org",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def test_q10_strip_attribution_takes_out_the_lines_and_keeps_the_rest(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    raw = ("A change\n\nThe body.\n\nCo-Authored-By: colleague <colleague@example.org>\n%s\n\n%s\n\n"
           "# Please enter the commit message\n# ------------------------ >8 ------------------------\n"
           "%s\n" % (GENERATED, CO_AUTHOR, CO_AUTHOR))
    msg.write_text(raw, encoding="utf-8")
    assert gate.strip_attribution(msg) == 2
    assert msg.read_text(encoding="utf-8") == (
        "A change\n\nThe body.\n\nCo-Authored-By: colleague <colleague@example.org>\n\n"
        "# Please enter the commit message\n# ------------------------ >8 ------------------------\n%s\n" % CO_AUTHOR)
    assert gate.strip_attribution(msg) == 0
    clean = "A change\n\nThe body.\n"
    msg.write_text(clean, encoding="utf-8")
    assert gate.strip_attribution(msg) == 0 and msg.read_text(encoding="utf-8") == clean
    # a comment in the middle keeps the blank line after the subject
    msg.write_text("A change\n\n# a comment\nbody\n%s\n" % CO_AUTHOR, encoding="utf-8")
    assert gate.strip_attribution(msg) == 1
    assert msg.read_text(encoding="utf-8") == "A change\n\n# a comment\nbody\n"


def test_q10_the_gate_strips_only_with_the_flag(tmp_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("A change\n\n%s\n" % CO_AUTHOR, encoding="utf-8")
    assert gate.main(["--no-register", "--message", str(msg)]) == 1          # the refusal of the plain gate stays
    assert "attribution" in capsys.readouterr().out
    assert gate.main(["--no-register", "--message", str(msg), "--strip-attribution"]) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [gate.STRIPPED_LINE % 1]
    assert msg.read_text(encoding="utf-8") == "A change\n"
    # another attribution finding (the address in prose) is no co-author line and still refuses
    msg.write_text("A change\n\nmail went to noreply@" + "anthropic.com\n", encoding="utf-8")
    assert gate.main(["--no-register", "--message", str(msg), "--strip-attribution"]) == 1
    assert "attribution" in capsys.readouterr().out


def test_q10_an_installed_project_hook_strips_the_lines_and_the_commit_passes(tmp_path, register_path,
                                                                              monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = tmp_path / PROJECT
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    gate.install_hook(repo, installed=NOT_INSTALLED)
    text = (repo / ".git" / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert '--message "$msg" --strip-attribution' in text
    assert subprocess.run(["sh", "-n", str(repo / ".git" / "hooks" / "commit-msg")]).returncode == 0
    (repo / "notes.txt").write_text("lab notes\n", encoding="utf-8")
    assert _git(repo, "add", "notes.txt").returncode == 0
    done = _git(repo, "commit", "-q", "-m", "Lab notes\n\nThe body.\n\n%s\n\n%s" % (GENERATED, CO_AUTHOR))
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [x for x in done.stderr.splitlines() if x.startswith("awb gate:")]
    assert lines == [gate.STRIPPED_LINE % 2]
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert body.rstrip("\n") == "Lab notes\n\nThe body."
    # any other finding of the message still refuses the commit
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    assert _git(repo, "add", "more.txt").returncode == 0
    refused = _git(repo, "commit", "-q", "-m", "More\n\nlog under %s\n%s" % (LINUX, CO_AUTHOR))
    assert refused.returncode != 0 and "homepath" in refused.stdout + refused.stderr
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
