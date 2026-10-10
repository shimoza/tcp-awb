"""awb/tcp/tcp_facts_mcp.py: the MCP server every build of the dataset TCP Facts carries, driven over pipes on a
dataset built by the build code from invented facts."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from awb.tcp import dataset
from tests.test_dataset import RAW, S_ECS, S_IAM, S_OBS, TODAY, new, snapshot

SERVER = dataset.MCP_TEMPLATE
TOOLS = {"tcp_facts_search", "tcp_service_check", "tcp_price_find", "tcp_calculate"}


@pytest.fixture
def built(home, tmp_path):
    """A dataset of 2026-10-08 with three facts that leave and a price snapshot of eu-de, built without the network."""
    ids = {"ecs": new(S_ECS, grade="live", source="live API call in eu-de").id,
           "obs": new(S_OBS, grade="docs", tags=["obs", "storage"]).id,
           "iam": new(S_IAM, grade="contract", tags=["iam"]).id}
    new("GPU flavor p2 needs a quota increase in eu-nl", grade="said", tags=["gpu"])
    snapshot(home, "eu-de", "2026-10-07", [dict(RAW, priceAmount="0.051000 EUR")])
    b = dataset.build(home, out=tmp_path / "ds", today=TODAY, live=False)
    return b.folder, ids


class Client:
    """The server as a child process; one JSON-RPC message per line on its stdin and stdout."""

    def __init__(self, *args: str, cwd: Path | None = None, env: dict | None = None, server: Path = SERVER):
        self.proc = subprocess.Popen([sys.executable, str(server), *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env)
        self.next_id = 0

    def send(self, msg) -> None:
        self.proc.stdin.write((json.dumps(msg) if not isinstance(msg, str) else msg).encode() + b"\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read().decode()
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        got = self.recv()
        assert got["id"] == self.next_id
        return got

    def call(self, name: str, /, **args) -> dict:
        got = self.request("tools/call", {"name": name, "arguments": args})
        assert "result" in got, got
        return got["result"]

    def answer(self, name: str, /, **args) -> dict:
        res = self.call(name, **args)
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def close(self) -> int:
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


@pytest.fixture
def client(built):
    c = Client("--data", str(built[0]))
    yield c
    c.close()


def test_initialize_and_tools_list_answer_as_the_protocol_says(client):
    got = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "0"}})
    res = got["result"]
    assert got["jsonrpc"] == "2.0" and res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "tcp-facts" and res["capabilities"]["tools"] == {"listChanged": False}
    assert "2026-10-08" in res["instructions"] and "best before 2026-11-07" in res["instructions"]
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})        # no answer to a notification
    assert client.request("ping")["result"] == {}
    tools = client.request("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == TOOLS
    for t in tools:
        schema = t["inputSchema"]
        assert t["description"] and schema["type"] == "object" and schema["required"]
        assert set(schema["required"]) <= set(schema["properties"]) and schema["additionalProperties"] is False
    old = client.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert old["protocolVersion"] == "2025-11-25"


def test_every_answer_carries_the_dates_and_the_rule(client):
    for name, args in (("tcp_facts_search", {"query": "s3.large.2"}), ("tcp_service_check", {"name": "ECS"}),
                       ("tcp_price_find", {"query": "s3.large.2"}), ("tcp_calculate", {"expression": "1 + 1"})):
        got = client.answer(name, **args)
        assert got["dataset"] == "TCP Facts" and got["date"] == "2026-10-08", name
        assert got["best_before"] == "2026-11-07" and got["rule"] == \
            "Answer from these facts, cite the id, say when no fact covers it.", name
        if "warning" in got:                                       # a run after the best-before date
            assert "today is after the best-before date 2026-11-07" in got["warning"], name


def test_facts_search_ranks_by_word_overlap_and_filters_by_tag_and_grade(client, built):
    _, ids = built
    got = client.answer("tcp_facts_search", query="which flavor s3.large.2 in eu-de")
    first = got["facts"][0]
    assert first["id"] == ids["ecs"] and first["statement"] == S_ECS and first["grade"] == "live"
    assert first["checked"] == "2026-10-08" and first["source"] == "live API call in eu-de"
    assert set(first) >= {"id", "statement", "grade", "checked", "source", "expires"} and first["expires"]
    assert [f["id"] for f in client.answer("tcp_facts_search", query="eu-de", tag="iam")["facts"]] == [ids["iam"]]
    assert [f["id"] for f in client.answer("tcp_facts_search", query="eu-de", grade="docs")["facts"]] == [ids["obs"]]
    assert {f["id"] for f in client.answer("tcp_facts_search", query="eu-de")["facts"]} == set(ids.values())
    none = client.answer("tcp_facts_search", query="quantum annealing")
    assert none["count"] == 0 and none["facts"] == [] and "no checked fact covers this" in none["note"]
    assert "GPU" not in json.dumps(client.answer("tcp_facts_search", query="gpu p2 quota"))   # a said fact never left


def test_facts_search_returns_at_most_twenty(home, tmp_path):
    for i in range(25):
        new("ECS flavor family x%d.large.%d is offered in eu-de" % (i, i), grade="docs", force_new=True)
    folder = dataset.build(home, out=tmp_path / "many", today=TODAY, live=False).folder
    c = Client("--data", str(folder))
    try:
        got = c.answer("tcp_facts_search", query="ecs flavor family offered")
        assert got["count"] == 20 and len(got["facts"]) == 20
    finally:
        c.close()


def test_service_check_answers_offered_or_not_with_the_revision(client, built):
    revision = dataset.offered.revision_label(dataset.offered.load())
    for name in ("ECS", "elastic cloud server", "ecs"):
        got = client.answer("tcp_service_check", name=name)
        assert got["offered"] is True and got["service_description"] == revision, name
        assert any(s.startswith("3.1.1 ") for s in got["services"]), name
    assert client.answer("tcp_service_check", name="CCE Turbo")["offered"] is True        # a second short name
    bms = client.answer("tcp_service_check", name="BMS")
    assert bms["offered"] is False and "does not list it" in bms["note"] and revision in bms["note"]
    assert client.answer("tcp_service_check", name="bare metal server")["offered"] is False
    close = client.answer("tcp_service_check", name="load balancer")
    assert close["offered"] == "unclear" and any("ELB" in s for s in close["services"])
    gone = client.answer("tcp_service_check", name="quantum annealer")
    assert gone["offered"] is False and gone["service_description"] == revision
    services_md = (built[0] / "services.md").read_text()
    assert "## Not offered" in services_md and "(BMS)" in services_md and "CCE Turbo" in services_md


def test_price_find_reads_the_snapshot_with_its_date(client):
    got = client.answer("tcp_price_find", query="s3.large.2 linux")
    assert got["count"] == 1 and got["region"] == "eu-de"
    row = got["rows"][0]
    assert row["id"] == "OTC_ECS_S3L2" and row["payg"] == "0.051000" and row["unit"] == "h"
    assert "fetched 2026-10-07 06:00 UTC" in got["source"] and "source of truth" in got["note"]
    assert client.answer("tcp_price_find", query="s3.large.2", region="eu-nl")["count"] == 0
    assert client.answer("tcp_price_find", query="s3.large.2 windows")["count"] == 0


class FakePriceAPI(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):
        FakePriceAPI.seen.append(self.path)
        body = json.dumps({"response": {"code": "Success", "stats": {"count": 1}, "result": {"ecs": [
            dict(RAW, priceAmount="0.052000 EUR")]}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def price_api():
    FakePriceAPI.seen = []
    srv = HTTPServer(("127.0.0.1", 0), FakePriceAPI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d/api/" % srv.server_port
    srv.shutdown()
    srv.server_close()


def test_no_network_without_live_and_the_price_api_with_it(built, price_api):
    c = Client("--data", str(built[0]), "--price-api", price_api)
    try:
        got = c.answer("tcp_price_find", query="s3.large.2", service="ecs")
        assert got["rows"][0]["payg"] == "0.051000" and "snapshot" in got["source"]
        assert FakePriceAPI.seen == []
    finally:
        c.close()
    c = Client("--data", str(built[0]), "--live", "--price-api", price_api)
    try:
        got = c.answer("tcp_price_find", query="s3.large.2", service="ecs")
        assert got["source"].startswith("live public price API, fetched ") and got["rows"][0]["payg"] == "0.052000"
        assert len(FakePriceAPI.seen) == 1 and "sn=ecs" in FakePriceAPI.seen[0] and "rn=eu-de" in FakePriceAPI.seen[0]
        snap = c.answer("tcp_price_find", query="s3.large.2")              # no service: the snapshot, and why
        assert "snapshot" in snap["source"] and "service short name" in snap["live"]
    finally:
        c.close()


def test_calculate_is_exact_and_counts_in_hours(client):
    got = client.answer("tcp_calculate", expression="0.051 EUR/h * 720 h * 3")
    assert got["result"] == "110.16"
    assert client.answer("tcp_calculate", expression="0.051 EUR/h * 3 x 1 month")["result"] == "110.16"
    assert "months = 720 h" in client.answer("tcp_calculate", expression="2 months")["hours"]
    assert client.answer("tcp_calculate", expression="0.1 + 0.2")["result"] == "0.3"
    assert client.answer("tcp_calculate", expression="10 / 3", places=2)["result"] == "3.33"
    assert client.answer("tcp_calculate", expression="round(2.675, 2) + 1e3")["result"] == "1002.68"
    for bad in ("1 / 0", "__import__('os')", "2 ** 100000", "7 % 2", "3 apples"):
        res = client.call("tcp_calculate", expression=bad)
        assert res["isError"] is True and "does not compute" in json.loads(res["content"][0]["text"])["error"], bad


def test_an_unknown_tool_and_a_bad_argument_answer_with_json_rpc_errors(client):
    def error(params) -> dict:
        got = client.request("tools/call", params)
        assert "result" not in got, got
        return got["error"]

    assert error({"name": "tcp_delete_everything", "arguments": {}}) == {"code": -32602,
                                                                       "message": "unknown tool: tcp_delete_everything"}
    assert "missing argument: query" in error({"name": "tcp_facts_search", "arguments": {}})["message"]
    assert error({"name": "tcp_facts_search", "arguments": {"query": 5}})["code"] == -32602
    assert error({"name": "tcp_facts_search", "arguments": {"query": "x", "grade": "said"}})["code"] == -32602
    assert error({"name": "tcp_facts_search", "arguments": {"query": "x", "path": "/"}})["message"] == \
        "unknown argument: path"
    assert error({"name": "tcp_calculate", "arguments": {"expression": "1", "places": 99}})["code"] == -32602
    assert error({"name": "tcp_calculate", "arguments": {"expression": "1", "places": True}})["code"] == -32602
    assert error({"name": "tcp_price_find", "arguments": {"query": "x", "service": "../ecs"}})["code"] == -32602
    assert client.request("resources/write", {})["error"]["code"] == -32601
    client.send("{not json")
    assert client.recv() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
    client.send({"id": 9, "method": "ping"})
    assert client.recv()["error"]["code"] == -32600
    assert client.request("ping")["result"] == {}                  # the server answers on after every error


def test_a_path_outside_the_dataset_is_refused(client, built, tmp_path):
    folder, _ = built
    got = client.request("tools/call", {"name": "tcp_price_find",
                                        "arguments": {"query": "x", "region": "../../../etc/passwd"}})
    assert got["error"]["code"] == -32602
    outside = tmp_path / "outside.csv"
    outside.write_text("# planted, fetched 2026-01-01. Prices\nid,service\nPLANTED,planted\n")
    csv_path = folder / "prices" / "eu-de.csv"
    csv_path.unlink()
    csv_path.symlink_to(outside)
    got = client.request("tools/call", {"name": "tcp_price_find", "arguments": {"query": "planted"}})
    assert got["error"] == {"code": -32001, "message": "refused: a file outside the dataset folder"}
    assert "PLANTED" not in json.dumps(got)
    flag = sys.dont_write_bytecode
    try:
        from awb.tcp import tcp_facts_mcp as server
    finally:
        sys.dont_write_bytecode = flag
    ds = server.Dataset(folder)
    for rel in ("../outside.csv", str(outside), "prices/../../outside.csv", "prices/eu-de.csv"):
        with pytest.raises(server.RpcError) as err:
            ds.path(rel)
        assert err.value.code == -32001, rel
    assert ds.path("facts.jsonl") == folder.resolve() / "facts.jsonl"


def _tree(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def test_the_server_never_writes_a_file(built, tmp_path, price_api):
    folder, _ = built
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = dict(os.environ, HOME=str(cwd), TMPDIR=str(cwd))
    before = _tree(tmp_path)
    c = Client("--live", "--price-api", price_api, cwd=cwd, env=env, server=folder / dataset.MCP_FILE)
    try:
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        c.request("tools/list")
        c.answer("tcp_facts_search", query="eu-de")
        c.answer("tcp_service_check", name="ECS")
        c.answer("tcp_price_find", query="s3.large.2", service="ecs")
        c.answer("tcp_price_find", query="s3.large.2")
        c.answer("tcp_calculate", expression="1 h * 2")
        c.request("tools/call", {"name": "nope"})
    finally:
        assert c.close() == 0
    assert _tree(tmp_path) == before
    source = SERVER.read_text()
    for call in (".write_text(", ".write_bytes(", ".open(", "mkdir(", "unlink(", "rmtree(", "os.remove(", "rename(",
                 "os.replace(", "tempfile", "subprocess", "shutil"):
        assert call not in source, call
    assert not re.search(r"(?<![\w.])open\(", source)


def test_the_build_carries_the_server_and_scans_it(home, built, tmp_path, monkeypatch):
    folder, _ = built
    assert (folder / dataset.MCP_FILE).read_bytes() == SERVER.read_bytes()
    manifest = json.loads((folder / "MANIFEST.json").read_text())
    assert dataset.MCP_FILE in manifest["files"]
    import zipfile
    with zipfile.ZipFile(folder.parent / (folder.name + ".zip")) as z:
        assert "%s/%s" % (folder.name, dataset.MCP_FILE) in z.namelist()
    assert dataset.MCP_FILE in dataset.GITHUB_FILES
    assert dataset.MCP_FILE in (folder / "PROMPT.md").read_text()
    for plant, why in (("# a call on test-10491\n", "a tenant alias was left in tcp_facts_mcp.py"),
                       ("# made with awb dataset build\n", "a name of the tooling was left in tcp_facts_mcp.py")):
        bad = tmp_path / ("bad-%d" % len(plant)) / dataset.MCP_FILE
        bad.parent.mkdir()
        bad.write_text(SERVER.read_text() + plant)
        monkeypatch.setattr(dataset, "MCP_TEMPLATE", bad)
        with pytest.raises(dataset.DatasetError) as err:
            dataset.build(home, out=tmp_path / "scan", today=TODAY, live=False, force=True)
        assert why in str(err.value)
        assert not (tmp_path / "scan" / folder.name).exists()


def test_the_server_finds_its_dataset_beside_it_and_refuses_a_folder_without_facts(built, tmp_path):
    folder, ids = built
    c = Client(server=folder / dataset.MCP_FILE)                  # no --data: the folder the file lies in
    try:
        assert c.answer("tcp_facts_search", query="iam user quota")["facts"][0]["id"] == ids["iam"]
    finally:
        c.close()
    res = subprocess.run([sys.executable, str(SERVER), "--data", str(tmp_path / "empty")], capture_output=True,
                         text=True, timeout=10, stdin=subprocess.DEVNULL)
    assert res.returncode == 2 and "no facts.jsonl" in res.stderr
