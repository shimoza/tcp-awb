"""The contract of the web API (awb/tcp/web/contract.py, docs/api/openapi.yaml).

The file is written from the module and equal to it, every example fits its schema, every operation says
whether it is deployed, each check is shown to fail, and the contract follows the backend: the kinds, the
codes and the limits of the chat.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from awb import codes, projects
from awb.cli import main as awb
from awb.tcp import ask
from awb.tcp.web import contract


@pytest.fixture(scope="module")
def doc():
    return contract.build()


def _body(doc, path, method, status, media="application/json"):
    return doc["paths"][path][method]["responses"][status]["content"][media]


def _first_example(doc, path, method, status):
    return next(iter(_body(doc, path, method, status)["examples"].values()))["value"]


def test_the_file_is_written_from_the_module(doc):
    assert contract.CONTRACT_FILE.read_text(encoding="utf-8") == contract.to_yaml(doc), "run: awb api write"


def test_the_contract_passes_its_own_checks(doc):
    assert contract.check(doc) == []


def test_every_operation_says_whether_it_is_deployed(doc):
    ops = list(contract.operations(doc))
    assert len(ops) >= 20
    assert {op["x-awb-state"] for _, _, op in ops} <= set(contract.STATES)
    paths = {path for _, path, _ in ops}
    for must in ("/api/projects", "/api/projects/{code}", "/api/project-options", "/api/customers", "/api/tenants",
                 "/api/projects/{code}/materials/imports", "/api/projects/{code}/chat", "/login"):
        assert must in paths


def test_every_refusal_of_an_api_route_is_listed_with_its_text(doc):
    for _, path, op in contract.operations(doc):
        if not path.startswith("/api/"):
            continue
        signed_out = op["responses"]["401"]["content"]["application/json"]["examples"]
        assert [e["value"] for e in signed_out.values()] == [{"error": contract.SIGNED_OUT}]
        for status, resp in op["responses"].items():
            for body in resp.get("content", {}).values():
                if int(status) >= 400:
                    assert body["examples"], "%s %s has no example" % (path, status)


def test_each_check_can_fail(doc):
    broken = copy.deepcopy(doc)
    _first_example(broken, "/api/projects", "get", "200")["projects"][0]["open_items"] = "twelve"
    assert any("open_items: not of type" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    broken["paths"]["/api/projects/{code}"]["get"]["parameters"] = []
    assert any("path parameters do not match" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    del broken["components"]["schemas"]["Budget"]
    assert any("unresolved reference" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    _body(broken, "/api/tenants", "get", "503")["schema"] = {"type": "object"}
    assert any("without the Error schema" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    del broken["paths"]["/api/tenants"]["get"]["x-awb-state"]
    assert any("no x-awb-state" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    _first_example(broken, "/api/projects/{code}/chat", "get", "200")["budget"]["spare"] = 1
    assert any("spare is not in the schema" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    del _first_example(broken, "/api/projects/{code}", "get", "200")["documents"]
    assert any("documents is missing" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    _first_example(broken, "/api/project-operations/{request_id}", "get", "200")["status"] = "done"
    assert any("not one of the listed values" in p for p in contract.check(broken))

    broken = copy.deepcopy(doc)
    broken["paths"]["/api/customers"]["post"]["operationId"] = "listCustomers"
    assert any("used twice" in p for p in contract.check(broken))


def test_the_kinds_follow_the_backend(doc):
    kind = doc["components"]["schemas"]["Kind"]
    assert kind["x-awb-reads-as"] == projects.LEGACY_KINDS
    assert set(kind["enum"]) == set(projects.PROJECT_KINDS) | set(projects.LEGACY_KINDS)
    options = _body(doc, "/api/project-options", "get", "200")["examples"]
    assert options["repository"]["value"]["kinds"] == list(projects.PROJECT_KINDS)
    assert options["deployed"]["value"]["kinds"] == ["engagement", "lab", "topic", "code"]


def test_the_chat_limits_follow_ask(doc):
    question = doc["components"]["schemas"]["ChatRequest"]["properties"]["question"]
    assert question["maxLength"] == ask.MAX_QUESTION
    budget = _first_example(doc, "/api/projects/{code}/chat", "get", "200")["budget"]
    assert (budget["daily_questions"], budget["daily_tokens"]) == (ask.DAILY_QUESTIONS, ask.DAILY_TOKENS)
    model = _first_example(doc, "/api/projects/{code}/chat", "get", "200")["model"]
    assert model == ask.MODEL


def test_the_codes_follow_the_issued_alphabet(doc):
    schemas = doc["components"]["schemas"]
    for _ in range(50):
        assert re.search(schemas["ProjectCode"]["pattern"], codes.new_project_code("tcp"))
        assert re.search(schemas["CustomerCode"]["pattern"], codes.new_code("CUST"))
    assert not re.search(schemas["ProjectCode"]["pattern"], "tcp-q7m1")
    assert not re.search(schemas["CustomerCode"]["pattern"], "CUST-Q7M1")


def test_the_writer_quotes_what_yaml_would_read_otherwise():
    text = contract.to_yaml({"a": "yes", "b": "2026-10-02", "c": "", "d": "x: y", "e": "#x", "f": "200",
                             "g": "plain words", "h": None, "i": True, "j": 3, "k": "two\nlines\n", "l": [],
                             "m": {}, "n": "<name>", "o": " lead", "p": ["one", {"q": 1, "r": [2]}]})
    lines = text.splitlines()
    for quoted in ('a: "yes"', 'b: "2026-10-02"', 'c: ""', 'd: "x: y"', 'e: "#x"', 'f: "200"', '"n": "<name>"',
                   'o: " lead"'):
        assert quoted in lines
    for plain in ("g: plain words", "h: null", "i: true", "j: 3", "k: |", "  two", "  lines", "l: []", "m: {}",
                  "p:", "  - one", "  - q: 1", "    r:", "      - 2"):
        assert plain in lines


def test_a_text_that_a_literal_block_would_change_is_quoted():
    for value in ("trailing space \nx", " leading\nx", "two\n\n", "tab\tx\ny"):
        line = contract.to_yaml({"v": value}).splitlines()[0]
        assert line.startswith('v: "'), "a literal block would not read back the same"


def test_the_command_writes_and_checks(tmp_path):
    out = tmp_path / "openapi.yaml"
    assert awb(["api", "write", "--out", str(out)]) == 0
    assert awb(["api", "check", "--file", str(out)]) == 0
    out.write_text(out.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    assert awb(["api", "check", "--file", str(out)]) == 1
    assert awb(["api", "check", "--file", str(tmp_path / "missing.yaml")]) == 1
    assert awb(["api"]) == 2


# --- every text the web backend can send is in the contract -------------------------------------------------

# texts the moved code holds that a browser behind the gateway never receives, with the reason
UNREACHABLE = {
    "Not allowed.": "a peer check of the sockets: only the gateway's user reaches them",
    "Invalid request length.": "the gateway checked the length and sends one Content-Length",
    "Use JSON.": "the gateway checked the content type and forwards JSON",
    "Request is too large.": "the gateway's own 413 comes first and is listed",
    "Incomplete request.": "the gateway's own 400 comes first and is listed",
    "No such project endpoint.": "a path of the projects backend the gateway never forwards",
    "Projects are read only.": "the gateway answers a write to these paths itself",
    "No such endpoint.": "a path of the chat backend the gateway never forwards",
    "Submit questions from the AWB site.": "the gateway checked the origin and forwards its own",
    "A single content length is required.": "the gateway's own 400 comes first and is listed",
    "The request is too large.": "the gateway's own 413 comes first and is listed",
    "Unsupported request format.": "the gateway's own 415 comes first and is listed",
    "Inputs belong to a project conversation.": "only a general question could carry inputs, and none sends any",
    "Invalid working copy request.": "the owner's internal publish route, never forwarded by the gateway",
    "Working copy is too large.": "the owner's internal publish route, never forwarded by the gateway",
    "Incomplete working copy.": "the owner's internal publish route, never forwarded by the gateway",
    "Invalid working copy.": "the owner's internal publish route, never forwarded by the gateway",
    "Invalid working copy digest or size.": "the owner's internal publish route, never forwarded by the gateway",
    "A single IAM domain could not be identified.": "replaced by The full IAM domain name is unavailable.",
    "The IAM domain name could not be displayed.": "replaced by The full IAM domain name is unavailable.",
}
ERROR_CALLS = {"Problem", "ChatError", "Unavailable", "InventoryError", "login_page", "reply", "fail", "update",
               "append"}


def _pattern(node, names=None) -> str:
    """A regular expression for the text an expression builds; '.+' for a part only known at run time. `names` maps
    the module's constants to their text, so a refusal that names a constant is read like one that spells it."""
    import ast
    names = names or {}
    if isinstance(node, ast.Name) and node.id in names:
        return re.escape(names[node.id].strip())
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        value = node.value.decode() if isinstance(node.value, bytes) else node.value
        if value.startswith('{"error":'):
            value = json.loads(value)["error"]
        return re.escape(value.strip())
    if isinstance(node, ast.JoinedStr):
        return "".join(re.escape(v.value) if isinstance(v, ast.Constant) else ".+" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pattern(node.left, names) + _pattern(node.right, names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant):
        return re.sub(r"%[sd]", ".+", re.escape(node.left.value).replace(r"\%", "%"))
    if isinstance(node, ast.IfExp):
        return "(?:%s|%s)" % (_pattern(node.body, names), _pattern(node.orelse, names))
    return ".+"


def _texts_of_the_code() -> dict[str, str]:
    """{pattern: file} for every sentence the moved web modules send as an error, a refusal or a state message."""
    import ast
    found: dict[str, str] = {}
    for path in sorted((contract.REPO / "awb" / "tcp" / "web").glob("*.py")):
        if path.name in ("__init__.py", "contract.py"):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {}
        for node in tree.body:     # module constants that hold a sentence, NAME = '...' or A = B = '...'
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = node.value.value
        for node in ast.walk(tree):
            parts = []
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in ERROR_CALLS:
                    parts += list(node.args) + [k.value for k in node.keywords if k.arg in ("error", "message")]
            if isinstance(node, ast.Dict):
                parts += [v for k, v in zip(node.keys, node.values)
                          if isinstance(k, ast.Constant) and k.value in ("error", "message")]
            for part in parts:
                if isinstance(part, ast.Dict):
                    continue
                p = _pattern(part, names)
                literal = re.sub(r"\\(.)", r"\1", p.replace(".+", " "))
                if len(literal) >= 8 and " " in literal.strip() and literal[:1].isupper():
                    found[p] = path.name
        # messages written by SQL statements
        for m in re.finditer(r"(?:message|error)='([^']+)'", source):
            found[re.escape(m.group(1))] = path.name
    return found


def _texts_of_the_contract(doc) -> list[str]:
    out = []

    def walk(node, inside=False):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, inside or key in ("examples", "x-awb-texts"))
        elif isinstance(node, list):
            for value in node:
                walk(value, inside)
        elif isinstance(node, str) and inside:
            out.append(node)
    walk(doc)
    return out


