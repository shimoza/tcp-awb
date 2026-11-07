"""The gateway serves the page's one font itself (Inter, SIL OFL 1.1), before the login, only with its pinned hash
(2026-11-07). A fake font in a temporary folder stands in for /srv/awb-web/fonts."""
import hashlib
import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests import test_web_gateway as tw
from awb.tcp.web import gateway


class FontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        tw.write_auth(root / "auth.json", "awb", tw.PASSWORD)
        (root / "index.html").write_text("<html>page</html>")
        cls.font = b"wOF2" + bytes(range(256)) * 4
        (root / "fonts").mkdir()
        (root / "fonts" / "InterVariable.woff2").write_bytes(cls.font)
        (root / "fonts" / "OFL-Inter.txt").write_text("licence")
        cls.saved = (gateway.FONT_DIR, gateway.FONTS)
        gateway.FONT_DIR = root / "fonts"
        gateway.FONTS = {"/fonts/InterVariable.woff2": ("InterVariable.woff2", hashlib.sha256(cls.font).hexdigest())}
        cls.root = root
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), tw.Backend)
        cls.server = gateway.make_server(0, gateway.Auth(root / "auth.json"), root / "index.html",
                                         cls.backend.server_port, domain=tw.DOMAIN)
        for s in (cls.backend, cls.server):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.backend, cls.server):
            s.shutdown()
            s.server_close()
        gateway.FONT_DIR, gateway.FONTS = cls.saved
        cls.temp.cleanup()

    def get(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        c.request(method, path, headers={"Host": tw.DOMAIN})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def test_the_font_is_served_without_a_session_with_its_type_and_a_long_cache(self):
        status, h, body = self.get("/fonts/InterVariable.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.font)
        self.assertTrue(body.startswith(b"wOF2"))
        self.assertEqual(h["Content-Type"], "font/woff2")
        self.assertIn("immutable", h["Cache-Control"])
        self.assertNotIn("Set-Cookie", h)

    def test_no_other_file_of_the_font_folder_or_a_path_trick_is_served(self):
        for path in ["/fonts/OFL-Inter.txt", "/fonts/", "/fonts/../gateway.py", "/fonts/InterVariable.woff2/",
                     "/fonts/intervariable.woff2"]:
            status, h, body = self.get(path)
            self.assertNotEqual(status, 200, path)
            self.assertNotEqual(h.get("Content-Type"), "font/woff2", path)

    def test_a_post_to_the_font_is_not_served(self):
        status, h, body = self.get("/fonts/InterVariable.woff2", method="POST")
        self.assertNotEqual(h.get("Content-Type"), "font/woff2")

    def test_a_font_file_with_another_hash_is_not_served(self):
        path = self.root / "fonts" / "InterVariable.woff2"
        try:
            path.write_bytes(self.font + b"planted")
            status, h, body = self.get("/fonts/InterVariable.woff2")
            self.assertEqual(status, 404)
            self.assertNotIn(b"planted", body)
        finally:
            path.write_bytes(self.font)

    def test_the_pinned_hash_and_the_licence_are_in_the_repository(self):
        self.assertEqual(self.saved[0], Path("/srv/awb-web/fonts"))
        name, digest = self.saved[1]["/fonts/InterVariable.woff2"]
        self.assertEqual((name, len(digest)), ("InterVariable.woff2", 64))
        licence = Path(gateway.__file__).resolve().parent / "fonts" / "OFL-Inter.txt"
        self.assertIn("SIL Open Font License", licence.read_text())
