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
    assert got["error"] == {"code": -32050, "message": "refused: a file outside the dataset folder"}
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
        assert err.value.code == -32050, rel
    assert ds.path("facts.jsonl") == folder.resolve() / "facts.jsonl"


def test_the_refusal_code_is_outside_the_reserved_range_and_every_code_the_specification_names():
    flag = sys.dont_write_bytecode
    try:
        from awb.tcp import tcp_facts_mcp as server
    finally:
        sys.dont_write_bytecode = flag
    named = {-32700, -32600, -32601, -32602, -32603,      # JSON-RPC 2.0
             -32002, -32022, -32042}                        # MCP: resource not found, unsupported version, URL elicitation
    assert server.REFUSED == -32050
    assert not -32019 <= server.REFUSED <= -32000
    assert server.REFUSED not in named
    assert server.REFUSED not in {server.PARSE_ERROR, server.INVALID_REQUEST, server.METHOD_NOT_FOUND,
                                  server.INVALID_PARAMS, server.INTERNAL_ERROR, server.UNSUPPORTED_VERSION}


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


# --------------------------------------------------------------------------- conformance to the MCP revision 2026-07-28

M = "io.modelcontextprotocol/"
META = {M + "protocolVersion": "2026-07-28", M + "clientCapabilities": {}, M + "clientInfo": {"name": "t", "version": "0"}}


def modern(client, method: str, **params) -> dict:
    return client.request(method, dict(params, _meta=META))


def test_the_current_revision_needs_no_handshake_and_server_discover_names_the_server(client):
    got = modern(client, "server/discover")["result"]
    assert got["resultType"] == "complete" and got["supportedVersions"][0] == "2026-07-28"
    assert "2025-11-25" in got["supportedVersions"] and got["capabilities"]["tools"] == {}
    assert got["_meta"][M + "serverInfo"]["name"] == "tcp-facts" and "best before 2026-11-07" in got["instructions"]
    assert got["cacheScope"] == "public" and got["ttlMs"] > 0
    listed = modern(client, "tools/list")["result"]                 # no initialize before it
    assert listed["resultType"] == "complete" and {t["name"] for t in listed["tools"]} == TOOLS
    assert listed["ttlMs"] > 0 and listed["cacheScope"] == "public"
    res = modern(client, "tools/call", name="tcp_calculate", arguments={"expression": "1 + 1"})["result"]
    assert res["resultType"] == "complete" and res["isError"] is False
    assert res["_meta"][M + "serverInfo"]["version"] == listed["_meta"][M + "serverInfo"]["version"]


def test_a_request_with_an_unknown_version_or_without_capabilities_is_refused_as_the_revision_says(client):
    bad = {M + "protocolVersion": "1900-01-01", M + "clientCapabilities": {}}
    err = client.request("tools/list", {"_meta": bad})["error"]
    assert err["code"] == -32022 and err["message"] == "Unsupported protocol version"
    assert err["data"]["requested"] == "1900-01-01" and "2026-07-28" in err["data"]["supported"]
    assert client.request("server/discover", {"_meta": bad})["error"]["code"] == -32022
    no_caps = {M + "protocolVersion": "2026-07-28"}
    assert client.request("tools/list", {"_meta": no_caps})["error"]["code"] == -32602
    assert client.request("tools/list", {"_meta": []})["error"]["code"] == -32602
    old = {M + "protocolVersion": "2025-11-25"}                    # a version of the handshake era is served as such
    assert "result" in client.request("tools/list", {"_meta": old})


def test_a_bad_argument_is_a_tool_result_with_is_error_from_2025_11_25_and_a_json_rpc_error_before(client):
    def bad(params):
        return client.request("tools/call", params)

    cases = [{"name": "tcp_facts_search", "arguments": {}}, {"name": "tcp_facts_search", "arguments": {"query": 5}},
             {"name": "tcp_calculate", "arguments": {"expression": "1", "places": 99}},
             {"name": "tcp_facts_search", "arguments": {"query": "x", "path": "/"}}]
    client.request("initialize", {"protocolVersion": "2025-06-18"})
    for c in cases:
        assert bad(c)["error"]["code"] == -32602, c
    client.request("initialize", {"protocolVersion": "2025-11-25"})
    for c in cases:
        res = bad(c)["result"]
        assert res["isError"] is True and json.loads(res["content"][0]["text"])["error"], c
    got = modern(client, "tools/call", name="tcp_facts_search", arguments={})["result"]
    assert got["isError"] is True and "missing argument: query" in got["content"][0]["text"]
    for params in ({"name": "tcp_nope", "arguments": {}}, {"arguments": {}},
                   {"name": "tcp_facts_search", "arguments": ["x"]}):     # the request itself is wrong: protocol error
        assert modern(client, "tools/call", **params)["error"]["code"] == -32602, params


