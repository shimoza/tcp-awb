"""UX21: status-file decisions and the owner page's real JavaScript, without npm dependencies."""
import json
import re
import subprocess
from pathlib import Path
from email.message import Message
from types import SimpleNamespace
import time

import pytest

from awb import config, ledger, ownerwait, projects
from awb.tcp import owner
from awb.tcp.web import contract, gateway, owner_actions
from tests import fixtures as fx
from tests.test_web_owner_api import OWNER, status_doc
from tests.test_owner_take_default import Body, Site, DOMAIN

PAGE = Path(__file__).resolve().parents[1] / "awb/tcp/web/owner.html"
ROUTE = "/api/owner/items"


@pytest.fixture
def decisions(home, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    pr = projects.spawn(home, "project", "check the cluster", fx.CUSTOMER_CODE, home.register)
    conf = tmp_path / "paths.conf"
    conf.write_text("work_host = awb-test\nprojects = %s\n" % home.projects_root)
    monkeypatch.setenv("AWB_CONF", str(conf))
    text = ["(tooling) fix the runner", "owner: review ecs-1. Default: keep ecs-1 stopped.",
            "owner: review ecs-2.", "owner: " + "wait for capacity " * 10 + "Default: keep ecs-3 stopped."]
    text += ["owner: decide ecs-%d." % n for n in range(4, 8)]
    (Path(pr.path) / "OPEN.md").write_text("## Waiting on the owner\n" +
                                         "".join("- %s\n" % t for t in text))
    return home, pr


def test_written_status_keeps_checked_items_original_indexes_and_app(decisions, monkeypatch, tmp_path):
    home, pr = decisions
    for name, value in (("daemons", {"vault": status_doc()["vault"], "keys": status_doc()["keys"]}),
                        ("deployed", status_doc()["deployed"]), ("page", status_doc()["page"]),
                        ("ui_runs", [])):
        monkeypatch.setattr(owner, name, lambda *a, _value=value, **k: _value)
    data = owner.collect(home)
    assert [r["item"] for r in data["decisions"]] == [1, 2, 3, 4, 5]
    rows = data["decisions"]
    assert [r["has_default"] for r in rows[:3]] == [True, False, True]
    shown, _ = ownerwait.collect(home)
    assert [r["text"] for r in rows] == [it.text for it in shown if it.kind == "owner"]
    assert all(r["code"] == pr.code for r in rows)
    assert all(r["app"] == "app: SSH host awb-test, folder %s" % pr.path for r in rows)
    path = tmp_path / "status.json"
    owner.write(data, path, checker=lambda text: "passed")
    assert owner_actions.read_status(path) == data
    fx.assert_no_fixture_name(path.read_text())


def test_names_and_unavailable_checks_never_offer_a_default(decisions, monkeypatch):
    home, pr = decisions
    (Path(pr.path) / "OPEN.md").write_text("## Waiting on the owner\n- owner: " +
                                         fx.PERSON_FORMS[0] + ". Default: keep ecs-1 stopped.\n")
    rows = ownerwait.page_items(home)
    assert rows and rows[0]["text"] == ledger.WITHHELD and not rows[0]["has_default"]
    fx.assert_no_fixture_name(json.dumps(rows))
    def unavailable(*args):
        raise ledger.LedgerError("unavailable")
    monkeypatch.setattr(ledger, "hit_indexes", unavailable)
    assert ownerwait.page_items(home) == []


def test_app_line_is_checked_and_never_guessed(decisions, monkeypatch):
    home, pr = decisions
    monkeypatch.setattr(config, "host_conf", lambda: {"work_host": "awb-test", "projects": "/different"})
    assert all(r["app"] == "app: destination unavailable." for r in ownerwait.page_items(home))
    monkeypatch.setattr(config, "host_conf", lambda: {"work_host": fx.CUSTOMER_FORMS[1],
                                                    "projects": str(home.projects_root)})
    rows = ownerwait.page_items(home)
    fx.assert_no_fixture_name(json.dumps(rows))
    assert all(r["app"] == ledger.WITHHELD for r in rows)


def test_get_decisions_uses_only_the_status_file_and_owner_host(home, tmp_path, monkeypatch):
    row = {"code": "tcp-q7m4", "item": 7, "text": "owner: review ecs-1.",
           "has_default": False, "app": "app: SSH host awb-test, folder /work/tcp-q7m4"}
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status_doc(decisions=[row])))
    site = Site(tmp_path, home)
    token = site.auth.new_owner_session("owner1")
    def forbidden(*args, **kwargs):
        pytest.fail("owner-actions tried to read projects")
    monkeypatch.setattr(projects, "load", forbidden)

    def request(host=OWNER, cookie=token):
        answers = []
        handler = object.__new__(gateway.Gateway)
        handler.server, handler.command, handler.path = site.server, "GET", ROUTE
        handler.headers = Message()
        handler.headers["Host"] = host
        handler.headers["Cookie"] = gateway.OWNER_COOKIE + "=" + cookie
        handler.rfile = Body()
        handler.connection = SimpleNamespace(settimeout=lambda seconds: None)
        handler.client_address = ("127.0.0.1", 12345)
        handler.reply = lambda status, body=b"", headers=None: answers.append((status, body))
        def forward(target, route, body, extra=None):
            assert target == ("unix", site.server.owner_socket)
            service = object.__new__(owner_actions.Handler)
            service.server = SimpleNamespace(status=path, clock=time.time)
            service.path, service.command = route, "GET"
            service.headers = {"X-AWB-Level": handler.level}
            service.reply = lambda status, data, word: answers.append((status, json.dumps(data).encode()))
            service.do_GET()
        handler.forward = forward
        handler.dispatch()
        assert len(answers) == 1
        return answers[0]

    status, raw = request()
    assert status == 200
    data = json.loads(raw)
    assert data["items"] == [row] and "written" in data and "stale" in data
    assert contract.validate(contract.build(), data, contract.ref("OwnerItems")) == []
    assert request(cookie="")[0] == 401
    assert request(host=DOMAIN)[0] != 200


