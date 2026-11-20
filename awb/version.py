"""UXR9: the live release in one line, so a session checks itself whether a fix is live instead of asking him.

    awb version live           the live release: its commit, when the deploy finished
    awb version live COMMIT    whether COMMIT (7 to 40 hex characters) is part of the live release

The source is the deploy journal /opt/tcp-awb/DEPLOYED (root, 644, readable by every user; awb/deploy.py writes it)
and, when the journal is missing or unreadable, the installed source link /opt/tcp-awb/src -> releases/<commit>.
A deploy that is running or left a unit pending says so: its commits are not taken as live. Exit codes: 0 the
release is known (and COMMIT is live), 1 COMMIT is not live or the release is unknown, 2 a bad argument.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

JOURNAL = Path("/opt/tcp-awb/DEPLOYED")
SRC = Path("/opt/tcp-awb/src")
FULL_RE = re.compile(r"[0-9a-f]{40}")
PREFIX_RE = re.compile(r"[0-9a-f]{7,40}")


def _journal(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _src(path: Path) -> str | None:
    try:
        name = Path(os.readlink(path)).name
    except OSError:
        return None
    return name if FULL_RE.fullmatch(name) else None


def live(journal: Path = JOURNAL, src: Path = SRC) -> dict:
    """{release, finished, state, commits, source}: release None when neither source names one; commits is the
    set of live commits, empty unless a finished deploy with nothing pending recorded them."""
    data = _journal(journal)
    link = _src(src)
    if data is None:
        return dict(release=link, finished=None, state=None, commits=set(), source="link" if link else None,
                    link=link)
    state = data.get("state") if isinstance(data.get("state"), str) else None
    target = data.get("target") if isinstance(data.get("target"), str) and FULL_RE.fullmatch(data["target"]) else None
    done = state == "done" and not data.get("pending")
    release = target if done else (data.get("previous") if isinstance(data.get("previous"), str) and
                                   FULL_RE.fullmatch(data["previous"]) else link)
    commits = set()
    raw = data.get("commits")
    if done and data.get("notes_ready") is not False and isinstance(raw, list) and target in raw and \
            all(isinstance(c, str) and FULL_RE.fullmatch(c) for c in raw):
        commits = set(raw)
    finished = data.get("finished") if isinstance(data.get("finished"), str) else None
    return dict(release=release, finished=finished if done else None, state=state, commits=commits,
                source="journal", link=link, target=target)


def lines(info: dict, commit: str | None = None) -> tuple[list[str], int]:
    out: list[str] = []
    rel = info["release"]
    if rel is None:
        return ["live release unknown: no deploy journal and no installed source link"], 1
    if info["source"] == "link":
        out.append("live release %s (%s), from the installed source link; no deploy journal" % (rel[:7], rel))
    elif info["state"] == "done" and info["finished"]:
        out.append("live release %s (%s), deployed %s" % (rel[:7], rel, info["finished"]))
    else:
        out.append("live release %s (%s); the last deploy of %s is %s" % (
            rel[:7], rel, (info.get("target") or "unknown")[:7], info["state"] or "unknown"))
    if info.get("link") and info["link"] != rel:
        out.append("the installed source link names %s" % info["link"][:7])
    if commit is None:
        return out, 0
    if not info["commits"]:
        if rel.startswith(commit):
            out.append("%s is live" % commit[:12])
            return out, 0
        out.append("%s: not known to be live (no finished deploy recorded its commits)" % commit[:12])
        return out, 1
    hits = [c for c in info["commits"] if c.startswith(commit)]
    if len(hits) > 1:
        out.append("%s: more than one live commit starts with it, give more characters" % commit[:12])
        return out, 2
    if hits:
        out.append("%s is live in release %s" % (hits[0][:12], rel[:7]))
        return out, 0
    out.append("%s is not live yet (release %s)" % (commit[:12], rel[:7]))
    return out, 1


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb version", description="The live release; with COMMIT whether that commit is live.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("live", help="the live release; with COMMIT whether that commit is part of it")
    lv.add_argument("commit", nargs="?", metavar="COMMIT", help="a commit id, 7 to 40 lowercase hex characters")
    args = ap.parse_args(argv)
    if args.commit is not None and not PREFIX_RE.fullmatch(args.commit):
        print("awb version: give a commit id of 7 to 40 lowercase hex characters", file=sys.stderr)
        return 2
    out, code = lines(live(JOURNAL, SRC), args.commit)
    for line in out:
        print(line)
    return code
