"""B-109: `awb kb purge ID... --owner-word TEXT` removes retired entries of another scope than tcp for good.
A temporary knowledge base (AWB_KB) that is a git repository, with invented entries."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awb import cli, gate, kb

S_HCS = "ECS flavor s3.large.2 is offered in eu-de with two vCPUs"
S_HCS_2 = "Object storage buckets keep versions when versioning is switched on"
S_TCP = "GPU flavor p2 needs a quota increase in eu-nl"
S_ACTIVE = "Windows images are imported as BMS images in eu-de through the console"
WORD = "no HCS knowledge on the TCP side"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
                           *args], capture_output=True, text=True, check=False)


@pytest.fixture(autouse=True)
def author(monkeypatch):
    for key, value in (("NAME", "awb-test"), ("EMAIL", "awb@example.org")):
        monkeypatch.setenv("GIT_AUTHOR_" + key, value)
        monkeypatch.setenv("GIT_COMMITTER_" + key, value)


def new(statement: str, *, scope: str = "hcs", retired: bool = True, tag: str = "ecs") -> kb.Entry:
    e = kb.add(statement, scope=scope, tags=[tag], grade="docs", cls="api", source="docs mirror, user guide",
               force_new=True)
    if retired:
        kb.retire(e.id, "moved to the other knowledge base")
    return e


def commit_base(home) -> None:
    assert git(home.kb, "init", "-q").returncode == 0
    assert git(home.kb, "add", "-A").returncode == 0
    assert git(home.kb, "commit", "-q", "--no-verify", "-m", "base").returncode == 0


def run(argv, capsys):
    code = cli.main(["kb", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def files(home) -> set[str]:
    return {p.name for p in (home.kb / "entries").glob("KB-*.md")}


def test_purge_removes_a_retired_hcs_entry_and_commits(home, capsys):
    gone, other = new(S_HCS), new(S_TCP, scope="tcp", retired=False, tag="ecs")
    commit_base(home)
    code, out, err = run(["purge", gone.id, "--owner-word", WORD], capsys)
    assert code == 0, out + err
    assert files(home) == {other.id + ".md"}
    listed = (home.kb / "entries" / kb.PURGED).read_text(encoding="utf-8")
    assert listed.startswith("%s %s owner's word: \"%s\", " % (gone.id, kb.date.today().isoformat(), WORD))
    assert kb.purged() == {gone.id: kb.date.today().isoformat()}
    assert gone.id not in (home.kb / "INDEX.md").read_text(encoding="utf-8")
    # one commit with exactly these paths, nothing left over
    assert git(home.kb, "status", "--porcelain").stdout == ""
    changed = git(home.kb, "show", "--name-status", "--format=", "HEAD").stdout.split("\n")
    assert sorted(line.split("\t")[1] for line in changed if line) == sorted(
        ["entries/%s.md" % gone.id, "entries/PURGED", "INDEX.md"])
    assert git(home.kb, "rev-list", "--count", "HEAD").stdout.strip() == "2"
    with pytest.raises(kb.KBError, match="purged"):
        kb.get(gone.id)


def test_a_purged_id_is_never_generated_again(home, monkeypatch):
    gone = new(S_HCS)
    commit_base(home)
    kb.purge([gone.id], WORD)
    letters = iter(list(gone.id[3:]) + list("AAAA") + list("BBBB"))
    monkeypatch.setattr(kb.secrets, "choice", lambda seq: next(letters))
    got = kb.add(S_HCS_2, scope="tcp", tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide")
    assert got.id == "KB-AAAA"


def test_an_active_entry_and_a_retired_tcp_entry_are_refused_and_nothing_goes(home, capsys):
    ok, active = new(S_HCS), new(S_ACTIVE, retired=False)
    tcp = new(S_TCP, scope="tcp")
    commit_base(home)
    before = files(home)
    for ids in ([active.id], [tcp.id], [ok.id, active.id], [ok.id, tcp.id]):
        code, out, err = run(["purge", *ids, "--owner-word", WORD], capsys)
        assert code == 1 and "nothing was purged" in out
        assert files(home) == before and not (home.kb / "entries" / kb.PURGED).exists()
        assert git(home.kb, "status", "--porcelain").stdout == ""
    assert "is not retired" in run(["purge", active.id, "--owner-word", WORD], capsys)[1]
    assert "has scope tcp" in run(["purge", tcp.id, "--owner-word", WORD], capsys)[1]
    assert git(home.kb, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_purge_needs_the_owner_word_and_a_known_id(home, capsys):
    gone = new(S_HCS)
    commit_base(home)
    assert run(["purge", gone.id], capsys)[0] == 2
    code, out, _ = run(["purge", "KB-AAAA", "--owner-word", WORD], capsys)
    assert code == 1 and "there is no entry KB-AAAA" in out
    code, out, _ = run(["purge", gone.id, "--owner-word", "  "], capsys)
    assert code == 1 and files(home) == {gone.id + ".md"}


def test_a_commit_the_hook_refuses_leaves_the_files_as_they_were(home):
    gone = new(S_HCS)
    commit_base(home)
    hook = home.kb / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    index = (home.kb / "INDEX.md").read_text(encoding="utf-8")
    with pytest.raises(kb.KBError, match="refused the purge"):
        kb.purge([gone.id], WORD)
    assert files(home) == {gone.id + ".md"} and not (home.kb / "entries" / kb.PURGED).exists()
    assert (home.kb / "INDEX.md").read_text(encoding="utf-8") == index
    assert git(home.kb, "status", "--porcelain").stdout == ""


def test_the_pre_commit_hook_lets_the_purge_commit_pass(home, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
    gone = new(S_HCS)
    commit_base(home)
    gate.install_hook(home.kb, kb=True, installed=Path("/nonexistent/usr/local/bin/awb"))
    kb.purge([gone.id], WORD)
    assert files(home) == set() and git(home.kb, "rev-list", "--count", "HEAD").stdout.strip() == "2"


def test_a_plain_git_rm_of_an_entry_is_still_refused(home):
    gone = new(S_HCS)
    commit_base(home)
    assert git(home.kb, "rm", "-q", "entries/%s.md" % gone.id).returncode == 0
    problems = kb.verify(staged_repo=home.kb)
    assert [label for label, _ in problems] == ["entries/%s.md" % gone.id]
    assert "retire" in problems[0][1]


def test_a_git_rm_with_its_line_in_the_list_passes(home):
    gone, kept = new(S_HCS), new(S_HCS_2)
    commit_base(home)
    assert git(home.kb, "rm", "-q", "entries/%s.md" % gone.id).returncode == 0
    assert kb.verify(staged_repo=home.kb)           # no line yet
    (home.kb / "entries" / kb.PURGED).write_text("%s 2026-11-27 owner's word: \"%s\", 2026-11-27T09:00Z\n"
                                                 % (gone.id, WORD), encoding="utf-8")
    assert git(home.kb, "add", "entries/PURGED").returncode == 0
    assert kb.verify(staged_repo=home.kb) == []
    # the line of another id does not cover this deletion
    assert git(home.kb, "rm", "-q", "entries/%s.md" % kept.id).returncode == 0
    assert [label for label, _ in kb.verify(staged_repo=home.kb)] == ["entries/%s.md" % kept.id]


def test_a_staged_entry_with_a_purged_id_is_refused(home):
    gone = new(S_HCS)
    commit_base(home)
    kb.purge([gone.id], WORD)
    (home.kb / "entries" / (gone.id + ".md")).write_text(kb.render(gone), encoding="utf-8")
    assert git(home.kb, "add", "-A").returncode == 0
    assert "never taken again" in " ".join(why for _, why in kb.verify(staged_repo=home.kb))


def test_a_bench_case_that_names_only_purged_ids_is_dropped(home):
    gone, kept = new(S_HCS), new(S_HCS_2, retired=False, scope="tcp")
    commit_base(home)
    kb.purge([gone.id], WORD)
    questions = [{"question": "ECS flavor s3.large.2 vCPUs", "accept": [gone.id]},
                 {"question": "object storage versioning", "accept": [gone.id, kept.id]}]
    r = kb.bench(questions)
    assert r["answerable"] == 1 and r["misses"] == []
