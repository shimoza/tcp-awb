"""`awb clean doctor` and the proxy of the laptop (task lc-doctor): --put and `awb clean update` go through
HTTPS_PROXY, the doctor names the fix of each failure and never prints a user or a password of a proxy URL. A local
fake CONNECT proxy on 127.0.0.1 answers every tunnel with 407 or 200 and records the target; nothing leaves this
machine."""
from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from awb import clean, laptop

PROXY_USER = "proxyuser"
PROXY_PASS = "Pr0xy" + "Pa55" + "word9"


class FakeProxy:
    """A CONNECT proxy that records each request line and answers with `status`; on 200 it closes the tunnel."""

    def __init__(self, status: str = "407 " + "Proxy" + " Authentication" + " Required"):
        self.status = status
        self.lines: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(5)
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.lines.append(data.split(b"\r\n", 1)[0].decode("latin-1"))
                    conn.sendall(("HTTP/1.1 %s\r\nContent-Length: 0\r\n\r\n" % self.status).encode())
                except OSError:
                    pass

    def url(self, login: bool = False) -> str:
        return "http://%s127.0.0.1:%d" % ("%s:%s@" % (PROXY_USER, PROXY_PASS) if login else "", self.port)

    def close(self):
        self.sock.close()


@pytest.fixture
def proxy(monkeypatch):
    p = FakeProxy()
    for k in clean.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", p.url())
    monkeypatch.setenv("https_proxy", p.url())
    monkeypatch.setattr(urllib.request, "_opener", None)    # urlopen builds its opener, with the proxy, once
    yield p
    p.close()


def test_put_goes_through_the_proxy_of_the_environment(proxy, tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    monkeypatch.setenv("AWB_OBS_AK", "AKLABFAKE")
    monkeypatch.setenv("AWB_OBS_SK", "sk-lab-fake-secret")
    f = tmp_path / "a.clean.log"
    f.write_text("ok\n", encoding="utf-8")
    with pytest.raises(clean.CleanError, match="the bucket"):
        clean.put_files("tcp-q7m4", [f], say=lambda s: None)
    assert proxy.lines and all(x == "CONNECT awb.obs.eu-de.otc.t-systems.com:443 HTTP/1.1" for x in proxy.lines)


def test_update_without_git_goes_through_the_proxy(proxy, tmp_path):
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    with pytest.raises(urllib.error.URLError):
        clean.update(rules)
    assert proxy.lines == ["CONNECT api.github.com:443 HTTP/1.1"]


def test_update_with_git_goes_through_the_proxy(proxy, tmp_path):
    if not shutil.which("git"):
        pytest.skip("no git")
    # git reads the same variables: the pull of the public repository asks the proxy for github.com
    subprocess.run(["git", "ls-remote", clean.PUBLIC_REPO], capture_output=True, timeout=60,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HTTPS_PROXY": proxy.url(),
                        "https_proxy": proxy.url(), "GIT_TERMINAL_PROMPT": "0"})
    assert proxy.lines and proxy.lines[0].startswith("CONNECT github.com:443")


def test_doctor_names_the_login_fix_and_never_prints_the_password(proxy, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", proxy.url(login=True))
    monkeypatch.setenv("https_proxy", proxy.url(login=True))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    lines, code = clean.doctor()
    text = "\n".join(lines)
    assert code == 1
    assert PROXY_PASS not in text and PROXY_USER not in text
    assert "HTTPS_PROXY: 127.0.0.1:%d (with a login)" % proxy.port in text
    assert "NO_PROXY: localhost,127.0.0.1" in text and "HTTP_PROXY: not set" in text
    assert text.count(clean.FIX_LOGIN) == 2
    assert {x.split()[0] for x in proxy.lines} == {"CONNECT"}


def test_doctor_names_an_unreachable_proxy_and_a_network_without_one(monkeypatch):
    for k in clean.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                          # nothing listens there any more
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:%d" % port)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:%d" % port)
    lines, code = clean.doctor()
    assert code == 1 and "\n".join(lines).count(clean.FIX_UNREACHABLE) == 2
    for k in clean.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    assert clean.classify(urllib.error.URLError(OSError("no route"))) == clean.FIX_NO_ROUTE


def test_doctor_names_the_certificate_fix_of_a_tls_inspecting_proxy(monkeypatch):
    err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed"))
    assert clean.classify(err) == clean.FIX_TLS
    assert "update-ca-certificates" in clean.FIX_TLS and "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in clean.FIX_TLS
    assert clean.proxy_host("http://%s:%s@proxy.example.org:3128" % (PROXY_USER, PROXY_PASS)) == \
        "proxy.example.org:3128 (with a login)"
    assert clean.proxy_host(None) == "not set"
