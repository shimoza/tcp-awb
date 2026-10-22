"""Every public README carries two statements at its top (the owner's rule of 2026-10-22): it is not an official
product, and it is built only from public sources with respect for robots.txt; the dataset put refuses without them."""

from __future__ import annotations

from pathlib import Path

import pytest

from awb.tcp import dataset

ROOT = Path(__file__).resolve().parent.parent
GOOD = ("> **Unofficial and independent.** Not a product of, and not endorsed by, the operator. Built only from public "
        "sources (the public documentation), with respect for robots.txt and the terms of those sources.")


def test_a_readme_with_both_statements_passes():
    assert dataset.public_statements_missing("# title\n\n" + GOOD) == []


@pytest.mark.parametrize("text, missing", [
    ("# title\n\nBuilt only from public sources, with respect for robots.txt.", ["unofficial"]),
    ("# title\n\nUnofficial. Not a product of the operator.", ["public sources and robots.txt"]),
    ("# title", ["unofficial", "public sources and robots.txt"]),
])
def test_a_missing_statement_is_named(text, missing):
    assert dataset.public_statements_missing(text) == missing


def test_statements_far_down_the_readme_do_not_count():
    assert dataset.public_statements_missing("# title\n" + "x\n" * 2000 + GOOD) != []


def test_the_github_put_refuses_a_build_without_the_statements(tmp_path):
    day = "2026-10-22"
    folder = tmp_path / ("%s-%s" % (dataset.SLUG, day))
    folder.mkdir()
    for name in dataset.GITHUB_ASSETS + dataset.GITHUB_FILES:
        f = folder / name
        if not f.exists():
            f.mkdir(parents=True) if "." not in name else f.write_text("x", encoding="utf-8")
    (folder / "README.md").write_text("# TCP Facts\n\nfacts", encoding="utf-8")
    (tmp_path / (folder.name + ".zip")).write_bytes(b"zip")
    with pytest.raises(dataset.DatasetError, match="lacks the statement unofficial and public sources"):
        dataset.github_put(None, folder, clone=tmp_path / "never-cloned")
    assert not (tmp_path / "never-cloned").exists()


def test_the_readme_of_this_repository_carries_both_statements():
    assert dataset.public_statements_missing((ROOT / "README.md").read_text(encoding="utf-8")) == []
