"""The reader script of the recipe `disk-reader` (tcp-bytg ask 2026-11-21-tcp-bytg-10): the user_data of a small Linux
ECS that reads the system volume of a stopped Windows server, read-only and puts a short list of files into the
run's lab bucket.

The recipe attaches the volume to the reader after the reader booted, so the script waits for the disk (found by its
serial, the first 20 characters of the volume id, under /dev/disk/by-id), mounts the largest NTFS partition of it with
ntfs-3g and `ro`, puts these files and writes `manifest.json` last, after the unmount:

    Windows/INF/setupapi*.log
    Windows/System32/winevt/Logs/System.evtx and Application.evtx
    Windows/System32/config/SYSTEM and SOFTWARE
    and the files of the recipe parameter `files` (a path inside the volume; `:` between files, `+` for a space)

Every object goes to `files/<path inside the volume>` of the bucket. The manifest says per file put, missing or
failed, with the size and the SHA-256 and for the whole run `result` read or failed with its reason.

The text holds no key, only two references: `{{secret:obs-ak}}` and `{{secret:obs-sk}}` (the owner's word of
2026-11-25: no IAM agency, no extra lab keys). The recipe sends the text as `server.user_data_text`; the key service
fills the references with the key of the call (the lab key of the tenant), sends the result as base64 user_data and
never returns it (tcp-bytg ask 2026-11-25-tcp-bytg-6). The script fetches nothing from a public bucket and signs its
puts the V2 way of OBS (awb/obs.py) with that key; the key never leaves the reader. Standard library only, on our side
and on the reader's (python3 of the image).
"""
from __future__ import annotations

import base64
import json
import re

FIXED = ("Windows/INF/setupapi*.log",
         "Windows/System32/winevt/Logs/System.evtx",
         "Windows/System32/winevt/Logs/Application.evtx",
         "Windows/System32/config/SYSTEM",
         "Windows/System32/config/SOFTWARE")
"""What the reader reads always; a part with a * is a pattern (a file name of that folder)."""

PREFIX = "files/"
MANIFEST = "manifest.json"
MAX_FILES = 20
WAIT_MINUTES = 20

_VOLUME_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{2,62}$")
_REGION_RE = re.compile(r"^eu-(de|nl)$")
_PART_RE = re.compile(r"^[A-Za-z0-9_. -]+$")


class ReaderError(ValueError):
    """A value the reader script cannot be made from; the message names the value, never a path."""


def files_of(spec: str) -> list[str]:
    """The extra files of the parameter `files`: paths inside the volume joined by `:`, `+` for a space. A path is
    relative, has no empty or `..` part and holds letters, digits, spaces and `_.-` only."""
    out: list[str] = []
    for item in (spec or "").split(":"):
        if not item:
            continue
        path = item.replace("+", " ")
        parts = path.split("/")
        if path.startswith("/") or len(path) > 180 or not all(_PART_RE.match(p) and p not in (".", "..")
                                                              for p in parts):
            raise ReaderError("the file list holds a path the reader does not take")
        if path not in out:
            out.append(path)
    if len(out) > MAX_FILES:
        raise ReaderError("the file list holds more than %d files" % MAX_FILES)
    return out


def config_of(bucket: str, region: str, volume: str, files: str = "", wait_minutes: int = WAIT_MINUTES) -> dict:
    """The values the script runs on, checked."""
    if not _BUCKET_RE.match(bucket or ""):
        raise ReaderError("the bucket name is not one the reader takes")
    if not _REGION_RE.match(region or ""):
        raise ReaderError("the region is not one the reader takes")
    if not _VOLUME_RE.match(volume or ""):
        raise ReaderError("the volume id is not an id")
    return {"bucket": bucket, "region": region, "volume": volume, "files": list(FIXED) + files_of(files),
            "prefix": PREFIX, "manifest": MANIFEST, "wait_minutes": int(wait_minutes)}


def usertext(env: dict) -> str:
    """The script as plain text for the values of a lab run (`name` is the bucket, `region`, `sys_volume` the id of
    the volume the recipe read from the server and `files` the parameter): the text of `server.user_data_text`."""
    for k in ("name", "region", "sys_volume"):
        if not env.get(k):
            raise ReaderError("no value for %s" % k)
    return render(env["name"], env["region"], env["sys_volume"], env.get("files", ""))


def render(bucket: str, region: str, volume: str, files: str = "", wait_minutes: int = WAIT_MINUTES) -> str:
    """The script as text: a python3 script that cloud-init runs at the first boot of the reader."""
    cfg = config_of(bucket, region, volume, files, wait_minutes)
    return SCRIPT.replace("@@CONFIG@@", json.dumps(json.dumps(cfg, sort_keys=True)))


def userdata(env: dict) -> str:
    """The base64 of the script for the values of a lab run: `name` is the bucket, `region`, `sys_volume` the id of
    the volume the recipe read from the server and `files` the parameter."""
    return base64.b64encode(usertext(env).encode("utf-8")).decode("ascii")


