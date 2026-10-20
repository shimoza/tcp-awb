"""K5 and K17 of the attacker review 2026-10-20: the status socket drops an idle client; the front door cuts a
request by one deadline, not by a wait per read."""
import os
import socket
import threading
import time

from awb.tcp.web import gateway


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()


def test_k5_the_status_handler_has_a_short_timeout():
    assert 0 < gateway.StatusHandler.timeout <= 5


def test_k5_an_idle_client_of_the_status_socket_is_dropped(tmp_path):
    path = str(tmp_path / "s.sock")
    server = gateway.make_status_server(gateway.unix_listener(path, 0o666))
    _serve(server)
    try:
        idle = socket.socket(socket.AF_UNIX)
        idle.connect(path)
        idle.settimeout(10)
        start = time.monotonic()
        assert idle.recv(10) == b""          # the gateway closed the idle connection
        assert time.monotonic() - start < 6
        idle.close()
    finally:
        server.shutdown()
        server.server_close()


def test_k17_the_deadline_reader_stops_a_trickle_and_keeps_a_fast_request():
    a, b = socket.socketpair()
    try:
        b.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\nrest")
        reader = gateway.DeadlineReader(a, 1)
        assert reader.readline() == b"GET / HTTP/1.0\r\n"
        assert reader.readline() == b"Host: x\r\n"
        assert reader.readline() == b"\r\n"
        assert reader.read(4) == b"rest"
        stop = threading.Event()

        def trickle():
            while not stop.is_set():
                try:
                    b.sendall(b"a")
                except OSError:
                    return
                time.sleep(0.3)

        threading.Thread(target=trickle, daemon=True).start()
        start = time.monotonic()
        try:
            reader.readline()
            raised = False
        except socket.timeout:
            raised = True
        stop.set()
        assert raised and time.monotonic() - start < 2.5   # a byte every 0.3 s never lets one read time out
    finally:
        a.close()
        b.close()


def test_k17_a_slow_header_is_cut_at_the_deadline_not_per_byte(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "UNAUTHENTICATED_TIMEOUT", 1)
    (tmp_path / "auth.json").write_text('{"username": "awb", "salt": "00", "rounds": 1000, "hash": "00"}')
    sock = gateway.unix_listener(str(tmp_path / "f.sock"), 0o666)
    server = gateway.make_front_server(sock, gateway.Auth(tmp_path / "auth.json"), tmp_path / "index.html", None, 1,
                                       domain="site.invalid", peer=os.getuid())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(tmp_path / "f.sock"))
        start = time.monotonic()
        data = None
        # one byte every 0.4 s: no single read waits the second, the request as a whole does
        for byte in b"GET / HTTP/1.0\r\nX-A: " + b"a" * 40:
            try:
                client.sendall(bytes([byte]))
            except OSError:
                break
            client.settimeout(0.4)
            try:
                data = client.recv(4096)
                break
            except socket.timeout:
                pass
        assert data == b""   # the gateway closed the connection of the headers that did not arrive in time
        assert time.monotonic() - start < 3
    finally:
        server.shutdown()
        server.server_close()
