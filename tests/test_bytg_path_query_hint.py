"""A query written into the path is refused with a hint where the query goes (ask 2026-11-21-tcp-bytg-6).

A session wrote GET ims /v2/cloudimages?__imagetype=private and got "a path is plain", so it took the list with a
query for refused; the same call with the query beside the path works. The key service is the stand-in of
tests/test_keys.py.
"""
from __future__ import annotations

import pytest

from awb.tcp import keys
from tests import test_keys as tk
from tests.test_keys import gw  # noqa: F401


@pytest.fixture
def ksvc(gw, tmp_path):  # noqa: F811
    with tk.running_service(gw, tmp_path / "log") as s:
        tk.load(s)
        yield s


def test_bytg_a_query_in_the_path_is_refused_with_the_hint(ksvc, gw):  # noqa: F811
    before = len(gw.calls)
    a = tk.call(ksvc, method="GET", service="ims", path="/v2/cloudimages?__imagetype=private")
    assert not a["ok"] and "a path is plain" in a["error"] and "--query K=V" in a["error"], a
    assert len(gw.calls) == before


def test_bytg_a_bad_path_without_a_query_keeps_the_plain_refusal():
    said = keys.path_refusal("/v1/../x")
    assert said == "a path is plain: /v1/{project_id}/cloudservers"