def _missing(doc) -> list[str]:
    texts = _texts_of_the_contract(doc)
    return sorted("%s: %s" % (f, p) for p, f in _texts_of_the_code().items()
                  if not any(re.search(p, t) for t in texts) and not any(re.fullmatch(p, u) for u in UNREACHABLE))


def test_every_text_of_the_web_backend_is_in_the_contract(doc):
    assert len(_texts_of_the_code()) > 100, "the reader of the code finds too little: it is broken"
    missing = _missing(doc)
    assert not missing, "texts the contract does not list:\n" + "\n".join(missing)


def test_every_unreachable_text_is_still_in_the_code():
    code = _texts_of_the_code()
    stale = [u for u in UNREACHABLE if not any(re.fullmatch(p, u) for p in code)]
    assert stale == [], "remove from UNREACHABLE: %s" % stale


def test_the_text_check_can_fail(doc):
    broken = copy.deepcopy(doc)
    _body(broken, "/api/tenants", "get", "503")["examples"] = {"other": {"value": {"error": "Something else."}}}
    assert _missing(broken) == ["projects_api.py: " + re.escape("The tenant inventory is unavailable.")]
    broken = copy.deepcopy(doc)
    broken["components"]["schemas"]["ChatTurn"]["properties"]["error"]["x-awb-texts"].remove(
        "The model declined this question.")
    assert _missing(broken) == ["chat_service.py: " + re.escape("The model declined this question.")]
    # a refusal that names a module constant is read too: drop that constant's text from the whole contract
    from awb.tcp.web import materials_api
    assert _missing(_without(doc, materials_api.KEYS_LOCKED)) == ["materials_api.py: " +
                                                                   re.escape(materials_api.KEYS_LOCKED)]


def _without(doc, text):
    """A copy of the contract with every example and listed text that holds `text` taken out."""
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()
                    if not (isinstance(v, dict) and text in json.dumps(v.get("value", ""), ensure_ascii=False))}
        if isinstance(node, list):
            return [walk(v) for v in node if not (isinstance(v, str) and text in v)]
        return node
    return walk(copy.deepcopy(doc))
