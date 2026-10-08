"""awb words update: the vocabulary of the public corpus for the intake (T4). A small mirror of invented pages."""
from __future__ import annotations

import pytest

from awb import words


def test_the_lists_load_with_their_threshold(tmp_path):
    f = tmp_path / "known-words.txt"
    f.write_text("# word\tfiles\tcount\nAlpha\t5\t9\nBeta\t1\t1\nGamma\tx\t1\n", encoding="utf-8")
    assert words.load(f) == {"alpha"} and words.load(f, min_files=1) == {"alpha", "beta"}
    assert words.load(tmp_path / "missing.txt") == set()
    assert len(words.load(words.WORDS_FILE)) > 1000 and words.revision()


def test_words_update_writes_both_lists_from_a_mirror_and_refuses_an_empty_one(tmp_path, monkeypatch):
    root = tmp_path / "mirrors"
    docs = root / "docs" / "repo"
    docs.mkdir(parents=True)
    made = ["W%s%sxx" % (chr(97 + n // 26), chr(97 + n % 26)) for n in range(150)]
    for i in range(3):
        (docs / ("page%d.rst" % i)).write_text("\n".join("Elastic %s here." % w for w in made), encoding="utf-8")
    (root / "service-description" / "2026-08-17").mkdir(parents=True)
    (root / "service-description" / "CURRENT").write_text("2026-08-17\n", encoding="utf-8")
    (root / "service-description" / "2026-08-17" / "service-description.txt").write_text("Elastic Volume", encoding="utf-8")
    out = tmp_path / "rules"
    out.mkdir()
    n_words, n_phrases, rev = words.update(root, out)
    assert rev == "2026-08-17" and n_words >= 1 and n_phrases >= 1
    assert words.load(out / "known-words.txt") and words.revision(out / "known-words.txt") == "2026-08-17"
    empty = tmp_path / "empty"
    (empty / "docs").mkdir(parents=True)
    with pytest.raises(words.WordsError):
        words.update(empty, out)
    assert words.load(out / "known-words.txt"), "a refused update leaves the lists as they were"


def test_words_update_is_refused_for_the_work_user(monkeypatch, tmp_path):
    from awb import config

    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(words.WordsError, match="work user"):
        words.update(tmp_path)
