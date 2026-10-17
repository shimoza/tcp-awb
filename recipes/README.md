# Lab recipes

A recipe is one kind of live check on a test tenant, as a JSON file in this folder. A project session of the kind
project runs it in one command:

```bash
awb lab list                                               # the recipes, their question and their cost guard
awb lab dry-run ecs-boot-ssh --param flavor=s3.large.2     # the calls it would make; no call is made
awb lab run ecs-boot-ssh --project tcp-q7m4 --tenant test-1 --param flavor=s3.large.2
```

The run checks the project's grant, creates the resources with the project and expiry tags, writes a row in the
project's `RESOURCES.md` for each one before its create call (state creating, with the name the resource gets), runs
the checks and always tears down: after the checks, on a failed check, on an error, when the time budget runs out
and on Ctrl-C. It proves each resource gone with a read and sets its row to deleted. A resource it cannot delete
keeps its row as live with one line for the owner and the command exits 4. The evidence goes to
`evidence/lab-<recipe>-<date>-<n>.md` of the project. When every check passed, it ends with a ready `awb kb add`
line with the grade live.

Before the first create the run reads the EVS disk types (`GET evs /v2/{project_id}/types`, read key) when a create
body names a disk type under `volumetype` or `volume_type`. The AZ is the `availability_zone` of the body around it,
else the parameter `az`. A type the answer does not list, a type whose `RESKEY:availability_zones` leaves out that AZ
and a type that lists it under `os-vendor-extended:sold_out_availability_zones` stop the run with one line and nothing
is made. The dry run shows the check as a step `0. disk types`. Use a type that is on sale: SATA is sold out in
eu-de-01 to eu-de-03 (KB-KYR6), the recipes here use SAS.

A recipe can read more than the disk types before its first create. `rds-create-refusal` names `GET rds
/v3/{project_id}/storage-type/SQLServer` under `preflight` and needs its `volume_type` (default CLOUDSSD) to be
`normal` in its AZ under `az_status`; ULTRAHIGH is unsupported in every eu-de AZ (RDS answered 400 DBS.280325, live
2026-10-17). A create step may carry `"shape_error": {"status": [400], "code": "^DBS\\.28[0-9]{4}$"}`: an answer that
matches it stops the run as a recipe error ("recipe error: the create tested answered 400, error_code DBS.280434")
and is never read by the checks, so only the control matched 500 DBS.200005 counts as a refusal there.

A run takes minutes: run it as a background command of the session, not in the foreground with its two-minute
limit. A SIGTERM tears down like Ctrl-C; only a hard kill leaves rows as live or creating; `awb cloud sweep` then
finds the resources by their tags or their lab name.

## The format

| key | what it holds |
|---|---|
| `name` | the file name without `.json`: lower case letters, digits and dashes |
| `question` | the question the run answers, with `${param}` values filled in |
| `about` | optional: what the recipe builds and how it checks, in plain words |
| `params` | each parameter with its `default`, optional `pattern` (a value outside it is refused by the cost guard), `help` and `choices` (each allowed value with the values it brings) |
| `vars` | fixed values of the recipe: an address range, a script for user data |
| `budget` | `minutes` for the run and `teardown_minutes` for the teardown |
| `guard` | the cost guard: `resources` (the most it may create) and `lifetime_minutes` (the longest a resource may live); budget plus teardown must fit into the lifetime |
| `preflight` | optional: reads made with the read key after the disk types and before the first create, each finding the list item whose `by` field (default `name`) equals `value` and needing its `field` (under `key` when that field is an object, such as `az_status` by AZ) to equal `equals`; a miss stops the run with one line and nothing is made |
| `steps` | the calls in order, each with an `id` and `do`: `create`, `call` or `wait` |
| `checks` | what must hold, each naming the step it reads |
| `teardown` | for every create step: the delete call and the read that proves the resource gone |
| `kb` | optional: scope, tags, class and the statement for the knowledge base when every check passed |

Every call goes through the key service with the lab key and the project code: `method`, `service`, `path`
(with `{project_id}` where the API wants it), optional `query` and `body`. A body may fill a password field with
`{{secret:NAME}}`; the key service puts the secret in and the session never sees it. Before its first create the
run asks the key service for the names of the tenant's secrets (never a value) and stops with one line such as
`the secret ecs-admin is missing for test-1` when one the recipe fills is not there, so it makes nothing. The dry
run names the secrets a recipe needs. A password nobody logs in with later (the admin password of a database the
run creates and deletes) takes `{{random:password}}` instead, also only as the whole value of a password field: the
key service draws a throw-away value at call time (16 characters, upper and lower case, a digit and one of
`!@%^-_=+`), sends it, takes it out of the answer like a secret and forgets it. It is never printed, never in the
evidence and never in a log.

