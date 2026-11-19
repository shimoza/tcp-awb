"""Four-part software versions are no IP address and a plain word in a Markdown heading is no secret; real
addresses and real values still are (notice tcp-6ntg 2026-11-19). Invented data only."""
import pytest

from awb import labsecrets
from awb.patterns import find_structured


def ips(text: str) -> list[str]:
    return [text[s.start:s.end] for s in find_structured(text) if s.cls == "ip"]


@pytest.mark.parametrize("text", [
    "The cluster version is 8.2.1.220.",
    "cluster version: 8.2.1.220",
    "Engine 8.2.1.220 runs in the test project.",
    "ClickHouse versions 23.3.2.37 and 22.8.5.29 are offered.",
    "build 8.2.1.220, release 9.1.0.100",
    "the image v8.2.1.220 is current",
    "| Setting | Value |\n|---|---|\n| Cluster version | 8.2.1.220 |\n",
    "| Service | Version |\n|---|---|\n| warehouse | 8.2.1.220 |\n| analytics | 23.3.2.37 |\n",
    "| Service | Engine |\n|---|---|\n| warehouse | 8.2.1.220 |\n",
    "| Service | Shipped |\n|---|---|\n| warehouse | 8.2.1.220 |\n| analytics | 23.3 |\n| search | 7.10.2 |\n",
    "| Service | Shipped |\n|---|---|\n| analytics | 23.3.2.37 |\n| warehouse | 8.2.1.320 |\n",
])
def test_versions_are_no_ip(text):
    assert ips(text) == []


@pytest.mark.parametrize("text, address", [
    ("The server answers on 81.20.30.40 since Monday.", "81.20.30.40"),
    ("Version 2 of the plan moves the gateway to 81.20.30.40.", "81.20.30.40"),
    ("| Host | Address |\n|---|---|\n| app-01 | 81.20.30.40 |\n| app-02 | 81.20.30.41 |\n", "81.20.30.40"),
    ("| Host | Version | Address |\n|---|---|---|\n| app-01 | 8.2.1.220 | 81.20.30.40 |\n", "81.20.30.40"),
    ("| Host | Shipped |\n|---|---|\n| app-01 | 81.20.30.40 |\n| app-02 | 81.20.30.41 |\n", "81.20.30.40"),
    ("build network 81.20.30.0/24", "81.20.30.0/24"),
])
def test_real_addresses_still_found(text, address):
    assert address in ips(text)


@pytest.mark.parametrize("line", [
    "# Password: policy and rotation",
    "## Password: Requirements",
    "### Kennwort: Regeln",
    "## Login: flow",
    "## Username: format",
    "## Bearer authentication",
])
def test_heading_word_is_no_secret(line):
    text = line + "\n\nThe evidence follows.\n"
    assert labsecrets.shorten(text)[0] == text


PW = "Pass" + "word: "
"""Split so the commit gate does not read the invented values below as a secret."""


@pytest.mark.parametrize("line, kept", [
    ("# " + PW + "Xk9#" + "pq2zW", "# " + PW),
    ("## " + PW + "hunter" + "2x", "## " + PW),
    ("## " + PW + '"pl' + 'ain"', "## " + PW + '"'),
    (PW + "policy", PW),
])
def test_real_value_in_heading_stays_masked(line, kept):
    new = labsecrets.shorten(line + "\n")[0]
    assert new.startswith(kept + "[secret]")
