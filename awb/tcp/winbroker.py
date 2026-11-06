"""The lab broker: the root owned connector to the owner's Windows lab laptops (task WL part 4 of 4, B-038).

    awb winlab serve [--socket PATH] [--key PATH]      run by root under its unit (seal/awb-winlab.service)

Same pattern as the key service (awb/tcp/keys.py): a project session never holds a key. The key here is the host's
private SSH key for the lab machines (default /etc/awb/winlab/id_ed25519, root's, mode 600; its .pub is what
`awb winlab bootstrap --host-key` puts on the laptop). The broker never reads it: ssh does, as root. A session asks
the broker over its Unix socket (default /run/awb-winlab/broker.sock, mode 660, the group of the unit) and gets
back what the machine answered, never a key, a path of the key or a host key.

A laptop is reached only through its own tunnel on a loopback port of this host (127.0.0.1:PORT, part 2) and only
while the owner opened a window on it for one project (part 1). Before every call, whatever the op, the broker
checks against the owner's registry (winlab.json beside keys.conf, AWB_KEYS_CONF of the unit) and refuses with one
line, nothing sent:

- a call without the key token of its project folder, or with the token of another folder (K4 of the key service:
  the project is the one whose token the call presents, the owner's grant or the folder's own .git/awb-key-token)
- an unregistered machine, no window, an expired window, a window of another project (winlab.allowed)
- a host key that does not match the pinned fingerprint: ssh-keyscan reads the machine's keys, the one whose SHA256
  fingerprint is the pin goes into a known_hosts file of the broker's own and ssh connects with
  StrictHostKeyChecking=yes against it alone, so the key of the connection that carries the command is the pinned one

The ops, one JSON object per line and one answer per connection (put_chunk sends `length` bytes after its line,
get_chunk answers `length` bytes after its line):

    check       the window is open for the caller's project: {"machine": {"name", "end"}}
    windows     the machines whose window is open for the caller's project: {"machines": [name, ...]}
    run         {"text", "timeout"} -> {"exit", "stdout", "stderr"}
    put_chunk   {"path", "offset", "length"} + bytes -> {"size"}
    get_chunk   {"path", "offset", "size"} -> {"length"} + bytes
    stat        {"path", "digest"} -> {"stat": {"size", "mtime"[, "sha256"]} or null}

A refusal answers {"ok": false, "kind": "refused", "error": LINE}; a machine that cannot be reached "offline"; a
put at another offset than the size of the file "offset" with the size; a step that failed on the machine "failed".
awb/winlabsession.py (connect, BrokerTransport) is the client. Every call is one line in the journal: the time, the
peer uid, the op, the machine, the project and the outcome; never a text, a path or a byte of a file.

On the machine every op is one ssh connection to the account of the machine (awblab unless `awb winlab add
--account` named another): powershell.exe reads the script from its standard input (a line with the length of the
script, the script in UTF-8), so no text of a session lands on a command line. A command that ends with exit code
255 on the machine reads as offline, the code ssh gives for its own failures.

The bytes of a put never go on that standard input: Windows sshd stops passing input to powershell.exe after about
2 KB (live check of 2026-11-06, B-039). put_chunk writes the chunk to a file of mode 600 in the broker's own private
folder, puts it with `sftp -b -` (the same key, known_hosts and options as ssh) to PART.awb-chunk next to the part
(the path as /C:/dir/file), then one ssh run checks the offset, appends PART.awb-chunk to the part, deletes it and
prints the size. The local file is removed in every case; a PART.awb-chunk left by a broken call is overwritten by
the next put, never appended.
Standard library only, besides the ssh, sftp and ssh-keyscan programs of the host.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import config, winlab
from awb.winlab import WinlabError

SOCKET_ENV = "AWB_WINLAB_SOCKET"
DEFAULT_SOCKET = Path("/run/awb-winlab/broker.sock")
KEY_ENV = "AWB_WINLAB_KEY"
DEFAULT_KEY = Path("/etc/awb/winlab/id_ed25519")
OPS = ("check", "windows", "run", "put_chunk", "get_chunk", "stat")
MAX_REQUEST = 16 * 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
"""The most of each output stream of a command that comes back; the session cuts at 8 MB anyway."""
READ_TIMEOUT = 30.0
SEND_TIMEOUT = 120.0
MAX_CONNECTIONS = 16
RUN_TIMEOUT = 300
RUN_TIMEOUT_MAX = 86400
CHUNK_TIMEOUT = 300
DIGEST_TIMEOUT = 3600
"""Seconds the SHA-256 of a file of 12 GB may take on the machine."""
KEYSCAN_TIMEOUT = 10
CONNECT_TIMEOUT = 10
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
WINPATH_RE = re.compile(r"^[A-Za-z]:\\[^\x00-\x1f\"*?<>|:]{1,256}$")
"""A whole path with a drive letter; no second colon (no alternate data stream), no wildcard."""
ALGORITHMS = {"ssh-ed25519": "ssh-ed25519", "ecdsa-sha2-nistp256": "ecdsa-sha2-nistp256",
              "ecdsa-sha2-nistp384": "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521": "ecdsa-sha2-nistp521",
              "ssh-rsa": "rsa-sha2-512,rsa-sha2-256"}
"""The host key types ssh-keyscan reports and the HostKeyAlgorithms that make ssh use that key."""
HOSTKEY_FAILED_RE = re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED"
                               r"|No \S+ host key is known", re.I)
UNREACHABLE_RE = re.compile(r"ssh: connect to host|connection refused|timed out|connection closed|connection reset"
                            r"|broken pipe|no route to host|network is unreachable|could not resolve"
                            r"|permission denied \(publickey", re.I)
"""What ssh says, through sftp, when the machine cannot be reached or did not take the key."""
CHUNK_SUFFIX = ".awb-chunk"
SFTP_PATH_RE = re.compile(r'^/[A-Za-z]:(/[^\x00-\x1f\x7f"\\/*?<>|:]+)+$')
"""A Windows path as sftp writes it: /C:/dir/file, no empty part, nothing a batch line of sftp reads its own way."""
LOCAL_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
NO_SFTP_PATH = ("a path on the machine goes to sftp as /C:/dir/file and this one cannot be written that way: "
                "nothing was sent")
UNBOUND_LINE = "the call carries no key token of its project folder (.git/awb-key-token): nothing was sent"
FOREIGN_LINE = "the key token of the call is not the one of %s: nothing was sent"
MISMATCH = "the host key of %s does not match its pinned fingerprint: nothing was sent"
NO_KEY = "the lab broker has no usable key: %s must be a regular file of its own user, mode 600"

BOOT = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { }
$awbIn = [Console]::OpenStandardInput()
$awbBuf = New-Object IO.MemoryStream
$awbIn.CopyTo($awbBuf)
$awbAll = $awbBuf.ToArray()
$awbBuf.Dispose()
$awbCut = [Array]::IndexOf($awbAll, [byte]10)
$awbLen = [int][Text.Encoding]::ASCII.GetString($awbAll, 0, $awbCut)
$AwbScript = [Text.Encoding]::UTF8.GetString($awbAll, $awbCut + 1, $awbLen)
Remove-Variable awbAll, awbBuf, awbIn
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. ([ScriptBlock]::Create($AwbScript))
exit $LASTEXITCODE
"""
"""What powershell.exe runs: the script from standard input, dot-sourced. No data chunk comes this way."""
REMOTE = ("powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
          + base64.b64encode(BOOT.encode("utf-16-le")).decode("ascii"))


