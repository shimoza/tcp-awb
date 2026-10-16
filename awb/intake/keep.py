"""The keep list of the intake: phrases the candidate rules never take for a name, kept on the vault side
(`Paths.keep_list`, sealed through the vault daemon when the vault is encrypted). One part of the intake package
(F1 step 8); the package text is awb/intake.py. Nothing here echoes a phrase.
"""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

from awb import config, normalize
from awb.intake.engine import IntakeError, keep_key


# --------------------------------------------------------------------------- the keep list


def _sealed_keep(p: config.Paths) -> Path:
    return p.keep_list.with_name(p.keep_list.name + ".gpg")


def _vault_call(p: config.Paths, op: str, **fields) -> dict:
    """One request to the vault daemon for the keep list. Errors carry no value."""
    from awb import vault

    try:
        return vault.admin_call(op, p.admin_sock, **fields)
    except vault.VaultLocked:
        raise IntakeError("keep list: the vault is locked, run awb vault unlock") from None
    except vault.VaultUnavailable:
        raise IntakeError("keep list: no vault daemon") from None
    except vault.VaultBusy:
        raise IntakeError("keep list: the vault daemon is reloading, try again") from None
    except vault.VaultError:
        raise IntakeError("keep list: the vault daemon refused the request") from None


def _read_keep(p: config.Paths) -> str:
    """The text of the keep list: the sealed copy through the vault daemon when there is one, plus a plaintext
    copy when there is one. Missing -> empty."""
    parts: list[str] = []
    sealed = _sealed_keep(p)
    if sealed.exists():
        answer = _vault_call(p, "open_file", path=str(sealed))
        try:
            parts.append(base64.b64decode(answer.get("data") or "", validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise IntakeError("keep list: the vault daemon answered in an unexpected form") from None
    try:
        parts.append(p.keep_list.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        raise IntakeError("keep list: the file is not UTF-8 text") from None
    return "\n".join(parts)


def _parse_keep(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        phrase = " ".join(line.split())
        key = keep_key(phrase)
        if phrase and key not in seen:
            seen.add(key)
            out.append(phrase)
    return out


def load_keep(p: config.Paths) -> list[str]:
    """The phrases of the keep list (`Paths.keep_list`, vault side), in the order they were added. One phrase per
    line. When the vault is encrypted the sealed copy is read through the vault daemon. Missing -> []."""
    return _parse_keep(_read_keep(p))


def _write_private(path: Path, text: str) -> None:
    """Atomic write, file mode 600. The folder is created with mode 700 when it is missing."""
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def keep_phrase(p: config.Paths, phrase: str) -> tuple[bool, int]:
    """Add one phrase to the keep list. Returns (added, phrases in the list); added is False when the phrase was
    kept before. Any phrase is taken, only an empty one is an error. When the vault is encrypted the list is
    sealed again through the vault daemon and no plaintext copy stays behind. Nothing here echoes the phrase."""
    added, count = keep_phrases(p, [phrase])
    return added == 1, count


def keep_phrases(p: config.Paths, phrases: list[str]) -> tuple[int, int]:
    """Add several phrases to the keep list in one write and, when the vault is encrypted, one seal (the review
    of a blocked intake keeps hundreds at once). Returns (added, phrases in the list). A phrase kept before is
    left as it is; an empty one refuses the whole call before anything is written. Nothing here echoes a phrase."""
    clean = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            raise IntakeError("keep list: the phrase must be text")
        phrase = " ".join(normalize.normalize(phrase).text.split())
        if not phrase:
            raise IntakeError("keep list: the phrase is empty")
        clean.append(phrase)
    kept = load_keep(p)
    keys = {keep_key(k) for k in kept}
    added = 0
    for phrase in clean:
        if keep_key(phrase) not in keys:
            kept.append(phrase)
            keys.add(keep_key(phrase))
            added += 1
    if not added:
        return 0, len(kept)
    encrypted = p.register_encrypted.exists() or _sealed_keep(p).exists()
    try:
        before = p.keep_list.read_bytes() if encrypted else None
    except FileNotFoundError:
        before = None
    _write_private(p.keep_list, "".join(k + "\n" for k in kept))
    if encrypted:
        try:
            _vault_call(p, "seal_file", path=str(p.keep_list))
        except BaseException:
            # no new plaintext stays behind; a plaintext copy that was there before is put back as it was
            if before is None:
                p.keep_list.unlink(missing_ok=True)
            else:
                _write_private(p.keep_list, before.decode("utf-8"))
            raise
    return added, len(kept)