@pytest.mark.parametrize("change", [{"item": True}, {"item": -1}, {"code": "../tcp-q7m4"},
                                     {"has_default": "yes"}, {"app": "bad\nline"}, {"extra": "value"}])
def test_malformed_decisions_are_refused_on_write_and_read(tmp_path, change):
    row = {"code": "tcp-q7m4", "item": 0, "text": "owner: review ecs-1.",
           "has_default": False, "app": "app: destination unavailable.", **change}
    path = tmp_path / "status.json"
    good = status_doc(decisions=[{"code": "tcp-q7m4", "item": 0, "text": "owner: review ecs-1.",
                                 "has_default": False, "app": "app: destination unavailable."}])
    owner.write(good, path, checker=lambda text: "passed")
    before = path.read_bytes()
    broken = status_doc(decisions=[row])
    with pytest.raises(owner.StatusError):
        owner.write(broken, path, checker=lambda text: "passed")
    assert path.read_bytes() == before
    path.write_text(json.dumps(broken))
    assert owner_actions.read_status(path) is None


# Run the complete inline script with a tiny DOM and deferred fetch. Assertions exercise
# actual click listeners and promise transitions; no copied implementation or source matching.
HARNESS = r'''
const assert = require("assert");
const vm = require("vm");
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.disabled=false; this.value=""; this.textContent=""; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c),1); }
  get firstChild() { return this.children[0]; }
  setAttribute(k,v) { this[k]=v; }
  addEventListener(k,f) { (this.listeners[k] ||= []).push(f); }
  fire() { for (const f of this.listeners.click||[]) f({preventDefault(){}}); }
}
const nodes={};
for (const id of INPUT.html.matchAll(/id="([^"]+)"/g)) nodes[id[1]]=new Element("div");
const document={getElementById:id=>nodes[id],createElement:tag=>new Element(tag)};
let calls=[],pending=[],intervals=[];
const row={code:"tcp-q7m4",item:7,text:"owner: review ecs-1.",has_default:true,app:"app: SSH host awb-test, folder /work/tcp-q7m4"};
function response(status,data) {return {status,ok:status>=200&&status<300,headers:{get:()=>"application/json"},json:()=>Promise.resolve(data)};}
function fetch(path,opts) {
  if (opts.method==="POST") { calls.push({path,...opts}); return new Promise(resolve=>pending.push(resolve)); }
  let data={};
  if(path==="/api/owner/state") data={csrf:"csrf-test",vault:{state:"unlocked"},keys:{state:"unlocked"}};
  if(path==="/api/owner/items") data={items:[row,{...row,item:8,has_default:false}],stale:false};
  if(path.endsWith("ui-runs")) data={runs:[]};
  if(path.endsWith("intake")||path.endsWith("customers")) data={customers:[]};
  if(path.endsWith("project-options")) data={kinds:[],tags:[]};
  if(path.endsWith("projects")) data={projects:[]};
  if(path.endsWith("tenants")) data={tenants:[]};
  return Promise.resolve(response(200,data));
}
vm.runInNewContext(INPUT.script,{document,fetch,location:{},setInterval:f=>intervals.push(f)});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function all(node,tag) { return (node.tagName===tag?[node]:[]).concat(...node.children.map(c=>all(c,tag))); }
function text(node) {return node.textContent+node.children.map(text).join(" ");}
(async()=>{
 await settle();
 assert(nodes.decisions,"Decisions section is missing");
 let rows=nodes.decisions.children;
 assert.equal(rows.length,2);
 let button=all(rows[0],"button")[0];
 assert(button,"default item has no button");
 assert.equal(button.textContent,"Take the default");
 if(INPUT.case==="no-default") {
   assert.equal(all(rows[1],"button").length,0);
   assert(text(rows[1]).includes(row.app));
 } else if(INPUT.case==="one-call") {
   button.fire();
   assert.equal(calls.length,1);
   assert.equal(calls[0].path,"/api/owner/items/take-default");
   assert.deepEqual(JSON.parse(calls[0].body),{code:row.code,item:7});
   assert.equal(calls[0].headers["X-AWB-CSRF"],"csrf-test");
   assert.equal(calls[0].credentials,"same-origin");
 } else if(INPUT.case==="disabled") {
   button.fire();
   assert(button.disabled,"button enabled during request");
   button.fire();
   assert.equal(calls.length,1,"duplicate click sent another request");
   pending.shift()(response(200,{taken:true,code:row.code,item:7}));
   await settle();
   assert(button.disabled,"button enabled after success");
   assert(text(rows[0]).includes("sent to the session"));
   button.fire();
   assert.equal(calls.length,1);
   for(const refresh of intervals) refresh();
   await settle();
   button=all(nodes.decisions.children[0],"button")[0];
   assert(button.disabled,"refresh reenabled the sent item");
 } else if(INPUT.case==="refusal") {
   button.fire();
   pending.shift()(response(409,{error:"The project or owner item is unavailable."}));
   await settle();
   assert(text(rows[0]).includes("The project or owner item is unavailable."));
   assert(!text(rows[0]).includes("sent to the session"));
   assert(!button.disabled,"refusal prevents retry");
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.parametrize("case", ["one-call", "disabled", "refusal", "no-default"])
def test_page_decision_interaction(case):
    html = PAGE.read_text()
    script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
    result = subprocess.run(["node", "-e", "const INPUT=" + json.dumps({"case": case, "html": html,
                             "script": script}) + ";\n" + HARNESS], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
