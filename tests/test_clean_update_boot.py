"""`awb clean` updates itself and shows its version (task clean-update, 2026-11-20): the version line, the update at
the first shell after a boot, the apt hook and the refusal of an update over local changes. Temporary git
repositories and a temporary home only; nothing leaves this machine."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from awb import clean, cleanupdate, laptop

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="no git")


def _git(*args, cwd=None) -> str:
    got = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                         env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
                                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org"))
    return got.stdout.strip()


def _commit(repo: Path, words: str, msg: str) -> None:
    (repo / "rules" / "common-words.txt").write_text(words, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", msg, cwd=repo)


def _short(repo: Path) -> str:
    return _git("rev-parse", "--short=7", "HEAD", cwd=repo)


@pytest.fixture
def lap(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(laptop.HOME_ENV, str(home / "awb-laptop"))
    monkeypatch.delenv("ZSH_CUSTOM", raising=False)
    for k in clean.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("0000-beef\n", encoding="ascii")
    monkeypatch.setattr(cleanupdate, "BOOT_ID", boot_id)
    monkeypatch.setattr(cleanupdate, "APT_FILE", tmp_path / "etc" / "99awb-clean")
    return home


@pytest.fixture
def repos(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    (upstream / "rules").mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(upstream))
    _commit(upstream, "alpha\n", "one")
    clone = tmp_path / "clone"
    _git("clone", "-q", str(upstream), str(clone))
    monkeypatch.setattr(clean, "PUBLIC_REPO", str(upstream))
    return upstream, clone


def _awb(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "bin" / "awb"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- the version line


def test_version_line_names_the_short_commit_and_date_of_the_clone(repos):
    _, clone = repos
    day = _git("log", "-1", "--format=%cs", cwd=clone)
    assert cleanupdate.version_line(clone / "rules") == "awb clean %s (%s)" % (_short(clone), day)


def test_version_without_git_comes_from_the_stamp_and_says_unknown_without_one(tmp_path):
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    assert "version unknown" in cleanupdate.version_line(rules)
    (rules / "VERSION").write_text("%s 2026-11-20\n" % ("ab" * 20), encoding="ascii")
    assert cleanupdate.version_line(rules) == "awb clean abababa (2026-11-20)"


def test_https_update_writes_the_stamp_only_for_the_commit_of_the_checked_tree(tmp_path):
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    data = b"alpha\nbeta\n"
    tree_sha, commit = "1" * 40, "c0ffee" + "0" * 34

    def fetch_for(commit_tree):
        def fetch(url, limit=0):
            if url == clean.TREE_URL:
                return json.dumps({"sha": tree_sha, "tree": [{"path": "rules/common-words.txt", "type": "blob",
                                   "mode": "100644", "sha": clean.git_blob_id(data)}]}).encode()
            if url == cleanupdate.COMMIT_URL:
                return json.dumps({"sha": commit, "commit": {"tree": {"sha": commit_tree},
                                   "committer": {"date": "2026-11-19T08:00:00Z"}}}).encode()
            return data
        return fetch

    clean.update(rules, fetch=fetch_for("2" * 40))
    assert not (rules / "VERSION").exists(), "another tree than the checked one: no stamp"
    lines = clean.update(rules, fetch=fetch_for(tree_sha))
    assert (rules / "VERSION").read_text(encoding="ascii") == "%s 2026-11-19\n" % commit
    assert "version: updated an unknown version -> c0ffee0" in lines
    assert cleanupdate.version_line(rules) == "awb clean c0ffee0 (2026-11-19)"


def test_the_version_line_comes_first_on_a_terminal_and_never_in_a_pipe(lap, monkeypatch, capsys):
    monkeypatch.setattr(cleanupdate, "interactive", lambda: True)
    assert clean.main(["init"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == cleanupdate.version_line() and first.startswith("awb clean ")
    monkeypatch.setattr(cleanupdate, "interactive", lambda: False)
    assert clean.main(["init"]) == 0
    assert not capsys.readouterr().out.startswith("awb clean ")


# --------------------------------------------------------------------------- the update


def test_update_names_the_versions_and_a_second_run_stays(repos):
    upstream, clone = repos
    old = _short(clone)
    _commit(upstream, "alpha\nbeta\n", "two")
    lines = clean.update(clone / "rules")
    assert lines[1] == "version: updated %s -> %s" % (old, _short(upstream))
    assert clean.update(clone / "rules")[1] == "version: still %s" % _short(upstream)


def test_an_update_never_touches_local_changes_and_keeps_the_old_version(lap, repos):
    upstream, clone = repos
    old = _short(clone)
    _commit(upstream, "alpha\nbeta\n", "two")
    (clone / "rules" / "common-words.txt").write_text("mine\n", encoding="utf-8")
    assert cleanupdate.run_quiet("apt", clone / "rules") == 0
    assert (clone / "rules" / "common-words.txt").read_text(encoding="utf-8") == "mine\n"
    assert _short(clone) == old
    assert cleanupdate.notice() == ("apt update: update failed: the clone has local changes: commit or drop them, "
                                    "then awb clean update, still on %s" % old)
    assert cleanupdate.notice() is None, "printed once"


def test_a_clone_with_its_own_commit_is_refused_by_ff_only_and_stays(lap, repos):
    upstream, clone = repos
    _commit(upstream, "alpha\nbeta\n", "two")
    _commit(clone, "alpha\nown\n", "own")
    mine = _short(clone)
    with pytest.raises(clean.CleanError, match="ff-only"):
        clean.update(clone / "rules")
    assert _short(clone) == mine
    assert (clone / "rules" / "common-words.txt").read_text(encoding="utf-8") == "alpha\nown\n"


def test_update_quiet_prints_nothing_and_never_fails(lap, monkeypatch, capsys):
    def broken(rules=None, **kw):
        raise clean.CleanError("git pull --ff-only of the public repository failed (exit 1)")

    monkeypatch.setattr(clean, "update", broken)
    monkeypatch.setattr(cleanupdate, "interactive", lambda: True)
    assert clean.main(["update", "--quiet"]) == 0
    assert capsys.readouterr() == ("", "")
    rec = cleanupdate.read_record()
    assert rec["how"] == "apt" and rec["state"] == "failed"
    log = (lap / "awb-laptop" / cleanupdate.LOG).read_text(encoding="utf-8")
    assert "apt update: update failed: git pull --ff-only" in log


def test_a_refused_update_on_the_terminal_says_so_and_names_the_version(lap, monkeypatch, capsys):
    def refused(rules=None, **kw):
        raise clean.CleanError("the clone has local changes: commit or drop them, then awb clean update")

    monkeypatch.setattr(clean, "update", refused)
    assert clean.main(["update"]) == clean.EXIT_ERROR
    said = capsys.readouterr()
    assert said.out.splitlines()[0] == cleanupdate.version_line(), "update prints the version line also in a pipe"
    assert "refused: the clone has local changes" in said.err and "nothing changed, still on" in said.err


# --------------------------------------------------------------------------- the boot


def test_the_boot_update_runs_once_per_boot_and_its_result_is_printed_once(lap, repos, monkeypatch, capsys):
    upstream, clone = repos
    old = _short(clone)
    _commit(upstream, "alpha\nbeta\n", "two")
    calls = []
    real = clean.update

    def counted(rules=None, **kw):
        calls.append(rules)
        return real(rules, **kw)

    assert cleanupdate.boot(clone / "rules", update=counted) == 0
    assert cleanupdate.boot(clone / "rules", update=counted) == 0
    assert len(calls) == 1, "once per boot"
    assert (lap / "awb-laptop" / cleanupdate.BOOT_STAMP).read_text().strip() == cleanupdate.BOOT_ID.read_text().strip()
    monkeypatch.setattr(cleanupdate, "interactive", lambda: True)
    assert clean.main(["init"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "boot update: updated %s -> %s" % (old, _short(upstream))
    assert clean.main(["init"]) == 0
    assert not any("boot update" in x for x in capsys.readouterr().out.splitlines())
    cleanupdate.BOOT_ID.write_text("0000-cafe\n", encoding="ascii")
    assert cleanupdate.boot(clone / "rules", update=counted) == 0
    assert len(calls) == 2, "a new boot runs again"
    assert cleanupdate.notice() == "boot update: already up to date, on %s" % _short(upstream)


def test_a_boot_update_cut_by_the_timeout_is_named_and_a_young_one_is_running(lap):
    home = lap / "awb-laptop"
    home.mkdir()
    rec = {"how": "boot", "state": "running", "from": "23dc2c9", "to": None, "reason": None,
           "time": int(time.time()), "shown": False}
    (home / cleanupdate.RECORD).write_text(json.dumps(rec), encoding="utf-8")
    assert cleanupdate.notice() == "boot update: running now"
    assert cleanupdate.notice(now=lambda: time.time() + 600) == ("boot update: did not finish within 60 s, still "
                                                                 "on 23dc2c9")
    assert cleanupdate.notice() is None


def test_shell_adds_the_boot_block_once_and_replaces_an_older_awb_path(lap, tmp_path, capsys):
    awb = _awb(tmp_path)
    (lap / ".oh-my-zsh" / "custom").mkdir(parents=True)
    lines = cleanupdate.boot_setup(str(awb))
    bashrc = (lap / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count(cleanupdate.BOOT_BEGIN) == 1 and "timeout 60 %s clean update --boot" % awb in bashrc
    zsh = lap / ".oh-my-zsh" / "custom" / cleanupdate.ZSH_UPDATE_FILE
    assert cleanupdate.boot_block(str(awb)) in zsh.read_text(encoding="utf-8")
    assert lines[-1].startswith("boot update: written to ~/.oh-my-zsh/custom/awb-update.zsh")
    assert cleanupdate.boot_setup(str(awb))[0].endswith("starts awb clean update once per boot")
    assert (lap / ".bashrc").read_text(encoding="utf-8") == bashrc
    other = tmp_path / "bin" / "awb2"
    shutil.copy(awb, other)
    cleanupdate.boot_setup(str(other))
    again = (lap / ".bashrc").read_text(encoding="utf-8")
    assert again.count(cleanupdate.BOOT_BEGIN) == 1 and str(other) in again and "%s clean" % awb not in again
    assert clean.main(["shell"]) == 0
    assert "boot update:" in capsys.readouterr().out


@pytest.mark.wallclock
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_the_boot_block_starts_awb_in_the_background_once_per_boot(lap, tmp_path, shell):
    if not shutil.which(shell) or not Path("/proc/sys/kernel/random/boot_id").is_file():
        pytest.skip("no %s or no boot id" % shell)
    mark = tmp_path / "called"
    awb = _awb(tmp_path, 'sleep 3; echo "$@" > %s' % mark)
    block = tmp_path / "block.sh"
    block.write_text(cleanupdate.boot_block(str(awb)), encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(lap), laptop.HOME_ENV: str(lap / "awb-laptop")}
    argv = [shell, "--norc", "-c", ". %s" % block] if shell == "bash" else [shell, "-f", "-c", ". %s" % block]
    started = time.monotonic()
    subprocess.run(argv, env=env, check=True, timeout=10)
    assert time.monotonic() - started < 2.5, "the prompt never waits for the update"
    for _ in range(100):
        if mark.exists() and mark.read_text():
            break
        time.sleep(0.1)
    assert mark.read_text().strip() == "clean update --boot"
    mark.unlink()
    stamp = lap / "awb-laptop" / cleanupdate.BOOT_STAMP
    stamp.write_text(Path("/proc/sys/kernel/random/boot_id").read_text(), encoding="ascii")
    subprocess.run(argv, env=env, check=True, timeout=10)
    time.sleep(3.5)
    assert not mark.exists(), "the stamp of this boot: no second start"


# --------------------------------------------------------------------------- the apt hook and the doctor


def test_the_apt_hook_runs_as_the_user_never_fails_apt_and_the_command_writes_exactly_it(lap, tmp_path):
    awb = _awb(tmp_path)
    content, cmd = cleanupdate.apt_command(str(awb), "laptopuser")
    assert "runuser -u laptopuser -- sh -c" in content and "clean update --quiet" in content
    assert content.rstrip().endswith('fi; true"; };') and "\n" not in cmd
    target = tmp_path / "written"
    for shell in ("bash", "zsh"):
        if not shutil.which(shell):
            continue
        subprocess.run([shell, "-c", cmd.replace("sudo tee %s" % cleanupdate.APT_FILE, "tee %s" % target)],
                       check=True, timeout=10, capture_output=True)
        assert target.read_text(encoding="utf-8") == content
    if shutil.which("apt-config"):
        dump = subprocess.run(["apt-config", "-c", str(target), "dump"], capture_output=True, text=True).stdout
        assert "runuser -u laptopuser" in dump


def test_apt_hook_check_says_missing_installed_or_different(lap, tmp_path, monkeypatch, capsys):
    awb = _awb(tmp_path)
    monkeypatch.setattr(cleanupdate, "awb_path", lambda given=None: str(awb))
    assert clean.main(["apt-hook"]) == 0
    out = capsys.readouterr().out
    assert "sudo tee" in out and not cleanupdate.APT_FILE.exists(), "the command writes nothing itself"
    assert clean.main(["apt-hook", "--check"]) == 1
    assert "not installed" in capsys.readouterr().out
    content, _ = cleanupdate.apt_command()
    cleanupdate.APT_FILE.parent.mkdir()
    cleanupdate.APT_FILE.write_text(content, encoding="utf-8")
    assert clean.main(["apt-hook", "--check"]) == 0
    assert "apt hook: installed" in capsys.readouterr().out
    cleanupdate.APT_FILE.write_text(content.replace(str(awb), "/opt/old/awb"), encoding="utf-8")
    assert clean.main(["apt-hook", "--check"]) == 1
    assert "another awb path or user" in capsys.readouterr().out
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert clean.main(["apt-hook"]) == 2
    assert "not as root" in capsys.readouterr().err


def test_doctor_says_how_far_behind_the_public_main_the_clone_is(repos):
    upstream, clone = repos
    assert cleanupdate.behind_line(clone / "rules") == "public main: up to date with it"
    _commit(upstream, "alpha\nbeta\n", "two")
    _commit(upstream, "alpha\nbeta\ngamma\n", "three")
    assert cleanupdate.behind_line(clone / "rules") == ("public main: 2 commits behind (awb clean update brings "
                                                        "them)")
    assert _short(clone) != _short(upstream), "the doctor fetches, it never moves the clone"


def test_doctor_without_git_asks_the_compare_of_the_stamp(tmp_path):
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    assert "no VERSION stamp" in cleanupdate.behind_line(rules, fetch=lambda u: pytest.fail("no request"))
    (rules / "VERSION").write_text("%s 2026-11-20\n" % ("cd" * 20), encoding="ascii")
    asked = []

    def fetch(url):
        asked.append(url)
        return json.dumps({"ahead_by": 1, "behind_by": 0}).encode()

    assert cleanupdate.behind_line(rules, fetch=fetch) == "public main: 1 commit behind (awb clean update brings it)"
    assert asked == [cleanupdate.COMPARE_URL % ("cd" * 20)]
    assert "cannot tell" in cleanupdate.behind_line(rules, fetch=lambda u: (_ for _ in ()).throw(OSError()))


def test_doctor_prints_the_version_first_and_the_behind_line_last(lap, monkeypatch, capsys):
    monkeypatch.setattr(clean, "doctor_targets", lambda: [])
    monkeypatch.setattr(cleanupdate, "behind_line", lambda rules=None, fetch=None: "public main: up to date with it")
    assert clean.main(["doctor"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == cleanupdate.version_line() and lines[-1] == "public main: up to date with it"
