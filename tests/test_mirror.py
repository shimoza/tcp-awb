"""awb/tcp/mirror.py: local git repositories stand in for the documentation organisation, a PDF made here for the
service description."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from awb import cli, config, jobs
from awb.tcp import mirror


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(cwd), "PATH": "/usr/bin:/bin"})


def origin(tmp: Path, name: str, text: str = "first") -> Path:
    repo = tmp / "origin" / name
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "api-ref.rst").write_text(text + "\n", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "one", cwd=repo)
    return repo


def listing(tmp: Path, names=("elastic-cloud-server", "virtual-private-cloud"), archived=("old-docs",)) -> list[dict]:
    out = []
    for n in names:
        out.append({"name": n, "clone_url": "file://%s" % origin(tmp, n), "archived": False, "default_branch": "main"})
    for n in archived:
        out.append({"name": n, "clone_url": "file:///nowhere/%s" % n, "archived": True, "default_branch": "main"})
    return out


def test_clone_then_update_then_unchanged(tmp_path):
    repos = listing(tmp_path)
    base = tmp_path / "mirrors"
    counts = mirror.sync_docs(base, repos, jobs.Job("a", None, out=io.StringIO()))
    assert counts == {"cloned": 2, "archived": 1, "gone": 0}
    assert (base / "docs" / "elastic-cloud-server" / "api-ref.rst").read_text() == "first\n"
    assert not (base / "docs" / "old-docs").exists()
    ecs = tmp_path / "origin" / "elastic-cloud-server"
    (ecs / "api-ref.rst").write_text("second\n", encoding="utf-8")
    git("commit", "-q", "-am", "two", cwd=ecs)
    counts = mirror.sync_docs(base, repos, jobs.Job("b", None, out=io.StringIO()))
    assert counts["updated"] == 1 and counts["unchanged"] == 1
    assert (base / "docs" / "elastic-cloud-server" / "api-ref.rst").read_text() == "second\n"
    manifest = json.loads((base / "docs" / "MANIFEST.json").read_text())
    assert manifest["repos"]["elastic-cloud-server"]["state"] == "updated"
    assert len(manifest["repos"]["elastic-cloud-server"]["commit"]) == 40


def test_a_failed_repository_is_recorded_and_the_rest_goes_on(tmp_path):
    repos = listing(tmp_path, names=("identity-access-management",), archived=())
    repos.insert(0, {"name": "broken", "clone_url": "file:///nowhere/broken", "archived": False,
                     "default_branch": "main"})
    base = tmp_path / "mirrors"
    counts = mirror.sync_docs(base, repos, jobs.Job("c", None, out=io.StringIO()))
    assert counts["failed"] == 1 and counts["cloned"] == 1
    m = mirror.load_manifest(base / "docs")
    assert m["repos"]["broken"]["state"] == "failed" and m["repos"]["broken"]["step"] == "clone"


def test_an_empty_repository_is_recorded_not_failed(tmp_path):
    repos = listing(tmp_path, names=("cloud-eye",), archived=())
    repos.append({"name": "doc-empty", "clone_url": "file:///nowhere/doc-empty", "archived": False,
                  "default_branch": "main", "size": 0})
    counts = mirror.sync_docs(tmp_path / "mirrors", repos, jobs.Job("f", None, out=io.StringIO()))
    assert counts == {"cloned": 1, "empty": 1, "gone": 0}
    m = mirror.load_manifest(tmp_path / "mirrors" / "docs")
    assert m["repos"]["doc-empty"]["state"] == "empty in the organisation"


def test_a_run_cut_off_by_its_budget_resumes(tmp_path):
    repos = listing(tmp_path, names=("a-docs", "b-docs", "c-docs"), archived=())
    base = tmp_path / "mirrors"
    state = tmp_path / "job"
    clock = [0.0]

    def tick():
        clock[0] += 4.0
        return clock[0]

    first = jobs.Job("docs", state, budget=10, clock=tick, out=io.StringIO())
    with pytest.raises(jobs.JobStopped):
        mirror.sync_docs(base, repos, first)
    done = first.done_count
    assert 0 < done < 3
    second = jobs.Job("docs", state, out=io.StringIO())
    counts = mirror.sync_docs(base, repos, second)
    assert second.skipped == done and sum(counts.get(s, 0) for s in ("cloned", "updated", "unchanged")) == 3
    second.finish()


def test_a_repository_that_left_the_organisation_is_marked_not_deleted(tmp_path):
    repos = listing(tmp_path, names=("x-docs", "y-docs"), archived=())
    base = tmp_path / "mirrors"
    mirror.sync_docs(base, repos, jobs.Job("d", None, out=io.StringIO()))
    counts = mirror.sync_docs(base, repos[:1], jobs.Job("e", None, out=io.StringIO()))
    assert counts["gone"] == 1 and (base / "docs" / "y-docs").is_dir()
    assert mirror.load_manifest(base / "docs")["repos"]["y-docs"]["state"] == "gone from the organisation"


def test_the_repository_list_has_three_states():
    page = [{"name": "cloud-eye", "clone_url": "https://example.invalid/cloud-eye.git", "archived": False,
             "default_branch": "main"}]
    assert mirror.list_repos(fetch_json=lambda url: page).state == jobs.LIST
    assert mirror.list_repos(fetch_json=lambda url: []).state == jobs.EMPTY
    assert mirror.list_repos(fetch_json=lambda url: {"message": "API rate limit exceeded"}).state == jobs.UNKNOWN
    dot = [{"name": ".github", "clone_url": "https://example.invalid/.github.git"}]
    assert mirror.list_repos(fetch_json=lambda url: dot).state == jobs.LIST       # the profile repository
    for name in ("../escape", "..", ".", "a/b"):
        bad = [{"name": name, "clone_url": "x"}]
        assert mirror.list_repos(fetch_json=lambda url: bad).state == jobs.UNKNOWN, name


def pdf(revised: str, body: str = "Service Specifications") -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, body)
    c.drawString(72, 730, "Last revised: %s" % revised)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_the_service_description_one_folder_per_revision(tmp_path):
    base = tmp_path / "mirrors"
    r = mirror.sync_sd(base, fetch=lambda url: (pdf("17.08.2026"), url + "/file.pdf"))
    assert (r["state"], r["revision"], r["current"]) == ("new", "2026-08-17", "2026-08-17")
    folder = base / "service-description" / "2026-08-17"
    assert "Last revised: 17.08.2026" in (folder / "service-description.txt").read_text()
    assert mirror.current_sd(base) == folder / "service-description.txt"
    assert mirror.sync_sd(base, fetch=lambda url: (pdf("17.08.2026"), url))["state"] == "unchanged"   # new export
    changed = mirror.sync_sd(base, fetch=lambda url: (pdf("17.08.2026", "Service Specifications, edited"), url))
    assert changed["state"] == "replaced"
    r = mirror.sync_sd(base, fetch=lambda url: (pdf("01.10.2026", "Service Specifications, new"), url))
    assert (r["state"], r["current"], r["archive"]) == ("new", "2026-10-01", 1)
    assert folder.is_dir() and (base / "service-description" / "CURRENT").read_text().strip() == "2026-10-01"


def test_the_service_description_refuses_what_is_not_a_revised_pdf(tmp_path):
    base = tmp_path / "mirrors"
    with pytest.raises(mirror.MirrorError, match="did not answer with a PDF"):
        mirror.sync_sd(base, fetch=lambda url: (b"<html>moved</html>", url))
    with pytest.raises(mirror.MirrorError, match="no 'Last revised' line"):
        mirror.sync_sd(base, fetch=lambda url: (pdf("soon"), url))
    assert not (base / "service-description").exists() or not any((base / "service-description").iterdir())


def test_the_root_and_the_work_user(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AWB_MIRRORS", str(tmp_path / "m"))
    assert mirror.root() == tmp_path / "m"
    assert cli.main(["mirror", "status"]) == 0
    assert "docs: 0 repositories" in capsys.readouterr().out
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert mirror.root() == Path("/srv/tcp-mirrors")
    assert cli.main(["mirror", "sd"]) == 2
    assert "never updates them" in capsys.readouterr().err
