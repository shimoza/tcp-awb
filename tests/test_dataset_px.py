"""Task PX: the dataset holds no price list. The build writes no price file, the texts name none, the MCP price tool
works only with --live and the put to the public repository removes a prices/ folder it still holds."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, github, live, _remote_files  # noqa: F401  (fixtures)
from tests.test_mcp import Client, FakePriceAPI, built, price_api  # noqa: F401  (fixtures)


def test_the_build_writes_no_price_file_and_no_text_names_one(home, facts, tmp_path):
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    root = built_.folder
    names = sorted(str(x.relative_to(root)) for x in root.rglob("*") if x.is_file())
    assert not (root / "prices").exists() and not [n for n in names if n.endswith(".csv")]
    manifest = json.loads((root / "MANIFEST.json").read_text())
    assert "prices" not in manifest and not [n for n in manifest["files"] if "price" in n or n.endswith(".csv")]
    readme = (root / "README.md").read_text()
    assert "prices/" not in readme and ".csv" not in readme and "price list of two regions" not in readme
    assert "live price API" in readme and "--live" in readme and "not part of" in readme
    for name in ("README.md", "PROMPT.md", "facts.md", "services.md", "MANIFEST.json"):
        text = (root / name).read_text()
        assert "prices/" not in text and ".csv" not in text and "snapshot" not in text.replace("snapshot of a tenant", ""), name
    assert not [m for m in re.findall(r"prices/\S*|\S*\.csv", json.dumps(manifest))]
    import zipfile
    with zipfile.ZipFile(built_.zip) as z:
        assert not [n for n in z.namelist() if "prices" in n or n.endswith(".csv")]
    with zipfile.ZipFile(root.parent / (root.name + ".mcpb")) as z:
        assert not [n for n in z.namelist() if "prices" in n or n.endswith(".csv")]
        assert "prices" not in json.loads(z.read("manifest.json"))["keywords"]


def test_the_build_still_checks_a_price_sample_against_the_live_api(home, facts, tmp_path, capsys):
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    assert any(line.startswith("live price check eu-de: 1 rows") for line in built_.lines)
    assert not (built_.folder / "prices").exists()


def test_the_price_tool_refuses_without_live_and_says_how_to_turn_it_on(built, price_api):
    c = Client("--data", str(built[0]), "--price-api", price_api)
    try:
        got = c.answer("tcp_price_find", query="s3.large.2", service="ecs")
        assert got["count"] == 0 and got["rows"] == []
        assert "prices are live only" in got["note"] and "--live" in got["note"]
        assert FakePriceAPI.seen == []
    finally:
        c.close()


def test_the_price_tool_works_with_a_stubbed_live_call(built):
    from awb.tcp import tcp_facts_mcp as server
    ds = server.Dataset(built[0], live=True)
    rows = [dict(id="OTC_ECS_S3L2", productId="Elastic Cloud Server", productName="s3.large.2 linux",
                 opiFlavour="s3.large.2", osUnit="Linux", vCpu="2", ram="4 GiB", unit="h", currency="EUR",
                 priceAmount="0.052000 EUR", productIdParameter="ecs", region="eu-de")]
    body = {"response": {"code": "Success", "result": {"ecs": rows}}}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return json.dumps(body).encode()

    import urllib.request
    old = urllib.request.urlopen
    urllib.request.urlopen = lambda url, timeout=None: Reply()
    try:
        got = server.price_find(ds, "s3.large.2", "eu-de", "ecs")
        assert got["count"] == 1 and got["rows"][0]["payg"] == "0.052000" and got["source"].startswith("live public")
        off = server.price_find(server.Dataset(built[0]), "s3.large.2", "eu-de", "ecs")
        assert off["count"] == 0 and "live only" in off["note"]
    finally:
        urllib.request.urlopen = old


def test_the_mcp_server_text_and_tool_say_live_only():
    from awb.tcp import tcp_facts_mcp as server
    tool = next(t for t in server.TOOLS if t["name"] == "tcp_price_find")
    assert "snapshot" not in tool["description"] and "live" in tool["description"]
    head = server.__doc__
    assert "prices/" not in head and "snapshot" not in head and "live prices" in head
    assert "tcp_calculate" in {t["name"] for t in server.TOOLS}


def _seed_remote_with_prices(remote, tmp_path):
    seed = tmp_path / "seed2"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    (seed / "prices").mkdir()
    (seed / "prices" / "eu-de.csv").write_text("id,payg\nOTC_ECS_S3L2,0.05\n")
    for cmd in (["add", "-A"], ["commit", "-q", "-m", "older release"], ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid", *cmd], cwd=seed,
                       check=True)


def test_the_put_removes_a_stale_prices_folder_with_git_rm_and_says_so(home, facts, tmp_path, github):
    _seed_remote_with_prices(github.remote, tmp_path)
    assert "prices/eu-de.csv" in _remote_files(github.remote, "main")
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built_.folder, clone=github.clone)
    assert not [f for f in _remote_files(github.remote, "main") if f.startswith("prices/")]
    assert "facts.md" in _remote_files(github.remote, "main")
    msg = subprocess.run(["git", "log", "-1", "--format=%B", "main"], cwd=github.remote, capture_output=True,
                         text=True, check=True).stdout
    assert "removed prices/" in msg
    # the older commit still has it: the folder left in a new commit, history is not rewritten
    assert "prices/eu-de.csv" in _remote_files(github.remote, "main~1")


def test_the_put_without_a_stale_prices_folder_says_nothing_of_it(home, facts, tmp_path, github):
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built_.folder, clone=github.clone)
    msg = subprocess.run(["git", "log", "-1", "--format=%B", "main"], cwd=github.remote, capture_output=True,
                         text=True, check=True).stdout
    assert "prices/" not in msg


def _git_in(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def _rewrite_remote(remote, tmp_path):
    """The owner's rewrite: prices/ gone from every commit, force pushed over main."""
    work = tmp_path / "rewrite"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    _git_in(work, "filter-branch", "-f", "--index-filter", "git rm -r -q --cached --ignore-unmatch prices",
            "--", "--all")
    _git_in(work, "push", "-q", "--force", str(remote), "HEAD:main")
    for tag in _git_in(remote, "tag", "--list").split():                     # the old tags went with the old history
        _git_in(remote, "tag", "-d", tag)


