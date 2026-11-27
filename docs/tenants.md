# Test tenants, the key service and lab runs

How a session reaches a TCP test tenant and the public documentation through the key service. Back to the [README](../README.md).

## Live tenants through the key service

A project builds and checks on real TCP test tenants. The session never holds a key. The key service runs on
the owner's side. It keeps the keys, logins and passwords of every test tenant in memory and signs each call a
session sends: a call to OBS the OBS way, every other service the way of the API gateway. The read key allows
reads only. A write needs the lab key and the code of an active project. A write to the identity service is
always refused. Every answer comes back with every key and secret taken out.

A session's call and lease are bound to its own project by a key token, never by the code the request names (K4 of
the review of 2026-10-20). `awb spawn` writes the token into the project folder (`.git/awb-key-token`, mode 640, never
tracked by git); the owner's grant (`awb projects kind CODE project`) records it in `<vault>/key-tokens.json` (mode 600). The
awb client sends the token of the folder it works in with each call and lease and with each exchange op (obs,
take and find). The key service takes the session's project from it: a request that names another project or comes
with no token or a wrong one is refused before anything is signed. An exchange op whose token names one project
and whose request names another is refused. The service counts at most 240 calls a minute for each project token
and 120 a minute for each uid for the calls without one, so one looping session does not starve the others. The service reads only the request and the owner's files, never the caller's process, so it works
with the service as the owner and the sessions as the work user. The owner's own calls carry no binding. A project
spawned before the token gets one with its first `awb cloud` call; the key service then refuses with `the grant of
CODE holds no key token` until the owner runs `awb projects kind CODE project` once more. The sessions of other
projects share the work user, so the token is kept from them by the place guard, as the rest of the folder is. A side
session's worktree (`<project>/.side/NAME`, `awb side`) presents the token of its project: its `.git` is a file,
so the client follows it and its `commondir` to the project's own `.git`, and only when that project is active or
parked in the register and lies right under the projects root. A worktree never writes a token of its own.

Every resource a project creates carries the project code as a tag and a row in the project's `RESOURCES.md`. A
daily snapshot tells what ran on which tenant on which day. `awb close` refuses while something is still running and while the project keeps a bucket (`awb lab kept drop NAME`
for each; `--force` closes anyway).
What a session learned live on the platform goes into the knowledge base with the grade `live`.