## The flavor deny list

`recipes/DENY.txt` holds one regular expression per line, matched against the whole value: the GPU and AI families
`p`, `pi`, `g`, `ai` and `kp`, every size above `2xlarge`, physical and bare-metal flavors and also RDS, DDS, DCS, CSS and
MRS sizes from `large.8` (fact-check design, default D4). A recipe whose parameter defaults, choices, vars or a flavor
field of a call body (`flavorRef`, `flavor`, `spec_code` and the like) match a line is refused when it is loaded, and
so is a run whose `--param` matches one, before any call. The refusal names the line number and never the value:
`cost guard: the value of flavor matches line 8 of recipes/DENY.txt (a denied flavor)`. The list is always the one of
the repository, also when `AWB_RECIPES` points elsewhere; a list that cannot be read refuses every recipe.

A recheck run (the fact-check service) caps every recipe at 30 minutes: the guard's `lifetime_minutes` or 30
(whichever is less) and the time budget shrinks so that budget plus teardown still fit. The `awb-expiry` tag follows.

A call that is refused or fails keeps in the evidence its status, its error code and the first 200 characters of its
error message, from `error_code` and `error_msg`, `code` and `message` or `error.code` and `error.message` of the
answer or from `<Code>` and `<Message>` of an OBS error, which comes in XML. The message is withheld when it looks
like a key, a token or a secret or when the name check finds a name in it or cannot run. The line that stops the run names the step and the error code, for example
`result: stopped (the create server answered 400, error_code Ecs.0005)`.

