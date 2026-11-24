"""The project kind ask: one standing session where the owner asks short questions about how TCP behaves.

Only the owner types there and no customer material ever enters it. So, in an ask project:

- the name check of the hooks does not run (prompt, post-write, the draft gate of the stop hook, the start's
  withholding of SCOPE.md, STATE.md and OPEN.md); the secret check stays everywhere;
- the tool guard lets a shell path it cannot read pass (UNREADABLE); every refused place still refuses;
- the commit gate of the project repository looks for secrets only;
- material is refused at every door (awb import, the paste door, awb paste, awb inbox take, awb xchg, awb bucket
  pull, awb fetch) with MATERIAL_LINE;
- the test tenants are reached directly, read and lab role, lab runs too (owner's word of 2026-11-24); a customer
  tenant, a read-only tenant and another project's code are refused.

The kind that lifts a check comes only from the owner's side, never from a file the session can write: the owner's
grant (`awb projects kind CODE ask`, <vault>/grants.json) records kind ask and the key token of the project folder
(K4). The owner's processes read the grant; the work user asks the key service (op `kind` on its call socket), which
answers for the token the folder holds. A row of projects.tsv or a kind line of SCOPE.md that says ask lifts nothing:
those only add refusals (`refuses_material`). When the answer cannot be had (no token, no key service, an error)
the project counts as not ask and every check runs.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

from awb import codes, config

ASK = "ask"
MATERIAL_LINE = ("%s is an ask project: no material ever enters it, only the owner's own questions; take the "
                 "material into a query or a project")
TENANT_LINE = ("%s is an ask project: it reaches the test tenants only (read and lab, owner's word of 2026-11-24); "
               "a customer tenant, a read-only tenant and the code of another project are refused")
KIND_TIMEOUT = 3.0
"""Seconds a hook waits for the key service's answer to `kind`; then the project counts as not ask."""


def _code_of(root) -> str | None:
    if root is None:
        return None
    name = Path(root).name
    return name if codes.is_project_code(name) and name.startswith("tcp-") else None


def granted_ask(p: config.Paths, code: str, token: str | None) -> bool:
    """True when the owner's grant (readable here) names `code` kind ask and recorded exactly `token` for it."""
    from awb import grants

    if not (isinstance(token, str) and grants.TOKEN_RE.match(token)):
        return False
    if grants.kind(p, code) != ASK:
        return False
    recorded = grants.ask_tokens(p).get(code)
    return isinstance(recorded, str) and hmac.compare_digest(recorded.encode("ascii"), token.encode("ascii"))


def _active_row(p: config.Paths, code: str, root: Path) -> bool:
    from awb import projects

    try:
        rows = projects.load(p)
    except projects.ProjectError:
        return False
    real = os.path.realpath(root)
    return any(r.code == code and r.state in projects.LIVE_STATES and os.path.realpath(r.path) == real for r in rows)


def is_ask(root, p: config.Paths | None = None, *, sock: Path | None = None) -> bool:
    """True only when the owner granted the project of the folder `root` the kind ask for the key token it holds.
    Read from the owner's grant where it can be read, else from the key service of a sealed host (`sock` names
    another socket, for the tests). Never raises: any doubt is False,
    and then every check runs."""
    try:
        code = _code_of(root)
        if code is None:
            return False
        from awb import grants, projects

        token = projects.read_key_token(Path(root))
        if token is None:
            return False
        p = p or config.paths()
        if not config.is_work_user() and grants.readable(p):
            return granted_ask(p, code, token) and _active_row(p, code, Path(root))
        from awb import receipt, toolguard

        if sock is None and not toolguard._sealed():
            # the key service of the seal answers for the work user (its socket path is fixed there, never one of
            # the environment); a host without the seal has none: every check runs
            return False
        answer = receipt.keys_call({"op": "kind", "token": token}, sock=sock, timeout=KIND_TIMEOUT)
        return answer.get("ok") is True and answer.get("kind") == ASK and answer.get("code") == code
    except Exception:
        return False


def _scope_kind(root: Path) -> str | None:
    try:
        text = (Path(root) / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if line.startswith("- kind:"):
            return line.split(":", 1)[1].strip()
    return None


def refuses_material(p: config.Paths, code: str, root: Path | None = None) -> bool:
    """True when material must not enter the project `code`: its row, its SCOPE.md or the owner's grant says ask, or
    the trusted answer of `is_ask` does. Any one is enough, since a refusal lifts no check."""
    from awb import grants, projects

    try:
        row = next((r for r in projects.load(p) if getattr(r, "code", None) == code), None)
    except projects.ProjectError:
        row = None
    if getattr(row, "kind", None) == ASK:
        return True
    try:
        if grants.readable(p) and grants.kind(p, code) == ASK:
            return True
    except Exception:
        pass
    path = getattr(row, "path", None)
    folder = Path(root) if root is not None else (Path(path) if isinstance(path, str) and path else None)
    if folder is not None and _scope_kind(folder) == ASK:
        return True
    return folder is not None and is_ask(folder, p)


def folder_refuses(root: Path | None, p: config.Paths | None = None) -> bool:
    """`refuses_material` for the project folder a session works in."""
    code = _code_of(root)
    if code is None:
        return False
    try:
        return refuses_material(p or config.paths(), code, Path(root))
    except Exception:
        return False


def has_material(folder: Path) -> bool:
    """True when input/ of the project folder holds anything but its .gitkeep, or cannot be read (fails closed)."""
    try:
        with os.scandir(Path(folder) / "input") as it:
            return any(e.name != ".gitkeep" for e in it)
    except FileNotFoundError:
        return False
    except OSError:
        return True
