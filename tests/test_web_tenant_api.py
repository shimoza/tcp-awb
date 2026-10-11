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


class ServiceTests(unittest.TestCase):
    """The inventory as its own service (F2): GET /api/tenants only, a plain 503 when the inventory fails."""

    def ask(self, inventory, method="GET", path="/api/tenants"):
        import http.client
        import threading
        server = api.make_server(0, inventory)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            c.request(method, path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, (json.loads(body) if body else None)
        finally:
            server.shutdown()
            server.server_close()

    def test_the_route_the_refresh_and_the_refusals(self):
        seen = []

        class Fake:
            def get(self, refresh=False):
                seen.append(refresh)
                return {"tenants": [], "collected_at": None}
        self.assertEqual(self.ask(Fake()), (200, {"tenants": [], "collected_at": None}))
        self.assertEqual(self.ask(Fake(), path="/api/tenants?refresh=1")[0], 200)
        self.assertEqual(seen, [False, True])
        self.assertEqual(self.ask(Fake(), path="/api/projects"), (404, {"error": "Not found."}))
        self.assertEqual(self.ask(Fake(), method="POST"), (405, {"error": "Method not allowed."}))
        self.assertEqual(self.ask(Fake(), method="HEAD"), (200, None))

        class Broken:
            def get(self, refresh=False):
                raise RuntimeError("no key service")
        self.assertEqual(self.ask(Broken()), (503, {"error": "The tenant inventory is unavailable."}))


def test_the_owner_view_is_on_its_socket_for_its_one_peer_and_never_on_the_port(tmp_path):
    """Finding 14 of the review of 2026-10-10: GET /api/owner/tenants, with the IAM domain names, answers on the
    owner socket alone, and there only a connection whose uid is the socket's one peer (the gateway's); another uid
    is closed before a byte is read. The TCP port keeps the reader view, the owner socket offers nothing else."""
    import http.client
    import os
    import socket
    import threading

    class Fake:
        def get(self, refresh=False):
            return {"tenants": [{"alias": "test-1", "domain_name": "sample-domain-00000001", "status": "complete"}]}

    def ask(path, peer, route):
        server = api.OwnerServer(api.owner_listener(str(path), environ={}), peer, Fake())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(str(path))
            out = b""
            try:
                s.sendall(b"GET " + route + b" HTTP/1.0\r\n\r\n")
                while True:
                    part = s.recv(65536)
                    if not part:
                        break
                    out += part
            except (ConnectionResetError, BrokenPipeError):
                pass            # closed before a byte was read
            s.close()
            return out
        finally:
            server.shutdown()
            server.server_close()

    answer = ask(tmp_path / "a.sock", os.getuid(), b"/api/owner/tenants")
    assert answer.startswith(b"HTTP/1.0 200") and b"sample-domain-00000001" in answer
    assert oct(os.stat(tmp_path / "a.sock").st_mode & 0o777) == "0o660"
    assert ask(tmp_path / "b.sock", os.getuid() + 1, b"/api/owner/tenants") == b""
    assert ask(tmp_path / "c.sock", os.getuid(), b"/api/tenants").startswith(b"HTTP/1.0 404")
    server = api.make_server(0, Fake())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for route, status in (("/api/owner/tenants", 404), ("/api/tenants", 200)):
            c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            c.request("GET", route)
            r = c.getresponse()
            body = r.read()
            c.close()
            assert r.status == status and b"sample-domain-00000001" not in body, route
    finally:
        server.shutdown()
        server.server_close()
