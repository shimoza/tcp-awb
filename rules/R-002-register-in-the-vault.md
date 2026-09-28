# R-002 The register lives in the vault

The only file where a written form and its code meet is `vault/register.tsv`. A working session never reads it. On a
host where the vault is not sealed yet, the rule is a promise and `build/DECISIONS.md` says so.

Reason: a session that holds a mapping can write it into a transcript, a memory or a project. Enforced by: the seal
(decision 1), the name check that returns positions only, the strict register format that refuses free text.
