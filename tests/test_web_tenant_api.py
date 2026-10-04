"""The tenant inventory (awb/tcp/web/tenant_api.py): counts only over complete listings, no raw cloud id or resource
name in the answer, stable handles, one refresh at a time, answers that fit the contract.

Ported from the tests the web side wrote for the deployed service; the contract checks are new.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from awb.tcp.tenants import Tenant
from awb.tcp.web import contract
from awb.tcp.web import tenant_api as api


class Tests(unittest.TestCase):
    def setUp(self):
        self.tenant = Tenant("test-one", ("eu-de",), "unused", "2026-10-02")
        self.calls = []

    def caller(self, alias, region, service, path, query=None):
        self.calls.append((alias, service, path, query))
        if path == "/v3/auth/domains":
            return {"domains": [{"name": "tenant.example.invalid"}]}
        key = next(s[3] for s in api.SOURCES if s[2] == path)
        if key == "servers":
            return {key: [{"id": "server-one", "status": "ACTIVE", "name": "do not expose", "user_id": "not-the-owner",
                           "flavor": {"id": "s3.large.2"}}, {"id": "server-two", "status": "SHUTOFF"}]}
        if key == "volumes":
            return {key: [{"id": "disk-one", "status": "in-use", "size": 40}]}
        return {key: []}

    def test_counts_domains_and_no_raw_identifiers(self):
        r = api.collect_tenant(self.tenant, self.caller)
        self.assertEqual(r["domain_name"], "tenant.example.invalid")
        self.assertEqual(r["counts"]["ecs"], 2)
        self.assertEqual(r["counts"]["running"], 1)
        self.assertEqual(r["counts"]["stopped"], 1)
        self.assertEqual(r["counts"]["disk_gib"], 40)
        self.assertFalse(r["ownership_verified"])
        self.assertEqual(r["scope"], "accessible")
        for private in ["server-one", "do not expose", "not-the-owner"]:
            self.assertNotIn(private, json.dumps(r))

    def test_failed_category_is_unknown_not_zero(self):
        def caller(*args):
            if args[3].endswith("cloudvolumes/detail"):
                raise api.InventoryError("Unavailable")
            return self.caller(*args)
        r = api.collect_tenant(self.tenant, caller)
        self.assertIsNone(r["counts"]["evs"])
        self.assertIsNone(r["counts"]["disk_gib"])
        self.assertEqual(r["counts"]["ecs"], 2)
        self.assertEqual(r["status"], "partial")

    def test_partial_region_does_not_look_complete(self):
        t = Tenant("test-one", ("eu-de", "eu-nl"), "unused", "2026-10-02")

        def caller(*args):
            if args[1] == "eu-nl":
                raise api.InventoryError("Unavailable")
            return self.caller(*args)
        r = api.collect_tenant(t, caller)
        self.assertIsNone(r["counts"]["running"])
        self.assertEqual(r["status"], "partial")

    def test_pagination_covers_all_pages(self):
        def caller(a, r, s, p, q):
            offset = int(q.get("offset", 0))
            return {"servers": [{"id": str(x)} for x in range(offset, min(offset + 100, 103))], "count": 103}
        self.assertEqual(len(api.listing("test-one", "eu-de", api.SOURCES[0], caller)), 103)

    def test_repeated_or_truncated_page_is_rejected(self):
        with self.assertRaises(api.InventoryError):
            api.listing("test-one", "eu-de", api.SOURCES[0],
                        lambda *args: {"servers": [{"id": str(i)} for i in range(100)]})
        with self.assertRaises(api.InventoryError):
            api.listing("test-one", "eu-de", api.SOURCES[0], lambda *args: {"servers": [{"id": "one"}], "count": 5})

    def test_domain_ambiguity_not_guessed(self):
        def caller(*args):
            if args[3] == "/v3/auth/domains":
                return {"domains": [{"name": "one.invalid"}, {"name": "two.invalid"}]}
            return self.caller(*args)
        r = api.collect_tenant(self.tenant, caller)
        self.assertIsNone(r["domain_name"])
        self.assertEqual(r["domain_status"], "unavailable")

    def test_handles_stable_and_names_do_not_escape(self):
        raw = {"id": "same", "name": "tcp-q7m4-lab", "status": "ACTIVE", "tags": ["awb-expiry=2026-10-10"]}
        a = api.item("test-one", "eu-de", "ecs", raw)
        b = api.item("test-one", "eu-de", "ecs", raw)
        self.assertEqual(a["handle"], b["handle"])
        self.assertEqual(a["project"], "tcp-q7m4")
        self.assertEqual(a["project_source"], "name")
        self.assertNotEqual(a["handle"], api.item("test-two", "eu-de", "ecs", raw)["handle"])

    def test_cache_single_flight_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            count = []

            def collector(t):
                count.append(t.alias)
                return api.collect_tenant(t, self.caller)
            inventory = api.Inventory(Path(tmp) / "cache.json", lambda: [self.tenant], collector)
            self.assertTrue(inventory.get()["refreshing"])
            for _ in range(100):
                if not inventory.get()["refreshing"]:
                    break
                time.sleep(.01)
            self.assertEqual(len(inventory.get()["tenants"]), 1)
            inventory.get(refresh=True)
            self.assertEqual(len(count), 1)
            saved = api.Inventory(Path(tmp) / "cache.json", lambda: [self.tenant], collector)
            self.assertEqual(saved.get()["tenants"][0]["counts"]["running"], 1)
            saved.loader = lambda: []
            self.assertEqual(saved.get()["tenants"], [])

    def test_the_answers_fit_the_contract(self):
        doc = contract.build()
        tenant = api.collect_tenant(self.tenant, self.caller)
        self.assertEqual(contract.validate(doc, tenant, contract.ref("Tenant")), [])

        def caller(*args):
            if args[3].endswith("cloudvolumes/detail"):
                raise api.InventoryError("Unavailable")
            return self.caller(*args)
        self.assertEqual(contract.validate(doc, api.collect_tenant(self.tenant, caller), contract.ref("Tenant")), [])
        with tempfile.TemporaryDirectory() as tmp:
            inventory = api.Inventory(Path(tmp) / "cache.json", lambda: [self.tenant],
                                      lambda t: api.collect_tenant(t, self.caller))
            first = inventory.get()
            self.assertEqual(contract.validate(doc, first, contract.ref("TenantInventory")), [])
            for _ in range(100):
                if not inventory.get()["refreshing"]:
                    break
                time.sleep(.01)
            filled = inventory.get()
            self.assertIn("timestamp", filled)
            self.assertEqual(contract.validate(doc, filled, contract.ref("TenantInventory")), [])