Values in a call: `${param}`, `${var}`, the id of an earlier create step as `${step}` (so a create step is named
with letters, digits and underscores) and a value an earlier step
saved (`"save": {"name": "field.path"}`). `${base64:var}` is the base64 of a value (user data), `${int:var}` a number.
Built in: `${project}`, `${tenant}`, `${region}`, `${expiry}` (an ISO date, the start plus the guard's lifetime),
`${expiry8}` (the same as YYYYMMDD), `${rand}` (four letters) and `${name}` (`<project>-lab-<expiry8>-<rand>`). In a
teardown `${id}` is the id of the resource.

A `create` step names the field of the answer that holds the new id (`id_field`; `id_value` in its place for a
resource whose name is its id, a bucket), the `resource` (`kind`, two to five letters, for its handle such as
`ecs-3`; `type`; `cost`: none, small or medium) and where its tags go:

- `"tags": "body"`: the body carries `"${tags:kv}"` (`[{"key": "awb-project", ...}, {"key": "awb-expiry", ...}]`),
  `"${tags:list}"` (`["awb-project=tcp-q7m4", ...]`) or `"${tags:dict}"`.
- `"tags": {a call}`: a tag call right after the create, with `${id}` and one of the tag forms in its body.
- `"tags": "name"`: for a service that takes no tags at create; the call carries `${name}`, which holds the project
  code and the expiry.

A create step also names the resource: `name` is the name its call gives it (default `${name}`; it holds `${name}`
or `${project}`, so the name carries the project code; the call must carry it). Every create of the recipes
here has a `find`: a read that looks the resource up by that name when the answer holds no id. Its forms:

- `"find": {"call": {a GET of the list}, "list": "vpcs", "field": "id", "within": 60}`: the item of the list under
  `list` whose `name` is the name of the resource; its `field` is the id. Matching the name in the answer also covers a
  list that takes no name filter.
- `"find": {"call": {...}, "field": "servers.0.id"}`: a field of the whole answer.
- `"find": {"call": {"method": "HEAD", ...}, "status": [200]}`: for an `id_value`, the statuses that say it exists.

A list find matches the item's `name`; `"by": "remark"` names another field of the item that holds the name, for a
resource without a name such as an SMN subscription.

The find reads every 10 seconds for `within` seconds (default 60). Only two answers prove the resource absent: a
list without its name (on a page that is not full, when the query names a `limit`) and a 404 of the read of a find
of statuses. Any other answer cannot tell, so a wrong shape never marks a row not created.

The row of a create goes like this:

| the create answers | the row |
|---|---|
| 2xx with the id | live with the id |
| 2xx without an id | the find places it (live with the id); else live with "id unknown" and a line for the owner; the run stops |
| no answer, a timeout or a 5xx | the find places it (live with the id); a find that proves it absent sets the row to not created with that answer as proof; a find that cannot tell (or no find) leaves it live with the name for the owner; the run stops unless it was placed |
| 3xx or 4xx; the key service refused | nothing was made: the row goes again and the run stops |

The teardown looks every row without an id up by its name once more: found, it is deleted and proven gone like the
others; a row proven not created stays so with both answers as proof. A row that is not created does not hold `awb
close`; a row still creating (a run that was killed) does.

A `wait` step repeats its call every `every` seconds (default 10) until the field of `until` `equals`, is `in` or
`contains` a value, for at most `within` seconds; a value of `fail_on` ends it early. A `call` step that does not
answer 2xx stops the run, unless a check expects its status.

Checks: `"reached": true` (a wait reached its state), `"status": [403]` (an expected refusal), `"field"` with
`"equals"`, `"contains"` or `"present"`. A step runs, then its checks; the first check that fails stops the run and
the teardown follows. A check with `"report": true` goes to the evidence as passed or reported and never stops the
run (a console that may stay empty on some flavor families).

A `status` check on a create step expects a refusal: a 3xx or a 4xx or a 5xx whose find proves nothing was made,
does not stop the run; the row goes again or reads not created and the check reads the status. A create that is
accepted gets its row as usual, so its check fails and the teardown deletes it.

A `gone` proof may carry `"also"`: a second read (`call` with `status` or with `field` and `in`) that must say gone as
well, read after the first one holds (an RDS instance: the read by id and the recycle bin).

A step or a teardown entry may carry `"confirm"`: the note that its API shape is not proven by a live call yet. The
dry run, the list and the evidence show these notes, so the first live run says which shapes held.

## An example

```json
{
  "name": "subnet-active",
  "question": "Does a subnet of ${cidr} in ${region} become ACTIVE within 90 seconds?",
  "params": {"region": {"default": "eu-de", "pattern": "^eu-(de|nl)$"},
             "cidr": {"default": "192.168.10.0/24", "pattern": "^192\\.168\\.[0-9]{1,3}\\.0/24$"}},
  "budget": {"minutes": 5, "teardown_minutes": 5},
  "guard": {"resources": 2, "lifetime_minutes": 15},
  "steps": [
    {"id": "vpc", "do": "create", "says": "a VPC",
     "call": {"method": "POST", "service": "vpc", "path": "/v1/{project_id}/vpcs",
              "body": {"vpc": {"name": "${name}", "cidr": "192.168.0.0/16"}}},
     "id_field": "vpc.id", "name": "${name}",
     "find": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs"}, "list": "vpcs", "field": "id"},
     "resource": {"kind": "vpc", "type": "vpc", "cost": "none"}, "tags": "name"},
    {"id": "subnet", "do": "create", "says": "a subnet",
     "call": {"method": "POST", "service": "vpc", "path": "/v1/{project_id}/subnets",
              "body": {"subnet": {"name": "${name}", "cidr": "${cidr}", "vpc_id": "${vpc}"}}},
     "id_field": "subnet.id", "name": "${name}",
     "find": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/subnets", "query": {"vpc_id": "${vpc}"}},
              "list": "subnets", "field": "id"},
     "resource": {"kind": "sub", "type": "vpc subnet", "cost": "none"}, "tags": "name"},
    {"id": "active", "do": "wait", "says": "the subnet is ACTIVE",
     "call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/subnets/${subnet}"},
     "until": {"field": "subnet.status", "equals": "ACTIVE"}, "fail_on": ["ERROR"], "within": 90, "every": 5}
  ],
  "checks": [{"id": "active", "step": "active", "says": "the subnet is ACTIVE within 90 s", "reached": true}],
  "teardown": {
    "vpc": {"call": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/vpcs/${id}"},
            "gone": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs/${id}"}, "status": [404]}},
    "subnet": {"call": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/vpcs/${vpc}/subnets/${id}"},
               "gone": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/subnets/${id}"}, "status": [404]}}
  }
}
```

## The recipes here

| recipe | question | guard |
|---|---|---|
| `ecs-boot-ssh` | does an ECS of a flavor boot an image and answer on SSH port 22 inside its VPC (security group rule, no EIP) | 5 resources, 45 minutes |
| `obs-policy-deny` | does an OBS bucket whose policy denies a listed action refuse it | 1 resource, 15 minutes |
| `elb-health` | does a shared ELB listener with one ECS backend pass its health check | 9 resources, 60 minutes |
| `ecs-flavor-boot` | does an ECS of any flavor the deny list leaves (up to 2xlarge, the s9 family too) boot in an AZ whose `cond:operation:az` says normal and read ACTIVE with that flavor and AZ; the console read is reported, not failed | 4 resources, 30 minutes |
| `smn-http-confirm` | does an HTTP subscription of an SMN topic get a SubscriptionConfirmation with a subscribe_url on the console host and does a plain GET of it confirm the subscription (status 1) | 7 resources, 30 minutes |
| `rds-create-refusal` | does RDS for SQL Server refuse a collation under test as it refuses the invented control `Awb_No_Such_Collation_CI_AS` (HTTP 500), with nothing made | 5 resources, 30 minutes |

`elb-health` lets the port in from `100.125.0.0/16` (the parameter `hc_source`), the address range the health checks
of a shared ELB are said to come from; that range is to confirm live. Its first live run on 2026-10-12 left the member
OFFLINE and could not tell why, so the backend's user_data writes the web server as a systemd service
(`awb-web.service`, restarted when it ends) that outlives cloud-init. After `cloud-init status --wait` a check started
with `systemd-run` tries the port on 127.0.0.1 and writes `AWB-WEB-UP` or `AWB-WEB-DOWN` to the console. The step
`web` reads the console of the backend with the call of `ecs-boot-ssh` (`POST /v2/{project_id}/servers/ID/action`
with `os-getConsoleOutput`) before the load balancer is made, and the check `web-serves` wants `AWB-WEB-UP`. A failed
`web-serves` says the web server is down (no marker at all: the user_data did not run or cloud-init did not end); a
passed `web-serves` with a failed `health-passes` says the health checks do not reach the port, so the source range
is the one to look at.

The last three are the check recipes of the fact-check design F7 (`docs/strategy/F4-fact-check-design-2026-10-14.md`),
each within the 30 minutes of a recheck run. `ecs-flavor-boot` reads the extra specs of the flavor first and stops
before any create unless `cond:operation:az` says normal for the AZ. `smn-http-confirm` lets TCP 80 in, gives the
server an EIP of 1 Mbit/s and writes a web server that writes the type of each message and the host and path of its
subscribe_url to the console, never the token; for a SubscriptionConfirmation it waits 60 seconds, then GETs the URL
with no login, no cookie and no redirect. The run reads the subscription at status 0 before that GET (the control)
and at 1 after it. SMN takes no tags: the topic and the subscription carry the project code in their name and remark.
`rds-create-refusal` sends two creates with the same body but the collation: the value under test, then the
control. RDS answers a collation it does not take with 500 DBS.200005, so each create is followed by the find by
name and the instance count must not change. An accepted create fails the check (the value is taken); it builds
for about 26 minutes and refuses its delete meanwhile (409 DBS.200019), so the teardown repeats the delete and
proves it gone by the read by id and the recycle bin. The password is `{{random:password}}`.

Their VPC, subnet and shared ELB calls are the ones `awb cloud lease-check` already makes. Every other shape is
marked "to confirm live" in the recipe, with what to look at. That holds for the finds by name too: the list of VPCs
is read by `awb cloud sweep` already; the lists of subnets and security groups with the `vpc_id` filter, the ELB
v2.0 lists with the `name` filter and their keys are to confirm live. A find that answers otherwise than the recipe
expects cannot tell: the row stays live for the owner, never not created. The OBS recipe goes through the call
operation of the key service, which signs a call to obs the OBS way (the code of `awb/obs.py`, the bucket by its
own host) and every other service the way of the API gateway. Its first live run on 2026-10-12 stopped at the
create with HTTP 400 InvalidArgument, when the key service still signed OBS the gateway way (B-003). What stays to
confirm live: the create in the default region without a location body, the policy as JSON with the OBS form of
Principal and Action and the 403 of the denied action. Its bucket is named after the project, the only kind of
bucket the key service writes to.
