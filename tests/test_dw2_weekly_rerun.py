"""DW2 (2026-11-02): the weekly publish of 2026-11-02 put the bucket copy and then failed at GitHub; the rerun of the
same day refused with "the bucket holds the dataset of 2026-11-02 already; give --replace". A later run of the same
day now replaces that day's copy, in the bucket and on GitHub. Every text is invented.
"""
from __future__ import annotations

from awb.tcp import dataset
from tests.test_ks_fact_schedule import weekly  # noqa: F401 (fixture)


def test_dw2_a_rerun_after_a_failed_github_step_publishes_the_same_day(weekly, monkeypatch):  # noqa: F811
    home, calls, state, released = weekly
    held: set[str] = set()
    seen: list[tuple[str, bool]] = []

    def put(c, folder, replace=False):
        seen.append(("put", replace))
        if folder.name in held and not replace:
            raise dataset.DatasetError("the bucket holds the dataset already; give --replace")
        held.add(folder.name)
        return ["bucket key"]

    def broken(p, folder, replace=False, pinned=None):
        seen.append(("github", replace))
        raise dataset.DatasetError("gh release failed (exit 1)")

    monkeypatch.setattr(dataset, "put", put)
    monkeypatch.setattr(dataset, "github_put", broken)
    assert dataset.weekly(home) == 1
    monkeypatch.setattr(dataset, "github_put", lambda p, folder, replace=False, pinned=None:
                        seen.append(("github", replace)) or ["github release"])
    assert dataset.weekly(home) == 0
    assert seen == [("put", True), ("github", True), ("put", True), ("github", True)]


def test_dw2_the_put_by_hand_still_refuses_a_day_held_without_replace():
    import inspect

    assert "replace: bool = False" in str(inspect.signature(dataset.put_folder)).replace("'", "")
    assert "if not replace and c.head(" in inspect.getsource(dataset.put)
