"""setup.sh writes no winlab_checks line into the host file, not even when the old host file has one: the switch is
gone (the owner's word of 2026-11-25: no content check in WinLab for good, in code and tests, not behind a switch)."""
from __future__ import annotations

from tests.test_seal import SETUP, fakebin, run_script  # noqa: F401


def test_setup_keeps_winlab_checks_and_writes_none_of_its_own(fakebin):
    conf = fakebin[0].parent / "host-paths.conf"
    conf.write_text("owner = x\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert not [line for line in res.stdout.splitlines() if "winlab_checks" in line]
    conf.write_text("owner = x\nwinlab_checks = off\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert not [line for line in res.stdout.splitlines() if "winlab_checks" in line]
    assert any(line.startswith("    | work_user = ") for line in res.stdout.splitlines())