SCRIPT = r'''#!/usr/bin/env python3
# awb disk reader: reads a system volume read-only and puts files into a lab bucket. The two references below are
# filled with the key of the tenant by the key service before the server is made.
import datetime
import email.utils
import fnmatch
import glob
import hashlib
import hmac
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import base64

CFG = json.loads(@@CONFIG@@)
MOUNT = "/mnt/awb-win"
LOG = "/var/log/awb-disk-reader.log"
AK = "{{secret:obs-ak}}"
SK = "{{secret:obs-sk}}"


def say(text):
    line = "AWB-READER %s" % text
    for path in ("/dev/console", LOG):
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def run(argv, timeout=300):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def safe_key(rel):
    key = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "._/-" else "_" for ch in rel)
    return CFG["prefix"] + key.strip("/")


def put(key, path=None, data=b""):
    """PUT one object, signed the V2 way of OBS; the file is streamed. Returns the HTTP status."""
    host = "%s.obs.%s.otc.t-systems.com" % (CFG["bucket"], CFG["region"])
    size = os.path.getsize(path) if path else len(data)
    last = 0
    for attempt in range(4):
        date = email.utils.formatdate(usegmt=True)
        ctype = "application/octet-stream"
        text = "PUT\n\n%s\n%s\n/%s/%s" % (ctype, date, CFG["bucket"], key)
        sig = base64.b64encode(hmac.new(SK.encode(), text.encode(), hashlib.sha1).digest()).decode()
        conn = http.client.HTTPSConnection(host, timeout=300)
        try:
            conn.putrequest("PUT", "/" + key)
            conn.putheader("Host", host)
            conn.putheader("Date", date)
            conn.putheader("Content-Type", ctype)
            conn.putheader("Content-Length", str(size))
            conn.putheader("Authorization", "OBS %s:%s" % (AK, sig))
            conn.endheaders()
            if path:
                with open(path, "rb") as f:
                    while True:
                        block = f.read(1 << 20)
                        if not block:
                            break
                        conn.send(block)
            else:
                conn.send(data)
            r = conn.getresponse()
            r.read()
            last = r.status
            if 200 <= last < 300:
                return last
        except Exception:
            last = 0
        finally:
            conn.close()
        time.sleep(5 * (attempt + 1))
    return last


def find_disk():
    """The device of the volume: its serial is the first 20 characters of its id."""
    end = time.time() + CFG["wait_minutes"] * 60
    pattern = "/dev/disk/by-id/virtio-%s" % CFG["volume"][:20]
    while time.time() < end:
        hits = sorted(glob.glob(pattern))
        if hits:
            return os.path.realpath(hits[0])
        time.sleep(5)
    return None


def pick_partition(disk):
    """The largest NTFS partition of the disk (the system partition, not the small reserved one)."""
    for _ in range(12):
        code, out = run(["lsblk", "-nbrpo", "NAME,SIZE,FSTYPE,TYPE", disk])
        best = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2].lower() in ("ntfs", "ntfs3") and parts[3] in ("part", "disk"):
                if best is None or int(parts[1]) > best[0]:
                    best = (int(parts[1]), parts[0])
        if best:
            return best[1]
        run(["udevadm", "settle"], 30)
        time.sleep(5)
    return None


def mount(dev):
    os.makedirs(MOUNT, exist_ok=True)
    if not (shutil.which("ntfs-3g") or shutil.which("mount.ntfs-3g")):
        run(["apt-get", "install", "-y", "ntfs-3g"], 600)
    for argv in (["mount", "-t", "ntfs-3g", "-o", "ro,norecover,noatime", dev, MOUNT],
                 ["mount", "-t", "ntfs3", "-o", "ro,noatime", dev, MOUNT]):
        if run(argv, 120)[0] == 0:
            return argv[2]
    return None


def resolve(rel):
    """The paths a relative name stands for under the mount; every part matches without regard to case and a part
    with a * is a pattern (setupapi*.log)."""
    here = [MOUNT]
    for part in rel.split("/"):
        nxt = []
        for base in here:
            try:
                names = os.listdir(base)
            except OSError:
                continue
            if "*" in part:
                nxt += [os.path.join(base, n) for n in sorted(names) if fnmatch.fnmatchcase(n.lower(), part.lower())]
            else:
                nxt += [os.path.join(base, n) for n in names if n.lower() == part.lower()][:1]
        here = nxt
    return [p for p in here if os.path.isfile(p)]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {"result": "failed", "reason": "", "started": started, "mounted": "", "files": []}
    mounted = False
    try:
        dev = find_disk()
        if not dev:
            manifest["reason"] = "the volume did not show up in %d minutes" % CFG["wait_minutes"]
            raise SystemExit
        part = pick_partition(dev)
        if not part:
            manifest["reason"] = "no NTFS partition on the volume"
            raise SystemExit
        driver = mount(part)
        if not driver:
            manifest["reason"] = "the partition could not be mounted read-only"
            raise SystemExit
        mounted = True
        manifest["mounted"] = "read-only, " + driver
        failed = 0
        for want in CFG["files"]:
            found = resolve(want)
            if not found:
                manifest["files"].append({"want": want, "result": "missing"})
                continue
            for path in found:
                rel = os.path.relpath(path, MOUNT)
                key = safe_key(rel)
                row = {"want": want, "key": key, "size": os.path.getsize(path)}
                try:
                    row["sha256"] = sha256(path)
                    status = put(key, path=path)
                    row["result"] = "put" if 200 <= status < 300 else "failed"
                    row["status"] = status
                except OSError as exc:
                    row["result"] = "failed"
                    row["status"] = type(exc).__name__
                if row["result"] != "put":
                    failed += 1
                manifest["files"].append(row)
        manifest["reason"] = "%d file(s) failed" % failed if failed else ""
        manifest["result"] = "failed" if failed else "read"
    except SystemExit:
        pass
    except Exception as exc:
        manifest["reason"] = "error: %s" % type(exc).__name__
    finally:
        if mounted:
            run(["sync"], 120)
            if run(["umount", MOUNT], 120)[0] != 0:
                manifest["result"], manifest["reason"] = "failed", "the volume could not be unmounted"
        manifest["ended"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = json.dumps(manifest, sort_keys=True, indent=1).encode("utf-8")
        try:
            put(CFG["manifest"], data=body)
        except Exception:
            pass
        say("%s %s" % (manifest["result"], manifest["reason"]))


main()
'''
