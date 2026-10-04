"""The read API of the projects (awb/tcp/web/projects_api.py): the register as the console sees it, files read only
from verified project folders, the data check before anything is returned, answers that fit the contract.

Ported from the tests the web side wrote for the deployed service; the contract checks are new.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb import config, projects, vault
from awb.tcp.web import contract
from awb.tcp.web import projects_api as api


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = config.Paths(shared=self.root / "shared", vault=self.root / "vault-not-read",
                                  projects_root=self.root, kb=self.root / "kb")
        self.folder = self.root / "tcp-q7m4"
        self.folder.mkdir()
        self.row = projects.Project("tcp-q7m4", "lab", "none", "tcp", str(self.folder),
                                    projects.memory_key(self.folder), "active", "2026-10-01")
        (self.folder / "SCOPE.md").write_text("# Scope\n- goal: Test a sample deployment\n")
        (self.folder / "OPEN.md").write_text("# Open items\n- One question\n- Another question\n")
        (self.folder / "STATE.md").write_text("# State\n## Now\n- Work in progress\n")
        (self.folder / "RESOURCES.md").write_text("| id | type | state |\n|---|---|---|\n| one | server | live |\n"
                                                  "| two | disk | deleted |\n| three | image | kept |\n")
        self.load = patch.object(projects, "load", return_value=[self.row])
        self.load.start()
        self.check = patch.object(vault, "check_remote", return_value=[])
        self.check.start()

    def tearDown(self):
        self.load.stop()
        self.check.stop()
        self.tmp.cleanup()

    def test_real_registry_shape_and_counts(self):
        data = api.project_list(self.paths)
        row = data["projects"][0]
        self.assertEqual(row["goal"], "Test a sample deployment")
        self.assertEqual(row["open_items"], 2)
        self.assertEqual(row["live_resources"], 1)
        self.assertNotIn("path", row)
        self.assertNotIn("memory_key", row)

    def test_missing_file_not_reported_as_zero(self):
        (self.folder / "OPEN.md").unlink()
        row, _, _ = api.base_record(self.row, self.paths)
        self.assertIsNone(row["open_items"])

    def test_deleted_not_listed_or_opened(self):
        self.row.state = "deleted"
        self.assertEqual(api.project_list(self.paths)["projects"], [])
        self.assertIsNone(api.project_detail(self.row.code, self.paths))

    def test_unknown_codes_and_traversal_not_opened(self):
        for value in ["../vault", "tcp-zzzz", "/etc/passwd", "tcp-q7m4/../../vault"]:
            self.assertIsNone(api.project_detail(value, self.paths))

    def test_file_symlink_is_not_read(self):
        f = self.folder / "SCOPE.md"
        f.unlink()
        target = self.root / "outside"
        target.write_text("secret should not be read")
        f.symlink_to(target)
        doc = api.read_text(self.folder, "SCOPE.md")
        self.assertEqual(doc["status"], "unavailable")
        self.assertEqual(doc["text"], "")

    def test_outside_registered_path_is_not_read(self):
        self.row.path = str(self.root / "elsewhere" / "tcp-q7m4")
        with self.assertRaises(api.Unavailable):
            api.base_record(self.row, self.paths)

    def test_project_symlink_is_not_read(self):
        actual = self.root / "other"
        self.folder.rename(actual)
        self.folder.symlink_to(actual)
        with self.assertRaises(api.Unavailable):
            api.base_record(self.row, self.paths)

    def test_data_check_failure_is_closed(self):
        with patch.object(vault, "check_remote", side_effect=vault.VaultUnavailable("unavailable")):
            with self.assertRaises(api.Unavailable):
                api.project_list(self.paths)

    def test_redaction_preserves_safe_content(self):
        def check(text, sock):
            return [{"start": text.index("MASKME"), "length": 6, "cls": "name"}] if "MASKME" in text else []
        with patch.object(vault, "check_remote", side_effect=check):
            text, redacted = api.checked("Before MASKME after", self.paths)
            self.assertTrue(redacted)
            self.assertEqual(text, "Before [withheld] after")

    def test_deliverable_symlink_blocks_review_reader(self):
        (self.folder / "deliverables").mkdir()
        (self.folder / "deliverables" / "leak").symlink_to(self.root / "elsewhere")
        with patch.object(api.review, "status") as reader:
            self.assertEqual(api.safe_review_status(self.folder, self.paths)["status"], "unavailable")
            reader.assert_not_called()

    def test_detail_documents_and_review_status(self):
        rows = [{"file": "deliverables/result.md", "state": "valid", "tier": 2}]
        with patch.object(api.review, "status", return_value=rows):
            result = api.project_detail("tcp-q7m4", self.paths)
            self.assertEqual(set(result["documents"]), set(api.FILES))
            self.assertEqual(result["deliverables"]["items"][0]["state"], "valid")

    def test_the_answers_fit_the_contract(self):
        doc = contract.build()
        self.assertEqual(contract.validate(doc, api.project_list(self.paths), contract.ref("ProjectList")), [])
        rows = [{"file": "deliverables/result.md", "state": "valid", "tier": 2}]
        with patch.object(api.review, "status", return_value=rows):
            detail = api.project_detail("tcp-q7m4", self.paths)
        self.assertEqual(contract.validate(doc, detail, contract.ref("ProjectDetail")), [])
        (self.folder / "deliverables").mkdir()
        (self.folder / "deliverables" / "leak").symlink_to(self.root / "elsewhere")
        detail = api.project_detail("tcp-q7m4", self.paths)
        self.assertEqual(detail["deliverables"], {"status": "unavailable", "items": []})
        self.assertEqual(contract.validate(doc, detail, contract.ref("ProjectDetail")), [])


def test_the_projects_service_no_longer_answers_the_tenants():
    """F2: the tenants moved to their own service and user; this service reads projects only."""
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("GET", "/api/tenants")
        r = c.getresponse()
        assert (r.status, json.loads(r.read())) == (404, {"error": "No such project endpoint."})
        c.close()
    finally:
        server.shutdown()
        server.server_close()
