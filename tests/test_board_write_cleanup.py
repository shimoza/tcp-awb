"""board_write leaves no temporary file behind when the rename fails (2026-11-07)."""
from awb import build_units


def test_a_failed_board_write_leaves_no_temporary_file(tmp_path):
    (tmp_path / "state.tsv").mkdir()          # a rename over a folder fails as the sticky-folder case did
    assert build_units.board_write(tmp_path, "state.tsv", b"x\n") is False
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.tsv"]


def test_a_good_board_write_replaces_the_file(tmp_path):
    (tmp_path / "state.tsv").write_bytes(b"old\n")
    assert build_units.board_write(tmp_path, "state.tsv", b"new\n") is True
    assert (tmp_path / "state.tsv").read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.tsv"]
