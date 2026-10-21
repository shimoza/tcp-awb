"""G6 of the threat model: `awb kb review` lists the facts added or amended since the last dataset release with
their sources and grade and writes the mark `awb dataset put` needs; a put without the review of that range is
refused in one line that names the command.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from awb import cli
from awb import kbreview
from awb.tcp import dataset
from tests.obs_fake import FakeOBS
from tests.test_dataset import S_ECS, S_IAM, S_OBS, TODAY, facts, live, new  # noqa: F401

LATER = TODAY + timedelta(days=7)


@pytest.fixture
def bucket(monkeypatch):
    """The put reaches a fake bucket instead of the key service."""
    with FakeOBS() as fake:
        monkeypatch.setattr(dataset.bucket, "client", lambda: fake.client())
        yield fake


def _review(ds, capsys, *more) -> list[str]:
    assert cli.main(["kb", "review", "--out", str(ds), *more]) == 0
    return capsys.readouterr().out.splitlines()


def test_put_refuses_an_unreviewed_build_in_one_line_that_names_the_command(home, facts, tmp_path, bucket, capsys):
    ds = tmp_path / "ds"
    dataset.build(home, out=ds, today=TODAY)
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "were not reviewed" in err and "run awb kb review first" in err
    assert not bucket.objects and not kbreview.released_path(ds).exists()
    # a hand-made mark of other facts does not pass either
    kbreview.review_path(ds / "tcp-facts-2026-10-08").write_text(json.dumps({"facts_sha256": "0" * 64, "since": ""}))
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    assert "run awb kb review again" in capsys.readouterr().err and not bucket.objects


def test_the_first_review_lists_every_fact_with_source_and_grade_and_lets_the_put_release(home, facts, tmp_path,
                                                                                            bucket, capsys):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    lines = _review(ds, capsys)
    assert lines[0] == "tcp-facts-2026-10-08 against no release yet (every fact is new): 3 added, 0 amended, 0 retired"
    added = [l for l in lines if l.startswith("added ")]
    sources = [l for l in lines if l.startswith("  source: ")]
    assert len(added) == 3 and len(sources) == 3
    assert any("(live, checked 2026-10-08): " + S_ECS in l for l in added)
    assert any("(docs, checked 2026-10-08): " + S_OBS in l for l in added)
    assert any("a TCP test tenant" in s for s in sources) and not any("test-0000" in s for s in sources)
    assert lines[-1].startswith("reviewed: awb dataset put may release tcp-facts-2026-10-08")
    mark = json.loads(kbreview.review_path(built.folder).read_text())
    assert mark["since"] == "" and mark["added"] == 3 and len(mark["facts_sha256"]) == 64
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 0
    assert "datasets/tcp-facts/2026-10-08/tcp-facts-2026-10-08.zip" in capsys.readouterr().out
    since, rows = kbreview.released(ds)
    assert since == "2026-10-08" and len(rows) == 3


def test_the_next_review_lists_only_what_changed_since_the_release(home, facts, tmp_path, bucket, capsys):
    ds = tmp_path / "ds"
    dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 0
    capsys.readouterr()
    # a session adds a fact, corrects another and rechecks a third with a new grade and source
    new("EVS offers SSD disks in eu-nl", grade="docs", tags=["evs"], checked=LATER, today=LATER)
    obs_id = next(r["id"] for r in kbreview.read_facts(ds / "tcp-facts-2026-10-08" / "facts.jsonl")
                  if r["statement"] == S_OBS)
    iam_id = next(r["id"] for r in kbreview.read_facts(ds / "tcp-facts-2026-10-08" / "facts.jsonl")
                  if r["statement"].startswith("The IAM user quota"))
    from awb import kb
    kb.amend(obs_id, "OBS buckets in eu-de keep object versions only when versioning is on", checked=LATER,
             today=LATER, force_new=True)
    kb.recheck(iam_id, grade="docs", source="docs mirror, the IAM user guide", checked=LATER, today=LATER)
    dataset.build(home, out=ds, today=LATER)
    # the newer build is unreviewed: the put refuses it although the older one was released
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    assert "were not reviewed since the release of 2026-10-08; run awb kb review first" in capsys.readouterr().err
    lines = _review(ds, capsys)
    assert lines[0] == "tcp-facts-2026-10-15 against the release of 2026-10-08: 1 added, 2 amended, 0 retired"
    assert sum(1 for l in lines if l.startswith("added ")) == 1 and "EVS offers SSD disks" in lines[1]
    statement = next(l for l in lines if l.startswith("amended (statement) " + obs_id))
    assert "only when versioning is on" in statement
    assert any(l == "  statement before: " + S_OBS for l in lines)
    rechecked = next(l for l in lines if l.startswith("amended (source, grade) " + iam_id))
    assert "(docs, checked 2026-10-15): " + S_IAM in rechecked
    assert any(l.startswith("  before: live, source: a scripted call") for l in lines)
    assert not any(S_ECS in l for l in lines)                       # unchanged: not listed
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 0
    assert kbreview.released(ds)[0] == "2026-10-15"


def test_a_build_after_the_review_or_a_release_in_between_makes_the_mark_stale(home, facts, tmp_path, bucket,
                                                                               capsys):
    ds = tmp_path / "ds"
    dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    new("EVS offers SSD disks in eu-de", grade="docs", tags=["evs"])
    dataset.build(home, out=ds, today=TODAY, force=True)            # other facts under the reviewed mark
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    assert "not the review of its facts now" in capsys.readouterr().err
    _review(ds, capsys)
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 0
    capsys.readouterr()
    # the same folder again after its release: the mark compared with no release, the release is there now
    assert cli.main(["dataset", "put", "--out", str(ds), "--replace"]) == 1
    assert "run awb kb review again" in capsys.readouterr().err
    lines = _review(ds, capsys)
    assert lines[0].endswith("against the release of 2026-10-08: 0 added, 0 amended, 0 retired")
    assert cli.main(["dataset", "put", "--out", str(ds), "--replace"]) == 0


def test_the_review_refuses_without_a_build_and_the_mark_stays_out_of_the_zip(home, facts, tmp_path, capsys):
    ds = tmp_path / "ds"
    assert cli.main(["kb", "review", "--out", str(ds)]) == 2
    assert "no built dataset; run awb dataset build" in capsys.readouterr().err
    built = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    assert kbreview.review_path(built.folder).parent == ds and not list(built.folder.glob("*.review.json"))
    assert "review" not in json.loads((built.folder / "MANIFEST.json").read_text())["files"]
