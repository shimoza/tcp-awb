"""G1 of the attacker review of 2026-11-28, through the unlock: whatever tf_services of keys.conf allows, the lease
denies IAM, the reads of a server that answer its user data and the two exchange buckets."""
from __future__ import annotations

from awb.tcp import keys
from tests.test_keys_review import _lease_statements, buckets, quiet, svc  # noqa: F401

READS = ["ecs:cloudServers:get", "ecs:cloudServers:list", "ecs:servers:get", "ecs:servers:list"]


def test_a_narrowed_tf_services_of_keys_conf_is_what_the_lease_carries_with_three_denies(svc, monkeypatch,  # noqa: F811
                                                                                        tmp_path):
    """Replaces test_a_narrowed_tf_services_of_keys_conf_is_what_the_lease_carries (G1 of the review of 2026-11-28):
    the list of keys.conf is the Allow of the lease, obs left out is no OBS at all, an empty list allows nothing,
    and the three denies (IAM, the server reads, the exchange buckets) stay in every case."""
    head = "bucket_tenant = test-1\nlab_bucket = awb-lab-eu-de\nowner_bucket = awb\n"
    statements = _lease_statements(svc, monkeypatch, head + "tf_services = ecs, evs\n", tmp_path)
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert allows == [{"Effect": "Allow", "Action": ["ecs:*:*", "evs:*:*"]}]
    assert [s["Action"] for s in statements if s["Effect"] == "Deny"] == [["iam:*:*"], READS, ["obs:*:*"]]
    statements = _lease_statements(svc, monkeypatch, head + "tf_services = ecs, obs\n", tmp_path)
    assert {"Effect": "Allow", "Action": ["obs:*:*"], "Resource": ["obs:*:*:bucket:tcp-ab2c-*",
                                                                   "obs:*:*:object:tcp-ab2c-*/*"]} in statements
    statements = _lease_statements(svc, monkeypatch, head + "tf_services =\n", tmp_path)
    assert [s["Effect"] for s in statements] == ["Deny"] * 3, "an empty list allows nothing, not the default"
    statements = _lease_statements(svc, monkeypatch, head, tmp_path)
    assert statements[0]["Action"] == ["%s:*:*" % s for s in keys.TF_SERVICES if s != "obs"], "no line: the default"