def test_the_put_resets_a_clone_whose_history_the_owner_rewrote_and_never_forces(home, facts, tmp_path, github):
    _seed_remote_with_prices(github.remote, tmp_path)
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built_.folder, clone=github.clone)              # the clone now holds the old history
    old_head = _git_in(github.clone, "rev-parse", "HEAD").strip()
    _git_in(github.clone, "reset", "-q", "--hard", "HEAD~1")                 # local-only work on the old line
    (github.clone / "stray.txt").write_text("old local history\n")
    _git_in(github.clone, "add", "-A")
    _git_in(github.clone, "commit", "-q", "-m", "local only")
    local_only = _git_in(github.clone, "rev-parse", "HEAD").strip()
    _rewrite_remote(github.remote, tmp_path)
    rewritten = _git_in(github.remote, "rev-parse", "main").strip()
    assert _git_in(github.remote, "log", "--all", "--format=%H", "--", "prices").strip() == ""
    built2 = dataset.build(home, out=tmp_path / "ds2", today=TODAY, force=True)
    dataset.github_put(home, built2.folder, clone=github.clone, replace=True)
    log = _git_in(github.remote, "log", "--format=%H", "main").split()
    assert old_head not in log and local_only not in log and rewritten in log
    assert _git_in(github.remote, "log", "--all", "--format=%H", "--", "prices").strip() == ""
    assert "stray.txt" not in _remote_files(github.remote, "main")
    assert not any("--force" in call or "-f" in call for call in github.log())


def test_a_non_fast_forward_push_is_refused_and_nothing_is_pushed(home, facts, tmp_path, github):
    import pytest
    _seed_remote_with_prices(github.remote, tmp_path)
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built_.folder, clone=github.clone)
    before = _git_in(github.remote, "rev-parse", "main").strip()
    _rewrite_remote(github.remote, tmp_path)                                  # the remote moved after the sync
    _git_in(github.clone, "commit", "-q", "--allow-empty", "-m", "late local commit")
    with pytest.raises(dataset.DatasetError, match="moved or was rewritten"):
        dataset._push_branch(github.clone)
    assert _git_in(github.remote, "rev-parse", "main").strip() != before
    assert "late local commit" not in _git_in(github.remote, "log", "--format=%s", "main")


def test_the_github_put_code_never_forces_a_push():
    import inspect
    for fn in (dataset.github_put, dataset.sync_clone, dataset._push_branch):
        assert '"--force"' not in inspect.getsource(fn), fn.__name__


def test_the_built_readme_opens_with_the_centred_header_the_disclaimer_and_the_badges(home, facts, tmp_path):
    built_ = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    text = (built_.folder / "README.md").read_text()
    lines = text.splitlines()
    assert lines[0] == '<h1 align="center">TCP Facts</h1>' and lines[2].startswith('<p align="center"><b>')
    assert lines[4] == dataset.DISCLAIMER and text.index(dataset.DISCLAIMER) < text.index("img.shields.io")
    assert dataset.DISCLAIMER.startswith("> **Unofficial and independent.** Not a product of, and not endorsed by, "
                                         "T-Systems or Deutsche Telekom; it speaks for nobody but its author.")
    assert dataset.DISCLAIMER.endswith("No warranty: the provider's contract documents are binding.")
    badges = re.findall(r'alt="([^"]+)" src="https://img.shields.io/badge/([^"]+)"', text)
    assert [a.split(":")[0] for a, _ in badges] == ["Unofficial", "Facts", "Best before", "License", "MCP server"]
    assert "Best_before-2026--11--07-e0b43a" in text and "Facts-3-2f6fd1" in text and "CC_BY_4.0-3f9d5a" in text
    assert "logo" not in text and "e20074" not in text.lower()
    assert "Best before 2026-11-07" in text


def test_the_repository_readme_has_the_disclaimer_under_the_title_and_the_unofficial_badge_first():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    lines = text.splitlines()
    assert re.fullmatch(r'<h1 align="center">[^<]+</h1>', lines[0]) and lines[2] == dataset.DISCLAIMER
    first = re.search(r"<img alt=\"([^\"]+)\"", text).group(1)
    assert first == "Unofficial"
