"""The MCP Bundle of the dataset (SPDX licence, icon.png) and the revision of Q32 of the TCP Facts eval."""
from __future__ import annotations

import importlib.util
import json
import struct
import zipfile
from pathlib import Path

from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, live  # noqa: F401  (fixtures)

EVAL = Path(__file__).resolve().parent.parent / "eval" / "tcp-facts"


def _bundle(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    return zipfile.ZipFile(built.folder.parent / "tcp-facts-2026-10-08.mcpb")


def test_the_manifest_licence_is_the_spdx_id(home, facts, tmp_path):
    with _bundle(home, facts, tmp_path) as z:
        assert json.loads(z.read("manifest.json"))["license"] == "CC-BY-4.0"
    assert dataset.LICENCE == "CC BY 4.0"                 # the display texts keep their form


def test_the_bundle_holds_the_icon_and_the_manifest_names_it(home, facts, tmp_path):
    with _bundle(home, facts, tmp_path) as z:
        assert "icon.png" in z.namelist()
        assert json.loads(z.read("manifest.json"))["icon"] == "icon.png"


def test_the_icon_is_a_64_by_64_png_with_the_same_bytes_every_time():
    png = dataset.icon_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[12:16] == b"IHDR" and struct.unpack(">II", png[16:24]) == (64, 64)
    assert png[-12:-8] == b"\x00\x00\x00\x00" and png[-8:-4] == b"IEND"
    assert png == dataset.icon_png()


def test_q32_expects_the_revision_of_the_dataset_under_test(tmp_path):
    spec = importlib.util.spec_from_file_location("evalcase_run_polish", EVAL / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    folder = tmp_path / "tcp-facts-fake"
    folder.mkdir()
    (folder / "services.md").write_text("# Services\n\nFrom the service description, revision 03.11.2027. 2 services.\n",
                                        encoding="utf-8")
    questions = [json.loads(l) for l in (EVAL / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    q = {x["id"]: x for x in run.fill_revision(questions, run.revision_of(folder))}["Q32"]
    assert q["expected"] == ["03.11.2027"] and q["grading"]["items"] == ["03.11.2027"]
    (folder / "services.md").unlink()
    (folder / "MANIFEST.json").write_text(json.dumps({"service_description": "04.12.2027"}), encoding="utf-8")
    assert run.revision_of(folder) == "04.12.2027"
