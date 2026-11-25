"""setup.sh keeps the owner's switch winlab_checks of the host file when it rewrites it (his word of 2026-11-25:
no checks in WinLab); a host file without the line gets none, so the checks stay on."""
from __future__ import annotations

from tests.test_seal import SETUP, fakebin, run_script  # noqa: F401


def test_setup_keeps_winlab_checks_and_writes_none_of_its_own(fakebin):
    conf = fakebin[0].parent / "host-paths.conf"
    conf.write_text("owner = x\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert not [line for line in res.stdout.splitlines() if line.startswith("    | winlab_checks")]
    conf.write_text("owner = x\nwinlab_checks = off\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert "    | winlab_checks = off" in res.stdout.splitlines()
