"""setup.sh keeps the owner's line doc_hosts of the host file when it rewrites it (a deploy at 14:08 on 2026-11-24
dropped it and awb doc hosts add refused). A host file without the line gets none."""
from __future__ import annotations

from tests.test_seal import SETUP, fakebin, run_script  # noqa: F401


def test_setup_keeps_doc_hosts_and_writes_none_of_its_own(fakebin):
    conf = fakebin[0].parent / "host-paths.conf"
    conf.write_text("owner = x\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert not [line for line in res.stdout.splitlines() if line.startswith("    | doc_hosts")]
    conf.write_text("owner = x\ndoc_hosts = /etc/awb/doc-hosts.txt\n", encoding="utf-8")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    assert "    | doc_hosts = /etc/awb/doc-hosts.txt" in res.stdout.splitlines()