The most frequent live check (raise a few resources, check one thing, tear down) is a lab recipe: `awb lab run
ecs-boot-ssh --project tcp-q7m4 --tenant test-1` does it in one command (inside the project's folder both flags are optional: the code comes from SCOPE.md and the tenant from its `tenant:` line, which `awb spawn --tenant test-1` writes). The recipe (a JSON file under `recipes/`)
names the question, the calls, the checks, the teardown, a time budget and a cost guard. The run tags every resource
and writes its row in `RESOURCES.md` before the create call. When the answer of a create is lost it looks the
resource up by its name and records it or proves it not created. It always tears down, also on a failed check, an
error, the time budget or Ctrl-C, proves each resource gone and writes the evidence with a ready `awb kb add` line.
A recipe that fills a password from a tenant secret asks the key service for the names of the tenant's secrets
first and stops before any create when one is missing. A refused call keeps its error code and the start of its
error message in the evidence. A create whose refusal a check expects (the 500 of an RDS collation it does not take)
goes on when the find proves nothing was made; the recipes `ecs-flavor-boot`, `smn-http-confirm` and
`rds-create-refusal` are the write-tier checks of the fact-check service. A recipe may read before its first create
(`preflight`): `rds-create-refusal` reads the RDS storage types and stops with one line unless its `volume_type`
(default CLOUDSSD) is normal in the AZ. A create step may name a `shape_error`: a 400 with an error code DBS.28xxxx
stops the run as a recipe error and is never taken as the answer a check looks for.
Five more recipes (`eip-bandwidth-adjust`, `evs-snapshot-restore`, `nat-snat-egress`, `obs-lifecycle-rule` and
`dns-private-zone`) are tested on fake answers only; `recipes/README.md` lists the shapes still to confirm live.
The recipe `disk-reader` (tcp-bytg ask 2026-11-21-tcp-bytg-10) reads Windows state of a stopped server (setupapi.dev.log,
the event logs System.evtx and Application.evtx, the registry hives SYSTEM and SOFTWARE and the files of its parameter
`files`): `awb lab run disk-reader --param server_id=ID [--param az=eu-de-02] [--param files=PATH:PATH]`. It refuses a
server that is not stopped or not in its AZ before it makes anything. It makes a bucket named after the project, a
security group with no inbound rule and a small Linux reader ECS (no EIP) in the subnet and AZ of the server, detaches
the system volume of the server, attaches it to the reader, which mounts it read-only with ntfs-3g and puts the files
into the bucket (the script is `awb/tcp/diskreader.py`, the user_data; it fetches nothing from a public bucket and signs its puts with
the key of the tenant: the run names the script and its parameters as `user_data_script` and the key service renders
it itself and fills `{{secret:obs-ak}}` and `{{secret:obs-sk}}`, no IAM agency and no extra key), then attaches the
volume back to the server with the same device name and deletes the reader. The teardown always gives the volume back
first, also after a failed step or a signal (`restore` in the recipe), proves it back with a read and only then deletes
the reader; a volume that is not proven back keeps the reader alive and the command exits 4 with the line to put it
back by hand. The bucket stays (a create step with `keep`: its row says kept, its expiry tag ends it) because it holds
the files. The engine gained a wait on the status of an answer, `keep`, `restore`, a preflight on one object and
`${userdata:diskreader}` and `${script:diskreader}`; `recipes/README.md` describes them.
`awb lab list` shows the recipes and `awb lab dry-run` the calls a run would make. A resource it cannot delete stays
in `RESOURCES.md` as live with the line to delete it.

### Tenant aliases: tcp-NNNNN and the old test-NNNNN (B-097)

A tenant alias is `tcp-` and the five or six digits of the tenant (`tcp-10491`); it is never a project code, which is
`tcp-` and four characters (`tcp-q7m4`). A project code is never a tenant. The old name `test-NNNNN` (the same
digits) stays a synonym for 30 days from the date in `rules/tenant-alias-window.txt` (`window-from DATE`; until
2026-12-27 now): every command that takes an alias (`--tenant ALIAS` of `awb cloud call`, `awb lab run`, `awb lab obs`,
`awb lab`, `awb kb recheck-run`, `awb kb recheck-watch` and `awb spawn`; `awb tenant now|history|at|snapshot|tags`)
takes either name for the same tenant, the old one with one warning line "test-NNNNN is the old name of tcp-NNNNN, until
DATE" on standard error. After that day it refuses the old one with "use tcp-NNNNN". The key service maps a request to
the alias `keys.conf` and the register hold, so the host keeps its entries, keys and the line `lab_admin` as they are
until the owner renames them. An alias of another shape (`test-1`) is not touched. Customer tenant numbers stay in the
vault only.

### Lab runs of a project's own scripts (LR)

A project session may test its own scripts on the lab tenants tcp-10491, tcp-10497 and tcp-10503 (since
2026-11-20) with their admin login, only on his word in the session. The owner sets it up once: the logins in pass as
`awb/admin/<alias>/domain`, `user` and `password`, the line `lab_admin = tcp-10491 tcp-10497 tcp-10503` in
keys.conf, then `awb keys unlock`. Any other alias in the line refuses the unlock; tcp-10498, a customer tenant and a read-only
tenant are refused there. A customer tenant you switched to write (`awb tenant mode`, below) takes lab runs too,
for the projects granted it, with its write credential in place of an admin login. The session runs `awb lab run start --tenant tcp-10491 [--note TEXT]`, its scripts call
through the key service with

    /opt/tcp-awb/venv/bin/python3 evidence/<folder>/poc.py      # the script: from awb import labclient
    status, headers, body = labclient.call("POST", "iam", "/v3/users", body={...})

and `awb lab run end` deletes what the run made, newest first, and reports created, removed and failed (`awb lab
run status` lists the open runs and their leftovers). The id of a create is taken from `id`, `log_group_id` or
`log_stream_id` of the answer (LTS groups and streams are removed, streams first); a create with no such key is
listed as "not recorded". After each delete the end reads the resource again and only a 404 counts as removed: one
that still answers is "still there after delete", and an IAM project, which the platform keeps, is named as left
behind. Every call needs the open run of its project and tenant. The
key service takes the token itself (domain scoped for iam, project scoped for the other services, a temporary key
for obs; `scope="domain"` gives a GET to eps a domain token made for that call and never kept, any other method or
service with it is refused; eu-nl is closed on the test tenants by the owner's rule), answers in full and never hands over the password, the login or a whole token: tokens, temporary keys and
security tokens come shortened to 8 characters, "..." and the length, and such a shortened value of the run works
again as `X-Auth-Token` or `credential=` (a call with `credential=`, also the temporary key of an agency, is signed with its security token and names its project in X-Project-Id; `{project_id}` of a key from an assume_role is the project of the delegating account). Every create is recorded with its delete (a POST whose answer names a new
id or name, a PUT or PATCH of a path a read before did not find; not the upload of an image's data and not a PUT
below a resource the run made). A delete that answers 2xx or 404 removes the record it names, also when it names the
resource by another API version (`/v1.1/images/ID` or `/v1/cloudimages/ID` for `/v2/images/ID`), what was recorded below it and a leftover
of an earlier run of the project on the tenant. The call log is
`notes/lab-runs/<run>.log` of the project; a boot script (user_data) is written there only as a placeholder with its
length, a password the image prints on its serial console (the `user:password` lines after cloud-init's "Set the
following 'random' passwords", a chpasswd line and its list, `password for USER: x`) as [secret] in the answer of the
service and in the log; the commit gate checks no content of a project (DMZ 3). A log that grows over 2 MiB (a run log or a Windows
lab log under `notes/winlab/`) moves to `full/` beside it (the writer adds it to the project's `.gitignore`) and
git keeps `<run>.summary.md`: start, end, calls, errors, created resources, the path of the full log and its
SHA-256 (`awb/lablog.py`, written again at the end of the run). After the move the full log is the run log: every
append and `awb lab obs put` or `get` writes there. The commit gate (also inside a project) refuses any text file over 8 MiB at
once as `too-large` and names this rule. A run open for more than two hours or ended with failed deletes shows in
`awb owner status` and the 08:00 mail. The module docs are in `awb/tcp/labadmin.py` and `awb/labclient.py`.

A project that holds a stand (a taken-over or long-lived one, listed live in its RESOURCES.md) changes it in a run
too (LK). `labclient.call(..., keep="why")` keeps what the call creates, `awb lab run keep N --reason TEXT` keeps a
create the run recorded already (N from `awb lab run status`). The key service allows it on a lab tenant only, for a
project whose RESOURCES.md lists a live row naming that tenant or a live resource the create names (the security
group of a new rule). The end skips a kept create and reports it as kept, the client writes its row in RESOURCES.md
as live and `awb owner status` shows it for seven days. A PUT, PATCH or tag action on an existing resource records
nothing, so the end never undoes it. A delete of a resource listed live in RESOURCES.md is refused unless the call
says `delete_listed=True`; when it succeeds (2xx or 404) the run records it as removed, the end lists it and the
client sets its row to deleted. `awb lab run start` prints the python that runs a lab script.

A run that made a tools bucket the project guide links to ends with `awb lab run end --keep NAME[,NAME...]`: it
deletes everything else and the named OBS buckets (by bucket name, no other kind) stay. A name that is not a bucket of
the run is refused before anything is deleted. A kept bucket leaves the run's record and goes into the kept list of the
project (tenant, region, kind, name, run, date); it keeps the run's tags. `awb lab run status` and `awb owner status`
show the list, and `awb lab kept drop NAME` deletes a kept bucket later with the same delete and proof as a run end.

A large file (an IMS image of 10 to 128 GB and more) does not fit a lab call, which takes 256 KB of JSON or text, and
the egress filter keeps the session away from OBS. Inside the open run the key service moves it itself, as a
background job:

    awb lab obs put FILE obs://BUCKET/KEY [--tenant T]     labclient.obs_put(file, url)
    awb lab obs get obs://BUCKET/KEY FILE [--tenant T]     labclient.obs_get(url, file)
    awb lab obs fetch URL FILE [--sha256 H|--sha1 H]       labclient.obs_fetch(url, file, sha1=...)
    awb lab obs hosts                                      labclient.obs_hosts()
    awb lab obs status [JOB]                               labclient.obs_status(job), obs_wait(job)
    awb lab obs abort JOB                                  labclient.obs_abort(job)
    awb lab obs link BUCKET KEY [--minutes N]              labclient.obs_link(bucket, key, minutes=60)

put and get answer a job id at once; status shows bytes done, total, rate and state. The rules are those of a lab
call: the open run of the project on tcp-10491, tcp-10497, tcp-10503 or a customer tenant in write mode (never a read-only
tenant), a bucket named after the
project (`<code>-...`), never an exchange bucket, signed with the run's temporary credential. The session holds no
key. The file reaches the service as an open descriptor over the socket, never as a path, so a file the session
cannot read is refused; a get writes only inside the project folder or the session scratch folder and checks the
free disk first. A put is a multipart upload (within 10000 parts of 100 KB to 5 GB) whose parts are tried again
after a broken connection. The same put or get once more resumes an interrupted job, also one a reload of the key
service interrupted. At the end the object's size equals the file's and it carries the SHA-256 of the file as
`x-obs-meta-sha256`; the job result has the hash and the run log gets bucket, key, size, hash, duration and state. A
file or object over `lab_obs_max_gb` of keys.conf (default 200) is refused with the limit named. `awb lab run end`
stops the run's jobs and aborts the open multipart uploads of its buckets before it deletes the object and the
bucket. A reader without a key, such as an ECS of the run, gets a link instead of a public ACL, which the key
service refuses (the owner's decision of 2026-11-24): `awb lab obs link` prints a GET URL of one object, signed in its
query string with a temporary credential made for that link, valid 60 minutes by default and 12 hours at most, for a
bucket the open run made only and never on a customer tenant. A link is an exit (B-094): before it signs, the service
runs the exit check on the object (a registered name or a secret; an id is no finding) and refuses with one line when
it finds one; an object over 8 MiB (an image) is not read, and what the run's own disk reader wrote under `files/` and
`manifest.json` is signed as before. The secret key stays in the service; the URL is
printed once and the logs keep only the bucket, the key's hash and the minutes. The module doc is in
`awb/tcp/labobs.py`.

## Public documentation through the key service

A session cannot reach a documentation host: the egress filter of the work user lets it reach the model API, the
client's hosts and the public price API only, and opening it to the hosts of `rules/allow-domains.txt` would give a
session a way to send data out. So the key service, which runs as the owner and has internet, reads the page for it.
`awb doc fetch URL [--out FILE]` sends the op `doc_get` on the call socket and prints the text with the first line
`source: URL, fetched YYYY-MM-DD HH:MM UTC` (or writes it to FILE inside the project folder). With `--links` the text ends
with the links of the page (absolute, each once, a host off the list marked), so a session can walk from page to page.
A help built with MadCap Flare draws its contents with JavaScript and its pages do not link each other: `awb doc find
URL WORDS` makes the service read the help index the page names (Data/HelpSystem.xml, the Toc file and its chunks)
and prints every page whose title or path holds all the words, with its URL; the script itself never reaches the
session and a plain fetch still takes no script. Each file of a find counts in the rate and the ledger. The service fetches by
GET only, https only, the host exactly a line of `rules/allow-domains.txt` or of the owner-side file that the line
`doc_hosts = /ABSOLUTE/FILE` of `paths.conf` names (`seal/setup.sh` keeps that line when it rewrites the host file; `awb doc hosts` lists them with their source; `awb doc hosts add
HOST [--why TEXT]` on the owner side appends a host after the checks of the list, read at the next fetch, and
`awb owner status` counts the hosts not yet in the repository), no port, no
user info, no query string, no fragment and a path of at most 300 URL-safe characters. It follows at most 3
redirects and only to a URL that passes the same checks (a redirect target may carry a query string, the first URL may not), takes at most 5 MB of HTML, Markdown, plain text or JSON
and turns HTML into text (headings, paragraphs, lists, code, tables). It also reads a PDF of up to 32 MB (a service
description, a price list): `pdftotext -layout` of the poppler tools on a private temporary file, each page after a
line `--- page N ---`, and the first line then ends with `, pdf, N pages`; an encrypted PDF, a broken one and a scan
without a text layer are refused with the reason. A gzip body (the docs host sends one although the reader asks
for identity) is unpacked under the same cap; a broken one and any other encoding are refused. It sends no cookie
and no auth header, and
its User-Agent names the Workbench. A project gets at most 30 fetches a minute and 500 a day. Each fetch writes one
line to `<vault>/log/doc-YYYY-MM.tsv`: time, project, host, path, size and status, never page content. The page itself
runs no content check (B-089): a sample password or a name of a public page is shown as written; only a control character
is refused. The work rules
say to quote such a page with its source and date and never to state it from memory when it can be fetched. A host
off the list is refused with one line that tells the session to leave one owner item in OPEN.md under the owner
heading, `Steering: allow documentation host HOST (vendor, why)`, instead of asking. `rules/allow-domains.txt` now
also lists the official documentation hosts of Red Hat, Ubuntu, Cisco, Broadcom, VMware, NVIDIA and TCP
(`docs.otc.t-systems.com`, `docs-beta.otc.t-systems.com`); no forum, blog or Q&A site.

## Test tenants and keys

A working session never sees a key. The key service (`awb keys serve`, a systemd service of the owner) holds the
keys and secrets of every test tenant in memory and signs the calls the sessions send it. The owner keeps them in
the password store (`pass`), one folder per tenant alias:

| entry | what it is | what it allows |
|---|---|---|
| `awb/tenant/<alias>/ak`, `sk` | the read key, best an IAM user with a read-only role | GET and HEAD |
| `awb/tenant/<alias>/lab/ak`, `lab/sk` | the lab key | every method; a write needs the code of an active project |
| `awb/tenant/<alias>/secret/<name>` | a login or a password | fills a password field of a request, never shown |
| `awb/tenant/<alias>/write/ak`, `write/sk` (or `write/login/...`) | the write credential of a customer tenant; without it write mode uses `ak`, `sk` or `login/...` above | every method, only in write mode (`awb tenant mode`) |

The lab key signs a write (POST, PUT, PATCH, DELETE) only when the `lab_writes = METHOD service path[,...]` line in
`keys.conf` names it: method, service and path are one triple, a `{name}` segment stands for exactly one segment
(never a prefix) and the query keys are the ones after `?` (`PUT obs /{name}?policy`; none without `?`). Any other
write is refused with one fixed line that names the service and never the path. Without the line the lab key signs
no write; with the old `service:path-prefix` line the unlock goes on, reads work and it signs no write until the new
line is in (one line in the journal and in `awb keys unlock` says so). `awb keys unlock` prints the count of
triples. `awb keys lab-writes --from-recipes` prints the line for
every write of the recipes in `recipes/` and of the recheck watch, so the owner can paste it (each `${name}` becomes
`{name}`).

A new tenant needs its admin login in the store once; one command does the rest (the read group and user, the
key, the register, the key service and the first snapshot) and a second run changes nothing:

```bash
pass insert awb/admin/test-1/domain     # and user, password
awb tenant setup test-1 [--domain-id ID] [--lab-key | --lab-user] [--alerts]
awb keys unlock                  # after every start of the service
```

After changing a lab tenant's admin password, run `awb tenant password ALIAS` in your own terminal.
The alias must be enabled by `lab_admin` in keys.conf and loaded in the running key service. The command asks
for the new password hidden, replaces `awb/admin/ALIAS/password` through standard input to pass, and reloads
only that alias. It then asks `test with one login now? [y/N]`; only yes makes one login with the loaded value.
This updates the stored login after a password change in the tenant. If loading fails, the new store entry stays
and the command points to `awb keys unlock` without offering a login.

`awb keys status` in the owner's terminal compares each store file's modification time with the time captured
when its value was loaded. It marks `store newer than loaded` per field handle, without decrypting or logging in;
a successful password command leaves that alias clean. Missing files and unknown load times are shown explicitly.
A lab run receiving HTTP 401 stops with `the stored login may be stale: awb keys status` and makes no second try.

Without `--domain-id` setup takes the domain of the admin token and names it (the first 6 characters of its id).
A tenant connected before setup existed keeps its read key: setup finds the user of that key with the admin login,
puts it into awb-read (`read key: its user adopted into awb-read`) instead of making a new user and key, then takes
a snapshot and the cost estimate. It deletes nothing.

By hand the same is `pass insert awb/tenant/test-1/ak` and `sk`, then `awb tenant add test-1 --keys
pass:awb/tenant/test-1 --region eu-de`.

A tenant of a customer or partner where nothing may change is a read-only tenant. It holds a read key or only a
console login, which you put into pass yourself; the key service then allows GET and HEAD alone there, for every
role and caller, and only a project you granted it reaches it:

```bash
pass insert awb/tenant/cust-1/login/domain   # and user, password (or awb/tenant/cust-1/ak and sk)
awb tenant add-readonly cust-1 --domain-id ID --login   # asks account, user and password when pass lacks them
awb tenant grant cust-1 tcp-xxxx
awb cloud call GET ecs "/v1/{project_id}/cloudservers" --tenant cust-1     # in that project's session
awb tenant projects cust-1                                                 # the projects it can read there
awb cloud call GET ecs "/v1/{project_id}/cloudservers" --tenant cust-1 --scope eu-de_lab
```

A read-only tenant reads eu-de and eu-nl unless `--region` names others; `awb tenant regions ALIAS --region eu-de
--region eu-nl` changes the regions of one that is registered without asking for its login again (an old keys.conf
line of one region still works). `--scope` names a region project or a sub-project (eu-de_<suffix>) by name or id:
the key service checks it against GET /v3/auth/projects (the tenant's domain, one of its regions), takes a token
for that project and calls the endpoints of its region. `awb tenant projects ALIAS` lists them, GET only.

In a terminal `awb tenant add-readonly ALIAS --domain-id ID --login` asks for what pass lacks (account name, user, password; the password is hidden and never echoed or logged), writes the entries under `awb/tenant/ALIAS/login/` itself, registers the tenant, makes one test GET through the key service before any grant and says what failed (403: no programmatic access; 401: password wrong or change pending; MFA needed). It ends with a short text for the customer's admin in German and English: a read-only role, "Programmatic access" ticked, no access key, login protection by virtual MFA off. With the entries in pass already, or without a terminal, it works as before.

For a login the key service takes a token from IAM, keeps it in memory only and renews it before it expires. A
login with MFA is refused with one line: ask for a read-only AK/SK. `awb cloud tenants` shows the kind and whether
a key or a login reads it, never a value. `awb tenant revoke` and `awb tenant remove` take it back.

A customer tenant stays read-only until the customer gives you a written OK to work in it directly. Then you put a
write credential of the customer into pass beside the read one and switch the tenant to write in your own terminal
(task TW):

```bash
pass insert awb/tenant/cust-1/write/ak       # and write/sk (or write/login/domain, user, password)
awb tenant mode cust-1 write --note "mail OK of 2026-11-02"
awb tenant mode cust-1 readonly              # back, at once
```

A tenant registered with an ordinary key or login of the customer's admin needs no second credential: with nothing
under `write/` write mode uses the registered one (`awb/tenant/cust-1/ak` and `sk`, or `login/domain`, `user` and
`password`, as the readonly line names it), so a tenant and grants made before task TW get write mode with `awb
tenant mode cust-1 write` alone. The command checks the entries by name only; a partial write credential, or a write
key and a write login both, is refused with one line that names what is missing. It prints which credential write
mode uses (the write one or the registered one) by the names of its entries, never a value. A registered login takes
its tokens as the read path does. It keeps the day and the note in the `tenant_write` line of keys.conf and runs `awb keys unlock`, so the
mode travels to the key service like every other setting; a tenant the line does not name is read-only. In write
mode the key service signs every method for the projects granted the tenant, with the write credential: `awb cloud
call` (a write names an active project, any bucket of the customer for OBS, IAM only in a lab run) and a
lab run (`awb lab run start --tenant cust-1`, `labclient.call`, `awb lab obs`) with the same run log, never a key in
it. The session holds no key and gets no lease. The end of a run on a customer tenant deletes only what the run made:
a PUT or PATCH outside OBS records nothing and the delete of a server keeps its elastic IP and its data disks. The
switch back takes effect at once in the running key service: it forgets the write credential, refuses the next write
and aborts an open lab obs job of the tenant with the reason in its state. `awb owner status` lists every tenant in
write mode with its day and note for as long as it stays there; there is no expiry. `awb tenant list` shows the mode
of each customer tenant. The command is refused for the work user and in an assistant session.

The key service signs a call to `obs` the OBS way (`Authorization: OBS AK:SIGNATURE`, the code of `awb/obs.py`): the
path is `/BUCKET[/KEY]`, the bucket is reached by its own host and `GET /` lists the buckets. Every other service is
signed the way of the API gateway (SDK-HMAC-SHA256). An OBS call never reaches the two exchange buckets (they are
`awb xchg`'s) and an OBS write or read of a session reaches only a bucket named after its project (`tcp-xxxx-...`); the list of the buckets and the tags of a bucket (the sweep) stay open, and the owner's own calls are not limited. The code and
message of an OBS error, which comes in XML, are read like those of a JSON error. An OBS list in XML comes whole as
data: `GET /` gives the buckets (name, creation date, location when given), `GET /BUCKET` the keys of one page and
`GET /BUCKET?tagging` the tags. An answer that declares a DOCTYPE or an entity is never parsed. Any other answer that is not JSON is cut to its first 300 bytes; the command says so
with the size of the whole answer. An answer is read up to 16 MiB, a larger one is cut the same way. `awb cloud sweep --buckets` adds the OBS buckets of the region to the sweep
under the rules of the other types (untagged, expired or not in RESOURCES.md), with one call per bucket for its tags.

A request body may name a secret only as the whole value of a password field, `{"admin_pass": "{{secret:NAME}}"}`.
Two names are reserved for every tenant and role, `{{secret:obs-ak}}` and `{{secret:obs-sk}}`: the service fills them with
the key the call itself uses (the lab key of a test tenant) and only into a script the service renders itself: a POST
on ECS `cloudservers` (v1 and v1.1) names it in `server.user_data_script`, `{"name": "diskreader", "params":
{"bucket": ..., "region": ..., "volume": ..., "files": ...}}` (the reader of `awb/tcp/diskreader.py`, the only script
so far). Each parameter must match its pattern, the bucket must be one of the project (`<code>-...`) and the region the
one of the call. The service renders the script, fills the two names, base64-encodes the text into `server.user_data`
and sends it; the log line of that call says "user data with the tenant key". The service never fills a text the
caller wrote: `server.user_data_text` (sent as base64 `user_data`) takes no reference, a `user_data` that carries one
of the two names, plain or in base64, is refused and so are the names anywhere else (findings F1 and F2 of the
attacker review of 2026-11-26). An answer never carries the user data of a server (`user_data`,
`OS-EXT-SRV-ATTR:user_data`), so a session cannot read the script back with the key in it; a lab run (`lab_call`)
fills the same two names with the lab key of the tenant (the write key of a customer tenant) and shows a note of the
length of the data in its place. A leased key reads servers past the service, so a lease of a tenant (the lease
operation of the key service, the temporary key of Terraform and `awb cloud lease-check`) is refused while a server
made this way may still be there: the service notes each such create and asks the platform at the lease whether it is
gone (404 or `DELETED`). The answer comes back with every key and secret of the tenant taken out, also in base64 and JSON form. Every call is
logged on the owner's side without its path or body.

`awb tenant snapshot` records what runs on each tenant every day, so that `awb tenant at test-1 2026-09-30` tells
what ran on a given day and `awb tenant project tcp-xxxx` what a project created. Calls keep a pause between them and
wait when the API gateway says so (HTTP 429).

`awb tenant billed` pulls real billed costs from Financial Dashboard API v2 into the shared tenant cache.
The owner stores a Grafana service account token at `pass awb/fd/ALIAS` and runs the first pull with a
contract-scoped token: its first row pins the contract per alias. Other contracts are dropped and counted.
As ubuntu, schedule `awb tenant billed` daily after 03:30 UTC in ubuntu's cron, for example
`45 3 * * * /usr/local/bin/awb tenant billed` on a host whose cron timezone is UTC. The first three days of a
month also refresh the previous month. `awb tenant billed --hourly` pulls today and yesterday in Europe/Berlin
and retains at most 62 days. `--month YYYY-MM` or `--hourly --date YYYY-MM-DD` selects a historical period.
`awb cost billed [--tenant ALIAS] [--month YYYY-MM] [--project CODE] [--json]` reads only those caches.
Project totals include live and deleted resource ids in RESOURCES.md plus resources tagged in the latest
snapshot; the rest is one excluded total. `awb cost billed --resource ID --date YYYY-MM-DD --from 17:55 --to 18:05`
shows hourly product intervals, quantities, amounts and missing rows, including attached disks known at pull time.
Use `--project CODE --hourly` for every project resource. Times are Europe/Berlin; ambiguous clock-change times
are refused. Compute quantities determine runtime to the nearest second. GB and pc rows cover an hourly slot;
window amounts and quantities are prorated within each row. Missing rows are unknown usage, not proof of a stop:
the hourly dashboard is about 1.5 hours behind and daily rows arrive after the nightly run. Pull time is shown as
cache age; the API can reuse a daily response for one hour. Decimal money is rounded only for reports and the
month total is the sum of its rounded days. Cache files have mode 664 and contain no row names or tags.

`awb tenant cost` tells where the money on the test tenants goes: it prices the latest snapshot of each tenant with
one fetch of the live price API per region, PAYG and 730 hours a month (a stopped server counts its disks only, an
item without a price record is listed as no price and left out of the total, which says so), sums it per tenant,
kind and project and lists the top items and the waste (disks of stopped servers, disks not attached, elastic IPs
not bound). `awb tenant cost note test-1/ecs-3 "kept by the owner: an old-world project to move"` keeps an item out
of the waste. A tenant whose read key cannot list a kind is named as unknown, never priced as empty. The snapshot
runs the estimate after it and the console gets it as `cost` of GET /api/tenants.

Every create a project sends through the key service (`awb cloud call --role lab`, a recipe, a script of a lab run)
carries the tag `awb-project` with the project code. A service whose create body takes tags gets it there (ECS,
EVS, ELB, CCE nodes and the others in awb/tcp/projecttags.py); a VPC, a subnet and an elastic IP get one tag call right
after the create, and so does an ECS of the native API (`/v2.1/{project_id}/servers`, the ECS tag call). Any other
create passes and lands as one line in `<shared>/tenants/untagged-YYYY-MM.tsv`; the answer says `project tag
inline|after|untagged`. A lab run is stricter: a create whose body carries `awb-project` of another project is
refused, and so is an ECS create whose body cannot take the tag (no `{"server": {...}}`); nothing is sent. `awb tenant tags [ALIAS]` lists the servers, disks and elastic IPs
per project tag from the latest snapshot, the creates without the tag this month and the quotas now (ECS limits and
VPC quotas, read with the read key), each at 80 % marked near and at 100 % full.

### Windows lab machines (WL)

Your own Windows laptop serves as a lab (Hyper-V, an evaluation VM of Windows Server, no customer data). Part 1 is
the bookkeeping on your side: which machines exist and when a project may use one. Part 2 prints the scripts that
connect the laptop to the host and take it off again. Part 3 gives a project session its commands there. Part 4 is
the lab broker, which holds the key and is the one way to the laptop.

The lab is for tests with a Windows machine, that is for a Windows migration. A project spawned with Windows and a
migration in its tags or goal (the tags `windows` and `migration`, or the words of the goal) gets one sentence about
the lab machines in its CLAUDE.md. Every other project may use them on your word but is never offered them: its
CLAUDE.md names no winlab and the work rules say "winlab: only for a Windows migration test; do not offer it
otherwise."

```bash
awb winlab add lab-1 --port 2201 --fingerprint SHA256:... --no-customer-data
awb winlab open lab-1 --minutes 60 --project tcp-xxxx
awb winlab open lab-1 --until 18:30 --project tcp-xxxx   # the same project again: extends its open window to 18:30 local time
awb winlab extend lab-1 --minutes 120        # the open window now ends 120 minutes from now (never earlier than it did)
awb winlab close lab-1                       # before its end
awb winlab remove lab-1
awb winlab list                              # machines, their window, its end (in a project session: the windows open for it, from the broker)
```

A machine has a name, a loopback port (1024 to 65535), the pinned fingerprint of its host key (`SHA256:` and 43
base64 characters, as `ssh-keygen -l` prints it), the day of its registration and your statement that it holds no
customer data; add refuses a machine without `--no-customer-data`. A window gives one project one machine for 1 to
1440 minutes and ends by itself at its end. A machine has at most one open window. `open` for the project that
holds it extends that window: the new end is now plus `--minutes`, or `--until HH:MM` (local host time, today, or
tomorrow when that time has passed), never earlier than the end it had. The window of another project is still refused until you close it. An hour before the end the
project session is told once at its next prompt, with the open command to ask you for, and `awb owner status`
shows the window as "ends in N min" with `awb winlab open NAME --minutes 60 --project CODE`. The registry is `winlab.json`
beside keys.conf, mode 600. add, open, close and remove are refused for the work user; add and remove
are refused in an assistant session too, while open and close run there (UX10); list is for
anyone. The later parts ask `winlab.allowed(NAME, CODE)`, which returns the machine or refuses with one
line: an unregistered machine, no window, an expired window or another project. `awb owner status` lists every open
window with its project, its end and `awb winlab close NAME`, and every registered machine that is online (its
tunnel answers on its loopback port). `--account NAME` on add names the laptop's account when bootstrap made another
one than `awblab`.

Part 2 connects the laptop to the host. You print a setup script on the host, copy it to the laptop and run it there
once as administrator, sitting at the laptop; the removal script takes everything away again.

```bash
awb winlab bootstrap lab-1 --host-key host.pub --tunnel USER@HOST --port 2201 > setup.ps1
awb winlab remove lab-1 > remove.ps1         # forgets lab-1; --script-only keeps it registered
```

On the laptop: `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1`. It installs the OpenSSH server
and lets it listen on 127.0.0.1 port 22 only (stopped before its first start, the listen addresses read back after
it), creates a local account (`awblab` unless `--account` names another) in Administrators and Hyper-V Administrators with a random
password nobody keeps, writes the key of `--host-key` to `%ProgramData%\ssh\administrators_authorized_keys`
(inheritance off, Administrators and SYSTEM) and allows key logon only. It generates the laptop's own tunnel key and
the visible scheduled task `awb-winlab-tunnel`: from boot on it holds `ssh -N -R 127.0.0.1:PORT:127.0.0.1:22
USER@HOST` (ServerAliveInterval 30, ServerAliveCountMax 3, ExitOnForwardFailure yes, the host's server key of
`--server-key` pinned) and starts it again 30 seconds after it ends. Every step checks its own result and stops with
one line naming the step. At the end it prints the fingerprint for `awb winlab add` and the tunnel public key with
the line for the tunnel account's authorized_keys on the host. A machine already registered needs no `--port`.
Everything lives under the name awb-winlab; `%ProgramData%\awb-winlab\state.txt` records what was there before, so
`remove.ps1` removes the task, the service (or puts back the OpenSSH server as it was), the keys, the account and its
profile folder and `%ProgramData%\awb-winlab`. It removes nothing setup did not make.

Part 3 is the session side. A project session that holds the open window of a machine runs commands, long jobs and
file transfers there; each command asks `winlab.allowed(NAME, CODE)` first.

```bash
awb winlab run lab-1 -- Get-VM                    # prints the output; exit 3 when the command failed there
awb winlab run lab-1 --vm lab-vm -- hostname      # inside the VM, with PowerShell Direct
awb winlab start lab-1 -- .\build-lab.ps1         # a long job; prints its id wl-<date>-<time>-xxxx
awb winlab status JOB                             # running, ended with exit code N, stopped, lost, machine offline
awb winlab log JOB                                # the new part of its log
awb winlab wait JOB [--timeout 3600]               # blocks (asks after 2 s, 4 s, up to 60 s) and prints the end state
awb winlab stop JOB
awb winlab put winlab-out/setup-lab.ps1 C:\lab\setup-lab.ps1
awb winlab put winlab-out/setup-lab.ps1 C:\lab\setup-lab.ps1 --overwrite   # replaces the file there
awb winlab get C:\lab\export.csv results/export.csv --overwrite            # replaces results/export.csv
awb winlab get C:\lab\export.csv results/export.csv
awb winlab get C:\lab\disk.vhdx results/disk.vhdx           # an image, a program, a backup: the same get, no check
awb winlab get C:\lab\disk.vhdx results/disk.vhdx --recheck   # the SHA-256 compare and the publication of a kept part, no transfer
```

A refusal sends nothing. A command goes out only after three guards: it is not empty, it has no `{{secret:NAME}}`
reference (nothing is filled in for a lab machine) and nothing in it names the machine's own state (`awb-winlab`, the
ssh keys). No name check and no secret check runs on a command, a put or a get (the owner's word of 2026-11-25: WinLab
is used only from his lab laptop and only with images downloaded from the internet, so no customer data can be there;
the checks of the main work laptop, `awb intake`, `awb doc fetch` and the exit check of the push, stay). A VM guest password never passes through the session:
`--vm VM` names the VM and the laptop reads the credential from `%ProgramData%\awb-winlab\vm\<VM>.txt`, which you
write there yourself (first line the user, second the password; the command refuses a file anyone but
Administrators and SYSTEM may read). The CLIXML progress records of powershell.exe are stripped, the output lands
by the rules of `awb paste --own` and the command, its output, every job and every transfer go into
`notes/winlab/` of the project. A job is a scheduled task of the laptop's account with its log on the laptop, so it
goes on while the tunnel is down: status then says machine offline and running again once the tunnel is back.
status, log, wait and stop keep working for a job that started inside a window after the window ended; a new
command, put and get do not. A command with no Format or Out command gets `| Out-String -Width 200` after it
(several statements go in a script block first), so a bare `Get-VM` prints its table. Output over 2000 lines is
printed as the first 1000 and the last 1000 lines with one line that says how many were cut; the log of the project
keeps all of it. A VM whose credential file is missing gives one plain line. put and get never replace a file unless
you give `--overwrite` (get replaces it only once the SHA-256 matched). A run, a transfer and a job record their start
and their end time.
put takes files of the project's `winlab-out/` only and sends them as they are; get writes into
the project, never into `input/`. Both go in chunks, resume when you run the same command again after a break,
compare the SHA-256 of both sides at the end and never overwrite a file. They are built for 12 GB; a large file
belongs in a folder git ignores. Disk images (.vhdx, .vhd, .qcow2, .vmdk, .img, .iso, .raw, .ova in any case) and
the `.awb-part` of a transfer are ignored in every project by its `.git/info/exclude` (spawn and `awb projects sync`).
A received file is published once its SHA-256 matches the one of the machine; a part that does not match is deleted.
A complete transfer is never deleted for a SHA-256 the machine did not give: a SIGTERM or SIGHUP after the transfer
(a tool call that ran out of time) ends the get with one state line ("kept unpublished as FILE.awb-part ...
--recheck compares it without a transfer"), a get killed outright leaves its file under the private name its record
names; the next get or `awb winlab get WINPATH FILE --recheck` gives it its part name back, compares its SHA-256
with the one the get recorded and publishes it, without a transfer and without an open window; a part changed since
the get is deleted. A machine that gave no SHA-256 keeps the part for the same get again, without a transfer.
A large image is published right after the SHA-256 compare: no request goes to a check and no progress line is
printed. A get or put writes a start (events kind `transfer`, id `MACHINE@PID`) when it begins and an end with its
outcome (`ok`, `red` or `stopped`): "Running now" of `awb owner status` shows it and `sudo awb deploy` refuses while
it runs (see "Deploy" in [operations.md](operations.md#deploy)). A transfer that is killed outright leaves its start; once its process is gone the deploy calls
it stale and goes on.

Part 4 is the lab broker (`awb winlab serve`, `awb/tcp/winbroker.py`), the same pattern as the key service: a
project session never holds a key. Root runs it under its unit (the seal part of the steering session: the system
user `awb-winlab` without a shell that the laptop's tunnel logs on to, the sshd Match block that lets it forward its
ports and nothing else, the unit and the nftables rule that keeps the work user off the ports). It holds the host's
private key for the lab machines (`/etc/awb/winlab/id_ed25519`, mode 600; give its `.pub` to `bootstrap --host-key`)
and serves the session commands on `/run/awb-winlab/broker.sock`. Before every call it checks, from your registry
(`AWB_KEYS_CONF` of the unit), the key token of the session's project folder, the machine, the window and its end,
and then the laptop's host key: ssh-keyscan reads it on the port, only the key with the pinned fingerprint goes into
the broker's own known_hosts and ssh connects with `StrictHostKeyChecking=yes` against it alone. No window, an
expired window, another project, an unregistered machine and a host key that does not match the pin are refused
with one line each, and nothing is sent. Each call is one ssh connection to the laptop's account; powershell.exe
reads the script from its standard input. The bytes of a put go with sftp (the same key, known_hosts and options)
to `PART.awb-chunk` next to the part, as `/C:/dir/file`; then one ssh run checks the offset, appends that file to the
part once and deletes it. Windows sshd stops passing standard input to powershell.exe after about 2 KB, so no data
goes that way. A get still comes back on standard output. A session on a sealed host reads no registry of
its own, so its window check and the machine of a put or get come from the broker too. Without the broker's socket
the commands refuse with "no transport".
