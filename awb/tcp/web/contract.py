"""The contract of the web API: docs/api/openapi.yaml is written from this module and checked against it.

The web console talks to one gateway. Behind it the project, tenant, creation, materials and chat backends answer
under /api/. This module describes those routes as deployed on 2026-10-02, in OpenAPI 3.1: every field, every
status, every error text and a safe example of each answer. It is the only source of the file: `awb api write`
writes it, `awb api check` and tests/test_web_contract.py refuse a file that drifted from this module, an example
that does not fit its own schema, an unresolved reference or an operation without its state.

Every operation carries x-awb-state: deployed runs in production as written, repository exists in the repository
only. The examples carry codes and invented values only: no name, no secret and no id the gate would refuse (a
request id or a hash repeats one character).

Standard library only. The YAML writer covers this document: block mappings and sequences, plain scalars only where
YAML 1.1 and 1.2 both read them as the same string, double quotes everywhere else and literal blocks for texts of
several lines.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONTRACT_FILE = REPO / "docs" / "api" / "openapi.yaml"
VERSION = "0.1.0"
DEPLOYED = "2026-10-02"
STATES = ("deployed", "repository")
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

PROJECT_CODE = r"^tcp-[a-z2-7]{4}$"
CUSTOMER_CODE = r"^CUST-[A-Z2-7]{4}$"
REQUEST_ID = r"^[a-f0-9]{32}$"
MATERIAL_ID = r"^M-[A-Z]{24}$"
SOURCE_ID = r"^[a-f0-9]{64}$"
TENANT_KINDS = ["ecs", "evs", "eip", "vpc", "ims"]
DEPLOYED_KINDS = ["engagement", "lab", "topic", "code"]
REPOSITORY_KINDS = ["query", "project"]
READS_AS = {"engagement": "query", "topic": "query", "code": "query", "lab": "project"}
PORTAL_PAGES = ["/", "/kb", "/price", "/projects", "/reviews", "/tenants", "/health", "/portal", "/ask"]

# Invented sample values. An id or a hash repeats one character, so the gate never reads it as a real one.
REQ = "f" * 32
ETAG = "a" * 32
SOURCE = "e" * 64
SOURCE2 = "d" * 64
MAT = "M-ABCDEFGHIJKLMNOPQRSTUVWX"
MAT2 = "M-BCDEFGHIJKLMNOPQRSTUVWXY"
SNAPSHOT = dt.datetime(2026, 10, 4, 9, 25, 3, tzinfo=dt.timezone.utc)

# Error texts used by more than one route, exactly as the deployed backends send them.
SIGNED_OUT = "Sign in to open the project data."
LOCKED_REGISTER = "The customer register is locked or unavailable. Retry this submission when it is unlocked."
NOT_FINISHED = "The operation could not finish. Retry the same submission to check its result."
OTHER_SUBMISSION = "This request already belongs to another submission. Reopen the form."
INPUT_DOWN = "The input service is unavailable. Check the bucket connection and vault."
NO_PROJECT = "Project not found."
CUSTOMER_RETIRED = "The customer is unavailable or retired."
REOPEN = "Reopen this project before importing files."
CUSTOMER_CHANGED = "The project customer changed. This input cannot be used in its new context."
NOT_READY = "The selected input is not ready in this project."
KEYS_LOCKED = "The key service is locked: the owner runs awb keys unlock."
KEYS_DOWN = "The key service is unavailable. Try again later."
PROJECT_READ_FAILED = (
    "The data check is locked. Project content was not returned.",
    "The data check is unavailable. Project content was not returned.",
    "The registered project folder cannot be opened safely.",
    "The registered project folder is unavailable.",
    "Project data could not be read. Try again shortly.",
)
CHECK_LOCKED = "The data check is locked. Nothing was sent."
CHECK_DOWN = "The data check is unavailable. Nothing was sent."
USAGE_DOWN = "The usage record is unavailable. No new questions can be sent."
NAME_FOUND = "The text contains a name, address or protected value. Use project and customer codes."
BUSY = "Another answer is being prepared. Try again when it finishes."
BUDGET_SPENT = "The daily Ask budget is used up. It resets at 00:00 UTC."
PROJECT_GONE = "This project is unavailable."
CONTEXT_FAILED = "Project context could not be loaded. Nothing was sent."
SNAPSHOT_TOO_LARGE = "The project snapshot is too large."
COPY_FAILED = (
    "The imported working copy is unavailable.",
    "The imported working copy changed. Import it again before use.",
    "The working copy contains protected values. Owner review is required.",
)
SCOPE_NOTE = ("Counts cover resources visible to the connected account in the configured regions. "
              "They are not filtered by creator.")
COVERAGE_NOTE = ("Coverage: ECS servers, EVS disks, elastic IPs, VPCs and private images. "
                 "Other services are not included.")
LISTING_ERRORS = [
    "The read connection is unavailable.",
    "Read access unavailable (HTTP <status>).",
    "Read access unavailable.",
    "The service returned no readable inventory.",
    "The service returned an incomplete inventory.",
    "Inventory pagination repeated a resource.",
    "The inventory is incomplete.",
    "The inventory exceeds the supported page limit.",
]
GATEWAY_LINES = ["Invalid host.", "Invalid request target.", "Not found.", "Method not allowed."]
IMPORT_MESSAGES = [
    "Waiting for processing.",
    "Downloading and checking the selected version.",
    "Adding the checked working copy to the project.",
    "Checking publication of the saved working copy.",
    "Ready text copy. Embedded images are not included.",
    "The same content is already imported as <id>.",
    "Unrecognised names require owner review in the intake report. Register or approve them, then retry.",
    "The original could not be sealed. Owner review is required before release.",
    "The document could not be read completely. Review it or provide a supported text version.",
    "The document could not be read completely. Provide a supported text version.",
    "No usable text was extracted from this file.",
    "This task description may contain private information. Use the customer source or ask the owner to review it.",
    "This file contains no readable text.",
    "Extracted text is too large. Split this document into smaller files.",
    "The working copy contains protected values. Owner review is required.",
    "The staged document changed. Owner review is required.",
    "The filename is unsupported.",
    "The file exceeds the import limit.",
    "The source changed. Refresh the file list and import its current version.",
    "The project customer changed. Import again with the correct customer.",
    "Import stopped. Check the connection and vault, then retry. Source files were preserved.",
    "Processing was interrupted. Retry the import; source files were preserved.",
    "Processing timed out or stopped. Retry with a smaller supported document.",
    "The working copy failed its final data check.",
    "The project is unavailable or its customer changed.",
    "The registered project folder is unsupported.",
    "An input with this identifier already exists with different content.",
    "The local response exceeds the size limit.",
    "The AWB bucket connection is not configured.",
    "Bucket read access is unavailable (HTTP <status>).",
    "The bucket could not be read. Try again later.",
    "Project not found.",
    "Reopen this project before importing files.",
    "The customer is unavailable or retired.",
    KEYS_LOCKED,
    KEYS_DOWN,
    "Ready text copy from the project folder.",
]
TURN_FAILURES = [
    "Not enough daily token budget remains for this answer.",
    "The model returned no usable usage record. The token allowance remains reserved.",
    "The model declined this question.",
    "The model returned an empty answer.",
    "The model requested an unsupported tool.",
    "The model returned no usable tool request.",
    "The answer reached its tool limit. Ask a narrower question.",
    "The answer could not be completed. It will not be resent automatically.",
    "The service restarted before completion. This request will not be resent automatically.",
    CHECK_LOCKED,
    CHECK_DOWN,
    NAME_FOUND,
    USAGE_DOWN,
]

DESCRIPTION = "\n".join((
    "The HTTP interface of the Architect Workbench web console. The console page and every route below are served",
    "by one gateway. It checks the session and the request, then hands the request to the backend that owns the",
    "route.",
    "",
    "State: this document describes what is deployed, the gateway and the backends of 2026-10-02. Production is",
    "frozen while the backend moves into the repository. Every operation carries x-awb-state: deployed runs in",
    "production as written here, repository exists in the repository only and is not deployed yet.",
    "",
    "One change waits in the repository: the project kinds. The deployed backend knows engagement, lab, topic and",
    "code. The repository knows query and project and reads the old words as one of them (schema Kind).",
    "GET /api/project-options always serves the kinds of the running backend.",
    "",
    "Planned, not in this version: the same routes under /api/v1/ with the old paths kept until the interface has",
    "switched, and GET /api/v1/version with the backend commit and the time of the deploy.",
    "",
    "Rules for every route:",
    "- Customers, projects and tenants appear as codes (CUST-Q7M4, tcp-q7m4, test-1). Only /api/customers carries",
    "  registered names, for the owner's own console.",
    "- A backend answers JSON. A refusal is {\"error\": \"<one sentence>\"}, at times with field, terminal, code or",
    "  matches. A refusal of the gateway itself is one line of text/html.",
    "- A write carries request_id, 32 lower-case hex characters made by the browser for one submission. The same",
    "  request_id with the same content returns the first result, so a retry after a broken connection is safe.",
    "  The same request_id with other content is refused with 409.",
    "- A POST needs the session cookie, the site's own Origin, Sec-Fetch-Site same-origin or none, one",
    "  Content-Length of at most 16384 bytes and the content type of its route.",
    "- Times are ISO 8601 in UTC unless a field says otherwise. HEAD works wherever GET does.",
    "- Without a session every /api/ route answers 401 and a page answers 303 to /login.",
    "- Any path: a wrong host gets 400 Invalid host., a target that is not a path 400 Invalid request target.,",
    "  an unknown path 404 Not found. and a method other than GET, HEAD or POST 405 Method not allowed., each as",
    "  one line of text/html (info.x-awb-texts).",
    "",
    "The gateway also serves the older server-rendered pages /kb, /price, /projects, /reviews, /tenants, /health",
    "and /portal. They are HTML, the console does not call them and they are not part of this contract.",
))


# ---------------------------------------------------------------------------------------------------- builders

def _typed(kind: str, description: str, nullable: bool, extra: dict) -> dict:
    s: dict = {"type": [kind, "null"] if nullable else kind}
    if description:
        s["description"] = description
    s.update(extra)
    return s


def string(description: str = "", nullable: bool = False, **extra) -> dict:
    return _typed("string", description, nullable, extra)


def integer(description: str = "", nullable: bool = False, **extra) -> dict:
    return _typed("integer", description, nullable, extra)


def number(description: str = "", nullable: bool = False, **extra) -> dict:
    return _typed("number", description, nullable, extra)


def boolean(description: str = "") -> dict:
    return _typed("boolean", description, False, {})


def array(items: dict, description: str = "", **extra) -> dict:
    s: dict = {"type": "array"}
    if description:
        s["description"] = description
    s["items"] = items
    s.update(extra)
    return s


def obj(properties: dict, required: list | None = None, description: str = "", closed: bool = False) -> dict:
    """An object. Every property is required unless `required` names fewer. A request is closed: the backend
    refuses fields it does not know. An answer stays open, so a new field is an additive change."""
    s: dict = {"type": "object"}
    if description:
        s["description"] = description
    s["properties"] = properties
    req = list(properties) if required is None else list(required)
    if req:
        s["required"] = req
    if closed:
        s["additionalProperties"] = False
    return s


def ref(name: str) -> dict:
    return {"$ref": "#/components/schemas/" + name}


def _slug(text: str, taken: dict) -> str:
    base = "-".join(re.findall(r"[a-z0-9]+", text.lower())[:5]) or "example"
    slug, n = base, 2
    while slug in taken:
        slug, n = "%s-%d" % (base, n), n + 1
    return slug


def _examples(values: dict) -> dict:
    return {name: {"value": value} for name, value in values.items()}


def answer(description: str, schema: dict, examples: dict, media: str = "application/json", headers=None) -> dict:
    r: dict = {"description": description}
    if headers:
        r["headers"] = headers
    r["content"] = {media: {"schema": schema, "examples": _examples(examples)}}
    return r


def refusal(description: str, *messages) -> dict:
    """A refusal of a backend: {"error": ...}. One example per message, a str or a dict with the extra fields."""
    values: dict = {}
    for m in messages:
        value = {"error": m} if isinstance(m, str) else dict(m)
        values[_slug(value["error"], values)] = value
    return answer(description, ref("Error"), values)


def gateway(description: str, *lines: str) -> dict:
    """A refusal of the gateway itself: one line of text, sent as text/html."""
    values: dict = {}
    for line in lines:
        values[_slug(line, values)] = line + "\n"
    return answer(description, {"type": "string"}, values, media="text/html")


def redirect(description: str, location: str, cookie: str = "") -> dict:
    headers = {"Location": {"description": location, "schema": {"type": "string"}}}
    if cookie:
        headers["Set-Cookie"] = {"description": cookie, "schema": {"type": "string"}}
    return {"description": description, "headers": headers}


def merge(*maps: dict) -> dict:
    """Responses by status from several sources. One status keeps every content type and every example."""
    out: dict = {}
    for m in maps:
        for status, resp in m.items():
            if status not in out:
                out[status] = copy.deepcopy(resp)
                continue
            have = out[status]
            if resp["description"] not in have["description"]:
                have["description"] += " " + resp["description"]
            for media, body in resp.get("content", {}).items():
                content = have.setdefault("content", {})
                if media in content:
                    known = content[media]["examples"]
                    for name, ex in body["examples"].items():
                        if all(other["value"] != ex["value"] for other in known.values()):
                            known[_slug(name, known)] = copy.deepcopy(ex)
                else:
                    content[media] = copy.deepcopy(body)
    return out


API_COMMON = {
    "401": refusal("No valid session. Sign in first.", SIGNED_OUT),
    "502": gateway("The backend did not answer, or its answer was too large.",
                   "The AWB service is temporarily unavailable.", "The response was too large."),
}
POST_COMMON = {
    "400": gateway("The gateway refused the request.", "A single content length is required.", "Incomplete request."),
    "403": gateway("The gateway refused a request from another site.",
                   "Open Ask on the AWB site to submit a question.", "Cross-site requests are not allowed."),
    "408": gateway("The body did not arrive in time.", "Request timed out."),
    "413": gateway("The body is larger than 16384 bytes.", "Request is too large."),
    "415": gateway("The body has the wrong content type.", "Use the Ask form."),
}
PAGE_SIGNED_OUT = {"303": redirect("No valid session.", "/login?next=<the page, or / for any other path>")}


def operation(op_id: str, tag: str, summary: str, responses: dict, *, description: str = "",
              parameters: list | None = None, body: dict | None = None, api: bool = True, post: bool = False,
              security: list | None = None, state: str = "deployed") -> dict:
    op: dict = {"operationId": op_id, "tags": [tag], "summary": summary}
    if description:
        op["description"] = description
    if parameters:
        op["parameters"] = parameters
    if body is not None:
        op["requestBody"] = body
    resp = merge(responses, API_COMMON if api else {}, POST_COMMON if post else {})
    op["responses"] = dict(sorted(resp.items()))
    if security is not None:
        op["security"] = security
    op["x-awb-state"] = state
    return op


def json_body(schema: str, examples: dict, description: str = "") -> dict:
    b: dict = {"required": True}
    if description:
        b["description"] = description
    b["content"] = {"application/json": {"schema": ref(schema), "examples": _examples(examples)}}
    return b


def form_body(schema: dict, examples: dict) -> dict:
    return {"required": True,
            "content": {"application/x-www-form-urlencoded": {"schema": schema, "examples": _examples(examples)}}}


def path_param(name: str, description: str, pattern: str) -> dict:
    return {"name": name, "in": "path", "required": True, "description": description,
            "schema": {"type": "string", "pattern": pattern}}


CODE = path_param("code", "The project code: tcp- and four characters of a to z and 2 to 7.", PROJECT_CODE)
REQUEST = path_param("request_id", "The request_id of the submission.", REQUEST_ID)
MATERIAL = path_param("material_id", "An import of this project: M- and 24 capital letters.", MATERIAL_ID)


# ---------------------------------------------------------------------------------------------------- examples

P_QUERY = {"code": "tcp-k2wd", "kind": "query", "customer": "none", "state": "active", "created": "2026-10-01",
           "goal": "Compare the object storage classes of TCP for archive data", "goal_redacted": False,
           "open_items": 2, "live_resources": 0, "updated": "2026-10-03T16:40:12.511204+00:00",
           "status": {"summary": "The prices of the three storage classes are fetched.",
                      "next": "Compare the retrieval fees.", "updated": "2026-10-03T16:40:12.511204+00:00",
                      "behind": 0, "redacted": False}}
P_LAB = {"code": "tcp-m3fx", "kind": "lab", "customer": "none", "state": "active", "created": "2026-09-30",
         "goal": "Test a virtual firewall appliance on a test tenant", "goal_redacted": False,
         "open_items": 5, "live_resources": 3, "updated": "2026-10-02T11:05:47.902114+00:00",
         "status": {"summary": "The appliance image is imported; its boot waits on a disk controller.",
                    "next": "Repeat the boot test once the controller is offered.",
                    "updated": "2026-10-01T20:58:03.120411+00:00", "behind": 3, "redacted": False}}
P_CUSTOMER = {"code": "tcp-q7m4", "kind": "engagement", "customer": "CUST-Q7M4", "state": "closed",
              "created": "2026-09-22", "goal": "Plan the move of the customer's file servers to TCP",
              "goal_redacted": False, "open_items": 0, "live_resources": None, "updated": None,
              "status": {"summary": None, "next": None, "updated": None, "behind": None, "redacted": False}}
P_DETAIL = dict(P_QUERY, documents={
    "SCOPE.md": {"text": "# Scope\n\n- goal: Compare the object storage classes of TCP for archive data\n",
                 "status": "available", "redacted": False, "modified": "2026-10-01T09:02:31.104577+00:00"},
    "STATE.md": {"text": "# State\n\nStatus: The prices of the three storage classes are fetched.\nNext: Compare the "
                         "retrieval fees.\n", "status": "available",
                 "redacted": False, "modified": "2026-10-03T16:40:12.511204+00:00"},
    "OPEN.md": {"text": "# Open\n\n- confirm the retrieval fee\n- check the minimum storage time\n",
                "status": "available", "redacted": False, "modified": "2026-10-03T16:31:55.020931+00:00"},
    "RESOURCES.md": {"text": "", "status": "missing", "redacted": False, "modified": None},
}, deliverables={"status": "available", "truncated": False, "items": [
    {"file": "deliverables/storage-classes.md", "state": "valid", "tier": 1, "redacted": False}]},
    fetched_at="2026-10-04T09:30:00.120034+00:00")
TAGS = ["ecs", "obs", "vpc"]
BUDGET = {"questions_left": 31, "tokens_left": 254880, "daily_questions": 40, "daily_tokens": 300000,
          "resets": "00:00 UTC"}
TURN = {"id": REQ, "project": "tcp-k2wd", "question": "Which storage class fits data that is read once a year?",
        "answer": "The cold class fits: it has the lowest storage price, and reading data back costs a retrieval fee.",
        "status": "complete", "error": "", "created": "2026-10-04T09:31:02.551203+00:00",
        "completed": "2026-10-04T09:31:09.004417+00:00", "tokens": 5120, "tools": "[\"kb_find\", \"price_find\"]",
        "context_time": "2026-10-04 09:31 UTC", "material_ids": "[]", "material_sources": "[]"}
PENDING = dict(TURN, answer="", status="pending", completed=None, tokens=0, tools="[]",
               material_ids="[\"%s\"]" % MAT,
               material_sources="[{\"id\": \"%s\", \"file\": \"%s.md\", \"version\": 1, "
                                "\"imported\": \"2026-10-03T08:15\", \"truncated\": false}]" % (MAT, MAT))
RESOURCES = [
    {"handle": "ecs-1a2b3c4d", "kind": "ecs", "region": "eu-de", "state": "ACTIVE", "size": "s3.large.2",
     "amount": None, "project": "tcp-m3fx", "project_source": "tag", "expiry": "2026-10-31",
     "created": "2026-09-30T10:12:44+00:00"},
    {"handle": "evs-5e6f7a8b", "kind": "evs", "region": "eu-de", "state": "in-use", "size": "40 GiB", "amount": 40,
     "project": "tcp-m3fx", "project_source": "tag", "expiry": "2026-10-31", "created": "2026-09-30T10:12:40+00:00"},
    {"handle": "vpc-9c0d1e2f", "kind": "vpc", "region": "eu-de", "state": "OK", "size": "", "amount": None,
     "project": "", "project_source": None, "expiry": "", "created": None},
]
INVENTORY = {
    "tenants": [{
        "alias": "test-1", "regions": ["eu-de"], "domain_name": "sample-domain", "domain_status": "available",
        "scope": "accessible", "scope_label": "All resources visible to the connected account",
        "ownership_verified": False, "collected_at": "2026-10-04T09:25:03+00:00",
        "coverage": [{"region": "eu-de", "kind": "ecs", "status": "complete", "count": 1},
                     {"region": "eu-de", "kind": "evs", "status": "complete", "count": 1},
                     {"region": "eu-de", "kind": "eip", "status": "complete", "count": 0},
                     {"region": "eu-de", "kind": "vpc", "status": "complete", "count": 1},
                     {"region": "eu-de", "kind": "ims", "status": "unavailable", "count": None,
                      "error": "Read access unavailable (HTTP 403)."}],
        "resources": RESOURCES, "errors": [],
        "counts": {"ecs": 1, "evs": 1, "eip": 0, "vpc": 1, "ims": None, "running": 1, "stopped": 0,
                   "attached_disks": 1, "disk_gib": 40},
        "status": "partial"}],
    "collected_at": "2026-10-04T09:25:03+00:00", "timestamp": SNAPSHOT.timestamp(), "refreshing": False,
    "stale": False, "error": "", "registered_count": 1, "refresh_interval_seconds": 300,
    "scope_note": SCOPE_NOTE, "coverage_note": COVERAGE_NOTE,
}
IMPORT_READY = {"id": MAT, "name": "requirements.pdf", "source": "customer", "state": "ready",
                "message": "Ready text copy. Embedded images are not included.",
                "created": "2026-10-03T08:15:00+00:00", "version": 1, "filename": MAT + ".md", "size": 182344}
IMPORT_HELD = {"id": MAT2, "name": "network.xlsx", "source": "customer", "state": "held",
               "message": IMPORT_MESSAGES[6], "created": "2026-10-03T08:16:21+00:00", "version": 1,
               "filename": MAT2 + ".md", "size": 52211}
FILES = [
    {"id": SOURCE, "name": "requirements.pdf", "etag": ETAG, "size": 182344, "modified": "2026-10-03T07:58:12.000Z",
     "status": "imported", "supported": True, "location": "Project folder"},
    {"id": SOURCE2, "name": "diagram.vsdx", "etag": "b" * 32, "size": 52211, "modified": "2026-10-03T07:59:40.000Z",
     "status": "new", "supported": False, "location": "Unassigned inbox"},
]


# ---------------------------------------------------------------------------------------------------- schemas

def _schemas() -> dict:
    summary = {
        "code": ref("ProjectCode"),
        "kind": ref("Kind"),
        "customer": ref("CustomerOrNone"),
        "state": string("Deleted projects are not listed.", enum=["active", "closed"]),
        "created": string("The day the project was created.", format="date"),
        "goal": string("The goal line of SCOPE.md after the data check. A withheld part reads [withheld]; a goal "
                       "the check still refuses reads [Content withheld by the data check.]"),
        "goal_redacted": boolean("true when the data check withheld a part of the goal."),
        "open_items": integer("Lines of OPEN.md that start with a dash; null when OPEN.md cannot be read.",
                              nullable=True),
        "live_resources": integer("Rows of RESOURCES.md whose state is neither deleted nor kept; null when "
                                  "RESOURCES.md cannot be read.", nullable=True),
        "updated": string("The latest change of SCOPE.md, STATE.md, OPEN.md or RESOURCES.md; null when none can "
                          "be read.", nullable=True, format="date-time"),
        "status": ref("ProjectStatus"),
    }
    return {
        "Error": obj({
            "error": string("One sentence for the person at the screen. It carries no name and no submitted "
                            "value."),
            "field": string("The form field the refusal is about."),
            "terminal": boolean("true: this request_id will never succeed. Reopen the form, which makes a new "
                                "request_id."),
            "code": string("The code the stuck submission reserved."),
            "matches": array(ref("CustomerCode"), "Existing customers with the same name."),
        }, required=["error"], description="A refusal of a backend."),
        "ProjectCode": string("A project code.", pattern=PROJECT_CODE),
        "CustomerCode": string("A customer code.", pattern=CUSTOMER_CODE),
        "CustomerOrNone": string("A customer code, or none for an internal project.",
                                 pattern=r"^(none|CUST-[A-Z2-7]{4})$"),
        "RequestId": string("Made by the browser for one submission: 32 lower-case hex characters. A retry sends "
                            "the same one.", pattern=REQUEST_ID),
        "MaterialId": string("An import: M- and 24 capital letters.", pattern=MATERIAL_ID),
        "Kind": {
            "type": "string",
            "description": "The kind of a project as stored. A query works with the knowledge base, the docs and "
                           "live prices; a project also reaches a test tenant. The repository writes query and "
                           "project only, older projects keep their word and it reads as x-awb-reads-as says. "
                           "The deployed backend still writes the four old words.",
            "enum": REPOSITORY_KINDS + DEPLOYED_KINDS,
            "x-awb-reads-as": dict(READS_AS),
        },
        "ProjectStatus": obj({
            "summary": string("The Status: line of STATE.md after the data check, one sentence; null when STATE.md "
                              "has none yet.", nullable=True),
            "next": string("The Next: line of STATE.md after the data check, one sentence; null when STATE.md has "
                           "none yet.", nullable=True),
            "updated": string("When STATE.md last changed; null when it cannot be read.", nullable=True,
                              format="date-time"),
            "behind": integer("Commits of the project newer than STATE.md. More than 0: the recorded status lags "
                              "behind the work. null when the history cannot be read.", nullable=True),
            "redacted": boolean("true when the data check withheld a part of a line."),
        }, description="The recorded status of a project: the two lines STATE.md opens with. The working session "
                       "updates them after every step that changes the status; its stop hook holds a reply while "
                       "commits are newer than STATE.md."),
        "ProjectSummary": obj(dict(summary), description="One project of the list."),
        "ProjectList": obj({
            "source": string(enum=["AWB project register"]),
            "fetched_at": string(format="date-time"),
            "projects": array(ref("ProjectSummary"), "Latest change first."),
        }),
        "ProjectDocument": obj({
            "text": string("The file after the data check; empty when it is missing or cannot be read."),
            "status": string(enum=["available", "missing", "unavailable"]),
            "redacted": boolean("true when the data check withheld a part."),
            "modified": string(nullable=True, format="date-time"),
        }),
        "Deliverables": obj({
            "status": string("unavailable when the folders cannot be read safely.",
                             enum=["available", "unavailable"]),
            "items": array(obj({
                "file": string("deliverables/<name> after the data check."),
                "state": string(enum=["valid", "stale", "missing", "unreadable"]),
                "tier": integer("The tier of the review record; null without a record.", nullable=True),
                "redacted": boolean(),
            }), "At most 300."),
            "truncated": boolean("true when there were more than 300. Absent when status is unavailable."),
        }, required=["status", "items"]),
        "ProjectDetail": obj(dict(summary, **{
            "documents": obj({name: ref("ProjectDocument")
                              for name in ("SCOPE.md", "STATE.md", "OPEN.md", "RESOURCES.md")}),
            "deliverables": ref("Deliverables"),
            "fetched_at": string(format="date-time"),
        }), description="One project with its four files and its deliverables."),
        "ProjectOptions": obj({
            "kinds": array(string(), "The kinds the running backend accepts, in the order to show. Deployed: "
                                     "engagement, lab, topic, code. Repository: query, project."),
            "tags": array(string(), "The technology tags a project may carry."),
        }),
        "ProjectCreateRequest": obj({
            "request_id": ref("RequestId"),
            "kind": string("One of the kinds of GET /api/project-options."),
            "goal": string("What the project is for, without customer names: 1 to 500 characters after trimming.",
                           minLength=1, maxLength=500),
            "customer": ref("CustomerOrNone"),
            "tags": array(string(), "Tags of GET /api/project-options. Default: none.", maxItems=20),
        }, required=["request_id", "kind", "goal"], closed=True),
        "CreationResult": obj({
            "code": string("The new code.", pattern=r"^(tcp-[a-z2-7]{4}|CUST-[A-Z2-7]{4})$"),
            "created": boolean("Always true."),
            "warning": string("The work is done but a side step is not. Show it to the person."),
        }, required=["code", "created"]),
        "OperationStatus": obj({
            "status": string("unknown: no submission with this request_id. pending: reserved, not finished. "
                             "complete: done, the fields of the creation result follow.",
                             enum=["unknown", "pending", "complete"]),
            "code": string("The reserved or created code."),
            "created": boolean(),
            "warning": string(),
        }, required=["status"]),
        "Customer": obj({
            "code": ref("CustomerCode"),
            "name": string("The first registered form: a name, for the owner only."),
            "aliases": array(string(), "The other active forms."),
            "active": boolean("false when every form is retired."),
        }),
        "CustomerList": obj({"customers": array(ref("Customer"), "Ordered by code.")}),
        "CustomerCreateRequest": obj({
            "request_id": ref("RequestId"),
            "name": string("2 to 200 characters on one line, without brackets, pipes, hashes or notes.",
                           minLength=2, maxLength=200),
            "aliases": array(string(minLength=2, maxLength=200), "Other written forms.", maxItems=10),
        }, required=["request_id", "name"], closed=True),
        "Coverage": obj({
            "region": string(),
            "kind": string(enum=TENANT_KINDS),
            "status": string(enum=["complete", "unavailable"]),
            "count": integer(nullable=True),
            "error": {**string("Why the listing failed."), "x-awb-texts": list(LISTING_ERRORS)},
        }, required=["region", "kind", "status", "count"]),
        "Resource": obj({
            "handle": string("A stable handle: the kind and eight hex characters, never the cloud id.",
                             pattern=r"^(ecs|evs|eip|vpc|ims)-[0-9a-f]{8}$"),
            "kind": string(enum=TENANT_KINDS),
            "region": string(),
            "state": string("The status the service reports, or Unknown."),
            "size": string("The flavor of a server, GiB of a disk, Mbit/s of an elastic IP or the minimum disk of "
                           "an image; empty when unknown."),
            "amount": number("The number behind size; null when there is none.", nullable=True),
            "project": string("The project from the awb-project tag or else from the name; empty when none."),
            "project_source": string(nullable=True, enum=["tag", "name", None]),
            "expiry": string("The awb-expiry tag, YYYY-MM-DD; empty when none."),
            "created": string(nullable=True, format="date-time"),
        }),
        "Counts": obj({**{k: integer(nullable=True) for k in TENANT_KINDS + ["running", "stopped", "attached_disks"]},
                       "disk_gib": number(nullable=True)},
                      description="Counted over complete listings only; null when a listing of that kind is "
                                  "incomplete."),
        "Tenant": obj({
            "alias": string("The tenant alias, for example test-1."),
            "regions": array(string()),
            "domain_name": string("The IAM domain name of the account; null when it cannot be read.",
                                  nullable=True),
            "domain_status": string(enum=["available", "unavailable"]),
            "scope": string(enum=["accessible"]),
            "scope_label": string(),
            "ownership_verified": boolean("Always false: the counts are not filtered by creator."),
            "collected_at": string(nullable=True, format="date-time"),
            "coverage": array(ref("Coverage")),
            "resources": array(ref("Resource")),
            "errors": {**array(string()), "x-awb-texts": ["The full IAM domain name is unavailable."]},
            "counts": ref("Counts"),
            "status": string(enum=["complete", "partial"]),
        }),
        "TenantInventory": obj({
            "tenants": array(ref("Tenant"), "Only the tenants that are still registered."),
            "collected_at": string(nullable=True, format="date-time"),
            "timestamp": number("Seconds since 1970 of the snapshot; absent before the first one."),
            "refreshing": boolean(),
            "stale": boolean("true when the snapshot is older than refresh_interval_seconds."),
            "error": {**string("Why the last refresh failed; empty when it did not."),
                      "x-awb-texts": ["The inventory could not be refreshed. The previous snapshot is shown when "
                                      "available."]},
            "registered_count": integer(),
            "refresh_interval_seconds": integer(),
            "scope_note": string(),
            "coverage_note": string(),
        }, required=["tenants", "collected_at", "refreshing", "stale", "error", "registered_count",
                     "refresh_interval_seconds", "scope_note", "coverage_note"]),
        "MaterialImport": obj({
            "id": ref("MaterialId"),
            "name": string("The file name in the bucket."),
            "source": string("brief: the lab inbox. customer: the owner's bucket, through the intake.",
                             enum=["brief", "customer"]),
            "state": string("queued, processing and publishing run. ready: the working copy is in input/. "
                            "duplicate: the same content as an earlier import. held: the owner has to look. "
                            "failed: stopped, a new import may be tried.",
                            enum=["queued", "processing", "publishing", "ready", "duplicate", "held", "failed"]),
            "message": {**string("One sentence on the state."), "x-awb-texts": list(IMPORT_MESSAGES)},
            "created": string(format="date-time"),
            "version": integer("The version of this source file in the project."),
            "filename": string("The working copy in the project: <id>.md."),
            "size": integer("Bytes of the source file."),
        }),
        "MaterialHistory": obj({
            "items": array(ref("MaterialImport"), "Latest first, at most 500."),
            "busy": boolean("true while an import runs."),
        }),
        "SourceFile": obj({
            "id": string("Stable for this project, source and file.", pattern=SOURCE_ID),
            "name": string(),
            "etag": string("The version of the file in the bucket."),
            "size": integer(),
            "modified": string("As the bucket reports it."),
            "status": string("new: never imported. updated: imported before, changed since. imported: this "
                             "version is ready or a duplicate. processing: this version is being imported.",
                             enum=["new", "updated", "imported", "processing"]),
            "supported": boolean("false for an unknown type, an empty file or one above the limit."),
            "location": string(enum=["Unassigned inbox", "Project folder"]),
        }),
        "MaterialSources": obj({
            "source": string(enum=["brief", "customer"]),
            "files": array(ref("SourceFile")),
            "limit_mb": integer("The largest file in MB."),
        }),
        "ImportRequest": obj({
            "request_id": ref("RequestId"),
            "files": array(obj({
                "id": string(pattern=SOURCE_ID),
                "etag": string("The etag of the listing. A file that changed since is refused."),
            }, closed=True), "1 to 10 files of the last listing, each once.", minItems=1, maxItems=10),
        }, closed=True),
        "ImportAccepted": obj({
            "imports": array(ref("MaterialId"), "One id per file, in the order sent. A version imported before "
                                                "keeps its id."),
        }),
        "MaterialText": obj({
            "id": ref("MaterialId"),
            "version": integer(),
            "filename": string(),
            "imported": string(format="date-time"),
            "text": string("The checked working copy, at most 256 KiB."),
        }),
        "ChatTurn": obj({
            "id": ref("RequestId"),
            "project": ref("ProjectCode"),
            "question": string("After the data check. [Text withheld by the data check.] when a later check "
                               "withholds it."),
            "answer": string("Empty while pending and after a failure."),
            "status": string(enum=["pending", "complete", "failed"]),
            "error": {**string("Why it failed; empty otherwise. A failed turn is never resent by itself."),
                      "x-awb-texts": list(TURN_FAILURES)},
            "created": string(format="date-time"),
            "completed": string(nullable=True, format="date-time"),
            "tokens": integer("Tokens this answer used."),
            "tools": string("A JSON array as text: the sources the answer used (kb_find, price_find, tenant_now and, "
                            "with selected inputs, input_find)."),
            "context_time": string("When the project snapshot was taken, YYYY-MM-DD HH:MM UTC.", nullable=True),
            "material_ids": string("A JSON array as text: the inputs the question selected."),
            "material_sources": string("A JSON array as text: id, file, version, imported and truncated of each "
                                       "input, and for a long input the places of the passages sent, such as "
                                       "\"3, 7 of 12\"."),
        }),
        "Budget": obj({
            "questions_left": integer(),
            "tokens_left": integer(),
            "daily_questions": integer(),
            "daily_tokens": integer(),
            "resets": string(enum=["00:00 UTC"]),
        }),
        "ChatHistory": obj({
            "project": ref("ProjectCode"),
            "turns": array(ref("ChatTurn"), "Oldest first, the last 100."),
            "older_turns": integer("Turns before the first one shown."),
            "budget": ref("Budget"),
            "model": string("The model that answers."),
        }),
        "ChatRequest": obj({
            "question": string("1 to 1000 characters after trimming, checked for names before it leaves.",
                               minLength=1, maxLength=1000),
            "request_id": ref("RequestId"),
            "material_ids": array(ref("MaterialId"), "Up to five ready inputs of this project, each once.",
                                  maxItems=5),
        }, required=["question", "request_id"], closed=True),
        "ChatAccepted": obj({"turn": ref("ChatTurn"), "budget": ref("Budget")}),
    }


# ---------------------------------------------------------------------------------------------------- paths

def _session_paths() -> dict:
    page = {"type": "string"}
    sign_in = "<!doctype html><html lang=\"en\"><head><title>Sign in · AWB</title></head><body>" \
              "<form action=\"/login\" method=\"post\">...</form></body></html>"
    form = obj({
        "csrf": string("The hidden field of the sign-in page; it must equal the __Host-awb-login cookie and is "
                       "valid for 600 seconds."),
        "username": string(maxLength=100),
        "password": string(maxLength=200),
        "next": string("Where to go after the sign-in.", enum=PORTAL_PAGES),
    }, required=["csrf", "username", "password"], closed=True)
    return {
        "/login": {
            "get": operation("signInPage", "session", "The sign-in page.", {
                "200": answer("The form. Sets the __Host-awb-login cookie for 600 seconds.", page,
                              {"page": sign_in}, media="text/html"),
                "303": redirect("Already signed in.", "The next page."),
                "400": gateway("The query cannot be read.", "Invalid query."),
            }, parameters=[{"name": "next", "in": "query", "required": False,
                            "description": "A page to open after the sign-in. Anything else becomes /.",
                            "schema": {"type": "string"}}], api=False, security=[]),
            "post": operation("signIn", "session", "Sign in with the username and the password.", {
                "303": redirect("Signed in.", "The next page.",
                                "__Host-awb-session: valid for 8 hours, Secure, HttpOnly, SameSite=Lax."),
                "200": answer("The username or the password is wrong: the form again, with the reason.", page,
                              {"page": sign_in.replace("<form", "<p role=\"alert\" class=\"error\">The username "
                                                       "or password is incorrect.</p><form")}, media="text/html"),
                "403": merge(
                    {"403": answer("The form expired or came from another site: the form again, with the reason.",
                                   page, {"page": sign_in.replace("<form", "<p role=\"alert\" class=\"error\">The "
                                                                  "sign-in form expired. Please try again.</p><form")},
                                   media="text/html")},
                    {"403": gateway("The form was not sent from the site.", "Open this form on the AWB site.")},
                )["403"],
                "429": answer("Eight failed attempts within a minute.", page,
                              {"page": sign_in.replace("<form", "<p role=\"alert\" class=\"error\">Too many "
                                                       "attempts. Try again in one minute.</p><form")},
                              media="text/html"),
                "400": gateway("The form cannot be read.", "A single content length is required.", "Invalid form."),
                "413": gateway("The form is larger than 16384 bytes.", "Request is too large."),
                "415": gateway("The body is not a form.", "Use the sign-in form."),
            }, body=form_body(form, {"sign-in": {"csrf": "<the hidden field>", "username": "<username>",
                                                 "password": "<password>", "next": "/"}}),
                api=False, security=[]),
        },
        "/logout": {
            "get": operation("signOutPage", "session", "A page that asks before signing out.", {
                "200": answer("The page with one button.", page,
                              {"page": "<html><head><title>Sign out - AWB</title></head><body><h1>Sign out of AWB?"
                                       "</h1><form method=\"post\" action=\"/logout\"><button>Sign out</button>"
                                       "</form></body></html>"}, media="text/html"),
                **PAGE_SIGNED_OUT,
            }, api=False),
            "post": operation("signOut", "session", "End the session.", {
                "303": redirect("Signed out.", "/login", "__Host-awb-session cleared."),
                "403": gateway("Not sent from the site, or no session.", "Open this form on the AWB site.",
                               "Sign in before submitting a question."),
                "400": gateway("The form cannot be read.", "A single content length is required.", "Invalid form."),
                "413": gateway("The form is larger than 16384 bytes.", "Request is too large."),
                "415": gateway("The body is not a form.", "Use the sign-in form."),
            }, body=form_body(obj({}, closed=True), {"empty": {}}), api=False),
        },
    }


def _page_paths() -> dict:
    page = {"type": "string"}
    console = ("<!doctype html><html lang=\"en\"><head><title>Architect Workbench</title></head><body>...</body>"
               "</html>")
    ask_page = "<!doctype html><html lang=\"en\"><head><title>Ask · AWB</title></head><body>...</body></html>"

    def console_op(op_id: str) -> dict:
        return operation(op_id, "pages", "The console: one HTML page with its own script.", {
            "200": answer("The page, with a Content-Security-Policy that allows its inline scripts only.", page,
                          {"page": console}, media="text/html"),
            "503": gateway("The page cannot be read.", "The workspace is temporarily unavailable."),
            **PAGE_SIGNED_OUT,
        }, api=False)

    return {
        "/": {"get": console_op("getConsole")},
        "/architect-workbench.html": {"get": console_op("getConsoleByName")},
        "/ask": {
            "get": operation("askPage", "pages", "The Ask page: one general question, answered only from the "
                             "checked sources.", {
                                 "200": answer("The page.", page, {"page": ask_page}, media="text/html"),
                                 "502": API_COMMON["502"],
                                 **PAGE_SIGNED_OUT,
                             }, api=False),
            "post": operation("askGeneral", "pages", "Send one general question from the Ask page.", {
                "200": answer("The page with the answer and its sources, or with the reason it failed.", page,
                              {"page": ask_page}, media="text/html"),
                "400": refusal("The question cannot be used.", "Write a question of 1 to 1000 characters.",
                               "The request could not be read."),
                "403": gateway("No session.", "Sign in before submitting a question."),
                "415": gateway("The body is not a form.", "Use the Ask form."),
                "422": refusal("The question holds a name or a protected value.", NAME_FOUND),
                "429": refusal("One answer at a time, and a daily budget.", BUSY, BUDGET_SPENT),
                "503": refusal("A check or the usage record is unavailable.", CHECK_LOCKED, CHECK_DOWN, USAGE_DOWN,
                               "The question could not be accepted. Check the conversation before trying again."),
                "502": API_COMMON["502"],
            }, body=form_body(obj({"q": string("The question.", minLength=1, maxLength=1000)}, closed=True),
                              {"question": {"q": "Which flavors does the region eu-de offer for SAP HANA?"}}),
                api=False, post=True),
        },
    }


def _project_paths() -> dict:
    read_failed = refusal("The data check or a project folder is unavailable. Nothing was returned.",
                          *PROJECT_READ_FAILED)
    created = {"code": "tcp-k2wd", "created": True}
    return {
        "/api/projects": {
            "get": operation("listProjects", "projects", "The registered projects that are not deleted.", {
                "200": answer("The list.", ref("ProjectList"), {"three-projects": {
                    "source": "AWB project register", "fetched_at": "2026-10-04T09:30:00.120034+00:00",
                    "projects": [P_QUERY, P_LAB, P_CUSTOMER]}}),
                "503": read_failed,
            }),
            "post": operation("createProject", "projects", "Create a project.", {
                "201": answer("Created, or the result of the first submission with this request_id.",
                              ref("CreationResult"), {
                                  "created": created,
                                  "recovered": dict(created, warning="Project recovered after an interrupted "
                                                    "request. Its first activity entry may be missing."),
                                  "no-activity-entry": dict(created, warning="Project created. The first activity "
                                                            "entry could not be written."),
                              }),
                "400": refusal("The content is refused. The project check of the backend may also refuse with a "
                               "sentence of its own.",
                               "Unsupported form fields.", "A valid request ID is required.",
                               "Choose a project type and enter a goal of up to 500 characters.",
                               "Select a valid customer or Internal project.", "Select tags from the list.",
                               {"error": "Select a customer for customer work.", "field": "customer"},
                               {"error": "This customer is unavailable or retired. Select an active customer.",
                                "field": "customer"},
                               "Invalid request."),
                "409": refusal("The request_id belongs to another submission, or a reserved project needs the "
                               "owner's look.",
                               {"error": OTHER_SUBMISSION, "terminal": True},
                               {"error": "The reserved project needs inspection.", "terminal": True,
                                "code": "tcp-k2wd"},
                               {"error": "Creation was interrupted. The reserved folder needs inspection before "
                                         "retrying.", "terminal": True, "code": "tcp-k2wd"}),
                "503": refusal("A register or the creation service is unavailable. Retry with the same request_id.",
                               "The customer register is unavailable. Try again later.", LOCKED_REGISTER,
                               NOT_FINISHED),
            }, description="Creates the project folder with its files and registers it. No AI call and no cloud "
                           "resource. The engagement rule (a customer is required) exists in the deployed backend "
                           "only.",
                body=json_body("ProjectCreateRequest", {
                    "internal-query": {"request_id": REQ, "kind": "query",
                                       "goal": "Compare the object storage classes of TCP for archive data",
                                       "customer": "none", "tags": ["obs"]},
                    "customer-work": {"request_id": REQ, "kind": "engagement",
                                      "goal": "Plan the move of the customer's file servers to TCP",
                                      "customer": "CUST-Q7M4", "tags": []},
                }), post=True),
        },
        "/api/projects/{code}": {
            "get": operation("getProject", "projects", "One project with its files and deliverables.", {
                "200": answer("The project.", ref("ProjectDetail"), {"query": P_DETAIL}),
                "404": refusal("No such project.", "This project is not registered or has been deleted."),
                "503": read_failed,
            }, parameters=[CODE]),
        },
        "/api/project-options": {
            "get": operation("getProjectOptions", "projects", "What the create form offers.", {
                "200": answer("The kinds and the tags of the running backend.", ref("ProjectOptions"), {
                    "deployed": {"kinds": list(DEPLOYED_KINDS), "tags": list(TAGS)},
                    "repository": {"kinds": list(REPOSITORY_KINDS), "tags": list(TAGS)},
                }),
                "503": refusal("The creation service could not answer.", NOT_FINISHED),
            }),
        },
        "/api/project-operations/{request_id}": {
            "get": operation("getProjectOperation", "projects", "The state of a project submission, for a retry "
                             "after a broken connection.", {
                                 "200": answer("The state.", ref("OperationStatus"), {
                                     "unknown": {"status": "unknown"},
                                     "pending": {"status": "pending", "code": "tcp-k2wd"},
                                     "complete": {"status": "complete", "code": "tcp-k2wd", "created": True},
                                 }),
                                 "503": refusal("The creation service could not answer.", NOT_FINISHED),
                             }, parameters=[REQUEST]),
        },
    }


def _customer_paths() -> dict:
    return {
        "/api/customers": {
            "get": operation("listCustomers", "customers", "The customer register for the owner's console.", {
                "200": answer("Every customer code with its registered forms.", ref("CustomerList"), {
                    "one-customer": {"customers": [{"code": "CUST-Q7M4", "name": "<registered name>",
                                                    "aliases": ["<short form>"], "active": True}]}}),
                "503": refusal("The register cannot be read.", LOCKED_REGISTER, NOT_FINISHED),
            }, description="The only route that carries registered names. It answers the signed-in owner only. "
                           "A name read here never goes into a project, a chat, a task or a log."),
            "post": operation("createCustomer", "customers", "Register a new customer.", {
                "201": answer("Registered, or the result of the first submission with this request_id.",
                              ref("CreationResult"), {"created": {"code": "CUST-Q7M4", "created": True}}),
                "400": refusal("The content is refused.", "Unsupported form fields.", "A valid request ID is required.",
                               "Enter a customer name and at most 10 alternative names.",
                               "Names must contain 2 to 200 characters on one line.",
                               "Use a name without brackets, pipes, hashes or notes.", "Invalid request."),
                "409": refusal("The request_id belongs to another submission, or the name is taken.",
                               {"error": OTHER_SUBMISSION, "terminal": True},
                               {"error": "This customer was retired. Open the customer register.", "terminal": True,
                                "code": "CUST-Q7M4"},
                               {"error": "The saved customer needs inspection before retrying.", "terminal": True,
                                "code": "CUST-Q7M4"},
                               {"error": "A customer with this name already exists. Select the existing customer.",
                                "matches": ["CUST-Q7M4"]}),
                "503": refusal("The register is unavailable. Retry with the same request_id.",
                               "The customer was saved but is not ready for projects. Retry this submission.",
                               LOCKED_REGISTER, NOT_FINISHED),
            }, description="The name and its other forms go into the owner's register and the customer gets a new "
                           "code. A name that exists already is never merged: the answer names the existing codes.",
                body=json_body("CustomerCreateRequest", {
                    "new-customer": {"request_id": REQ, "name": "<registered name>", "aliases": ["<short form>"]}}),
                post=True),
        },
        "/api/customer-operations/{request_id}": {
            "get": operation("getCustomerOperation", "customers", "The state of a customer submission, for a retry "
                             "after a broken connection.", {
                                 "200": answer("The state.", ref("OperationStatus"), {
                                     "unknown": {"status": "unknown"},
                                     "pending": {"status": "pending", "code": "CUST-Q7M4"},
                                     "complete": {"status": "complete", "code": "CUST-Q7M4", "created": True},
                                 }),
                                 "503": refusal("The register is unavailable.", LOCKED_REGISTER, NOT_FINISHED),
                             }, parameters=[REQUEST]),
        },
    }


def _tenant_paths() -> dict:
    return {
        "/api/tenants": {
            "get": operation("getTenants", "tenants", "What runs on the test tenants, from the last snapshot.", {
                "200": answer("The snapshot.", ref("TenantInventory"), {"one-tenant": INVENTORY}),
                "503": refusal("The inventory cannot be read.", "The tenant inventory is unavailable."),
            }, description="Read only. Servers, disks, elastic IPs, VPCs and private images of each registered test "
                           "tenant in its regions. A snapshot older than 300 seconds starts a refresh in the "
                           "background and is still returned with stale true. In the repository version the "
                           "inventory runs as its own service and system user (awb-console), which alone may read "
                           "the test tenants without a project; sessions still need an active project.",
                parameters=[{"name": "refresh", "in": "query", "required": False,
                             "description": "1 starts a refresh in the background, at most every 30 seconds.",
                             "schema": {"type": "string", "enum": ["1"]}}]),
        },
    }


def _material_paths() -> dict:
    return {
        "/api/projects/{code}/materials": {
            "get": operation("listMaterials", "materials", "The imports of a project.", {
                "200": answer("Latest first.", ref("MaterialHistory"),
                              {"two-imports": {"items": [IMPORT_HELD, IMPORT_READY], "busy": False}}),
                "404": refusal("No such project.", NO_PROJECT),
                "409": refusal("The customer of the project is retired.", CUSTOMER_RETIRED),
                "503": refusal("The input service cannot answer.", INPUT_DOWN),
            }, description="The imports of this service, latest first, then the text copies that reached the "
                           "project's input/ folder another way (the owner's intake on the command line, a take of "
                           "the exchange): ready, version 1, source customer in a project with a customer and brief "
                           "in one without, with a stable id. Both kinds can be read and used in the chat. The copies "
                           "of the project folder come with the repository version.",
                parameters=[CODE]),
        },
        "/api/projects/{code}/materials/sources": {
            "get": operation("listMaterialSources", "materials", "The files of one source, with their versions.", {
                "200": answer("The files.", ref("MaterialSources"),
                              {"customer-source": {"source": "customer", "files": FILES, "limit_mb": 25}}),
                "400": refusal("No source, or an unknown one.", "Choose a source.", "Invalid request."),
                "404": refusal("No such project.", NO_PROJECT),
                "409": refusal("The project cannot import now.", REOPEN,
                               "Select a customer for this project before importing customer material.",
                               CUSTOMER_RETIRED),
                "413": refusal("The source is too large to list.",
                               "This source has too many files. Narrow the source folder first.",
                               "The source has too many files.", "This source exceeds the supported listing limit.",
                               "The customer bucket layout needs a narrower binding.",
                               "The customer bucket layout exceeds the supported limit."),
                "429": refusal("Another listing runs.", "Another source is being listed. Try again shortly."),
                "503": refusal("The bucket cannot be read.", "The AWB bucket connection is not configured.",
                               "Bucket read access is unavailable (HTTP 403).",
                               "The bucket could not be read. Try again later.",
                               "The bucket returned an inconsistent listing.",
                               "The bucket listing could not be completed.", INPUT_DOWN, KEYS_LOCKED, KEYS_DOWN),
            }, description="brief lists the unassigned lab inbox. customer lists the unassigned owner inbox and the "
                           "project's own in/ folders under every month; it needs a project with a customer. In the "
                           "repository version the buckets are read through the owner's key service, so this service "
                           "holds no key; its two refusals about the key service come with that version.",
                parameters=[CODE, {"name": "source", "in": "query", "required": True,
                                   "description": "brief or customer.",
                                   "schema": {"type": "string", "enum": ["brief", "customer"]}}]),
        },
        "/api/projects/{code}/materials/imports": {
            "post": operation("importMaterials", "materials", "Import chosen files into the project.", {
                "202": answer("Queued. GET /api/projects/{code}/materials shows the state of each import.",
                              ref("ImportAccepted"), {"queued": {"imports": [MAT]}}),
                "400": refusal("The content is refused.", "Invalid import request.", "Select between 1 and 10 files.",
                               "Select each file once.", "Invalid request."),
                "404": refusal("No such project.", NO_PROJECT),
                "409": refusal("The choice or the project changed.", "This request belongs to another import.",
                               "The source selection changed. Refresh the file list.", "A customer is required.",
                               REOPEN, CUSTOMER_RETIRED),
                "413": refusal("A chosen file is of an unknown type, empty or too large.",
                               "The selected file type or size is unsupported."),
                "429": refusal("Too many imports wait.", "The import queue is full. Wait for current files to finish."),
                "503": refusal("The input service cannot answer.", INPUT_DOWN),
            }, description="Copies the chosen versions; the originals stay in the bucket. Each file is read at the "
                           "version of the listing, so a changed file is refused. It is checked, sanitised through "
                           "the intake when it comes from the customer source and added to the project's input/ as "
                           "<id>.md. The same version imported again keeps its first id. Nothing starts an AI call. In the "
                           "repository version each file is read through the owner's key service at its version.",
                parameters=[CODE],
                body=json_body("ImportRequest", {"one-file": {"request_id": REQ, "files": [{"id": SOURCE,
                                                                                         "etag": ETAG}]}}),
                post=True),
        },
        "/api/projects/{code}/materials/{material_id}": {
            "get": operation("getMaterial", "materials", "The working copy of a ready import.", {
                "200": answer("The checked text.", ref("MaterialText"), {"ready": {
                    "id": MAT, "version": 1, "filename": MAT + ".md", "imported": "2026-10-03T08:15:00+00:00",
                    "text": "# Requirements\n\nThe workloads move to the region eu-de.\n"}}),
                "404": refusal("No such project or no such ready import.", NO_PROJECT, NOT_READY),
                "409": refusal("The customer of the project changed or is retired.", CUSTOMER_CHANGED,
                               CUSTOMER_RETIRED),
                "422": refusal("The working copy no longer passes its checks.", *COPY_FAILED),
                "503": refusal("The input service cannot answer.", INPUT_DOWN),
            }, parameters=[CODE, MATERIAL]),
        },
    }


def _chat_paths() -> dict:
    return {
        "/api/projects/{code}/chat": {
            "get": operation("getChat", "chat", "The conversation of a project, the budget and the model.", {
                "200": answer("The last 100 turns.", ref("ChatHistory"), {"one-turn": {
                    "project": "tcp-k2wd", "turns": [TURN], "older_turns": 0, "budget": dict(BUDGET),
                    "model": "claude-haiku-4-5-20251001"}}),
                "404": refusal("No such project.", "No such project.", PROJECT_GONE),
                "503": refusal("The project, a check or the usage record is unavailable.", PROJECT_GONE,
                               CONTEXT_FAILED, SNAPSHOT_TOO_LARGE, CHECK_LOCKED, CHECK_DOWN, USAGE_DOWN,
                               "Conversation history is unavailable."),
            }, parameters=[CODE]),
            "post": operation("askProject", "chat", "Ask about a project.", {
                "202": answer("Accepted. The answer is prepared in the background: read GET "
                              "/api/projects/{code}/chat until the turn is complete or failed.",
                              ref("ChatAccepted"), {"pending": {"turn": PENDING, "budget": dict(BUDGET,
                                                                                               questions_left=30)}}),
                "200": answer("This request_id was sent before: its turn as it stands.", ref("ChatAccepted"),
                              {"complete": {"turn": TURN, "budget": dict(BUDGET)}}),
                "400": refusal("The content is refused.", "Select up to five ready inputs.",
                               "A valid request ID is required.", "Write a question of 1 to 1000 characters.",
                               "A JSON object is required.", "The request could not be read."),
                "404": refusal("No such project or no such ready input.", "No such project.", PROJECT_GONE,
                               NO_PROJECT, NOT_READY),
                "409": refusal("The request_id belongs to another question, or an input cannot be used.",
                               "This request ID belongs to another question.", CUSTOMER_CHANGED, CUSTOMER_RETIRED),
                "413": refusal("The inputs are too large together.",
                               "These inputs exceed the context limit. Select fewer documents."),
                "422": refusal("The question or an input holds a name or a protected value, or an input no longer "
                               "passes its checks.", NAME_FOUND, *COPY_FAILED),
                "429": refusal("One answer at a time, and a daily budget.", BUSY, BUDGET_SPENT),
                "503": refusal("The project, a check, an input or the usage record is unavailable. Nothing was sent.",
                               "The question could not be accepted. Check the conversation before trying again.",
                               CHECK_LOCKED, CHECK_DOWN, CONTEXT_FAILED, PROJECT_GONE, SNAPSHOT_TOO_LARGE,
                               USAGE_DOWN, "The selected input could not be read. Nothing was sent.", INPUT_DOWN,
                               "The local response exceeds the size limit."),
            }, description="The question is checked for names before anything leaves. The project files and up to "
                           "five ready inputs go along as source data, never as instructions: a long input as the "
                           "passages that match the question best, about 16,000 characters for all inputs together, "
                           "and the model may search the selected inputs with input_find. One answer at a time "
                           "for the whole site; 40 questions and 300,000 tokens a day, reset at 00:00 UTC. A failed "
                           "turn carries its reason in error and is never resent by itself.",
                parameters=[CODE],
                body=json_body("ChatRequest", {
                    "question": {"question": "Which storage class fits data that is read once a year?",
                                 "request_id": REQ},
                    "with-an-input": {"question": "Which requirements in this document affect the network design?",
                                      "request_id": REQ, "material_ids": [MAT]},
                }), post=True),
        },
    }


def build() -> dict:
    """The whole document."""
    paths: dict = {}
    for part in (_session_paths, _page_paths, _project_paths, _customer_paths, _tenant_paths, _material_paths,
                 _chat_paths):
        paths.update(part())
    return {
        "openapi": "3.1.0",
        "info": {"title": "Architect Workbench web API", "version": VERSION, "description": DESCRIPTION,
                 "x-awb-deployed": DEPLOYED, "x-awb-texts": list(GATEWAY_LINES)},
        "servers": [{"url": "/", "description": "The site itself: the console and the API share one origin."}],
        "security": [{"session": []}],
        "tags": [
            {"name": "session", "description": "Sign in and sign out. A session lasts up to 8 hours."},
            {"name": "pages", "description": "The HTML pages the console uses."},
            {"name": "projects", "description": "The project register, read and create."},
            {"name": "customers", "description": "The customer register. Owner only: it carries names."},
            {"name": "tenants", "description": "The test tenants, read only."},
            {"name": "materials", "description": "Inputs from the buckets, copied into a project."},
            {"name": "chat", "description": "Questions about a project, answered from checked sources only."},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {"session": {
                "type": "apiKey", "in": "cookie", "name": "__Host-awb-session",
                "description": "Set by POST /login. Valid for 8 hours, Secure, HttpOnly, SameSite=Lax."}},
            "schemas": _schemas(),
        },
    }


def operations(doc: dict):
    """(method, path, operation) for every operation of the document."""
    for path, item in doc.get("paths", {}).items():
        for method, op in item.items():
            if method in METHODS:
                yield method, path, op


# ---------------------------------------------------------------------------------------------------- checks

def _resolve(doc: dict, schema: dict) -> dict:
    for _ in range(20):
        if not (isinstance(schema, dict) and "$ref" in schema):
            return schema
        target = schema["$ref"]
        name = target[len("#/components/schemas/"):] if target.startswith("#/components/schemas/") else None
        if name is None or name not in doc.get("components", {}).get("schemas", {}):
            raise KeyError(target)
        schema = doc["components"]["schemas"][name]
    raise KeyError("a loop of references")


def _is(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "boolean": bool, "object": dict, "array": list,
                              "null": type(None)}.get(kind, ()))


def validate(doc: dict, value, schema: dict, where: str = "$") -> list[str]:
    """What is wrong with `value` against `schema`. Strict: a property the schema does not list is a finding even
    in an open object, because an example shows only documented fields. Findings name paths, never values."""
    try:
        schema = _resolve(doc, schema)
    except KeyError:
        return ["%s: unresolved reference" % where]
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is(value, k) for k in kinds):
            return ["%s: not of type %s" % (where, " or ".join(kinds))]
    out = []
    if "enum" in schema and not any(value == e and type(value) is type(e) for e in schema["enum"]):
        out.append("%s: not one of the listed values" % where)
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append("%s: does not match the pattern" % where)
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            out.append("%s: length out of range" % where)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            out.append("%s: number of items out of range" % where)
        if "items" in schema:
            for i, item in enumerate(value):
                out += validate(doc, item, schema["items"], "%s[%d]" % (where, i))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        out += ["%s: %s is missing" % (where, name) for name in schema.get("required", []) if name not in value]
        for name, item in value.items():
            if name in props:
                out += validate(doc, item, props[name], "%s.%s" % (where, name))
            else:
                out.append("%s: %s is not in the schema" % (where, name))
    return out


def _refs(node, where: str = "$"):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield where, node["$ref"]
        for key, value in node.items():
            yield from _refs(value, "%s.%s" % (where, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _refs(value, "%s[%d]" % (where, i))


def _check_body(doc: dict, where: str, body: dict) -> list[str]:
    if "schema" not in body:
        return [where + ": no schema"]
    examples = body.get("examples") or {}
    out = [] if examples else [where + ": no example"]
    for name, example in examples.items():
        if "value" not in example:
            out.append("%s example %s: no value" % (where, name))
        else:
            out += validate(doc, example["value"], body["schema"], "%s example %s" % (where, name))
    return out


def check(doc: dict) -> list[str]:
    """Every finding of the document. Empty when it is consistent."""
    out = []
    if doc.get("openapi") != "3.1.0":
        out.append("openapi: not 3.1.0")
    for key in ("title", "version"):
        if not doc.get("info", {}).get(key):
            out.append("info: no %s" % key)
    tags = {t.get("name") for t in doc.get("tags", [])}
    seen: set = set()
    for path, item in doc.get("paths", {}).items():
        if not path.startswith("/"):
            out.append("%s: a path starts with a slash" % path)
        for key in item:
            if key not in METHODS and key != "parameters":
                out.append("%s: unknown key %s" % (path, key))
        template = set(re.findall(r"{([^}]+)}", path))
        for method, op in ((m, o) for m, o in item.items() if m in METHODS):
            where = "%s %s" % (method.upper(), path)
            op_id = op.get("operationId")
            if not op_id:
                out.append(where + ": no operationId")
            elif op_id in seen:
                out.append(where + ": the operationId is used twice")
            seen.add(op_id)
            if not op.get("summary"):
                out.append(where + ": no summary")
            if not op.get("tags") or any(t not in tags for t in op["tags"]):
                out.append(where + ": a tag that is not declared")
            if op.get("x-awb-state") not in STATES:
                out.append(where + ": no x-awb-state (%s)" % " or ".join(STATES))
            params = op.get("parameters", [])
            declared = {p.get("name") for p in params if p.get("in") == "path"}
            if declared != template:
                out.append(where + ": the path parameters do not match the path")
            if any(p.get("in") == "path" and p.get("required") is not True for p in params):
                out.append(where + ": a path parameter that is not required")
            responses = op.get("responses") or {}
            if not responses:
                out.append(where + ": no responses")
            for status, resp in responses.items():
                if not re.fullmatch(r"[1-5]\d\d", str(status)):
                    out.append("%s %s: not a status code" % (where, status))
                    continue
                if not resp.get("description"):
                    out.append("%s %s: no description" % (where, status))
                for media, body in resp.get("content", {}).items():
                    out += _check_body(doc, "%s %s %s" % (where, status, media), body)
                    if media == "application/json" and int(status) >= 400 and body.get("schema") != ref("Error"):
                        out.append("%s %s: a JSON refusal without the Error schema" % (where, status))
            for media, body in (op.get("requestBody") or {}).get("content", {}).items():
                out += _check_body(doc, "%s request %s" % (where, media), body)
    for where, target in _refs(doc):
        try:
            _resolve(doc, {"$ref": target})
        except KeyError:
            out.append("%s: unresolved reference" % where)
    return out


# ---------------------------------------------------------------------------------------------------- YAML

_PLAIN = re.compile(r"[A-Za-z_/][A-Za-z0-9 _./()+,'<>=!?-]*")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}


def _plain(s: str) -> bool:
    return bool(_PLAIN.fullmatch(s)) and s.lower() not in _RESERVED and not s.endswith(" ")


def _scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return value if _plain(value) else json.dumps(value, ensure_ascii=False)
    raise TypeError("no YAML form for %s" % type(value).__name__)


def _literal(s: str) -> list[str] | None:
    """The lines of a literal block for `s`, or None when a block would not read back the same."""
    if "\n" not in s or s.endswith("\n\n") or s.startswith((" ", "\n")) or "\t" in s or "\r" in s:
        return None
    lines = s[:-1].split("\n") if s.endswith("\n") else s.split("\n")
    if any(line != line.rstrip() or any(ord(c) < 32 for c in line) for line in lines):
        return None
    return lines


def _entry(head: str, value, indent: int, lines: list[str]) -> None:
    if isinstance(value, dict) and value:
        lines.append(head)
        _mapping(value, indent + 2, lines)
    elif isinstance(value, list) and value:
        lines.append(head)
        _sequence(value, indent + 2, lines)
    elif isinstance(value, (dict, list)):
        lines.append(head + (" {}" if isinstance(value, dict) else " []"))
    elif isinstance(value, str) and _literal(value) is not None:
        lines.append(head + (" |" if value.endswith("\n") else " |-"))
        lines.extend((" " * (indent + 2) + line) if line else "" for line in _literal(value))
    else:
        lines.append(head + " " + _scalar(value))


def _key(key: str) -> str:
    return key if _plain(key) else json.dumps(key, ensure_ascii=False)


def _mapping(d: dict, indent: int, lines: list[str]) -> None:
    for key, value in d.items():
        _entry(" " * indent + _key(key) + ":", value, indent, lines)


def _sequence(items: list, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    for value in items:
        if isinstance(value, dict) and value:
            for n, (key, item) in enumerate(value.items()):
                _entry((pad + "- " if n == 0 else pad + "  ") + _key(key) + ":", item, indent + 2, lines)
        elif isinstance(value, list) and value:
            lines.append(pad + "-")
            _sequence(value, indent + 2, lines)
        else:
            _entry(pad + "-", value, indent, lines)


def to_yaml(doc: dict) -> str:
    lines: list[str] = []
    _mapping(doc, 0, lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- command

def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb api", description="The contract of the web API: docs/api/openapi.yaml.")
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("write", help="write docs/api/openapi.yaml from awb/tcp/web/contract.py")
    s.add_argument("--out", type=Path, default=CONTRACT_FILE)
    s = sub.add_parser("check", help="check the contract and that the file was written from it")
    s.add_argument("--file", type=Path, default=CONTRACT_FILE)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command not in ("write", "check"):
        ap.print_usage(sys.stderr)
        return 2
    doc = build()
    problems = check(doc)
    count = sum(1 for _ in operations(doc))
    if args.command == "write":
        if not problems:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(to_yaml(doc), encoding="utf-8")
            print("awb api: wrote %s, %d operations" % (args.out.name, count))
    else:
        try:
            if args.file.read_text(encoding="utf-8") != to_yaml(doc):
                problems.append("the file differs from awb/tcp/web/contract.py: run awb api write")
        except OSError:
            problems.append("the file cannot be read")
        if not problems:
            print("awb api: the contract is consistent, %d operations" % count)
    for problem in problems:
        print("awb api: " + problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
