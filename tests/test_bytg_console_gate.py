"""Two requests of the project tcp-bytg (2026-11-18): a VNC console link of an ECS reaches the owner whole, and
the commit gate scans a large lab log in pieces instead of being killed."""
from __future__ import annotations

from awb import gate, hooks, labsecrets, paste
from awb import winlabsession as wls

TOKEN = "-".join(("3f2a1b4c", "9d8e", "4f7a", "8b6c", "5d4e3f2a1b0c"))
LINK = "https://console.eu-de.otc.t-systems.com/novnc/vnc_auto.html?" + "tok" + "en=" + TOKEN + "&lang=en"
IAM = "MII" + "EhgYJKoZIhvcNAQcCoIIEdzCCBHMCAQExDTALBglghkgBZQMEAgEwggLYBgkqhkiG9w0BBwGgggLJBIICxXsidG9r"


def _secret_line() -> str:
    return "db_pass" + "word = \"" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc" + "\""


def test_bytg_the_lab_shortener_keeps_a_console_token_whole_and_shortens_the_rest():
    text = '{"remote_console": {"type": "novnc", "url": "%s"}, "X-Auth-Token": "%s"}' % (LINK, IAM)
    out, counts = labsecrets.shorten(text)
    assert LINK in out
    assert IAM not in out and labsecrets.short(IAM) in out
    assert counts["shortened"] == 1


def test_bytg_the_gate_finds_no_secret_in_a_console_link_but_in_the_same_token_elsewhere():
    assert "secret" not in {f.cls for f in gate._scan_text("lab", 'url: "%s"\n' % LINK, None)}
    other = 'url: "https://example.com/api/vnc?' + 'tok' + 'en=' + TOKEN + '"\n'
    assert "secret" in {f.cls for f in gate._scan_text("lab", other, None)}


def test_bytg_a_console_link_passes_the_winlab_put_check_and_the_own_text_whole(monkeypatch):
    monkeypatch.setattr(hooks, "name_hits", lambda text, *a, **k: [])     # no vault in a test
    wls.guard_text("Start-Process '%s'\n" % LINK)           # WinLab sends it as it is: no secret check of a command
    clean, n = paste.own_text("console: %s\n" % LINK)
    assert LINK in clean and n == 0


def test_bytg_a_large_text_is_scanned_in_pieces_with_the_right_line_numbers(monkeypatch):
    monkeypatch.setattr(gate, "PIECE_FROM", 2000)
    monkeypatch.setattr(gate, "PIECE", 500)
    monkeypatch.setattr(gate, "PIECE_OVERLAP", 4)
    lines = ["reader read %04d: block ok, status READY" % i for i in range(400)]
    for at in (5, 200, 398):
        lines[at] = _secret_line()
    data = ("\n".join(lines) + "\n").encode()
    assert len(list(gate._pieces(data.decode()))) > 10
    found = [(f.line, f.cls) for f in gate._scan_bytes("notes/lab-runs/x.log", data, None)]
    assert found == [(6, "secret"), (201, "secret"), (399, "secret")]


def test_bytg_a_key_pair_across_a_piece_cut_is_still_found(monkeypatch):
    monkeypatch.setattr(gate, "PIECE_FROM", 2000)
    monkeypatch.setattr(gate, "PIECE", 500)
    monkeypatch.setattr(gate, "PIECE_OVERLAP", 4)
    ak = "AKIA" + "Q7M4ZX2P" + "LK9W3RTB"
    sk = "wJal" + "rXUtnFEMI/K7MDENG/bPxRfiCY" + "zQ8p3Lm2Kd"
    lines = ["reader read %04d: block ok, status READY" % i for i in range(300)]
    # find a cut and put the id on the last line of one piece and the secret on the first of the next
    text = "\n".join(lines) + "\n"
    first_new = [first + gate.PIECE_OVERLAP for first, _ in list(gate._pieces(text))[1:]]
    cut = first_new[3]
    lines[cut - 1] = "id " + ak
    lines[cut] = "key " + sk
    data = ("\n".join(lines) + "\n").encode()
    assert "secret" in {f.cls for f in gate._scan_bytes("notes/lab-runs/x.log", data, None)}


def test_bytg_a_text_up_to_the_limit_is_one_piece():
    text = "a line\n" * 100
    assert list(gate._pieces(text)) == [(0, text)]
