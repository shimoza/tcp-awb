"""K5 of the attacker review 2026-10-20: the status socket drops an idle client."""
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
