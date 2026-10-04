"""A small client for the object storage (OBS) of T Cloud Public: list, head, get, put, copy and delete, signed with
the native V2 scheme (`Authorization: OBS <access key>:<signature>`). Standard library only.

The key pair comes from the password store at call time (T-70: the store stays the only place for secrets). A
setting names an entry of the store as `pass:<entry>`, whose `<entry>/ak` and `<entry>/sk` hold the pair; a key
itself is never written into a setting. Nothing here prints or keeps a secret: errors carry the HTTP status and
the OBS error code, never a key, a secret, a signature or an object name.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DOMAIN = "otc.t-systems.com"
TIMEOUT = 60
CHUNK = 1 << 20
PAGE = 1000


class OBSError(Exception):
    """A call failed. `status` is the HTTP status (0 when the service could not be reached), `code` the OBS error
    code when the answer carried one."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Keys:
    ak: str = field(repr=False)
    sk: str = field(repr=False)


def keys_from_pass(entry: str) -> Keys:
    """The key pair of a password store entry: `<entry>/ak` and `<entry>/sk`."""
    def one(name: str) -> str:
        try:
            r = subprocess.run(["pass", "show", name], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as err:
            raise OBSError("the password store cannot be read (%s)" % type(err).__name__) from None
        lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
        if not lines:
            raise OBSError("the password store gives nothing for the entry of the setting")
        return lines[0].strip()

    return Keys(one(entry + "/ak"), one(entry + "/sk"))


def keys_from_reference(ref: str | None) -> Keys:
    """`pass:<entry>` to the key pair of that entry. Nothing else is accepted."""
    if not ref or not ref.startswith("pass:") or not ref[5:].strip():
        raise OBSError("the key setting must read pass:<entry of the password store>")
    return keys_from_pass(ref[5:].strip())


@dataclass
class Listing:
    objects: list[tuple[str, int, str]]   # key, size, etag
    prefixes: list[str]
    modified: dict[str, str] = field(default_factory=dict)    # key -> LastModified as the service wrote it


def xml_local(tag: str) -> str:
    """An element's tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def xml_child(el: ET.Element, name: str) -> ET.Element | None:
    """The first child of that name, whatever its namespace."""
    for c in el:
        if xml_local(c.tag) == name:
            return c
    return None


def xml_text(el: ET.Element | None) -> str:
    """The text of an element, empty for none."""
    return (el.text or "") if el is not None else ""


# The private names stay for the web adapters still deployed outside the repository until they are switched.
_local, _child, _text = xml_local, xml_child, xml_text


class Client:
    """One bucket. `endpoint` (for tests) addresses the bucket by path on another host; without it the bucket is
    reached as `https://<bucket>.obs.<region>.otc.t-systems.com`."""

    def __init__(self, bucket: str, keys: Keys, region: str = "eu-de", endpoint: str | None = None,
                 timeout: float = TIMEOUT):
        self.bucket = bucket
        self.keys = keys
        self.region = region
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.timeout = timeout

    # -- signing and sending

    def _path(self, key: str) -> str:
        return "/" + urllib.parse.quote(key, safe="/~")

    def url(self, key: str, query: str) -> str:
        """The address of a key with its query."""
        if self.endpoint:
            base = "%s/%s" % (self.endpoint, self.bucket)
        else:
            base = "https://%s.obs.%s.%s" % (self.bucket, self.region, DOMAIN)
        return base + self._path(key) + ("?" + query if query else "")

    def sign(self, method: str, key: str, headers: dict[str, str], sub: str = "") -> None:
        """Add Date and the V2 Authorization header for this request to `headers`."""
        date = email.utils.formatdate(usegmt=True)
        headers["Date"] = date
        obs = sorted((k.lower(), v.strip()) for k, v in headers.items() if k.lower().startswith("x-obs-"))
        resource = "/%s%s%s" % (self.bucket, self._path(key), "?" + sub if sub else "")
        text = "\n".join([method, headers.get("Content-MD5", ""), headers.get("Content-Type", ""), date]) + "\n"
        text += "".join("%s:%s\n" % kv for kv in obs) + resource
        mac = hmac.new(self.keys.sk.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
        headers["Authorization"] = "OBS %s:%s" % (self.keys.ak, base64.b64encode(mac).decode("ascii"))

    _url, _sign = url, sign       # kept for the web adapters still deployed outside the repository

    def _send(self, method: str, key: str = "", *, query: str = "", sub: str = "", data=None,
              headers: dict[str, str] | None = None, to: Path | None = None) -> tuple[int, dict, bytes]:
        headers = dict(headers or {})
        if data is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/octet-stream"   # else urllib adds one outside the signature
        self.sign(method, key, headers, sub)
        q = "&".join(x for x in (sub, query) if x)
        req = urllib.request.Request(self.url(key, q), data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except OSError:
                pass
            code = ""
            try:
                code = xml_text(xml_child(ET.fromstring(body), "Code")) if body else ""
            except ET.ParseError:
                pass
            raise OBSError("HTTP %d%s" % (exc.code, " " + code if code else ""), exc.code, code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise OBSError("the object storage cannot be reached (%s)" % type(exc).__name__) from None
        with resp:
            if to is not None:
                with open(to, "wb") as f:
                    shutil.copyfileobj(resp, f, CHUNK)
                return resp.status, dict(resp.headers), b""
            return resp.status, dict(resp.headers), resp.read()

    # -- operations

    def list(self, prefix: str = "", delimiter: str = "", page: int = PAGE) -> Listing:
        """Every object under `prefix` (all pages); with `delimiter` the common prefixes one level down."""
        objects: list[tuple[str, int, str]] = []
        prefixes: list[str] = []
        modified: dict[str, str] = {}
        marker = ""
        while True:
            params = [("max-keys", str(page))]
            if prefix:
                params.append(("prefix", prefix))
            if delimiter:
                params.append(("delimiter", delimiter))
            if marker:
                params.append(("marker", marker))
            _, _, body = self._send("GET", "", query=urllib.parse.urlencode(params, quote_via=urllib.parse.quote))
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise OBSError("the listing is not XML") from None
            last = ""
            for el in root:
                name = xml_local(el.tag)
                if name == "Contents":
                    k = xml_text(xml_child(el, "Key"))
                    objects.append((k, int(xml_text(xml_child(el, "Size")) or 0), xml_text(xml_child(el, "ETag")).strip('"')))
                    modified[k] = xml_text(xml_child(el, "LastModified"))
                    last = k
                elif name == "CommonPrefixes":
                    p = xml_text(xml_child(el, "Prefix"))
                    prefixes.append(p)
                    last = max(last, p)
            if xml_text(xml_child(root, "IsTruncated")).lower() != "true":
                break
            marker = xml_text(xml_child(root, "NextMarker")) or last
            if not marker:
                break
        return Listing(objects, prefixes, modified)

    def head(self, key: str) -> dict | None:
        """The headers of an object, None when it is not there."""
        try:
            _, headers, _ = self._send("HEAD", key)
        except OBSError as err:
            if err.status == 404:
                return None
            raise
        return headers

    def get(self, key: str, to: Path, if_match: str | None = None) -> Path:
        """The object into `to`. With `if_match` only that version (its ETag): another one fails with status 412."""
        headers = {"If-Match": '"%s"' % if_match.strip('"')} if if_match else None
        self._send("GET", key, headers=headers, to=Path(to))
        return Path(to)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._send("PUT", key, data=data, headers={"Content-Type": content_type,
                                                   "Content-Length": str(len(data))})

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        path = Path(path)
        with open(path, "rb") as f:
            self._send("PUT", key, data=f, headers={"Content-Type": content_type,
                                                    "Content-Length": str(path.stat().st_size)})

    def copy(self, source: str, target: str) -> None:
        """Copy inside the bucket: the data does not leave the object storage."""
        self._send("PUT", target, data=b"", headers={
            "Content-Type": "application/octet-stream", "Content-Length": "0",
            "x-obs-copy-source": "/%s%s" % (self.bucket, self._path(source))})

    def delete(self, key: str) -> None:
        self._send("DELETE", key)