class Offline(Exception):
    """The machine cannot be reached or the connection dropped; one line."""


class Failed(Exception):
    """A step of the broker's own script failed on the machine; one line."""


class OffsetError(ValueError):
    """A chunk at another offset than the size of the file there."""

    def __init__(self, size: int):
        super().__init__("the offset is not the size of the file on the machine (%d bytes)" % size)
        self.size = size


def _journal(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _ps(value: str) -> str:
    return "'%s'" % value.replace("'", "''")


def fingerprint(blob_b64: str) -> str:
    """SHA256:... of a public key blob, as ssh-keygen -l prints it."""
    digest = hashlib.sha256(base64.b64decode(blob_b64, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def scanned_keys(text: str) -> list[tuple[str, str]]:
    """(type, base64) of every host key line ssh-keyscan printed; comments and other lines are left out."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not parts[0].startswith("#") and parts[1] in ALGORITHMS:
            try:
                base64.b64decode(parts[2], validate=True)
            except ValueError:
                continue
            out.append((parts[1], parts[2]))
    return out


def check_key(path: Path) -> None:
    """The key file is a regular file (no link) of this process's user that nobody else may read."""
    try:
        st = os.lstat(path)
    except OSError:
        raise WinlabError(NO_KEY % path) from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise WinlabError(NO_KEY % path)


def run_command(argv: list[str], data: bytes, timeout: float) -> tuple[int, bytes, bytes]:
    """ssh or ssh-keyscan with `data` on standard input; raises subprocess.TimeoutExpired, OSError."""
    r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False,
                       env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    return r.returncode, r.stdout, r.stderr


def _ssh_reason(text: str, name: str, port: int) -> str:
    low = text.lower()
    if "connection refused" in low:
        return "%s does not answer on its port %d: its tunnel is down" % (name, port)
    if "timed out" in low:
        return "%s did not answer on its port %d in time" % (name, port)
    if "permission denied" in low:
        return "%s did not take the host's key (its account or administrators_authorized_keys)" % name
    if "closed" in low or "reset" in low or "broken pipe" in low:
        return "the connection to %s dropped" % name
    return "ssh to %s failed" % name


def _winpath(value) -> str:
    if not isinstance(value, str) or not WINPATH_RE.match(value):
        raise WinlabError("a path on the machine is a whole path with a drive letter, without wildcards")
    return value


def sftp_path(path: str) -> str:
    """C:\\dir\\file as sftp writes it on Windows: /C:/dir/file. Raises WinlabError for a path that cannot be written
    that way (an empty part, a trailing backslash, a control character)."""
    out = "/" + path.replace("\\", "/")
    if not SFTP_PATH_RE.match(out):
        raise WinlabError(NO_SFTP_PATH)
    return out


def _count(value, what: str, top: int) -> int:
    if type(value) is not int or not 0 <= value <= top:
        raise WinlabError("%s is a whole number from 0 to %d" % (what, top))
    return value


# --------------------------------------------------------------------------- the Transport of B-037 over ssh


class SshTransport:
    """The Transport of awb/winlabsession.py (part 3) over ssh to 127.0.0.1:PORT, the tunnel of the machine: run,
    put_chunk, get_chunk, stat, close. Every method raises Offline when the machine cannot be reached and WinlabError
    when its host key is not the pinned one. Only the broker builds it: it needs the key."""

    def __init__(self, broker: "Broker", name: str, machine: dict):
        self.broker = broker
        self.name = name
        self.port = machine["port"]
        self.machine = machine
        self.account = machine.get("account") or winlab.ACCOUNT

    def _options(self, known: Path, key_type: str) -> list[str]:
        options = {
            "BatchMode": "yes", "IdentitiesOnly": "yes", "IdentityAgent": "none", "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(known), "GlobalKnownHostsFile": "/dev/null",
            "HostKeyAlias": "awb-winlab-%s" % self.name, "HostKeyAlgorithms": ALGORITHMS[key_type],
            "CheckHostIP": "no", "UpdateHostKeys": "no", "PasswordAuthentication": "no",
            "KbdInteractiveAuthentication": "no", "ForwardAgent": "no", "ForwardX11": "no",
            "ClearAllForwardings": "yes", "ControlMaster": "no", "ControlPath": "none", "RequestTTY": "no",
            "ConnectTimeout": str(CONNECT_TIMEOUT), "ServerAliveInterval": "15", "ServerAliveCountMax": "4",
            "LogLevel": "ERROR"}
        argv = []
        for k, v in options.items():
            argv += ["-o", "%s=%s" % (k, v)]
        return argv

    def argv(self, known: Path, key_type: str) -> list[str]:
        return (["ssh", "-F", "none", "-T", "-i", str(self.broker.key), "-p", str(self.port)]
                + self._options(known, key_type) + ["%s@%s" % (self.account, winlab.LOOPBACK), REMOTE])

    def sftp_argv(self, known: Path, key_type: str) -> list[str]:
        """sftp with the options of argv(): the batch on its standard input (`-b -`), the port with -P."""
        return (["sftp", "-F", "none", "-b", "-", "-i", str(self.broker.key), "-P", str(self.port)]
                + self._options(known, key_type) + ["%s@%s" % (self.account, winlab.LOOPBACK)])

    def _ssh(self, script: str, timeout: float = CHUNK_TIMEOUT) -> tuple[int, bytes, bytes]:
        known, key_type = self.broker.pin(self.name, self.machine)
        body = script.encode("utf-8")
        try:
            code, out, err = self.broker.runner(self.argv(known, key_type), b"%d\n" % len(body) + body, timeout)
        except subprocess.TimeoutExpired:
            raise Offline("%s gave no answer within %d s: the connection was cut" % (self.name, timeout)) from None
        except OSError:
            raise Offline("ssh cannot be started on this host") from None
        if code == 255:
            text = err.decode("utf-8", "replace")
            if HOSTKEY_FAILED_RE.search(text):
                self.broker.unpin(self.name)
                raise WinlabError(MISMATCH % self.name)
            raise Offline(_ssh_reason(text, self.name, self.port))
        return code, out, err

    def run(self, text: str, timeout: float | None = None) -> tuple[int, str, str]:
        code, out, err = self._ssh(text, timeout=timeout or RUN_TIMEOUT)
        return (code, out[:MAX_OUTPUT].decode("utf-8", "replace"), err[:MAX_OUTPUT].decode("utf-8", "replace"))

    def put_chunk(self, path: str, offset: int, data: bytes) -> int:
        """The chunk over sftp to PART.awb-chunk, then one ssh run: the offset checked against the size of the part,
        PART.awb-chunk appended to it once and deleted in every case."""
        chunk = path + CHUNK_SUFFIX
        self._sftp_put(data, sftp_path(chunk))
        script = "\n".join([
            "$p = %s" % _ps(path),
            "$c = %s" % _ps(chunk),
            "$o = [long]%d" % offset,
            "$want = [long]%d" % len(data),
            "$code = 0",
            "$n = 0",
            "try {",
            "    if ($o -ne 0 -and -not [IO.File]::Exists($p)) { $code = 5 }",
            "    elseif ((New-Object IO.FileInfo $c).Length -ne $want) { $code = 6 }",
            "    else {",
            "        $m = if ($o -eq 0) { [IO.FileMode]::Create } else { [IO.FileMode]::Open }",
            "        $f = [IO.File]::Open($p, $m, [IO.FileAccess]::Write, [IO.FileShare]::None)",
            "        try {",
            "            $n = $f.Length",
            "            if ($n -ne $o) { $code = 5 } else {",
            "                [void]$f.Seek(0, [IO.SeekOrigin]::End)",
            "                $r = [IO.File]::Open($c, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)",
            "                try { $r.CopyTo($f) } finally { $r.Dispose() }",
            "                $f.Flush()",
            "                $n = $f.Length",
            "            }",
            "        } finally { $f.Dispose() }",
            "    }",
            "} catch { $code = 7 } finally {",
            "    try { [IO.File]::Delete($c) } catch { }",
            "}",
            "if ($code -eq 0 -or $code -eq 5) { [Console]::Out.Write(\"size=$n\") }",
            "exit $code"])
        code, out, _err = self._ssh(script)
        values = _values(out)
        size = values.get("size", "")
        if code not in (0, 5) or not size.isdigit():
            raise Failed("a chunk could not be written on %s (exit %d)" % (self.name, code))
        if code == 5:
            raise OffsetError(int(size))
        return int(size)

    def _sftp_put(self, data: bytes, remote: str) -> None:
        """`data` to the file `remote` (/C:/dir/file) of the machine over sftp, its folders made first; a file there
        is overwritten. The bytes wait in a file of mode 600 in the broker's private folder, removed in every case."""
        known, key_type = self.broker.pin(self.name, self.machine)
        fd, local = tempfile.mkstemp(prefix="chunk-", dir=self.broker._chunk_dir())
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            if not LOCAL_PATH_RE.match(local):
                raise Failed("the broker's own folder for a chunk has a name sftp cannot take")
            parts = remote.split("/")
            batch = ['-mkdir "%s"' % "/".join(parts[:i]) for i in range(3, len(parts))]
            batch.append('put "%s" "%s"' % (local, remote))
            try:
                code, _out, err = self.broker.runner(self.sftp_argv(known, key_type),
                                                     ("\n".join(batch) + "\n").encode("utf-8"), CHUNK_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise Offline("%s gave no answer within %d s: the connection was cut"
                              % (self.name, CHUNK_TIMEOUT)) from None
            except OSError:
                raise Offline("sftp cannot be started on this host") from None
        finally:
            try:
                os.unlink(local)
            except FileNotFoundError:
                pass
        if code != 0:
            text = err.decode("utf-8", "replace")
            if HOSTKEY_FAILED_RE.search(text):
                self.broker.unpin(self.name)
                raise WinlabError(MISMATCH % self.name)
            if code == 255 or UNREACHABLE_RE.search(text):
                raise Offline(_ssh_reason(text, self.name, self.port))
            raise Failed("a chunk could not be put on %s over sftp (exit %d)" % (self.name, code))

    def get_chunk(self, path: str, offset: int, size: int) -> bytes:
        script = "\n".join([
            "$p = %s" % _ps(path),
            "$want = [int]%d" % size,
            "$b = New-Object byte[] $want",
            "$got = 0",
            "$f = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)",
            "try {",
            "    [void]$f.Seek([long]%d, [IO.SeekOrigin]::Begin)" % offset,
            "    while ($got -lt $want) { $n = $f.Read($b, $got, $want - $got); if ($n -le 0) { break }; $got += $n }",
            "} finally { $f.Dispose() }",
            "[Console]::Out.Write('data=' + [Convert]::ToBase64String($b, 0, $got))",
            "exit 0"])
        code, out, _err = self._ssh(script)
        values = _values(out)
        if code != 0 or "data" not in values:
            raise Failed("a chunk could not be read on %s (exit %d)" % (self.name, code))
        try:
            return base64.b64decode(values["data"], validate=True)
        except ValueError:
            raise Failed("a chunk of %s came back damaged" % self.name) from None

    def stat(self, path: str, digest: bool = False) -> dict | None:
        script = "\n".join([
            "$p = %s" % _ps(path),
            "if (-not [IO.File]::Exists($p)) { [Console]::Out.Write('none=1'); exit 0 }",
            "$i = New-Object IO.FileInfo $p",
            "$t = 'size=' + $i.Length + \"`n\" + 'mtime=' + $i.LastWriteTimeUtc.ToString("
            "'yyyy-MM-ddTHH:mm:ss.fffffffZ', [Globalization.CultureInfo]::InvariantCulture)",
            "if (%s) { $t += \"`n\" + 'sha256=' + (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash }"
            % ("$true" if digest else "$false"),
            "[Console]::Out.Write($t)",
            "exit 0"])
        code, out, _err = self._ssh(script, timeout=DIGEST_TIMEOUT if digest else CHUNK_TIMEOUT)
        values = _values(out)
        if code == 0 and "none" in values:
            return None
        if code != 0 or not values.get("size", "").isdigit() or not values.get("mtime"):
            raise Failed("the file could not be read on %s (exit %d)" % (self.name, code))
        result = {"size": int(values["size"]), "mtime": values["mtime"]}
        if digest:
            if not re.match(r"^[0-9A-Fa-f]{64}$", values.get("sha256", "")):
                raise Failed("the SHA-256 of the file could not be taken on %s" % self.name)
            result["sha256"] = values["sha256"]
        return result

    def close(self) -> None:
        """Every op is its own connection: nothing stays open."""


def _values(out: bytes) -> dict[str, str]:
    """key=value lines of what the broker's own script printed."""
    values = {}
    for line in out.decode("utf-8", "replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and re.match(r"^[a-z0-9]+$", key):
            values[key] = value.strip()
    return values


# --------------------------------------------------------------------------- the broker


class Broker:
    """The service. `start()` binds the socket, `stop()` ends it. `handle()` is one call, also for the tests."""

    def __init__(self, socket_path: Path, key: Path, *, registry: Path | None = None, paths_fn=config.paths,
                 runner=None, clock=None, journal=None):
        self.socket_path = Path(socket_path)
        self.key = Path(key)
        self.registry = registry
        self.paths_fn = paths_fn
        self.runner = runner or run_command
        self.clock = clock or winlab._now
        self.journal = journal or _journal
        self.pinned: dict[str, tuple[tuple, str, str]] = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.known_dir: Path | None = None
        self._srv: socket.socket | None = None
        self._ident: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._open = 0

    # the caller
    def _tokens(self, code: str) -> list[str]:
        """The key tokens of the project `code`: the owner's grant (K4) and its folder's own token."""
        from awb import grants, projects

        try:
            p = self.paths_fn()
        except Exception:
            return []
        out = []
        try:
            out.append(grants.token(p, code))
        except Exception:
            pass
        out.append(projects.read_key_token(Path(p.projects_root) / code))
        return [t for t in out if t]

    def caller(self, req: dict) -> str:
        """The project of the call: the code it names, when the key token it carries is that project's."""
        code, token = req.get("project"), req.get("token")
        if not isinstance(code, str) or not winlab.CODE_RE.match(code):
            raise WinlabError("a call names the project code of its session (tcp-xxxx)")
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            raise WinlabError(UNBOUND_LINE)
        if not any(hmac.compare_digest(t.encode("ascii"), token.encode("ascii")) for t in self._tokens(code)):
            raise WinlabError(FOREIGN_LINE % code)
        return code

    # the pin
    def pin(self, name: str, machine: dict) -> tuple[Path, str]:
        """(known_hosts file, key type) holding the machine's host key whose fingerprint is the pin, read with
        ssh-keyscan once and kept while the port and the pin stay. Raises WinlabError when no key matches."""
        ident = (machine["port"], machine["fingerprint"])
        with self.lock:
            held = self.pinned.get(name)
        if held is None or held[0] != ident:
            try:
                code, out, _err = self.runner(["ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT), "-p", str(machine["port"]),
                                               "-t", "ed25519,ecdsa,rsa", winlab.LOOPBACK], b"",
                                              KEYSCAN_TIMEOUT * 3 + 5)
            except subprocess.TimeoutExpired:
                raise Offline("%s did not answer on its port %d in time" % (name, machine["port"])) from None
            except OSError:
                raise Offline("ssh-keyscan cannot be started on this host") from None
            keys = scanned_keys(out.decode("ascii", "replace"))
            if not keys:
                raise Offline("%s does not answer on its port %d: its tunnel is down" % (name, machine["port"]))
            match = [k for k in keys if fingerprint(k[1]) == machine["fingerprint"]]
            if not match:
                raise WinlabError(MISMATCH % name)
            held = (ident, match[0][0], match[0][1])
            with self.lock:
                self.pinned[name] = held
        _ident, key_type, blob = held
        known = self._known_dir() / name
        tmp = known.with_name(".%s.%d.%d" % (name, os.getpid(), threading.get_ident()))
        tmp.write_text("awb-winlab-%s %s %s\n" % (name, key_type, blob), encoding="ascii")
        os.replace(tmp, known)
        return known, key_type

    def unpin(self, name: str) -> None:
        with self.lock:
            self.pinned.pop(name, None)

    def _known_dir(self) -> Path:
        with self.lock:
            if self.known_dir is None:
                self.known_dir = Path(tempfile.mkdtemp(prefix="awb-winlab-known-"))
            return self.known_dir

    def _chunk_dir(self) -> Path:
        """The broker's private folder for the bytes of a put on their way to sftp: .chunks in its own temporary
        folder (no machine name starts with a dot), mode 700, removed with it."""
        folder = self._known_dir() / ".chunks"
        with self.lock:
            try:
                folder.mkdir(mode=0o700)
            except FileExistsError:
                pass
        return folder

    # one call
    def handle(self, req: dict, uid: int = -1, payload=None) -> tuple[dict, bytes]:
        """The answer to one request and the bytes that follow it. `payload()` reads the bytes of a put, called only
        after the window was checked."""
        op = req.get("op") if isinstance(req, dict) else None
        name = req.get("machine") if isinstance(req, dict) else None
        code = None
        data = b""
        try:
            if op not in OPS:
                raise WinlabError("the lab broker takes the ops %s" % ", ".join(OPS))
            code = self.caller(req)
            if op == "windows":
                names = [n for n, w in winlab.windows(self.registry, self.clock()) if w["project"] == code]
                return self._answer(uid, op, name, code, {"ok": True, "machines": names}), b""
            if not isinstance(name, str) or not winlab.NAME_RE.match(name):
                raise WinlabError("a call names a lab machine (awb winlab list)")
            machine = winlab.allowed(name, code, path=self.registry, now=self.clock())
            if op == "check":
                return self._answer(uid, op, name, code, {"ok": True, "machine": {
                    "name": name, "end": machine["window"]["end"]}}), b""
            transport = SshTransport(self, name, machine)
            if op == "run":
                text = req.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise WinlabError("run takes the text of a PowerShell script")
                timeout = req.get("timeout", RUN_TIMEOUT)
                if type(timeout) not in (int, float) or not 1 <= timeout <= RUN_TIMEOUT_MAX:
                    raise WinlabError("the timeout of a command is 1 to %d seconds" % RUN_TIMEOUT_MAX)
                exit_code, out, err = transport.run(text, timeout=timeout)
                answer = {"ok": True, "exit": exit_code, "stdout": out, "stderr": err}
            elif op == "put_chunk":
                path = _winpath(req.get("path"))
                offset = _count(req.get("offset"), "the offset of a chunk", 1 << 50)
                length = _count(req.get("length"), "the length of a chunk", MAX_CHUNK)
                body = payload() if payload is not None else b""
                if len(body) != length:
                    raise WinlabError("the chunk ended before its length")
                answer = {"ok": True, "size": transport.put_chunk(path, offset, body)}
            elif op == "get_chunk":
                path = _winpath(req.get("path"))
                offset = _count(req.get("offset"), "the offset of a chunk", 1 << 50)
                size = _count(req.get("size"), "the size of a chunk", MAX_CHUNK)
                data = transport.get_chunk(path, offset, size)
                answer = {"ok": True, "length": len(data)}
            else:
                path = _winpath(req.get("path"))
                answer = {"ok": True, "stat": transport.stat(path, digest=req.get("digest") is True)}
        except WinlabError as err:
            return self._answer(uid, op, name, code, {"ok": False, "kind": "refused", "error": str(err)}), b""
        except Offline as err:
            return self._answer(uid, op, name, code, {"ok": False, "kind": "offline", "error": str(err)}), b""
        except OffsetError as err:
            return self._answer(uid, op, name, code, {"ok": False, "kind": "offset", "error": str(err),
                                                      "size": err.size}), b""
        except Failed as err:
            return self._answer(uid, op, name, code, {"ok": False, "kind": "failed", "error": str(err)}), b""
        return self._answer(uid, op, name, code, answer), data

    def _answer(self, uid: int, op, name, code, answer: dict) -> dict:
        outcome = "ok" if answer.get("ok") else "%s: %s" % (answer["kind"], answer["error"])
        self.journal("%s uid=%d op=%s machine=%s project=%s %s" % (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), uid,
            op if op in OPS else "-", name if isinstance(name, str) and winlab.NAME_RE.match(name) else "-",
            code or "-", outcome))
        return answer

    # the socket
    def start(self) -> None:
        from awb import handover

        check_key(self.key)
        self._srv, self._ident = handover._bind(self.socket_path, 0o660, None)
        self._thread = threading.Thread(target=self._accept, daemon=True, name="awb-winlab")
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self._srv is not None:
            self._srv.close()
            try:
                st = os.lstat(self.socket_path)
                if (st.st_dev, st.st_ino) == self._ident:
                    self.socket_path.unlink()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.known_dir is not None:
            shutil.rmtree(self.known_dir, ignore_errors=True)

    def serve_forever(self) -> None:
        import signal

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        try:
            while not self.stopping.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _accept(self) -> None:
        from awb import handover

        while not self.stopping.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                time.sleep(0.05)
                continue
            with self.lock:
                if self._open >= MAX_CONNECTIONS:
                    conn.close()
                    continue
                self._open += 1
            try:
                uid = handover._peer_uid(conn)
            except OSError:
                self._done(conn)
                continue
            threading.Thread(target=self._serve, args=(conn, uid), daemon=True).start()

    def _done(self, conn: socket.socket) -> None:
        try:
            conn.close()
        finally:
            with self.lock:
                self._open -= 1

    def _serve(self, conn: socket.socket, uid: int) -> None:
        try:
            conn.settimeout(READ_TIMEOUT)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > MAX_REQUEST + MAX_CHUNK:
                    _send(conn, {"ok": False, "kind": "refused", "error": "the request is too large"})
                    return
            cut = buf.index(b"\n")
            if cut > MAX_REQUEST:
                _send(conn, {"ok": False, "kind": "refused", "error": "the request is too large"})
                return
            try:
                req = json.loads(bytes(buf[:cut]))
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                _send(conn, {"ok": False, "kind": "refused", "error": "a request is one JSON object on one line"})
                return
            rest = buf[cut + 1:]
            read = []

            def payload() -> bytes:
                length = req["length"]
                body = bytearray(rest[:length])
                while len(body) < length:
                    chunk = conn.recv(min(1 << 16, length - len(body)))
                    if not chunk:
                        break
                    body += chunk
                read.append(True)
                return bytes(body)

            answer, data = self.handle(req, uid, payload)
            length = req.get("length")
            if req.get("op") == "put_chunk" and not read and type(length) is int and 0 < length <= MAX_CHUNK:
                payload()       # a refused put: its bytes are read and dropped, so the client reads the answer
            conn.settimeout(SEND_TIMEOUT)
            _send(conn, answer)
            if data:
                conn.sendall(data)
        except (OSError, socket.timeout):
            pass
        finally:
            self._done(conn)


def _send(conn: socket.socket, answer: dict) -> None:
    conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")


def main(args) -> int:
    """`awb winlab serve`: the broker until SIGTERM."""
    from awb import handover
    from awb.vault_client import VaultError

    if config.is_work_user():
        print("awb winlab: the lab broker runs as root under its unit", file=sys.stderr)
        return 2
    handover.no_dump()
    sock = Path(getattr(args, "socket", None) or os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
    key = Path(getattr(args, "key", None) or os.environ.get(KEY_ENV) or DEFAULT_KEY)
    broker = Broker(sock, key)
    try:
        broker.start()
    except (WinlabError, VaultError) as err:
        print("awb winlab: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb winlab: the socket cannot be bound (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    handover.sd_notify("READY=1")
    _journal("the lab broker serves on %s" % sock)
    broker.serve_forever()
    return 0
