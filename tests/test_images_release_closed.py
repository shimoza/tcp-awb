"""G14 of the attacker review of 2026-11-28: `awb images release` puts the pictures into a project folder only
when the project is active or parked. A customer whose only project is closed gets them in the outbox, where the
module text says they go, and a closed project named with --project is refused. Invented codes only."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from awb import images, projects
from tests import fixtures as fx

CODE = "tcp-a2bc"
OTHER = "tcp-d3ef"


def _png(color=(200, 0, 0), size: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _plant(home, rows: list[tuple[str, str]]) -> dict[str, Path]:
    out, saved = {}, []
    for code, state in rows:
        folder = home.projects_root / code
        folder.mkdir(parents=True)
        saved.append(projects.Project(code, "project", fx.CUSTOMER_CODE, "tcp", str(folder),
                                      projects.memory_key(folder), state, "2026-11-02"))
        out[code] = folder
    projects._save(home, saved)
    return out


def _held(home, tmp_path) -> None:
    src = tmp_path / "plan.png"
    src.write_bytes(_png())
    assert images.hold(home, fx.CUSTOMER_CODE, "f01", src, "image", today="2026-11-28") == 1


def test_a_customer_with_one_closed_project_gets_the_pictures_in_the_outbox(home, tmp_path):
    folders = _plant(home, [(CODE, "closed")])
    _held(home, tmp_path)
    assert images.project_folder(home, fx.CUSTOMER_CODE) is None
    out = images.release(home, fx.CUSTOMER_CODE, "f01", [1], today="2026-11-28")
    assert [f.parent for f in out] == [home.outbox / fx.CUSTOMER_CODE / images.IMAGES]
    assert not (folders[CODE] / "input").exists()


def test_a_closed_project_beside_an_active_one_is_not_counted(home, tmp_path):
    folders = _plant(home, [(CODE, "closed"), (OTHER, "active")])
    assert images.project_folder(home, fx.CUSTOMER_CODE) == folders[OTHER]


def test_the_only_parked_project_still_takes_them(home, tmp_path):
    folders = _plant(home, [(CODE, "parked"), (OTHER, "deleted")])
    assert images.project_folder(home, fx.CUSTOMER_CODE) == folders[CODE]


def test_a_closed_project_named_with_project_is_refused(home, tmp_path):
    folders = _plant(home, [(CODE, "closed")])
    _held(home, tmp_path)
    with pytest.raises(images.ImageError, match="closed"):
        images.release(home, fx.CUSTOMER_CODE, "f01", [1], project=CODE)
    assert not (folders[CODE] / "input").exists()
    assert images.load_manifest(images.folder(home, fx.CUSTOMER_CODE, "f01"))["images"][0]["state"] == "held"
