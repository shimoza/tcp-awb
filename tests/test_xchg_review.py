"""The exchange on the service side against a hostile project session or a planted file, from the attacker review of
2026-10-10; each part names its finding.

The setup is the one of tests/test_xchg.py (two stand-in buckets, the key service on short socket paths); every name
is an invented fixture name.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from awb import projects
from awb.tcp import keys, xchg
from tests import fixtures as fx
from tests.test_xchg import (AK, SK, GOAL, buckets, cust_project, lab_project, quiet, run, short_dir,  # noqa: F401
                             svc)


# --------------------------------------------------------------------------- owner names by oracle (finding 2)


def _owner_hit(svc, code, alternatives):
    """True when one alternative of `alternatives` lies inside the name of a file of the owner inbox."""
    a = keys.request(svc.call_path, {"op": "inbox_find", "words": "|".join(alternatives), "project": code})
    assert a["ok"], a
    return any(m["bucket"] == "owner" for m in a["matches"])


def _bisect(svc, code, cands):
    """One candidate that lies inside an owner name, found by halving the |-separated alternatives."""
    if not cands or not _owner_hit(svc, code, cands):
        return None
    while len(cands) > 1:
        half = cands[:len(cands) // 2]
        cands = half if _owner_hit(svc, code, half) else cands[len(cands) // 2:]
    return cands[0]


def test_the_find_is_no_oracle_for_the_names_of_the_owner_inbox(svc, buckets, cust_project):
    """Promise (xchg docstring, serve_inbox_find, D-F3): a session sees an id, kind, size and time of an owner-inbox
    file, never its name. Attack: a hostile session sends words to op inbox_find and reads only WHETHER an owner
    file matched; the match is a substring test, so the name comes out piece by piece."""
    _, own = buckets
    own.objects["inbox/%s.pdf" % fx.CUSTOMER_FORMS[1]] = b"%PDF-1.4 offer\n"
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    bigrams = [a + b for a in alphabet for b in alphabet]
    seed = None
    for i in range(0, len(bigrams), 160):          # 160 alternatives of 3 characters stay under the 500 limit
        seed = _bisect(svc, cust_project.code, bigrams[i:i + 160])
        if seed:
            break
    recovered = seed or ""
    while recovered:
        right = _bisect(svc, cust_project.code, [recovered + c for c in alphabet])
        if right:
            recovered = right
            continue
        left = _bisect(svc, cust_project.code, [c + recovered for c in alphabet])
        if not left:
            break
        recovered = left
    assert fx.CUSTOMER_FORMS[1].lower() not in recovered, \
        "a session rebuilt the owner-inbox file name %r from the matches of inbox_find" % recovered


# --------------------------------------------------------------------------- the customer of the row (finding 5)


def test_a_project_without_a_customer_cannot_get_one_by_its_own_row(svc, buckets, home, lab_project):
    """Promise (62004d7 F5 and the xchg docstring): a project without a customer takes nothing from the owner
    inbox, and a session's word never chooses the customer of an owner-inbox take. Attack: the session rewrites
    its own row in projects.tsv (the work user's file); the project was never granted by the owner (most lab
    projects never are), so project_customer takes the row as the source."""
    from dataclasses import replace

    _, own = buckets
    rel = "%s/task.txt" % fx.CUSTOMER_CODE
    own.objects["inbox/" + rel] = b"Migrate the database cluster.\n"
    a = keys.request(svc.call_path, {"op": "inbox_find", "all": True, "project": lab_project.code})
    (ident,) = [m["id"] for m in a["matches"] if m["bucket"] == "owner"]
    projects._save(home, [replace(r, customer=fx.CUSTOMER_CODE) if r.code == lab_project.code else r
                          for r in projects.load(home)])
    a = keys.request(svc.call_path, {"op": "take_owner", "id": ident, "project": lab_project.code})
    assert not a["ok"] and "inbox/" + rel in own.objects, \
        "a lab project took customer material after its session rewrote its own row: %r" % a


@pytest.mark.parametrize("req", [{"op": "owner_has", "name": "offer.txt"},
                                 {"op": "take_owner", "name": "offer.txt"}])
def test_a_session_asks_no_owner_inbox_name(svc, buckets, cust_project, req):
    """Finding 2, the exact-name oracles: owner_has and a take of the owner inbox by name answer the owner's own uid
    alone; a session gets one refusal whether the name is there or not, and takes by id."""
    import os

    _, own = buckets
    own.objects["inbox/offer.txt"] = b"The offer.\n"
    answers = []
    for name in ("offer.txt", "missing.txt"):
        answers.append(svc._call_socket(dict(req, name=name, project=cust_project.code), os.getuid() + 1))
    assert answers[0] == answers[1] and not answers[0]["ok"] and "by its id" in answers[0]["error"]
    assert "inbox/offer.txt" in own.objects
