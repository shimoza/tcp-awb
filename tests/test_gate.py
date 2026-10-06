"""gate: one planted case per class, clean files pass, binaries are opaque, the staged content is what counts.

Every planted value is built from pieces so that this file passes the gate itself. Names come from
tests/fixtures.py, numbers are documentation examples.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from awb import check, config, gate, vault
from tests import fixtures
from tests.test_vault import encrypt_vault, fast_gpg, serving, sockets  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "pre-commit"

CUSTOMER = fixtures.CUSTOMER_FORMS[0]
SHORT = fixtures.CUSTOMER_FORMS[1]
ORG = fixtures.ORG_FORMS[0]
PLANTED_VALUE = "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
GH_BODY = "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
HEX32 = "4f1c9a7e2b6d" + "40f8a3c5e9b1d7f2a6c0"
UUID = "3f2b8c1e-9a4d-" + "4e7b-8c2f-1a5d6e7f8a9b"
CLEAN = "release 1.4.2 of 2026-09-22 for %s in tcp-q7m4, version v0.1.0\n" % fixtures.CUSTOMER_CODE


def assign(key: str, sep: str, value: str) -> str:
    return key + sep + value


def begin(kind: str) -> str:
    return "-----BEGIN " + kind + "PRI" + "VATE KEY-----"


# one line per class; each goes on line 3 of its own file
PLANTED = {
    "name": "offer for %s, second round" % CUSTOMER,
    "secret": assign("db_" + "pass" + "word", " = ", '"' + PLANTED_VALUE + '"'),
    "private-key": begin("RSA "),
    "token": "push with " + "gh" + "p_" + GH_BODY,
    "homepath": "written to /ho" + "me/builder/notes.txt",
    "blocklist": "a list in ~/legacy" + "-shared/index.md",
    "identifier": "project " + HEX32,
}

MORE_CASES = [
    ("secret", assign("OTC_" + "SK", "=", PLANTED_VALUE)),
    ("secret", '"api_' + 'key": "' + PLANTED_VALUE + '",'),
    ("secret", assign("  access" + "Key", ": ", PLANTED_VALUE)),
    ("secret", assign("token", ": '", PLANTED_VALUE + "'")),
    ("private-key", begin("OPENSSH ")),
    ("private-key", begin("EC ")),
    ("private-key", begin("")),
    ("token", "gith" + "ub_pat_" + "11ABCDEFG0" + "abcdefghij_KLMNOPQRS1"),
    ("token", "key s" + "k-" + "proj-" + "Ab3Cd5Ef7Gh9Ij2Kl4Mn6"),
    ("token", "slack xo" + "xb-" + "12345-67890-" + "AbCdEf"),
    ("token", "id AK" + "IA" + "Z7QX4MLP2R9TVW3B"),
    ("token", "id HP" + "UA" + "Z7QX4MLP2R9TVW3B"),
    ("homepath", "memory under -ho" + "me-builder-tcp-awb"),
    ("homepath", "cd /ho" + "me/builder"),
    ("identifier", "uuid " + UUID),
    ("identifier", "tenant " + "12" + "345"),
    ("identifier", "domain_id: " + "123" + "456"),
]

BLOCKED = [
    ("scheme-client", "CLI" + "ENT3"),
    ("scheme-partner", "PART" + "NR2"),
    ("scheme-ticket", "TIC" + "KT7"),
    ("scheme-marker", "MAR" + "KR_X"),
    ("names-main", "~/.config/names" + ".main"),
    ("names-local", "project/.names" + ".local"),
    ("names-marker", "names" + ".marker-only"),
    ("shared-folder", "~/legacy" + "-shared/x"),
    ("shared-folder-underscore", "~/legacy" + "_shared/y"),
    ("kb-export", "~/legacy" + "-kb" + "-export/z"),
    ("scrub", "~/legacy" + "-scrub/run.py"),
]

NOT_FOUND = [
    ("secret-reference", assign("db_" + "pass" + "word", " = ", '"${DB_PASSWORD_FROM_ENV}"')),
    ("secret-placeholder", assign("api_" + "key", ": ", "<your key goes here>")),
    ("secret-low-entropy", assign("sec" + "ret", " = ", '"' + "a" * 24 + '"')),
    ("secret-short", assign("pass" + "word", "=", "Zq8Lw4")),
    ("secret-env-name", assign("sk", ": ", "OTC_SECRET_ACCESS_KEY_NAME")),
    ("secret-dotted", assign("access_" + "key", " = ", "settings.cloud.access_key_value")),
    ("secret-plain-key", assign("region", " = ", PLANTED_VALUE)),
    ("token-kebab", "see s" + "k-" + "risk-assessment-for-the-new-platform"),
    ("token-inside-word", "the ta" + "sk-" + "0123456789abcdefghijklmn"),
    ("homepath-generic", "copy to /ho" + "me/user/project"),
    ("homepath-url", "https://www.example.org/ho" + "me/start"),
    ("identifier-git-sha", "commit " + "0123456789abcdef" * 2 + "01234567"),
    ("identifier-sha256", "digest " + "0123456789abcdef" * 4),
    ("identifier-nil-uuid", "uuid " + "0" * 8 + "-0000-0000-0000-" + "0" * 12),
    ("identifier-long-number", "tenant " + "1234" + "567"),
    ("blocklist-new-codes", "codes %s and tcp-q7m4" % fixtures.CUSTOMER_CODE),
]


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    """No git variable of a calling hook and no user or system git config leaks into these tests."""
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def plant(tmp_path: Path, line: str, name: str = "planted.txt") -> Path:
    f = tmp_path / name
    f.write_text("first line\nsecond line\n" + line + "\nlast line\n", encoding="utf-8")
    return f


# --------------------------------------------------------------------------- one class per planted file


@pytest.mark.parametrize("cls", list(PLANTED))
def test_one_planted_file_per_class_gives_exactly_that_finding(tmp_path, register_path, cls):
    f = plant(tmp_path, PLANTED[cls])
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, cls)]


@pytest.mark.parametrize("cls,line", MORE_CASES, ids=["%s-%d" % (c, i) for i, (c, _) in enumerate(MORE_CASES)])
def test_more_shapes_of_each_class(tmp_path, register_path, cls, line):
    f = plant(tmp_path, line)
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [(cls, 3)]


@pytest.mark.parametrize("value", [v for _, v in BLOCKED], ids=[i for i, _ in BLOCKED])
def test_what_the_blocklist_names_is_refused(tmp_path, register_path, value):
    f = plant(tmp_path, "see " + value + " for details")
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [("blocklist", 3)]


def test_without_a_blocklist_the_class_finds_nothing_and_a_bad_line_is_an_error(tmp_path, register_path,
                                                                                 monkeypatch):
    f = plant(tmp_path, "see ~/legacy" + "-shared/x")
    monkeypatch.setenv("AWB_BLOCKLIST", "")
    assert gate.scan_files([f], register_path) == []
    bad = tmp_path / "bad.txt"
    bad.write_text("# a comment\n(unclosed\n", encoding="utf-8")
    monkeypatch.setenv("AWB_BLOCKLIST", str(bad))
    with pytest.raises(gate.GateError, match="line 2 of the blocklist"):
        gate.scan_files([f], register_path)


@pytest.mark.parametrize("line", [v for _, v in NOT_FOUND], ids=[i for i, _ in NOT_FOUND])
def test_lookalikes_are_not_findings(tmp_path, register_path, line):
    assert gate.scan_files([plant(tmp_path, line)], register_path) == []


def test_clean_file_with_codes_version_and_date_gives_nothing(tmp_path, register_path):
    f = tmp_path / "clean.md"
    f.write_text(CLEAN + "near %s\n" % fixtures.CONTROL_UNREGISTERED, encoding="utf-8")
    assert gate.scan_files([f], register_path) == []


def test_every_planted_class_in_one_file_one_finding_each(tmp_path, register_path):
    f = tmp_path / "all.txt"
    f.write_text("\n".join(PLANTED.values()) + "\n", encoding="utf-8")
    got = gate.scan_files([f], register_path)
    assert [(x.cls, x.line) for x in got] == [(cls, i) for i, cls in enumerate(PLANTED, start=1)]


def test_name_line_is_right_after_crlf_and_soft_hyphen(tmp_path, register_path):
    broken = SHORT[:3] + "\u00ad" + SHORT[3:]
    f = tmp_path / "crlf.txt"
    f.write_bytes(("a\r\nb\r\nc\r\nsee " + broken + " here\r\n").encode("utf-8"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 4, "name")]


def test_latin1_file_is_decoded(tmp_path, register_path):
    f = tmp_path / "legacy.txt"
    f.write_bytes(("x\nsupplier %s\n" % ORG).encode("latin-1"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 2, "name")]


def test_without_a_register_names_are_not_checked_but_the_rest_is(tmp_path):
    name_file = plant(tmp_path, PLANTED["name"], "a.txt")
    token_file = plant(tmp_path, PLANTED["token"], "b.txt")
    assert gate.scan_files([name_file, token_file], None) == [gate.Finding(str(token_file), 3, "token")]


def test_findings_carry_file_line_and_class_only():
    assert [f.name for f in dataclasses.fields(gate.Finding)] == ["file", "line", "cls"]


# --------------------------------------------------------------------------- opaque


def test_binary_file_is_opaque(tmp_path, register_path):
    f = tmp_path / "image.bin"
    f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + PLANTED["token"].encode())
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 0, "opaque")]


def test_utf16_text_is_opaque_not_silently_clean(tmp_path, register_path):
    f = tmp_path / "wide.txt"
    f.write_bytes(("offer for %s\n" % CUSTOMER).encode("utf-16"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 0, "opaque")]


def test_nul_after_the_first_8_kb_is_scanned_as_text(tmp_path, register_path):
    f = tmp_path / "late.txt"
    f.write_bytes(b"a" * 9000 + b"\x00\n" + PLANTED["token"].encode() + b"\n")
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 2, "token")]


def test_unreadable_path_is_opaque(tmp_path, register_path):
    missing = tmp_path / "gone.txt"
    assert gate.scan_files([missing], register_path) == [gate.Finding(str(missing), 0, "opaque")]


# --------------------------------------------------------------------------- git


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=awb-test", "-c", "user.email=awb-test@example.org",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert git(path, "init", "-q").returncode == 0
    return path


def commit_all(repo: Path, *files: str) -> None:
    assert git(repo, "add", "--", *files).returncode == 0
    assert git(repo, "commit", "-q", "--no-verify", "-m", "base").returncode == 0


def test_staged_files_lists_added_and_modified_only(tmp_path):
    repo = new_repo(tmp_path / "repo")
    for name in ("kept.txt", "changed.txt", "removed.txt"):
        (repo / name).write_text("base\n", encoding="utf-8")
    commit_all(repo, "kept.txt", "changed.txt", "removed.txt")
    (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "sub" / "dir").mkdir(parents=True)
    (repo / "sub" / "dir" / "nested.txt").write_text("nested\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "kept.txt").write_text("edited, not staged\n", encoding="utf-8")
    assert git(repo, "add", "changed.txt", "new.txt", "sub/dir/nested.txt").returncode == 0
    assert git(repo, "rm", "-q", "removed.txt").returncode == 0

    want = sorted((repo / p).resolve() for p in ("changed.txt", "new.txt", "sub/dir/nested.txt"))
    assert sorted(p.resolve() for p in gate.staged_files(repo)) == want
    # from a sub folder the paths are still those of the work tree
    assert sorted(p.resolve() for p in gate.staged_files(repo / "sub")) == want


def test_staged_files_in_a_repo_without_commits(tmp_path):
    repo = new_repo(tmp_path / "fresh")
    assert gate.staged_files(repo) == []
    (repo / "first.txt").write_text("first\n", encoding="utf-8")
    assert git(repo, "add", "first.txt").returncode == 0
    assert [p.name for p in gate.staged_files(repo)] == ["first.txt"]


def test_staged_files_outside_a_repo_is_an_error(tmp_path):
    with pytest.raises(gate.GateError):
        gate.staged_files(tmp_path)


def run_staged(repo: Path, register_path: Path, capsys) -> tuple[int, list[str]]:
    code = gate.main(["--staged", "--repo", str(repo), "--register", str(register_path)])
    return code, capsys.readouterr().out.splitlines()


def test_staged_content_counts_not_the_work_tree(tmp_path, register_path, capsys):
    repo = new_repo(tmp_path / "repo")
    (repo / "notes.txt").write_text("intro\n" + PLANTED["token"] + "\n", encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    (repo / "notes.txt").write_text("intro\nnothing here\n", encoding="utf-8")
    assert run_staged(repo, register_path, capsys) == (1, ["token  notes.txt:2"])

    assert git(repo, "add", "notes.txt").returncode == 0
    (repo / "notes.txt").write_text("intro\n" + PLANTED["token"] + "\n", encoding="utf-8")
    assert run_staged(repo, register_path, capsys) == (0, [])


def test_staged_path_and_binary_are_checked(tmp_path, register_path, capsys):
    repo = new_repo(tmp_path / "repo")
    named = "offer-%s.md" % SHORT.lower()
    (repo / named).write_text("nothing inside\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert git(repo, "add", named, "blob.bin").returncode == 0
    code, lines = run_staged(repo, register_path, capsys)
    assert code == 1
    assert sorted(re.sub(r"  .*:", "  :", line) for line in lines) == ["name  :0", "opaque  :0"]
    # the printed path carries the class, never the form it was refused for
    fixtures.assert_no_fixture_name("\n".join(lines), "gate output")
    assert "name  offer-[name].md:0" in lines


def test_staged_nothing_is_clean(tmp_path, register_path, capsys):
    repo = new_repo(tmp_path / "repo")
    assert run_staged(repo, register_path, capsys) == (0, [])


# --------------------------------------------------------------------------- command line


def test_cli_prints_class_and_place_never_a_value(tmp_path, register_path, capsys):
    f = tmp_path / "all.txt"
    f.write_text("\n".join(PLANTED.values()) + "\n", encoding="utf-8")
    assert gate.main(["--register", str(register_path), str(f)]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == ["%s  %s:%d" % (cls, f, i) for i, cls in enumerate(PLANTED, start=1)]
    for stream in (out, err):
        fixtures.assert_no_fixture_name(stream, "gate output")
        for value in (PLANTED_VALUE, GH_BODY, HEX32, "builder", "shared"):
            assert value not in stream


def test_cli_walks_folders_and_exits_0_when_clean(tmp_path, register_path, capsys):
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "clean.md").write_text(CLEAN, encoding="utf-8")
    (work / ".git").mkdir()
    (work / ".git" / "skipped.txt").write_text(PLANTED["token"], encoding="utf-8")
    assert gate.main(["--register", str(register_path), str(work)]) == 0
    (work / "docs" / "dirty.md").write_text(PLANTED["homepath"], encoding="utf-8")
    assert gate.main(["--register", str(register_path), str(work)]) == 1
    assert capsys.readouterr().out.splitlines() == ["homepath  %s:1" % (work / "docs" / "dirty.md")]


def test_cli_without_arguments_is_a_usage_error(capsys):
    assert gate.main([]) == 2


def test_cli_broken_register_is_an_error_without_content(tmp_path, capsys):
    bad = tmp_path / "register.tsv"
    bad.write_text("code\tkind\n%s\tCUST\t%s\n" % (fixtures.CUSTOMER_CODE, SHORT), encoding="utf-8")
    f = plant(tmp_path, "nothing")
    assert gate.main(["--register", str(bad), str(f)]) == 2
    out, err = capsys.readouterr()
    fixtures.assert_no_fixture_name(out + err, "gate error")


# --------------------------------------------------------------------------- selftest


def test_selftest_passes(register_path):
    assert gate.selftest(None) == []
    assert gate.selftest(register_path) == []


@pytest.mark.parametrize("cls", list(gate.DETECTORS))
def test_selftest_fails_when_a_detector_is_removed(monkeypatch, cls):
    monkeypatch.setitem(gate.DETECTORS, cls, lambda text: [])
    failures = gate.selftest(None)
    assert len(failures) == 1 and failures[0].startswith(cls + ":")


def test_selftest_fails_when_names_are_not_matched(monkeypatch):
    monkeypatch.setattr(gate, "_detect_names", lambda text, matcher: [])
    failures = gate.selftest(None)
    assert len(failures) == 1 and failures[0].startswith("name:")


def test_selftest_fails_when_a_detector_fires_on_everything(monkeypatch):
    monkeypatch.setitem(gate.DETECTORS, "identifier", lambda text: [0])
    failures = gate.selftest(None)
    assert any(f.startswith("clean:") for f in failures)


def test_selftest_fails_when_binaries_pass_as_text(monkeypatch):
    monkeypatch.setattr(gate, "BINARY_PROBE", 0)
    assert any(f.startswith("opaque:") for f in gate.selftest(None))


def test_selftest_reports_a_broken_register_without_content(tmp_path):
    bad = tmp_path / "register.tsv"
    bad.write_text("code\tkind\tform\tadded\tstatus\n%s\tCUST\t%s #x\t2026-09-22\tactive\n"
                   % (fixtures.CUSTOMER_CODE, SHORT), encoding="utf-8")
    failures = gate.selftest(bad)
    assert len(failures) == 1 and "register" in failures[0]
    fixtures.assert_no_fixture_name(failures[0], "selftest failure")


def test_selftest_form_is_the_invented_fixture_form():
    assert gate._SELFTEST_FORM == fixtures.SELFTEST_FORM
    assert fixtures.SELFTEST_FORM not in fixtures.ALL_REGISTERED


def test_cli_selftest(monkeypatch, register_path, capsys):
    assert gate.main(["--selftest"]) == 0
    assert capsys.readouterr().out.startswith("selftest passed")
    monkeypatch.setitem(gate.DETECTORS, "token", lambda text: [])
    assert gate.main(["--selftest"]) == 1
    assert capsys.readouterr().out.startswith("selftest failed: token:")


# --------------------------------------------------------------------------- the hook


def test_hook_is_an_executable_posix_script():
    text = HOOK.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert HOOK.stat().st_mode & stat.S_IXUSR
    assert ".venv/bin/python" in text and "-m awb.gate --staged" in text
    assert subprocess.run(["sh", "-n", str(HOOK)], check=False).returncode == 0


def hook_repo(tmp_path: Path, with_python: bool = True) -> Path:
    repo = new_repo(tmp_path / "repo")
    if with_python:
        (repo / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, repo / ".venv" / "bin" / "python")
    return repo


def commit(repo: Path) -> subprocess.CompletedProcess:
    return git(repo, "-c", "core.hooksPath=%s" % HOOK.parent, "commit", "-q", "-m", "change")


def run_hook(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(HOOK)], cwd=repo, capture_output=True, text=True, check=False)


def refusal_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in (result.stdout + result.stderr).splitlines() if line.startswith("awb gate:")]


def test_hook_refuses_a_finding_and_lets_a_clean_commit_pass(tmp_path, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = hook_repo(tmp_path)
    (repo / "notes.txt").write_text(PLANTED["token"] + "\n", encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = commit(repo)
    assert refused.returncode != 0
    assert len(refusal_lines(refused)) == 2
    assert "token  notes.txt:1" in refused.stdout + refused.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    # the hook itself exits with the gate's code
    assert run_hook(repo).returncode == 1

    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    passed = commit(repo)
    assert passed.returncode == 0, refusal_lines(passed)
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def test_hook_refuses_without_the_virtual_environment(tmp_path, register_path):
    repo = hook_repo(tmp_path, with_python=False)
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = commit(repo)
    assert refused.returncode != 0
    assert len(refusal_lines(refused)) == 2
    assert run_hook(repo).returncode == 2


def test_own_files_pass_the_gate(register_path):
    own = [ROOT / "awb" / "gate.py", ROOT / "awb" / "check.py", HOOK,
           ROOT / "tests" / "test_gate.py", ROOT / "tests" / "test_check.py"]
    assert shutil.which("git") is not None
    assert gate.scan_files(own, register_path) == []


# --------------------------------------------------------------------------- review findings (2026-09-22)


def test_path_mode_masks_a_path_that_carries_a_form(tmp_path, register_path, capsys):
    folder = tmp_path / SHORT
    folder.mkdir()
    named = folder / ("%s-notes.md" % ORG.split()[0])
    named.write_text("nothing inside\n", encoding="utf-8")
    code = gate.main(["--register", str(register_path), str(named), str(folder / "gone.txt")])
    out, err = capsys.readouterr()
    assert code == 1
    fixtures.assert_no_fixture_name(out + err, "gate output")
    lines = out.splitlines()
    assert any(line.startswith("name  ") and line.endswith("[name]/[name]-notes.md:0") for line in lines)
    assert any(line.startswith("opaque  ") and line.endswith("[name]/gone.txt:0") for line in lines)


def test_scan_files_checks_the_path_for_names(tmp_path, register_path):
    f = tmp_path / ("angebot-%s.md" % SHORT.lower())
    f.write_text(CLEAN, encoding="utf-8")
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 0, "name")]
    assert gate.scan_files([f], None) == []


def test_cli_never_echoes_an_unknown_option(register_path, capsys):
    assert gate.main(["--register", str(register_path), "--" + SHORT]) == 2
    out, err = capsys.readouterr()
    fixtures.assert_no_fixture_name(out + err, "usage error")


def test_cli_without_a_register_is_an_error_unless_asked_for(tmp_path, capsys):
    f = plant(tmp_path, PLANTED["token"])
    missing = tmp_path / "no-register.tsv"
    assert gate.main(["--register", str(missing), str(f)]) == 2
    assert "no register" in capsys.readouterr().err
    assert gate.main(["--register", str(missing), "--no-register", str(f)]) == 1
    assert capsys.readouterr().out.startswith("token  ")


@pytest.mark.parametrize("line", [
    "memory key -ho" + "me-builder",
    "under -ho" + "me-builder/notes",
    "key -ho" + "me-builder.",
])
def test_dashed_home_path_that_ends_the_path(tmp_path, register_path, line):
    f = plant(tmp_path, line)
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "homepath")]


@pytest.mark.parametrize("line", [
    "scaffold ~/legacy" + "-default/x",
    "scaffold legacy" + "_default",
    "project ~/legacy" + "-demo01/notes",
    "project ~/legacy" + "-angebot/notes",
])
def test_blocked_scaffolds_and_project_folders(tmp_path, register_path, line):
    f = plant(tmp_path, line)
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "blocklist")]


def test_a_hanging_git_is_an_error(tmp_path, monkeypatch):
    def hang(*args, **kwargs):
        assert kwargs.get("timeout") == gate.GIT_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(gate.subprocess, "run", hang)
    with pytest.raises(gate.GateError) as err:
        gate.staged_files(tmp_path)
    assert "timed out" in str(err.value)


# --------------------------------------------------------------------------- the vault daemon (release 2)

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of mode 000")


def unreadable_copy(register_path: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "locked-away" / "register.tsv"
    copy.parent.mkdir()
    shutil.copy(register_path, copy)
    os.chmod(copy, 0)
    return copy


@needs_permissions
def test_gate_exits_2_and_reports_nothing_when_the_name_check_is_unavailable(tmp_path, register_path, sockets,
                                                                             capsys):
    copy = unreadable_copy(register_path, tmp_path)
    f = tmp_path / "all.txt"
    f.write_text("\n".join(PLANTED.values()) + "\n", encoding="utf-8")
    assert gate.main(["--register", str(copy), str(f)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == "awb gate: name check unavailable: no vault daemon"
    with pytest.raises(check.CheckUnavailable):
        gate.scan_files([f], copy)


@needs_permissions
def test_gate_finds_names_through_the_daemon(tmp_path, register_path, sockets, capsys):
    folder = tmp_path / SHORT
    folder.mkdir()
    f = folder / "all.txt"
    f.write_text("\n".join(PLANTED.values()) + "\n", encoding="utf-8")
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(("a\r\nb\r\nsee " + SHORT[:3] + "\u00ad" + SHORT[3:] + " here\r\n").encode("utf-8"))
    assert gate.main(["--register", str(register_path), str(f), str(crlf)]) == 1
    local = capsys.readouterr().out
    copy = unreadable_copy(register_path, tmp_path)
    with serving(config.paths()):
        assert gate.scan_files([f, crlf], copy) == gate.scan_files([f, crlf], register_path)
        assert gate.main(["--register", str(copy), str(f), str(crlf)]) == 1
    out, err = capsys.readouterr()
    assert out == local and err == ""
    assert "name  %s:3" % crlf in out.splitlines()
    assert any(line.endswith("[name]/all.txt:1") for line in out.splitlines())
    fixtures.assert_no_fixture_name(out, "gate output")


def test_gate_with_a_locked_vault_exits_2(home, sockets, fast_gpg, monkeypatch, tmp_path, capsys):
    f = plant(tmp_path, PLANTED["name"])
    with serving(config.paths()):
        assert encrypt_vault(monkeypatch) == 0
        capsys.readouterr()
        assert gate.main([str(f)]) == 1
        assert capsys.readouterr().out == "name  %s:3\n" % f
        assert vault.main(["lock"]) == 0
        capsys.readouterr()
        assert gate.main([str(f)]) == 2
        out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == "awb gate: name check unavailable: vault locked"
    # the selftest needs no register of the vault
    assert gate.selftest(home.register) == []


@needs_permissions
@pytest.mark.parametrize("fail_at", [3, 6], ids=["while-scanning", "while-masking"])
def test_gate_prints_nothing_when_the_check_fails_after_the_first_file(tmp_path, register_path, sockets,
                                                                       monkeypatch, capsys, fail_at):
    # two files, two findings: calls 1 to 4 scan paths and contents, calls 5 and 6 mask the printed paths
    copy = unreadable_copy(register_path, tmp_path)
    files = [plant(tmp_path, PLANTED["name"], "a.txt"), plant(tmp_path, PLANTED["token"], "b.txt")]
    calls = []

    def flaky(text, sock):
        calls.append(len(text))
        if len(calls) >= fail_at:
            raise vault.VaultUnavailable("no vault daemon")
        return check.check_text(text, register_path)

    monkeypatch.setattr(vault, "ping", lambda sock: "unlocked")
    monkeypatch.setattr(vault, "check_remote", flaky)
    assert gate.main(["--register", str(copy)] + [str(f) for f in files]) == 2
    out, err = capsys.readouterr()
    assert out == "" and err.strip() == "awb gate: name check unavailable: no vault daemon"
    assert len(calls) == fail_at


# --------------------------------------------------------------------------- self-test first and the installed hook


def stub_python(repo: Path, fail_on: str) -> None:
    """A .venv/bin/python that exits 1 when its arguments carry `fail_on` and runs the real interpreter else."""
    stub = repo / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text('#!/bin/sh\ncase "$*" in *%s*) exit 1;; esac\nexec %s "$@"\n'
                    % (fail_on, shlex.quote(sys.executable)), encoding="utf-8")
    os.chmod(stub, 0o755)


def test_the_repository_hook_runs_the_selftest_before_the_scan():
    text = HOOK.read_text(encoding="utf-8")
    assert "-m awb.gate --selftest" in text
    assert text.index("-m awb.gate --selftest") < text.index("-m awb.gate --staged")


def test_the_repository_hook_refuses_when_the_selftest_fails(tmp_path, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = hook_repo(tmp_path, with_python=False)
    stub_python(repo, "--selftest")
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = commit(repo)
    assert refused.returncode != 0
    assert any("self-test" in line for line in refusal_lines(refused))
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


NOT_INSTALLED = Path("/nonexistent/usr/local/bin/awb")


def installed(repo: Path, **kw) -> Path:
    kw.setdefault("installed", NOT_INSTALLED)
    return gate.install_hook(repo, **kw)


def plain_commit(repo: Path) -> subprocess.CompletedProcess:
    return git(repo, "commit", "-q", "-m", "change")


def test_an_installed_hook_refuses_a_finding_and_lets_a_clean_commit_pass(tmp_path, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = new_repo(tmp_path / "kb")
    hook = installed(repo)
    assert hook == repo / ".git" / "hooks" / "pre-commit"
    assert hook.stat().st_mode & stat.S_IXUSR
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and gate.HOOK_MARK in text
    assert subprocess.run(["sh", "-n", str(hook)], check=False).returncode == 0
    (repo / "notes.txt").write_text(PLANTED["token"] + "\n", encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = plain_commit(repo)
    assert refused.returncode != 0
    assert "token  notes.txt:1" in refused.stdout + refused.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    passed = plain_commit(repo)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_an_installed_hook_refuses_when_the_selftest_fails(tmp_path, register_path):
    repo = new_repo(tmp_path / "kb")
    stub = tmp_path / "awb-stub"
    stub.write_text('#!/bin/sh\ncase "$*" in *--selftest*) exit 1;; esac\nexit 0\n', encoding="utf-8")
    os.chmod(stub, 0o755)
    installed(repo, fallback=shlex.quote(str(stub)))
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = plain_commit(repo)
    assert refused.returncode != 0
    assert "self-test" in refused.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def test_the_installed_hook_prefers_the_awb_of_a_sealed_host(tmp_path):
    repo = new_repo(tmp_path / "kb")
    sealed = tmp_path / "usr-local-bin-awb"
    text = installed(repo, installed=sealed).read_text(encoding="utf-8")
    assert "[ -x %s ]" % shlex.quote(str(sealed)) in text


def test_install_leaves_a_foreign_hook_alone_unless_forced(tmp_path):
    repo = new_repo(tmp_path / "kb")
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(gate.GateError):
        installed(repo)
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    installed(repo, force=True)
    assert gate.HOOK_MARK in foreign.read_text(encoding="utf-8")
    installed(repo)   # its own hook is replaced without --force


def test_install_refuses_a_repository_with_a_hooks_path(tmp_path):
    repo = new_repo(tmp_path / "kb")
    assert git(repo, "config", "core.hooksPath", "hooks").returncode == 0
    with pytest.raises(gate.GateError):
        installed(repo)
    assert not (repo / "hooks" / "pre-commit").exists()


def test_gate_install_on_the_command_line(tmp_path, capsys):
    repo = new_repo(tmp_path / "kb")
    assert gate.main(["--install", "--repo", str(repo)]) == 0
    assert gate.HOOK_MARK in (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "commit hook" in capsys.readouterr().out


# --------------------------------------------------------------------------- the red team of 2026-09-27
# Every planted line is built from pieces (a key word split, a prefix split, an escape split from the rest) so
# that this file passes the gate itself, also over its normalised text. Values are invented.

VALUE = "Vb7Qk2Zn9" + "Lp4Rw8Xm3Ct6Yd"
USER_PASS_B64 = base64.b64encode(("builder:" + VALUE).encode()).decode()
LONG_B64 = base64.b64encode(bytes(range(200, 232))).decode()        # 44 characters, invented bytes
LETTERS62 = "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi9Qm"
JWT = "ey" + "J" + base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")[3:] + "." \
    + base64.urlsafe_b64encode(b'{"sub":"builder","iat":1}').decode().rstrip("=") + "." + LETTERS62[:38]
AK = "Q7M4KX2AB3" + "NP9ZTW5RHY"
SK = "k7Qz2Lp9Wx4Rv" + "8Nb3Mc6Yd1Tf5" + "Hg0Js7Ka2Le4Pq"
PLACE = fixtures.PLACE_FORMS[0]


def pw(sep: str = "=", value: str = VALUE) -> str:
    return "pass" + "word" + sep + value


SECRET_SHAPES = [
    ("flag-double-dash", "mysqldump --user=app --" + pw() + " app > dump.sql"),
    ("flag-single-dash", "tool -" + pw()),
    ("flag-quoted", 'tool --' + pw('="', VALUE + '"')),
    ("java-D", "java -Ddb." + pw() + " -jar app.jar"),
    ("exec-start", "ExecStart=/usr/bin/tool --" + pw()),
    ("inner-query", "url=jdbc:postgresql://db.example/app?user=builder&" + pw()),
    ("inner-env", "environment: PASS" + "WORD=" + VALUE),
    ("inner-cookie-first", "Cookie: tok" + "en=" + VALUE + "; theme=dark"),
    ("inner-set-cookie", "Set-Cookie: auth_tok" + "en=" + VALUE + "; Path=/"),
    ("inner-flow-map", "db: {" + pw(": ") + "}"),
    ("inner-json-url", '{"url": "https://api.example/v1?api' + 'key=' + VALUE + '"}'),
    ("userinfo-env", "DATABASE_URL=postgres://builder:" + VALUE + "@db.example:5432/app"),
    ("userinfo-bare", "https://builder:" + VALUE + "@scm.example"),
    ("userinfo-empty-user", "REDIS_URL=redis://:" + VALUE + "@cache.example:6379/0"),
    ("quoted-bang", pw(' = "', "!" + VALUE + '"')),
    ("quoted-at", pw(' = "', "@" + VALUE + '"')),
    ("quoted-bracket", pw(' = "', "[" + VALUE + '"')),
    ("quoted-dollar", pw('="', "$" + VALUE + '"')),
    ("quoted-brace", pw('="', "{" + VALUE + '}"')),
    ("quoted-parens", pw(' = "', "Vb7Qk2(Zn9)Lp4Rw8Xm3Ct6Yd" + '"')),
    ("snake-shaped-value", "sec" + "ret = k7q2zp9lw4rx8vn3_bm6yc0dh5t1"),
    ("env-shaped-value", "sec" + "ret = K7Q2ZP9LW4RX8VN3_BM6YC0DH5T1"),
    ("key-db-pass", "DB_PA" + "SS=" + VALUE),
    ("key-pw", "P" + "W=" + VALUE),
    ("key-pass-colon", "pa" + "ss: " + VALUE),
    ("key-passphrase", "PASS" + "PHRASE=" + VALUE),
    ("key-camel", "Pass" + "Word=" + VALUE),
    ("key-snake", "pass_" + "word=" + VALUE),
    ("key-passwort", "Pass" + "wort=" + VALUE),
    ("key-kennwort", "Kenn" + "wort: " + VALUE),
    ("key-geheimnis", "Gehe" + 'imnis = "' + VALUE + '"'),
    ("key-compound-de", "db_pass" + "wort=" + VALUE),
    ("key-digit-first", "2FA_SEC" + "RET=" + VALUE),
    ("key-client-key-data", "client-key" + "-data: " + LONG_B64),
    ("key-wireguard", "Private" + "Key = " + LONG_B64),
    ("key-account-key", "STORAGE=DefaultEndpointsProtocol=https;AccountName=stor01;Account" + "Key=" + LONG_B64
     + ";EndpointSuffix=core.windows.net"),
    ("key-master-key", "Master" + "Key=" + LONG_B64),
    ("key-signing-key", "SIGNING" + "_KEY=" + VALUE),
    ("key-credentials", "creden" + 'tials = "' + VALUE + '"'),
    ("key-auth", "au" + 'th = "' + VALUE + '"'),
    ("key-docker-auth", '{"auths": {"registry.example": {"au' + 'th": "' + USER_PASS_B64 + '"}}}'),
    ("xml-element", "<server><id>repo</id><pass" + "word>" + VALUE + "</pass" + "word></server>"),
    ("xml-element-upper", "<Pass" + "word>" + VALUE + "</Pass" + "word>"),
    ("xml-key-value", '<add key="pass' + 'word" value="' + VALUE + '" />'),
    ("yaml-anchor", pw(": &pw ")),
    ("yaml-tag", pw(": !!str ")),
    ("netrc", "machine api.example login builder pass" + "word " + VALUE),
    ("word-tab", "pass" + "word\t" + VALUE),
    ("dockerfile-env", "ENV PASS" + "WORD " + VALUE),
    ("sql-identified-by", "CREATE USER builder IDENTIFIED BY '" + VALUE + "';"),
    ("pgpass", "db.example:5432:app:builder:" + VALUE),
    ("htpasswd-apr1", "builder:$apr1$Zx8Kq3Lw$Rt2Vn5Bm9Yc4Hd6Jf1Gs0P"),
    ("htpasswd-bcrypt", "builder:$2y$10$Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi9QmAb"),
    ("header-basic", "Authorization: Basic " + USER_PASS_B64),
    ("header-token", "Authorization: Token " + VALUE),
    ("header-quoted", 'curl -H "Authorization: Basic ' + USER_PASS_B64 + '" https://api.example/v1'),
    ("curl-u", "curl -u builder:" + VALUE + " https://api.example/v1"),
    ("flag-p-space", "sshpass -p " + VALUE + " ssh app@db.example"),
    ("flag-p-glued", "mysql -uapp -p" + VALUE + " app"),
    ("nbsp-before-equals", "pass" + "word\u00a0= " + VALUE),
    ("pair-blank-line-between", AK + "\n\n" + SK),
    ("pair-nested-yaml", "credentials:\n  access:\n    key: " + AK + "\n  secret:\n    key: " + SK),
    ("pair-secret-first", SK + "\n" + AK),
    ("pair-slash-and-plus", AK + "\n" + SK[:12] + "/" + SK[13:30] + "+" + SK[31:]),
]

NEXT_LINE_SHAPES = [
    ("yaml-block", "db:\n  " + pw(": |", "") + "\n    " + VALUE),
    ("yaml-folded", pw(": >-", "") + "\n  " + VALUE),
    ("yaml-empty-key", "db:\n  " + pw(":", "") + "\n    " + VALUE),
    ("hcl-heredoc", pw(" = <<EOT", "") + "\n" + VALUE + "\nEOT"),
    ("json-next-line", '{\n  "pass' + 'word":\n    "' + VALUE + '"\n}'),
]

TABLE_SHAPES = [
    ("markdown", "| user | pass" + "word |\n|---|---|\n| builder | " + VALUE + " |"),
    ("csv", "user,pass" + "word\nbuilder," + VALUE),
    ("semicolon", "user;sec" + "ret\nbuilder;" + VALUE),
    ("tsv-three-words", "user\tsecret access key\nbuilder\t" + SK),
]

NOT_SECRET = [
    ("quoted-dollar-name", pw('="', "$DB_PASS" + 'WORD"')),
    ("quoted-dollar-path", 'mkdir -p "$work_home/.claude/skills"'),
    ("quoted-dollar-brace", pw('="', "${DB_PASS" + 'WORD}"')),
    ("unquoted-dollar-name", pw("=", "$DB_PASS" + "WORD")),
    ("env-shaped-no-digit", "sk: OTC_SECRET_ACCESS_KEY_NAME"),
    ("dotted-no-digit", "access_" + "key = settings.cloud.access_key_value"),
    ("kebab", "tok" + "en = some-long-kebab-case-reference-name"),
    ("url-value", "tok" + "en_url = https://login.example/oauth2/token"),
    ("userinfo-placeholder", "DATABASE_URL=postgres://user:pass" + "word@db.example/app"),
    ("userinfo-short", "https://builder:pw@scm.example"),
    ("next-line-is-a-mapping", "db:\n  " + pw(":", "") + "\n    file: /run/secrets/db"),
    ("next-line-not-indented", "PASS" + "WORD=\nDB_HOST=db.example"),
    ("next-line-list", pw(":", "") + "\n  - " + VALUE),
    ("flag-p-port", "docker run -p 127.0.0.1:8080:80 img"),
    ("flag-p-path", "mkdir -p /opt/tcp-awb/src/awb"),
    ("word-then-prose", "the pass" + "word of the account is kept in the vault register only"),
    ("table-prose-with-comma", "names, sec" + "rets and tokens\nsee, CUST-Q7M4-PERS-1"),
    ("table-rule-row", "| user | pass" + "word |\n|---|---|\n| builder | see the vault |"),
    ("fixture-passphrase", "PA" + 'SS = "fixture passphrase of the tests 7"'),
    ("auth-header-scheme-only", "Authorization: OBS <access key>:<signature>"),
    ("pass-reference", "AWB_BUCKET_KEYS=pa" + "ss:<entry of your password store>"),
    ("xml-other-element", "<ro" + "ot><kunde>" + VALUE + "</kunde></ro" + "ot>"),
    ("jwk-without-kty", '{"crv": "Ed25519", "d": "' + LETTERS62[:32] + '"}'),
]


@pytest.mark.parametrize("line", [v for _, v in SECRET_SHAPES], ids=[i for i, _ in SECRET_SHAPES])
def test_secret_shapes_of_the_red_team(tmp_path, register_path, line):
    found = gate.scan_files([plant(tmp_path, line)], register_path)
    assert len(found) == 1 and found[0].cls == "secret"
    assert 3 <= found[0].line <= 3 + line.count("\n")


@pytest.mark.parametrize("line", [v for _, v in NEXT_LINE_SHAPES], ids=[i for i, _ in NEXT_LINE_SHAPES])
def test_a_value_on_the_line_after_the_key_is_a_secret(tmp_path, register_path, line):
    f = tmp_path / "values.yaml"
    f.write_text("first line\n" + line + "\nlast line\n", encoding="utf-8")
    found = gate.scan_files([f], register_path)
    assert [x.cls for x in found] == ["secret"]
    assert found[0].line == 2 + line.count("\n", 0, line.index(VALUE))


@pytest.mark.parametrize("text", [v for _, v in TABLE_SHAPES], ids=[i for i, _ in TABLE_SHAPES])
def test_a_credential_table_is_a_secret_on_the_row(tmp_path, register_path, text):
    f = tmp_path / "handover.md"
    f.write_text("# credentials\n\n" + text + "\n", encoding="utf-8")
    found = gate.scan_files([f], register_path)
    assert [(x.cls, x.line) for x in found] == [("secret", 3 + text.count("\n"))]


@pytest.mark.parametrize("line", [v for _, v in NOT_SECRET], ids=[i for i, _ in NOT_SECRET])
def test_references_placeholders_and_prose_are_not_secrets(tmp_path, register_path, line):
    assert gate.scan_files([plant(tmp_path, line)], register_path) == []


def test_a_placeholder_word_inside_a_real_value_is_a_kept_limit():
    assert gate._detect_secret(pw(' = "', "Vb7Qk2Zn9SampleLp4Rw8Xm3" + '"')) == []


def test_inner_assignments_are_scanned_again_at_their_own_offset():
    text = "url=jdbc:postgresql://db.example/app?user=builder&" + pw() + "\n"
    keys = {k: (v, off) for k, v, off, _ in gate._assignments(text)}
    assert set(keys) >= {"url", "user", "pass" + "word"}
    assert keys["pass" + "word"] == (VALUE, text.index(VALUE))
    nested = "environment: PASS" + "WORD=" + VALUE + "\n"
    assert [(k, off) for k, _, off, _ in gate._assignments(nested)][1] == ("PASS" + "WORD", nested.index(VALUE))


def test_a_jwt_is_a_token_wherever_it_stands(tmp_path, register_path):
    for line in ("Authorization: Bearer " + JWT, "tok" + "en = " + JWT, "use " + JWT + " here",
                 'curl -H "Authorization: Bearer ' + JWT + '" https://api.example/v1'):
        assert [x.cls for x in gate.scan_files([plant(tmp_path, line)], register_path)] == ["token"], line[:8]


TOKENS = [
    ("gh-oauth", "gh" + "o_" + LETTERS62[:36]),
    ("gh-server", "gh" + "s_" + LETTERS62[:36]),
    ("gh-user", "gh" + "u_" + LETTERS62[:36]),
    ("gh-refresh", "gh" + "r_" + LETTERS62[:36]),
    ("slack-app", "xa" + "pp-1-A0Q7M4KX2-1234567890123-" + LETTERS62),
    ("slack-refresh", "xo" + "xe-1-" + LETTERS62),
    ("slack-webhook", "https://hooks.sla" + "ck.com/services/T0Q7M4KX2/B0AB3NP9Z/" + LETTERS62[:24]),
    ("gitlab", "gl" + "pat-" + LETTERS62[:20]),
    ("google-api", "AI" + "zaSy" + LETTERS62[:33]),
    ("google-oauth", "ya" + "29." + LETTERS62 + LETTERS62),
    ("sendgrid", "S" + "G." + LETTERS62[:22] + "." + (LETTERS62 + LETTERS62)[:43]),
    ("npm", "//registry.npmjs.org/:_authToken=np" + "m_" + LETTERS62[:36]),
    ("pypi", "py" + "pi-AgEIcHlwaS5vcmcCJ" + LETTERS62 + LETTERS62),
    ("hugging-face", "h" + "f_" + LETTERS62[:34]),
    ("stripe", "sk_" + "live_" + LETTERS62[:24]),
    ("aws-temporary", "id AS" + "IA" + AK[:16]),
    ("telegram", "bot 123456789" + "0:AAF" + LETTERS62[:32]),
    ("discord", "MTIzNDU2Nzg5MDEyMzQ1Njc4" + "." + "Gabcde." + LETTERS62),
    ("twilio-key", "S" + "K" + HEX32),
    ("twilio-account", "sid A" + "C" + HEX32),
]


@pytest.mark.parametrize("line", [v for _, v in TOKENS], ids=[i for i, _ in TOKENS])
def test_token_prefixes_of_the_red_team(tmp_path, register_path, line):
    assert [(x.cls, x.line) for x in gate.scan_files([plant(tmp_path, line)], register_path)] == [("token", 3)]


PRIVATE_KEYS = [
    ("putty", "PuTTY-User-" + "Key-File-3: ssh-ed25519\nEncryption: none\nComment: builder\nPublic-Lines: 1\n"
     + LONG_B64 + "\nPrivate-" + "Lines: 1\n" + LONG_B64),
    ("jwk-with-d", '{"k' + 'ty": "OKP", "crv": "Ed25519", "x": "' + LETTERS62[:32] + '", "d": "'
     + LETTERS62[1:33] + '"}'),
    ("ssh2-armour-spaces", "---- BEGIN SSH2 ENCRYPTED PRI" + "VATE KEY ----\nComment: builder"),
    ("armour-with-spaces-inside", "----- BEGIN RSA PRI" + "VATE KEY -----"),
    ("armour-tab", "-----BEGIN RSA\tPRI" + "VATE KEY-----"),
    ("armour-double-space", "-----BEGIN RSA  PRI" + "VATE KEY-----"),
    ("armour-nbsp", "-----BEGIN RSA PRI" + "VATE\u00a0KEY-----"),
    ("age-secret-key", "AGE-SECRET-KEY-" + "1QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7LQPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L"),
    ("base64-of-rsa-armour", "LS0tLS1C" + "RUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQo" + LONG_B64),
    ("base64-of-pkcs8-armour", "LS0tLS1C" + "RUdJTiBQUklWQVRFIEtFWS0tLS0tCg" + LONG_B64),
]


@pytest.mark.parametrize("text", [v for _, v in PRIVATE_KEYS], ids=[i for i, _ in PRIVATE_KEYS])
def test_private_keys_without_the_plain_armour_line(tmp_path, register_path, text):
    f = tmp_path / "key.txt"
    f.write_text("first line\n" + text + "\n", encoding="utf-8")
    found = gate.scan_files([f], register_path)
    assert found and {x.cls for x in found} == {"private-key"}


def test_the_base64_of_a_certificate_armour_is_not_a_private_key(tmp_path, register_path):
    cert = base64.b64encode(("-----BEGIN CERTIFICATE-----\n" + LONG_B64 + "\n-----END CERTIFICATE-----\n").encode())
    f = tmp_path / "kubeconfig.yaml"
    f.write_text("certificate-authority-data: " + cert.decode() + "\n", encoding="utf-8")
    assert gate.scan_files([f], register_path) == []


def test_a_whole_pem_in_base64_is_found_on_the_line_of_the_block(tmp_path, register_path):
    pem = ("-----BEGIN " + "EC PRI" + "VATE KEY-----\n" + LONG_B64 + "\n" + LONG_B64 + "\n-----END EC PRI"
           + "VATE KEY-----\n")
    # base64 of text that starts one byte before the armour: the prefix rule cannot see it, the decoded block can
    block = base64.b64encode(("\n" + pem).encode()).decode()
    f = tmp_path / "kubeconfig.yaml"
    f.write_text("users:\n- name: builder\n  user:\n    client-key" + "-data: " + block + "\n", encoding="utf-8")
    found = gate.scan_files([f], register_path)
    assert {x.cls for x in found} == {"secret", "private-key"} and {x.line for x in found} == {4}


HOMEPATHS = [
    ("macos", "saved to /Us" + "ers/builder/Documents/offer.docx"),
    ("macos-file-url", "open file:///Us" + "ers/builder/Documents/offer.docx"),
    ("windows", "saved to C:\\Us" + "ers\\builder\\Documents\\offer.docx"),
    ("wsl-mount", "cp /mnt/c/Us" + "ers/builder/Documents/x ."),
    ("cygwin", "cd C:\\cygwin64\\ho" + "me\\builder"),
    ("wsl-unc", "open \\\\wsl$\\Ubuntu\\ho" + "me\\builder\\x"),
    ("super-user", "key in /ro" + "ot/.ssh/id_ed25519"),
    ("tilde-user", "log in ~buil" + "der/awb.log"),
    ("export-home", "cd /export/ho" + "me/builder"),
    ("double-slash", "cd /ho" + "me//builder"),
    ("umlaut-user", "cd /ho" + "me/\u00f6laf/x"),
    ("percent-encoded", "https://viewer.example/?path=%2Fho" + "me%2Fbuilder%2Fawb.log"),
    ("json-escaped-slash", '{"log": "\\/ho' + 'me\\/builder\\/awb.log"}'),
    ("json-unicode-slash", '{"log": "\\u002fho' + 'me\\u002fbuilder\\u002fawb.log"}'),
]


@pytest.mark.parametrize("line", [v for _, v in HOMEPATHS], ids=[i for i, _ in HOMEPATHS])
def test_home_path_spellings_of_the_red_team(tmp_path, register_path, line):
    assert [(x.cls, x.line) for x in gate.scan_files([plant(tmp_path, line)], register_path)] == [("homepath", 3)]


def test_a_tar_listing_line_is_a_home_path_only_at_the_line_start(tmp_path, register_path):
    f = tmp_path / "listing.txt"
    f.write_text("ho" + "me/builder/.ssh/id_rsa\n- home/x/y\n", encoding="utf-8")
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [("homepath", 1)]


@pytest.mark.parametrize("line", [
    "GET https://api.example.com/Us" + "ers/123 lists the users",
    "cd /Us" + "ers/username and ~user/x and ~/x",
    "<ro" + "ot><kunde>x</kunde></ro" + "ot>",
    "the document ro" + "ot is /srv/www/root-of-site",
])
def test_lookalikes_of_the_new_home_path_spellings(tmp_path, register_path, line):
    assert gate.scan_files([plant(tmp_path, line)], register_path) == []


BLOCKED_SPELLINGS = [
    ("underscore-import", "from legacy" + "_shared import client"),
    ("underscore-kb", "see legacy" + "_kb/x"),
    ("home-variable", "cat $HOME/legacy" + "-foo/config.yaml"),
    ("home-variable-braces", "cat ${HOME}/legacy" + "-foo/config.yaml"),
    ("tilde-underscore", "cat ~/legacy" + "_foo/config.yaml"),
    ("docs-folder", "see legacy" + "-docs/index.md"),
    ("scaffold-script", "run new-legacy" + "-project.sh foo"),
    ("memory-files", "see memory/legacy" + "-services/00-index.md and legacy" + "-api-endpoints.md"),
    ("json-escaped-dash", '{"src": "legacy' + '\\u002dshared/index.md"}'),
    ("html-entity-dash", "<p>legacy" + "&#45;shared/index.md</p>"),
    ("zero-width-space", "see legacy" + "\u200b-shared/index.md"),
]


@pytest.mark.parametrize("line", [v for _, v in BLOCKED_SPELLINGS], ids=[i for i, _ in BLOCKED_SPELLINGS])
def test_blocked_spellings_of_the_red_team(tmp_path, register_path, line):
    assert [(x.cls, x.line) for x in gate.scan_files([plant(tmp_path, line)], register_path)] == [("blocklist", 3)]


def test_a_tilde_with_a_user_before_a_blocked_folder_is_both_classes(tmp_path, register_path):
    f = plant(tmp_path, "cat ~buil" + "der/legacy" + "-foo/config.yaml")
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [("homepath", 3), ("blocklist", 3)]


IDENTIFIERS = [
    ("hex-0x", "id 0x" + HEX32),
    ("uuid-after-dash", "server ecs-" + UUID),
    ("uuid-before-dash", "volume " + UUID + "-backup"),
    ("uuid-both-sides", "volume ecs-" + UUID + "-disk"),
    ("project-id", "project id " + "48" + "213"),
    ("project-id-colon", "project_id: " + "48" + "213"),
    ("account", "account " + "48" + "213"),
    ("mandant", "Mandant " + "48" + "213"),
    ("tenant-json-wide", '{"tenant_id"   :   "' + "48" + '213"}'),
    ("tenant-url", "https://console.example/tenant/" + "48" + "213/overview"),
    ("tenants-url", "https://console.example/tenants/" + "48" + "213"),
]


@pytest.mark.parametrize("line", [v for _, v in IDENTIFIERS], ids=[i for i, _ in IDENTIFIERS])
def test_identifier_shapes_of_the_red_team(tmp_path, register_path, line):
    assert [(x.cls, x.line) for x in gate.scan_files([plant(tmp_path, line)], register_path)] == [("identifier", 3)]


def test_a_uuid_inside_a_longer_dashed_hex_id_is_not_one(tmp_path, register_path):
    for line in ("id abcd1234-" + UUID, "id " + UUID + "-9f8e", "id " + "5" * 8 + "-" + UUID + "-x"):
        assert gate.scan_files([plant(tmp_path, line)], register_path) == [], line[:6]


# ------------------------------------------------------------- the normalised text and what encodings hide


def test_every_detector_runs_over_the_normalised_text(tmp_path, register_path):
    grep_color = "kunde \x1b[01;31m\x1b[K" + PLACE[:8] + "\x1b[m\x1b[K" + PLACE[8:] + " heute"
    notebook = json.dumps({"cells": [{"cell_type": "code", "source": ["print(x)"], "outputs": [
        {"output_type": "stream", "name": "stdout", "text": ["kunde \u001b[1m" + PLACE[:8] + "\u001b[0m" + PLACE[8:]]}]}]})
    overstrike = "kunde " + "".join(c + "\x08" + c for c in PLACE) + " heute"
    python_x = 'ort = "' + "".join("\\x%02x" % ord(c) for c in PLACE) + '"'
    for line in (grep_color, notebook, overstrike, python_x):
        f = tmp_path / "n.log"
        f.write_text("x\n" + line + "\n", encoding="utf-8")
        assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [("name", 2)], line[:12]


ENCODED = [
    ("base64-name", lambda: "tag " + base64.b64encode(("kunde " + CUSTOMER).encode()).decode(), "name"),
    ("data-uri", lambda: '<a href="data:text/plain;base64,'
     + base64.b64encode(("kunde " + CUSTOMER).encode()).decode() + '">x</a>', "name"),
    ("hex-of-utf8", lambda: "h " + ("kunde " + PLACE).encode().hex(), "name"),
    ("base64-assignment", lambda: base64.b64encode((pw() + "\n").encode()).decode(), "secret"),
    ("base64-home-path", lambda: base64.b64encode(b"tail -f /ho" + b"me/builder/awb.log\n").decode(), "homepath"),
    ("base64-token", lambda: base64.b64encode(("push with " + "gh" + "p_" + GH_BODY).encode()).decode(), "token"),
]


@pytest.mark.parametrize("build,cls", [(b, c) for _, b, c in ENCODED], ids=[i for i, _, _ in ENCODED])
def test_what_a_block_decodes_to_is_found_on_the_line_of_the_block(tmp_path, register_path, build, cls):
    f = plant(tmp_path, build())
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [(cls, 3)]


def test_a_mime_body_in_base64_is_found_on_its_first_line(tmp_path, register_path):
    body = base64.b64encode(("kunde %s heute, " % CUSTOMER * 6).encode()).decode()
    wrapped = "\n".join(body[i:i + 76] for i in range(0, len(body), 76))
    f = tmp_path / "mail.eml"
    f.write_text("From: a@example.org\nSubject: offer\nContent-Transfer-Encoding: base64\n\n" + wrapped + "\n",
                 encoding="utf-8")
    assert [(x.cls, x.line) for x in gate.scan_files([f], register_path)] == [("name", 5)]


def test_encoded_blocks_carry_the_offset_of_the_block():
    from awb.extract import text as extract_text
    inner = base64.b64encode(b"inner text with letters only").decode()
    outer = base64.b64encode(("outer text " + inner).encode()).decode()
    text = "before\nblock " + outer + "\nafter\n"
    blocks = extract_text.encoded_blocks(text)
    assert [start for start, _ in blocks] == [text.index(outer)] * 2
    assert blocks[0][1].startswith("outer text ") and blocks[1][1] == "inner text with letters only"
    assert extract_text.encoded_texts(text) == [decoded for _, decoded in blocks]


@pytest.mark.parametrize("name,data", [
    ("small.pdf", b"%PDF-1.4\n1 0 obj << >> endobj\n"),
    ("notes.txt", b"%PDF-1.4\n1 0 obj << >> endobj\n"),
    ("bundle.zip", b"PK\x03\x04 plain"),
    ("notes.txt", b"\x1f\x8b\x08 plain"),
    ("deck.pptx", b"text that claims to be a deck\n"),
    ("photo.jpg", b"text that claims to be a picture\n"),
])
def test_documents_archives_and_images_are_opaque_without_a_nul_byte(tmp_path, register_path, name, data):
    f = tmp_path / name
    f.write_bytes(data)
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 0, "opaque")]


# --------------------------------------------------------------------------- the commit message


def test_message_text_drops_comment_lines_and_the_part_after_the_scissors():
    raw = ("subject\n\n# a comment\nbody\n# ------------------------ >8 ------------------------\n"
           "diff --git a/x b/x\n+" + pw())
    assert gate.message_text(raw) == "subject\n\n\nbody"
    assert gate.message_text("no comments\n") == "no comments\n"


def test_scan_message_finds_every_class_and_reports_the_line(tmp_path, register_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("subject\n\noffer for %s\nlog under /ho" % SHORT + "me/builder/awb.log\n"
                   + "Co-authored-by: %s <x@%s>\n# On branch offer-%s\n"
                   % (fixtures.PERSON_FORMS[0], fixtures.CUSTOMER_DOMAIN, SHORT.lower()), encoding="utf-8")
    found = gate.scan_message(msg, gate._load_matcher(register_path))
    assert [(x.file, x.line, x.cls) for x in found] == [
        (gate.MESSAGE_LABEL, 3, "name"), (gate.MESSAGE_LABEL, 4, "homepath"), (gate.MESSAGE_LABEL, 5, "name")]
    assert gate.scan_message(tmp_path / "missing", None) == [gate.Finding(gate.MESSAGE_LABEL, 0, "opaque")]


def test_cli_scans_a_message_file(tmp_path, register_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("subject\n\npush with " + "gh" + "p_" + GH_BODY + "\n", encoding="utf-8")
    assert gate.main(["--register", str(register_path), "--message", str(msg)]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == ["token  commit message:3"] and GH_BODY not in out + err
    msg.write_text(CLEAN, encoding="utf-8")
    assert gate.main(["--register", str(register_path), "--message", str(msg)]) == 0


def test_the_workbench_has_its_own_commit_msg_hook():
    hook = ROOT / "hooks" / "commit-msg"
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and os.access(hook, os.X_OK)
    assert "-m awb.gate --selftest" in text and '-m awb.gate --message "$msg"' in text
    assert text.index("--selftest") < text.index("--message")
    assert subprocess.run(["sh", "-n", str(hook)], check=False).returncode == 0


def test_the_repository_hooks_refuse_a_commit_message_with_a_finding(tmp_path, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = hook_repo(tmp_path)
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = git(repo, "-c", "core.hooksPath=%s" % HOOK.parent, "commit", "-q", "-m",
                  "notes\n\nlog under /ho" + "me/builder/awb.log")
    assert refused.returncode != 0
    assert "homepath  commit message:3" in refused.stdout + refused.stderr
    assert any("commit message" in line for line in refusal_lines(refused))
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    passed = git(repo, "-c", "core.hooksPath=%s" % HOOK.parent, "commit", "-q", "-m", "notes\n\n# a comment\nclean")
    assert passed.returncode == 0, refusal_lines(passed)


def test_install_writes_a_commit_msg_hook_and_leaves_a_foreign_one_alone(tmp_path):
    repo = new_repo(tmp_path / "kb")
    hook = installed(repo)
    msg_hook = hook.parent / "commit-msg"
    assert set(gate.HOOK_NAMES) == {"pre-commit", "commit-msg", "pre-push"}
    assert msg_hook.is_file() and os.access(msg_hook, os.X_OK)
    text = msg_hook.read_text(encoding="utf-8")
    assert gate.HOOK_MARK in text and 'gate --message "$msg"' in text and "gate --selftest" in text
    assert text.index("--selftest") < text.index("--message")
    assert subprocess.run(["sh", "-n", str(msg_hook)]).returncode == 0
    msg_hook.write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="commit-msg hook of another tool"):
        installed(repo)
    installed(repo, force=True)
    assert gate.HOOK_MARK in msg_hook.read_text(encoding="utf-8")


def test_an_installed_commit_msg_hook_refuses_a_message_with_a_finding(tmp_path, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repo = new_repo(tmp_path / "kb")
    installed(repo)
    (repo / "notes.txt").write_text(CLEAN, encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    refused = git(repo, "commit", "-q", "-m", "notes for " + SHORT)
    assert refused.returncode != 0 and "commit message" in refused.stderr
    fixtures.assert_no_fixture_name(refused.stdout + refused.stderr, "hook output")
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def test_selftest_proves_the_message_path(monkeypatch):
    assert gate.selftest(None) == []
    monkeypatch.setattr(gate, "message_text", lambda raw: raw)
    failures = gate.selftest(None)
    assert len(failures) == 1 and failures[0].startswith("message:")
    monkeypatch.setattr(gate, "scan_message", lambda path, matcher: [])
    assert [f.split(":")[0] for f in gate.selftest(None)] == ["message"]


# --------------------------------------------------------------------------- what a push carries besides files


def push_git(repo: Path, *args: str, author: tuple[str, str] = ("t", "t@example.invalid")) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def zero() -> str:
    return "0" * 40


def repo_with_one_commit(tmp_path: Path, content: str = "clean\n") -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    push_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text(content, encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", "clean start")
    return repo, push_git(repo, "rev-parse", "HEAD")


def test_the_author_the_committer_and_the_message_of_a_pushed_commit_are_scanned(tmp_path, register_path):
    repo, base = repo_with_one_commit(tmp_path)
    (repo / "a.txt").write_text("still clean\n", encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", "offer for %s\n\nlog under /ho" % SHORT + "me/builder/x",
             author=(fixtures.PERSON_FORMS[0], "x@" + fixtures.CUSTOMER_DOMAIN))
    head = push_git(repo, "rev-parse", "HEAD")
    updates = gate.parse_push_lines("refs/heads/main %s refs/heads/main %s\n" % (head, base))
    found = gate._scan_pushed(repo, updates, gate._load_matcher(register_path))
    assert [(f.file, f.line, f.cls) for f in found] == [("commit " + head[:10], 0, "name"),
                                                        ("commit " + head[:10], 0, "homepath")]
    clean = gate.parse_push_lines("refs/heads/main %s refs/heads/main %s\n" % (base, zero()))
    assert gate._scan_pushed(repo, clean, gate._load_matcher(register_path), "origin") == []


def test_branch_and_tag_names_and_an_annotated_tag_are_scanned(tmp_path, register_path):
    repo, head = repo_with_one_commit(tmp_path)
    branch = "refs/heads/offer-" + SHORT.lower()
    push_git(repo, "tag", "-a", "v1", "-m", "release for " + ORG)
    tag = push_git(repo, "rev-parse", "v1")
    lines = "%s %s %s %s\nrefs/tags/v1 %s refs/tags/v1 %s\n" % (branch, head, branch, zero(), tag, zero())
    matcher = gate._load_matcher(register_path)
    found = gate._scan_pushed(repo, gate.parse_push_lines(lines), matcher)
    assert [(f.file, f.line, f.cls) for f in found] == [("ref " + branch, 0, "name"), ("ref refs/tags/v1", 0, "name")]
    masked = [gate.masked_path(f.file, matcher) for f in found]
    assert masked[0] == "ref refs/heads/offer-[name]"
    fixtures.assert_no_fixture_name("\n".join(masked), "masked labels")


def test_a_merge_commits_own_tree_is_scanned(tmp_path, register_path):
    repo, base = repo_with_one_commit(tmp_path, "one\n")
    push_git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("side\n", encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", "side")
    push_git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", "second")
    push_git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "c.txt").write_text("resolved with\n" + pw() + "\n", encoding="utf-8")
    push_git(repo, "add", "-A")
    push_git(repo, "commit", "-q", "--no-verify", "-m", "merge")
    head = push_git(repo, "rev-parse", "HEAD")
    assert sorted(rel for rel, _, _ in gate._commit_entries(repo, head)) == ["a.txt", "b.txt", "c.txt"]
    updates = gate.parse_push_lines("refs/heads/main %s refs/heads/main %s\n" % (head, base))
    found = gate._scan_pushed(repo, updates, gate._load_matcher(register_path))
    assert [(f.file.split(":", 1)[1], f.line, f.cls) for f in found] == [("c.txt", 2, "secret")]


def test_a_new_branch_is_new_against_the_named_remote_only(tmp_path, register_path):
    repo, head = repo_with_one_commit(tmp_path, "keys %s %s\n" % (AK, SK))
    push_git(repo, "update-ref", "refs/remotes/fork/main", head)     # a fork already has the commit
    updates = gate.parse_push_lines("refs/heads/x %s refs/heads/x %s\n" % (head, zero()))
    assert gate.pushed_commits(repo, updates) == []                   # any remote counts, the fork has it
    assert gate.pushed_commits(repo, updates, "origin") == [head]     # the target remote does not have it
    found = gate._scan_pushed(repo, updates, gate._load_matcher(register_path), "origin")
    assert [(f.cls, f.line) for f in found] == [("secret", 1)]


def test_the_push_hooks_pass_the_remote_to_the_gate(tmp_path):
    text = (ROOT / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert 'remote="$1"' in text and '--remote "$remote"' in text
    repo = new_repo(tmp_path / "kb")
    hook = installed(repo).parent / "pre-push"
    text = hook.read_text(encoding="utf-8")
    assert 'remote="$1"' in text and '--remote "$remote"' in text
    assert subprocess.run(["sh", "-n", str(hook)]).returncode == 0


def test_a_commit_message_that_names_claude_as_co_author_is_refused(tmp_path, capsys):
    """Only the owner appears as author: a co-author or generator line naming Claude would make it a contributor."""
    msg = tmp_path / "COMMIT_EDITMSG"
    for line in ("Co-Authored-By: claude opus 5.5 <noreply@anthropic.com>",
                 "co-authored-by: claude <someone@example.org>",
                 "generated with [claude code](https://claude.com/claude-code)"):
        msg.write_text("A change\n\nThe body.\n\n%s\n" % line, encoding="utf-8")
        found = gate.scan_message(msg, None)
        assert [(f.line, f.cls) for f in found] == [(5, "attribution")], line
        assert gate.main(["--message", str(msg)]) == 1
        assert "attribution" in capsys.readouterr().out
    msg.write_text("A change\n\nCo-Authored-By: colleague <colleague@example.org>\n", encoding="utf-8")
    assert gate.scan_message(msg, None) == []


def test_selftest_passes_for_the_work_user_of_a_sealed_host(register_path, monkeypatch, tmp_path):
    # the work user ignores AWB_BLOCKLIST, so the self-test must bring its own blocklist in the process
    from awb import config

    monkeypatch.setattr(config, "is_work_user", lambda: True)
    planted = tmp_path / "env-blocklist.txt"
    planted.write_text("plantedword\n", encoding="utf-8")
    monkeypatch.setenv("AWB_BLOCKLIST", str(planted))
    assert gate.selftest(register_path) == []
    assert planted not in gate.blocklist_files()
    assert not any("awb-gate-selftest-" in str(f) for f in gate.blocklist_files())


def test_the_owner_identity_of_his_own_repository_passes_in_the_commit_lines(tmp_path, register_path, monkeypatch):
    """2026-10-06: the owner's name went into the register to keep it out of customer material, and every push of his
    own repository stopped at his own author line. His identity passes there, his name in a message never does."""
    from awb import config

    repo, base = repo_with_one_commit(tmp_path)
    owner = (fixtures.PERSON_FORMS[0], "owner@example.org")
    push_git(repo, "config", "user.name", owner[0])
    push_git(repo, "config", "user.email", owner[1])
    env = dict(os.environ, GIT_AUTHOR_NAME=owner[0], GIT_AUTHOR_EMAIL=owner[1], GIT_COMMITTER_NAME=owner[0],
               GIT_COMMITTER_EMAIL=owner[1])

    def commit(message: str) -> str:
        (repo / "a.txt").write_text("change %d\n" % len(message), encoding="utf-8")
        push_git(repo, "add", "-A")
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", message], check=True,
                       capture_output=True, env=env)
        return push_git(repo, "rev-parse", "HEAD")

    head = commit("a clean change")
    matcher = gate._load_matcher(register_path)
    updates = gate.parse_push_lines("refs/heads/main %s refs/heads/main %s\n" % (head, base))
    assert gate._scan_pushed(repo, updates, matcher) == []
    push_git(repo, "config", "user.name", "someone else")                 # not the identity of this repository
    assert [f.cls for f in gate._scan_pushed(repo, updates, matcher)] == ["name"]
    push_git(repo, "config", "user.name", owner[0])
    monkeypatch.setattr(config, "is_work_user", lambda: True)               # the work user never gets the exception
    assert [f.cls for f in gate._scan_pushed(repo, updates, matcher)] == ["name"]
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    named = commit("notes of %s" % owner[0])                                 # his name in a message is found
    updates = gate.parse_push_lines("refs/heads/main %s refs/heads/main %s\n" % (named, head))
    assert [(f.file, f.cls) for f in gate._scan_pushed(repo, updates, matcher)] == [("commit " + named[:10], "name")]
