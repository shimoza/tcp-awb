---
name: azure-to-tcp
description: Plan the move of Azure virtual machines to T Cloud Public (TCP) in phases, from the customer's inventory to a reviewed migration plan with a live price estimate. Use when a customer on Azure asks what their machines would be on TCP, what it costs or how to move.
---

# Azure to TCP in phases

You are the orchestrator. You run the deterministic steps yourself with `awb migrate` and hand each piece of
judgement to a worker in a fresh context. The state lives in `migration/state.json` of the project, so a run
resumes where it stopped. Never skip a gate and never write a phase's artifact yourself when the phase names a
worker.

## Before the first phase

1. Work inside a Workbench project (`awb spawn`). The customer's inventory reaches `input/` only through
   `awb intake`, as a sanitised copy with codes instead of names.
2. `awb migrate init --from azure`.

## The loop

1. `awb migrate next` prints the phase, its phase file, its worker, what it reads and what it must write.
2. Run the phase's commands yourself when the phase file names them for the orchestrator (`awb migrate inventory`,
   `awb migrate map`).
3. Dispatch the named worker (`awb:migration-worker-files` or `awb:migration-worker-shell`) with the phase file's
   path and the project folder. Give it nothing else: the phase file and the artifacts carry the context.
4. `awb migrate done PHASE`. When the gate lists problems, send them back to the same kind of worker, then run
   the gate again.
5. Repeat until `awb migrate next` says every phase is done.

`awb migrate status` shows every phase. A phase whose files changed after it was done turns stale, and so does
every phase after it: run them again from there.

## Rules for every phase

- The mapping and every number are code: `awb migrate map`, `awb migrate estimate`, `awb calc` (R-005). Never
  correct a flavor or a total by hand. Change the input and run the step again.
- A price record does not prove that a flavor can be ordered. The plan says so or checks it on a test tenant.
- Facts about TCP come from `awb kb find`, facts about Azure from Microsoft's documentation, both with a source.
