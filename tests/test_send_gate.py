"""The send gate after the red team of 2026-09-27 (calibration/redteam-2026-09-27.md, second run): what may leave
the machine is judged by the bytes that leave, a copy is a copy by its text, and nothing with a name leaves.
Every name comes from tests/fixtures.py; the review records are made the way tests/test_review_board.py makes them.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from awb import bucket, review
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_review_board import PLATFORM_LENS, REQUEST_LENS, TEXT, reviewed, spawn

FULL = fx.CUSTOMER_FORMS[0]
IBAN = "DE89 3704 0044 0532 0130 00"
LENSES = [("platform-tcp", PLATFORM_LENS), ("request", REQUEST_LENS)]


@pytest.fixture
def folder(home, register_path) -> Path:
    return spawn(home, register_path)


def tier3(folder: Path, tmp_path: Path, name: str = "reply.md") -> Path:
    return reviewed(folder, tmp_path, 3, lenses=LENSES, name=name)


def verdict(folder: Path, file: Path, for_customer: bool = True, home=None) -> str:
    return review.send_check(folder, file, for_customer, workbench=home)[0]


def test_a_changed_copy_of_a_reviewed_deliverable_is_refused(folder, tmp_path):
    d = tier3(folder, tmp_path)
    copy = tmp_path / "reply-final.md"
    copy.write_text(d.read_text(encoding="utf-8").replace("3 nodes", "4 nodes"), encoding="utf-8")
    assert verdict(folder, copy) == review.SEND_REFUSE
    plus = tmp_path / "reply-plus.md"
    plus.write_text(d.read_text(encoding="utf-8") + "One more sentence nobody reviewed.\n", encoding="utf-8")
    assert verdict(folder, plus) == review.SEND_REFUSE
    exact = tmp_path / "reply-copy.md"
    exact.write_bytes(d.read_bytes())
    assert verdict(folder, exact) == review.SEND_OK


def test_a_copy_of_an_unreviewed_deliverable_is_refused_too(folder, tmp_path):
    d = folder / "deliverables" / "draft.md"
    d.write_text(TEXT, encoding="utf-8")
    copy = tmp_path / "draft-final.md"
    copy.write_bytes(d.read_bytes())
    assert verdict(folder, copy) == review.SEND_REFUSE
    assert verdict(folder, copy, for_customer=False) == review.SEND_WARN


def test_the_deliverable_inside_a_container_is_that_deliverable(folder, tmp_path):
    import docx

    d = tier3(folder, tmp_path)
    doc = docx.Document()
    doc.add_paragraph(d.read_text(encoding="utf-8").strip())
    doc.save(str(tmp_path / "reply.docx"))
    assert verdict(folder, tmp_path / "reply.docx") == review.SEND_REFUSE
    with zipfile.ZipFile(tmp_path / "reply.zip", "w") as z:
        z.writestr("reply.md", d.read_bytes())
    assert verdict(folder, tmp_path / "reply.zip") == review.SEND_REFUSE


def test_the_path_that_is_judged_is_the_path_that_opens(folder, tmp_path):
    d = tier3(folder, tmp_path)
    elsewhere = tmp_path / "elsewhere" / "deliverables"
    elsewhere.mkdir(parents=True)
    (elsewhere / "reply.md").write_text("another text with the name %s and %s\n" % (FULL, IBAN), encoding="utf-8")
    os.symlink(tmp_path / "elsewhere", folder / "notes")
    tricky = folder / "notes" / ".." / "deliverables" / "reply.md"
    assert verdict(folder, tricky) == review.SEND_REFUSE
    link_root = tmp_path / "root-link"
    os.symlink(folder.parent, link_root)
    unreviewed = folder / "deliverables" / "draft.md"
    unreviewed.write_text(TEXT, encoding="utf-8")
    assert verdict(folder, link_root / folder.name / "deliverables" / "draft.md") == review.SEND_REFUSE
    assert verdict(folder, link_root / folder.name / "deliverables" / d.name) == review.SEND_OK


def test_a_record_whose_tier_was_edited_is_refused(folder, tmp_path):
    d = reviewed(folder, tmp_path, 1, lenses=LENSES)
    record = review.review_dir(folder, d.name) / review.RECORD
    data = json.loads(record.read_text(encoding="utf-8"))
    data["tier"] = 3
    record.write_text(json.dumps(data), encoding="utf-8")
    assert verdict(folder, d) == review.SEND_REFUSE


def test_a_request_lens_of_another_deliverable_does_not_count(folder, tmp_path):
    d = reviewed(folder, tmp_path, 3, lenses=[("platform-tcp", PLATFORM_LENS),
                                              ("request", {**REQUEST_LENS, "deliverable": "deliverables/other.md"})])
    assert verdict(folder, d) == review.SEND_REFUSE


def test_nothing_with_a_name_leaves_and_structured_data_is_named(folder, tmp_path):
    note = tmp_path / "notes.md"
    note.write_text("internal notes about %s\n" % FULL, encoding="utf-8")
    assert verdict(folder, note) == review.SEND_REFUSE
    assert verdict(folder, note, for_customer=False) == review.SEND_REFUSE
    named = tmp_path / ("plan-%s.md" % fx.CUSTOMER_FORMS[1])
    named.write_text("a plan\n", encoding="utf-8")
    assert verdict(folder, named) == review.SEND_REFUSE
    with_iban = tmp_path / "bank.md"
    with_iban.write_text("pay to %s\n" % IBAN, encoding="utf-8")
    v, message = review.send_check(folder, with_iban, True)
    assert v == review.SEND_WARN and "iban 1" in message
    plain = tmp_path / "plain.md"
    plain.write_text("internal notes of the day\n", encoding="utf-8")
    assert verdict(folder, plain) == review.SEND_WARN


def test_review_material_and_another_projects_deliverable_never_leave(home, register_path, tmp_path):
    a = spawn(home, register_path)
    b = spawn(home, register_path)
    d = tier3(a, tmp_path)
    claims = review.review_dir(a, d.name) / review.CLAIMS
    assert verdict(a, claims, home=home) == review.SEND_REFUSE
    copy = tmp_path / "borrowed.md"
    copy.write_bytes(d.read_bytes())
    assert verdict(b, copy, home=home) == review.SEND_REFUSE
    assert verdict(a, copy, home=home) == review.SEND_OK


def test_put_uploads_the_bytes_the_gate_judged(home, register_path, tmp_path, monkeypatch):
    folder = spawn(home, register_path)
    d = tier3(folder, tmp_path)
    code = folder.name
    with FakeOBS() as fake:
        c = fake.client()
        key, _ = bucket.put(home, c, code, d)
        assert key.endswith("/out/reply.md") and fake.objects[key] == d.read_bytes()
        original = review.send_check

        def then_swap(*args, **kwargs):
            out = original(*args, **kwargs)
            d.write_text("swapped after the gate\n", encoding="utf-8")
            return out

        monkeypatch.setattr(review, "send_check", then_swap)
        with pytest.raises(bucket.BucketError, match="changed after the send gate"):
            bucket.put(home, c, code, d, replace=True)
        assert fake.objects[key] != b"swapped after the gate\n"
