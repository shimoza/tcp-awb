"""The synthetic test set of the log rules (task FP): support log lines with invented hosts, ticket ids, error texts
and codes, measured before and after the learning of awb/fplearn.py.

    .venv/bin/python calibration/fp/support_logs.py --work DIR

HARMLESS lines carry nothing of a person or a customer: what the check blocks in them is a false positive. PLANTED
lines carry one of the invented names of tests/fixtures.py (a registered form, a first name of the list, a surname
glued into a host name): they must stay blocked after the learning. The measurement builds a throw-away vault with
the fixture register, scans every line, records the blocks as the doors do, runs one learning without a corpus (the
log rules alone) and scans again. It prints counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import fixtures as fx  # noqa: E402

_SURNAME = fx.PLANTED_PERSON.split()[1].lower()

HARMLESS: tuple[str, ...] = (
    "2026-10-21T08:15:02Z web01.ops-lab.de ERROR 0x80070005 access denied, ticket INC0042137",
    "2026-10-21 08:15:03 cache-2.node.eu WARN connection reset by peer (ECONNRESET)",
    "lb-07.edge-net.io health check failed: HTTP/1.1 503 Service Unavailable",
    "worker-3.queue-cluster.cloud retry 4 of 5 for job 9f86d081884c, code E1042",
    "GET https://status.monitor-net.de/v1/health returned 502 after 30012 ms",
    "dns lookup of db-02.backend-zone.de timed out, see ticket CHG-20431",
    "backup to /var/backups/daily/node7.tar.gz finished on gateway-1.edge-net.io",
    "metrics-4.monitor-net.de: disk /dev/vdb at 91 percent, alert ALR-7781",
    "api-11.backend-zone.de 404 Not Found for /api/v2/orders/58213",
    "2026-10-21T09:01:44Z mq-01.queue-cluster.cloud ERR_QUEUE_FULL dropped 12 messages",
    "proxy-2.edge-net.io upstream timeout, request " + "-".join(("3f2a9c1e", "77b0", "4c1d", "9e2a", "0b1c2d3e4f50")),
    "node-04.compute-zone.eu kernel: oom-killer invoked, pid 41870",
    "stage-1.backend-zone.de deploy 7c4e2a1 rolled back, ticket INC0042190",
    "cert for web02.ops-lab.de expires in 12 days",
    "sync from files-1.storage-net.de to files-2.storage-net.de failed: EIO",
)

PLANTED: tuple[str, ...] = (
    "2026-10-21T08:20:11Z web01.ops-lab.de login of %s failed" % fx.PERSON_FORMS[0],
    "host srv-%s-02.ops-lab.de unreachable, ticket INC0042201" % _SURNAME,
    "host %s-pc01.ops-lab.de unreachable" % fx.LISTED_FIRST.lower(),
    "cache-2.node.eu reported by %s, ticket INC0042202" % fx.CUSTOMER_FORMS[0],
    "lb-07.edge-net.io failover, %s informed" % fx.ORG_FORMS[0],
    "mail from %s about web01.ops-lab.de" % fx.PERSON_FORMS[1],
)
"""The names in prose are registered forms: the check finds registered forms, an unregistered name in prose is the
intake's to find (a learned host beside it relaxes the host alone, as it would on a line of its own)."""


def _blocked(names, line: str) -> list[dict]:
    from awb import check

    return names.scan(check.with_hidden_views(line))


def measure(work: Path) -> dict:
    """Counts of blocked lines before and after one learning, per set, and the learned entries."""
    os.environ["AWB_VAULT"] = str(work / "vault")
    os.environ["AWB_SHARED"] = str(work / "shared")
    os.environ.setdefault("AWB_MIRRORS", str(work / "no-mirrors"))
    os.environ.setdefault("AWB_KB", str(work / "no-kb"))
    from awb import check, config, fplearn

    p = config.ensure_layout(config.paths())
    p.register.write_text("\n".join(fx.register_lines()) + "\n", encoding="utf-8")
    os.chmod(p.register, 0o600)

    def blocked_lines() -> dict:
        names = check.checker(p.register)
        return {"harmless": sum(1 for l in HARMLESS if _blocked(names, l)),
                "planted": sum(1 for l in PLANTED if _blocked(names, l))}

    before = blocked_lines()
    names = check.checker(p.register)
    for line in HARMLESS + PLANTED:
        hits = _blocked(names, line)
        if hits:
            fplearn.record("prompt", check.with_hidden_views(line), hits, p=p)
    out = fplearn.learn(p, files=[])
    after = blocked_lines()
    return {"lines": {"harmless": len(HARMLESS), "planted": len(PLANTED)}, "before": before, "after": after,
            "learned": out.learned, "dropped": len(out.dropped), "note": out.note}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, default=None, help="a throw-away folder (default: a new temporary one)")
    args = ap.parse_args(argv)
    work = args.work or Path(tempfile.mkdtemp(prefix="fp-logs-"))
    print(json.dumps(measure(work), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
