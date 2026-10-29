"""DX: the MCP bundle of TCP Facts is easy to find. Every release carries tcp-facts.mcpb, the same bytes as the dated
tcp-facts-<date>.mcpb, so releases/latest/download/tcp-facts.mcpb is always the newest; the README names that link
right under its disclaimer and the how-to names it too."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awb import kbreview
from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, github, live, signing  # noqa: F401

URL = "https://github.com/shimoza/tcp-facts/releases/latest/download/tcp-facts.mcpb"
ONE_CLICK = " ".join(("Claude", "Desktop, one click"))      # split: a capitalised run outside the fixtures
NEEDS = " ".join(("needs", "Python", "3.10 or newer"))


def test_the_stable_link_points_at_the_latest_release_of_the_public_repository():
    assert dataset.MCPB_LATEST_URL == URL and kbreview.STABLE_MCPB == "tcp-facts.mcpb"


def test_the_asset_list_holds_the_dated_and_the_stable_bundle_with_equal_bytes(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    assets = {a.name: a for a in kbreview.release_assets(built.folder)}
    dated, stable = assets["tcp-facts-2026-10-08.mcpb"], assets["tcp-facts.mcpb"]
    assert dated.is_file() and stable.is_file() and dated.read_bytes() == stable.read_bytes()
    sums = kbreview.asset_sums(built.folder)
    assert sums["tcp-facts.mcpb"] == sums["tcp-facts-2026-10-08.mcpb"]
    assert any("tcp-facts-2026-10-08.mcpb" in line for line in built.lines)


def test_the_stable_copy_is_not_packed_into_the_bundle_or_the_zip(home, facts, tmp_path):
    import zipfile
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    with zipfile.ZipFile(built.folder.parent / "tcp-facts-2026-10-08.mcpb") as z:
        assert "tcp-facts.mcpb" not in z.namelist()
    with zipfile.ZipFile(built.zip) as z:
        assert not [n for n in z.namelist() if n.endswith("/tcp-facts.mcpb")]


def test_the_release_uploads_both_bundles_and_the_signed_sums_list_both(home, facts, tmp_path, signing):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    lines = dataset.github_put(home, built.folder, clone=signing.github.clone,
                               pinned=kbreview.asset_sums(built.folder))
    create = [a for a in signing.github.log() if a[:2] == ["release", "create"]][0]
    names = [Path(a).name for a in create]
    assert "tcp-facts-2026-10-08.mcpb" in names and "tcp-facts.mcpb" in names
    assert names.index("tcp-facts-2026-10-08.mcpb") < names.index("tcp-facts.mcpb") < names.index("SHA256SUMS")
    sums = dict(reversed(l.split("  ")) for l in (built.folder / "SHA256SUMS").read_text().splitlines())
    assert sums["tcp-facts.mcpb"] == sums["tcp-facts-2026-10-08.mcpb"]
    assert any(URL in line for line in lines)


def test_a_stable_copy_that_differs_from_the_dated_bundle_stops_the_release(home, facts, tmp_path, github):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    with (built.folder / "tcp-facts.mcpb").open("ab") as f:
        f.write(b"\n")
    with pytest.raises(dataset.DatasetError, match="no tcp-facts.mcpb equal to tcp-facts-2026-10-08.mcpb"):
        dataset.github_put(home, built.folder, clone=github.clone)
    assert not [a for a in github.log() if a[:2] == ["release", "create"]]


def test_the_readme_carries_the_stable_link_near_its_top_and_keeps_the_statements(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    text = (built.folder / "README.md").read_text()
    lines = text.splitlines()
    assert lines[4] == dataset.DISCLAIMER
    assert URL in lines[6] and ONE_CLICK in lines[6] and NEEDS in lines[6]
    assert text.index(URL) < text.index("img.shields.io")
    assert dataset.public_statements_missing(text) == []
    longer = [l for l in lines if l.startswith(ONE_CLICK + ": download")]
    assert len(longer) == 1 and URL in longer[0] and "tcp-facts-<date>.mcpb" in longer[0]


def test_the_how_to_names_the_stable_link(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    pdf = subprocess.run(["pdftotext", str(built.folder / "HOW-TO.pdf"), "-"], capture_output=True, text=True,
                         check=True).stdout
    assert URL in " ".join(pdf.split()).replace("/ ", "/")
    assert URL in (built.folder / "PROMPT.md").read_text()
