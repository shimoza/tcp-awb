---
name: test-tenant
description: Show what runs, ran or was created for a project on the TCP test tenants, and tag new lab resources so they are tracked
---

# Test tenants

The Workbench keeps a daily snapshot of the servers, disks and elastic IPs of every test tenant, and a history of
what appeared, changed and went.

- Every tenant and what runs there now: `awb tenant list`
- One tenant now, oldest first, with age, project, expiry and flags: `awb tenant now ALIAS`
- What happened and when: `awb tenant history ALIAS [--project tcp-xxxx] [--since YYYY-MM-DD]`
- What ran on a day: `awb tenant at ALIAS YYYY-MM-DD`
- Everything a project ran on every tenant: `awb tenant project tcp-xxxx`

Every resource you create for a project carries two tags: `awb-project` with the project code (tcp-xxxx) and
`awb-expiry` with an ISO date. A resource without them shows up as "no project" and is the first one to ask about.
Snapshots are taken on the owner side with a read-only key; ask the owner when the last one is old.