def test_tools_carry_a_title_and_read_only_annotations_and_live_opens_only_the_price_tool(built):
    def listed(*args):
        c = Client("--data", str(built[0]), *args)
        try:
            return {t["name"]: t for t in c.request("tools/list")["result"]["tools"]}
        finally:
            c.close()

    for name, t in listed().items():
        assert t["title"] and t["annotations"]["title"] == t["title"], name
        assert t["annotations"] == {"title": t["title"], "readOnlyHint": True, "destructiveHint": False,
                                    "idempotentHint": True, "openWorldHint": False}, name
    live = listed("--live")
    assert [n for n, t in live.items() if t["annotations"]["openWorldHint"]] == ["tcp_price_find"]


def test_requests_and_messages_that_break_json_rpc_are_invalid_requests(client):
    bad = [{"jsonrpc": "2.0", "id": None, "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
           {"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, {"jsonrpc": "2.0", "id": 4, "method": 7},
           {"jsonrpc": "2.0", "id": 5}, {"jsonrpc": "2.0"}, [], 7]
    for msg in bad:
        client.send(msg)
        got = client.recv()
        assert got["error"]["code"] == -32600 and got["id"] in (None, 4, 5), msg
    client.send({"jsonrpc": "2.0", "id": 6, "result": {}})            # a response of the client: no answer
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    client.send([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert client.request("initialize", {})["error"]["code"] == -32602
    assert client.request("tools/list", {"cursor": "x"})["error"]["code"] == -32602
    assert client.request("ping")["result"] == {}                    # and nothing came before this answer


def test_a_lone_surrogate_in_a_request_does_not_crash_the_server(client):
    lone = "\\ud800"
    client.send('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"tcp_calculate",'
                '"arguments":{"expression":"1 + %s"}}}' % lone)
    got = client.recv()
    assert got["id"] == 1 and got["result"]["isError"] is True
    client.send('{"jsonrpc":"2.0","id":"a%s","method":"ping"}' % lone)
    assert client.recv()["result"] == {}
    client.send('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"tcp_service_check",'
                '"arguments":{"name":"%s"}}}' % lone)
    assert client.recv()["id"] == 2
    assert client.request("ping")["result"] == {}
    assert client.close() == 0


def test_a_closed_stdout_ends_the_server_quietly(built):
    c = Client("--data", str(built[0]))
    c.proc.stdout.close()
    try:
        for _ in range(200):
            c.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    except BrokenPipeError:
        pass                                                         # the server was gone before the last write
    try:
        c.proc.stdin.close()
    except BrokenPipeError:
        pass
    assert c.proc.wait(timeout=10) == 0 and c.proc.stderr.read() == b""
    c.proc.stderr.close()


def test_stdout_carries_only_protocol_messages_and_the_handshake_negotiates_down(client):
    client.send("{not json")
    client.send("\u00e4\u00f6 not json either")
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    seen = [client.recv(), client.recv()]
    assert [m["error"]["code"] for m in seen] == [-32700, -32700]
    asked_new = client.request("initialize", {"protocolVersion": "2026-07-28"})["result"]
    assert asked_new["protocolVersion"] == "2025-11-25" and "title" in asked_new["serverInfo"]
    client.send([{"jsonrpc": "2.0", "id": 8, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])
    assert client.recv() == [{"jsonrpc": "2.0", "id": 8, "result": {}}]
    assert client.close() == 0


def test_ping_is_gone_in_the_current_revision_and_server_discover_is_complete_in_every_era(client):
    assert modern(client, "ping")["error"]["code"] == -32601        # removed in 2026-07-28
    assert client.request("ping")["result"] == {}                     # the handshake revisions keep it
    for got in (client.request("server/discover")["result"], modern(client, "server/discover")["result"]):
        assert got["resultType"] == "complete" and got["_meta"][M + "serverInfo"]["name"] == "tcp-facts"
        assert got["ttlMs"] > 0 and got["cacheScope"] == "public" and got["supportedVersions"][0] == "2026-07-28"


def test_a_deeply_nested_message_is_a_parse_error_and_the_server_goes_on(client):
    """Review of 2026-10-13: JSON nested deeper than the parser's recursion ended the server with a traceback."""
    client.send("[" * 200_000 + "]" * 200_000)
    got = client.recv()
    assert got["id"] is None and got["error"]["code"] == -32700
    client.send('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"tcp_calculate","arguments":'
                + '{"expression":' * 50_000 + '"1"' + "}" * 50_000 + "}}")
    got = client.recv()
    assert got["error"]["code"] == -32700
    assert client.request("tools/list")["result"]["tools"]
    assert client.close() == 0
