---
name: test-tenant
description: Work on the TCP test tenants - see what runs, ran or was created for a project, call the TCP API through the key service (read or build) and tag new lab resources so they are tracked
---

# Test tenants

The Workbench keeps a daily snapshot of the servers, disks and elastic IPs of every test tenant, and a history of
what appeared, changed and went.

- Every tenant and what runs there now: `awb tenant list`
- One tenant now, oldest first, with age, project, expiry and flags: `awb tenant now ALIAS`
- What happened and when: `awb tenant history ALIAS [--project tcp-xxxx] [--since YYYY-MM-DD]`
- What ran on a day: `awb tenant at ALIAS YYYY-MM-DD`
- Everything a project ran on every tenant: `awb tenant project tcp-xxxx`

## Calling the TCP API

You never hold a key. The key service of the owner signs every call:

- The tenants, their key roles and the names of their secrets: `awb cloud tenants`
- Look: `awb cloud call GET vpc "/v1/{project_id}/vpcs" --tenant ALIAS`
- Build: `awb cloud call POST ecs "/v1/{project_id}/cloudservers" --tenant ALIAS --role lab --body server.json`.
  A write runs for the project of the working folder and needs an active project code.
- A password goes into a body only as the whole value of a password field: `{"admin_pass": "{{secret:NAME}}"}`.
  The answer comes back with every key and secret taken out.
- List every resource you create in RESOURCES.md with its id, type, region, expiry and state.
- What you learn live (a call that behaves unlike the docs, a limit, an error and its fix) goes into the knowledge
  base at once: `awb kb add --grade live --class api --source "live call on ALIAS, project tcp-xxxx, DATE" ...`.

## Tags

Every resource you create for a project carries two tags: `awb-project` with the project code (tcp-xxxx) and
`awb-expiry` with an ISO date. A resource without them shows up as "no project" and is the first one to ask about.
Snapshots are taken on the owner side with a read-only key; ask the owner when the last one is old.
