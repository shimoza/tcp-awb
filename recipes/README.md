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
| `steps` | the calls in order, each with an `id` and `do`: `create`, `call` or `wait` |
| `checks` | what must hold, each naming the step it reads |
| `teardown` | for every create step: the delete call and the read that proves the resource gone |
| `kb` | optional: scope, tags, class and the statement for the knowledge base when every check passed |

Every call goes through the key service with the lab key and the project code: `method`, `service`, `path`
(with `{project_id}` where the API wants it), optional `query` and `body`. A body may fill a password field with
`{{secret:NAME}}`; the key service puts the secret in and the session never sees it. Before its first create the
run asks the key service for the names of the tenant's secrets (never a value) and stops with one line such as
`the secret ecs-admin is missing for test-1` when one the recipe fills is not there, so it makes nothing. The dry
run names the secrets a recipe needs.

A call that is refused or fails keeps in the evidence its status, its error code and the first 200 characters of its
error message, from `error_code` and `error_msg`, `code` and `message` or `error.code` and `error.message` of the
answer. The message is withheld when it looks like a key, a token or a secret or when the name check finds a name in
it or cannot run. The line that stops the run names the step and the error code, for example
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
the teardown follows.

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

Their VPC, subnet and shared ELB calls are the ones `awb cloud lease-check` already makes. Every other shape is
marked "to confirm live" in the recipe, with what to look at. That holds for the finds by name too: the list of VPCs
is read by `awb cloud sweep` already; the lists of subnets and security groups with the `vpc_id` filter, the ELB
v2.0 lists with the `name` filter and their keys are to confirm live. A find that answers otherwise than the recipe
expects cannot tell: the row stays live for the owner, never not created. The OBS recipe cannot pass through the
call operation of the key service as it is: that operation signs the way of the API gateway, OBS signs its own way.
Its first run is expected to stop at the create with HTTP 403 and nothing made.
