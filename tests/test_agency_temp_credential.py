"""Ask 2026-11-27-tcp-bj3o-1: a lab call with the temporary key of an agency (`labclient.call(..., credential=...)`)
answered 200 on IAM but 401 VPC.0008 on GET /v1/{project_id}/vpcs, also with the project id written in the path.

The stand-in gateway here takes the lab login and the admin token of tests/test_lr_lab_runs.py and adds what an
agency key needs: POST /v3.0/OS-CREDENTIAL/securitytokens with the method assume_role hands out a temporary key of
the delegating account, GET /v3/auth/projects signed with that key lists its projects, and a VPC call signed with it
answers only when the signature holds, X-Security-Token is sent and signed and X-Project-Id names the project of the
path (the public API docs: X-Security-Token for a temporary key, X-Project-Id for AK/SK calls of a project). Every
key, token and id here is invented.
"""
from __future__ import annotations

import hmac
import hashlib
import json

import pytest

from awb import sign
from tests.test_lr_lab_runs import FakeLab, _long, lab_call, run_op, session, svc  # noqa: F401
from tests.test_keys import calls_logged_here, no_waiting, short_dir  # noqa: F401

P_AGENCY = "b" * 32
"""The eu-de project of the delegating account; the lab login's own project is another one (P_DE)."""


class AgencyHandlerMixin:
    """SDK-HMAC calls of a temporary key of an agency, before the FakeLab handler sees a call."""

    agency: dict = {}
    seen: list = []

    def _maybe_agency(self, method) -> bool:
        path, _, query = self.path.partition("?")
        auth = self.headers.get("Authorization") or ""
        if method == "POST" and path == "/v3.0/OS-CREDENTIAL/securitytokens":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {}
            if (body.get("auth") or {}).get("identity", {}).get("methods") != ["assume_role"]:
                self._replay = raw          # the FakeLab handler reads the body again
                return False
            ak, sk, st = "AGAK" + "7" * 16, _long("agsk", 36), _long("gA", 300)
            type(self).agency[ak] = (sk, st)
            self._send(201, {"credential": {"access": ak, "secret": sk, "securitytoken": st,
                                            "expires_at": "2099-01-01T00:00:00.000000Z"}})
            return True
        if not auth.startswith(sign.ALGORITHM + " "):
            return False
        fields = dict(p.strip().split("=", 1) for p in auth[len(sign.ALGORITHM) + 1:].split(","))
        ak, names = fields.get("Access", ""), fields.get("SignedHeaders", "").split(";")
        sk, st = type(self).agency.get(ak, ("", ""))
        headers = {n: self.headers.get(n) or "" for n in names}
        sts, _ = sign.string_to_sign(method, "http://%s%s" % (self.headers.get("Host"), self.path), headers, b"",
                                     self.headers.get("X-Sdk-Date") or "")
        good = sk and hmac.compare_digest(fields.get("Signature", ""), hmac.new(
            sk.encode(), sts.encode(), hashlib.sha256).hexdigest())
        token_ok = "x-security-token" in names and self.headers.get("X-Security-Token") == st
        type(self).seen.append({"path": path, "project": self.headers.get("X-Project-Id"), "signed": names,
                                "token": token_ok})
        if not (good and token_ok):
            self._send(401, {"code": "VPC.0008", "message": "Invalid token in the header."})
            return True
        if path == "/v3/auth/projects":
            self._send(200, {"projects": [{"id": P_AGENCY, "name": "eu-de"}, {"id": "c" * 32, "name": "eu-nl"}]})
            return True
        segs = path.strip("/").split("/")
        if len(segs) >= 3 and segs[0] == "v1" and segs[2] == "vpcs":
            if segs[1] != P_AGENCY or self.headers.get("X-Project-Id") != P_AGENCY:
                self._send(401, {"code": "VPC.0008", "message": "Invalid token in the header."})
                return True
            self._send(200, {"vpcs": [{"id": "f" * 32, "name": "agency-vpc"}]})
            return True
        self._send(404, {"error_msg": "not found"})
        return True


@pytest.fixture
def fake():
    f = FakeLab()
    base = f.server.RequestHandlerClass

    class Handler(AgencyHandlerMixin, base):
        agency: dict = {}
        seen: list = []

        def _any(self, method):
            self._replay = None
            if self._maybe_agency(method):
                return
            if self._replay is not None:
                import io

                self.rfile = io.BytesIO(self._replay)
            base._any(self, method)

    f.server.RequestHandlerClass = Handler
    f.agency_handler = Handler
    with f:
        yield f


def _agency_key(svc, uid, run) -> str:
    body = {"auth": {"identity": {"methods": ["assume_role"],
                                  "assume_role": {"agency_name": "lab-agency", "domain_id": "9" * 32}}}}
    got = lab_call(svc, uid, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", run, body=body)
    assert got["ok"] and got["status"] == 201, got
    return got["data"]["credential"]["access"]


def test_agency_key_reads_its_projects_on_iam(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    access = _agency_key(svc, uid, run)
    got = lab_call(svc, uid, "GET", "iam", "/v3/auth/projects", run, credential=access)
    assert got["ok"] and got["status"] == 200
    assert fake.agency_handler.seen[-1]["token"] is True


def test_agency_key_reads_vpcs_with_the_project_placeholder(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    access = _agency_key(svc, uid, run)
    got = lab_call(svc, uid, "GET", "vpc", "/v1/{project_id}/vpcs", run, credential=access)
    assert got["ok"] and got["status"] == 200, got
    assert got["data"]["vpcs"][0]["name"] == "agency-vpc"
    last = fake.agency_handler.seen[-1]
    assert last["path"] == "/v1/%s/vpcs" % P_AGENCY and last["project"] == P_AGENCY
    assert last["token"] is True and "x-security-token" in last["signed"] and "x-project-id" in last["signed"]


def test_agency_key_reads_vpcs_with_the_project_id_in_the_path(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    access = _agency_key(svc, uid, run)
    got = lab_call(svc, uid, "GET", "vpc", "/v1/%s/vpcs" % P_AGENCY, run, credential=access)
    assert got["ok"] and got["status"] == 200, got
    last = fake.agency_handler.seen[-1]
    assert last["project"] == P_AGENCY and "x-project-id" in last["signed"] and last["token"] is True


def test_agency_key_with_a_project_scope_id(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    access = _agency_key(svc, uid, run)
    got = lab_call(svc, uid, "GET", "vpc", "/v1/{project_id}/vpcs", run, credential=access, scope=P_AGENCY)
    assert got["ok"] and got["status"] == 200, got
    assert fake.agency_handler.seen[-1]["project"] == P_AGENCY
