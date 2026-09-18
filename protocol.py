"""LocalSend Protocol v2.2 wire implementation (stdlib only).

Reference: https://github.com/localsend/protocol (v2.2, 2026-08-07).

Nothing in this module imports Hermes — it is a plain protocol client/server so
it can be unit-tested (and used) without the agent runtime.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import mimetypes
import os
import random
import socket
import ssl
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2.2"

# Device identity used when talking to peers in encrypted (HTTPS) mode. LocalSend
# requires a client certificate there, and identifies us by the certificate's
# SHA-256 fingerprint, so the announced fingerprint must be this certificate's.
_identity = None


def set_identity(identity) -> None:
    """Register the certificate presented to HTTPS peers (module-level, per process)."""
    global _identity
    _identity = identity


def get_identity():
    return _identity
MULTICAST_GROUP = "224.0.0.167"
MULTICAST_PORT = 53317
DEFAULT_PORT = 53317
API_PREFIX = "/api/localsend/v2"
CHUNK = 512 * 1024


class LocalSendError(Exception):
    """Protocol-level failure with the HTTP status that caused it."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        self.status = status
        self.body = body
        super().__init__(message)


@dataclass
class DeviceInfo:
    """LocalSend device identity (protocol section 3)."""

    alias: str = "Hermes"
    version: str = PROTOCOL_VERSION
    deviceModel: str = "Hermes Agent"
    deviceType: str = "headless"
    fingerprint: str = ""
    port: int = DEFAULT_PORT
    protocol: str = "http"
    download: bool = False

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = uuid.uuid4().hex

    def to_dict(self, announce: Optional[bool] = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "alias": self.alias,
            "version": self.version,
            "deviceModel": self.deviceModel,
            "deviceType": self.deviceType,
            "fingerprint": self.fingerprint,
            "port": self.port,
            "protocol": self.protocol,
            "download": self.download,
        }
        if announce is not None:
            out["announce"] = announce
        return out


@dataclass
class Peer:
    """A discovered LocalSend participant."""

    alias: str
    ip: str
    port: int = DEFAULT_PORT
    protocol: str = "http"
    fingerprint: str = ""
    deviceModel: str = ""
    deviceType: str = ""
    download: bool = False
    source: str = "multicast"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.ip}:{self.port}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "ip": self.ip,
            "port": self.port,
            "protocol": self.protocol,
            "fingerprint": self.fingerprint,
            "deviceModel": self.deviceModel,
            "deviceType": self.deviceType,
            "download": self.download,
            "base_url": self.base_url,
            "source": self.source,
        }


def _peer_from_payload(payload: dict[str, Any], ip: str, source: str = "http") -> Optional[Peer]:
    """Build a Peer from a register/announce payload; None if unusable."""
    if not isinstance(payload, dict) or "fingerprint" not in payload:
        return None
    try:
        port = int(payload.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    protocol = str(payload.get("protocol") or "http").lower()
    if protocol not in {"http", "https"}:
        protocol = "http"
    return Peer(
        alias=str(payload.get("alias") or "unknown"),
        ip=ip,
        port=port,
        protocol=protocol,
        fingerprint=str(payload.get("fingerprint") or ""),
        deviceModel=str(payload.get("deviceModel") or ""),
        deviceType=str(payload.get("deviceType") or ""),
        download=bool(payload.get("download", False)),
        source=source,
        raw=dict(payload),
    )


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------


def _ssl_context(insecure: bool = True, identity=None) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        # LocalSend peers use self-signed certificates; the fingerprint is the
        # identity check, not the CA chain.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # Present our client certificate: v2 receivers make client auth mandatory and
    # reject the handshake outright without one (tlsv13 "certificate required").
    identity = identity if identity is not None else _identity
    if identity is not None:
        ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


def _server_ssl_context(identity) -> ssl.SSLContext:
    """TLS for the receiver: present our certificate, pin-able by fingerprint.

    LocalSend's own server makes client auth *mandatory* and validates the peer
    certificate itself (``client_cert_verifier.rs``). Python's stdlib cannot do
    that: requesting a client certificate makes OpenSSL validate the chain against
    its trust store, so a peer's self-signed certificate fails the handshake with
    ``unknown ca`` and there is no permissive verify callback exposed. The receiver
    therefore serves server-authenticated TLS: the peer pins *our* certificate
    (its SHA-256 is what we announce), and sender identity is whatever the
    application layer can establish. Documented in the README rather than implied.
    """
    if identity is None:
        raise LocalSendError(
            "an HTTPS receiver needs a device certificate; run any send to an encrypted peer "
            "first (or set identity_dir) so ~/.hermes/localsend-identity exists"
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # No client-certificate request: see above — stdlib cannot accept self-signed ones.
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connection(peer: Peer, timeout: float) -> http.client.HTTPConnection:
    if peer.protocol == "https":
        return http.client.HTTPSConnection(peer.ip, peer.port, timeout=timeout, context=_ssl_context())
    return http.client.HTTPConnection(peer.ip, peer.port, timeout=timeout)


def peer_certificate_fingerprint(peer: Peer, timeout: float = 5.0) -> str:
    """The peer's certificate fingerprint as LocalSend advertises it (uppercase hex)."""
    ctx = _ssl_context()
    with socket.create_connection((peer.ip, peer.port), timeout=timeout) as raw_sock:
        with ctx.wrap_socket(raw_sock, server_hostname=peer.ip) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)
    if not der:
        return ""
    return hashlib.sha256(der).hexdigest().upper()


def _request_json(
    peer: Peer,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any], str]:
    """Send a JSON request; return (status, parsed_json_or_{}, raw_body)."""
    conn = _connection(peer, timeout)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if payload else {}
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        parsed: dict[str, Any] = {}
        if raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    parsed = loaded
            except json.JSONDecodeError:
                pass
        return resp.status, parsed, raw
    except (OSError, http.client.HTTPException) as exc:
        raise LocalSendError(f"cannot reach {peer.base_url}: {exc}") from exc
    finally:
        conn.close()


def _upload_stream(peer: Peer, path: str, file_path: str, size: int, timeout: float = 120.0) -> int:
    """POST a file body to an upload/receive route. Returns the HTTP status."""
    conn = _connection(peer, timeout)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                conn.send(chunk)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    except (OSError, http.client.HTTPException) as exc:
        raise LocalSendError(f"upload to {peer.base_url} failed: {exc}") from exc
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _multicast_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", port))
    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.settimeout(0.3)
    return sock


def _local_ipv4_addresses() -> list[str]:
    """Best-effort list of this host's local IPv4 addresses (for /24 scans)."""
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    # UDP connect trick: no packets sent, but the kernel picks the egress IP.
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 80))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(a for a in addrs if not a.startswith("127."))


class _RegisterCatcher:
    """Short-lived TCP listener that accepts peers' HTTP /register callbacks.

    When we multicast ``announce: true``, other LocalSend members reply with a
    ``POST /api/localsend/v2/register`` back to us (protocol section 3.1).
    """

    def __init__(self, port: int, on_register: Callable[[Peer], None]):
        self.port = port
        self._on_register = on_register
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    def start(self) -> bool:
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence stderr spam
                logger.debug("register catcher: %s", args)

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                peer = None
                if self.path.startswith(f"{API_PREFIX}/register"):
                    try:
                        payload = json.loads(body.decode() or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                    peer = _peer_from_payload(payload, self.client_address[0], source="register")
                    if peer:
                        catcher._on_register(peer)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        try:
            self._server = ThreadingHTTPServer(("", self.port), Handler)
        except OSError as exc:
            self.error = f"cannot bind TCP {self.port} for register callbacks: {exc}"
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)


def _scan_subnet(port: int, timeout: float, our_info: dict[str, Any], found: dict[str, Peer], local_ips: list[str]) -> None:
    """Legacy HTTP discovery (protocol section 3.2): register with every host."""
    subnets: set[str] = set()
    for ip in local_ips:
        try:
            subnets.add(str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address))
        except ValueError:
            continue

    def probe(prefix: str, host: int) -> None:
        ip = f"{prefix.rsplit('.', 1)[0]}.{host}"
        if ip in local_ips:
            return
        peer = Peer(alias="", ip=ip, port=port, protocol="http")
        try:
            status, payload, _ = _request_json(peer, "POST", f"{API_PREFIX}/register", our_info, timeout=timeout)
        except LocalSendError:
            return
        if status != 200 or not payload:
            return
        discovered = _peer_from_payload(payload, ip, source="http-scan")
        if discovered:
            found[discovered.fingerprint or discovered.ip] = discovered

    threads: list[threading.Thread] = []
    for prefix in sorted(subnets):
        for host in range(1, 255):
            t = threading.Thread(target=probe, args=(prefix, host), daemon=True)
            t.start()
            threads.append(t)
            if len(threads) % 64 == 0:
                for t2 in threads:
                    t2.join(timeout=timeout)
                threads = []
    for t in threads:
        t.join(timeout=timeout)


def discover(
    info: DeviceInfo,
    timeout: float = 3.0,
    scan_subnets: bool = True,
    bind_port: bool = DEFAULT_PORT,
) -> tuple[list[Peer], list[str]]:
    """Find LocalSend peers. Returns (peers, warnings)."""
    warnings: list[str] = []
    found: dict[str, Peer] = {}

    def on_register(peer: Peer) -> None:
        if peer.fingerprint != info.fingerprint:
            found[peer.fingerprint or peer.ip] = peer

    catcher = _RegisterCatcher(bind_port, on_register)
    if not catcher.start():
        warnings.append(catcher.error or "register callback listener unavailable")

    sock: Optional[socket.socket] = None
    try:
        try:
            sock = _multicast_socket(bind_port)
        except OSError as exc:
            warnings.append(f"multicast unavailable on UDP {bind_port}: {exc}")

        if sock is not None:
            payload = json.dumps(info.to_dict(announce=True)).encode()
            try:
                sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
            except OSError as exc:
                warnings.append(f"multicast announce failed: {exc}")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    warnings.append(f"multicast receive failed: {exc}")
                    break
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                # Only unsolicited responses (announce:false) count as peers.
                if msg.get("announce", False):
                    continue
                peer = _peer_from_payload(msg, addr[0], source="multicast")
                if peer and peer.fingerprint != info.fingerprint:
                    found[peer.fingerprint or peer.ip] = peer

        if scan_subnets:
            local_ips = _local_ipv4_addresses()
            if local_ips:
                _scan_subnet(bind_port, min(1.0, max(0.3, timeout / 3)), info.to_dict(), found, local_ips)
    finally:
        if sock is not None:
            sock.close()
        catcher.stop()

    peers = [p for p in found.values() if p.fingerprint != info.fingerprint]
    peers.sort(key=lambda p: (p.alias.lower(), p.ip))
    return peers, warnings


# --------------------------------------------------------------------------
# Sending (upload API, protocol section 4)
# --------------------------------------------------------------------------


def file_metadata(file_id: str, path: str, with_hash: bool = True) -> dict[str, Any]:
    """Build one entry of the prepare-upload ``files`` map."""
    size = os.path.getsize(path)
    file_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    meta: dict[str, Any] = {
        "id": file_id,
        "fileName": os.path.basename(path),
        "size": size,
        "fileType": file_type,
    }
    if with_hash:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
        meta["sha256"] = digest.hexdigest()
    meta["metadata"] = {"modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))}
    return meta


def send_files(peer: Peer, paths: Iterable[str], info: DeviceInfo, pin: str = "", timeout: float = 120.0) -> dict[str, Any]:
    """Run prepare-upload + upload for each file. Returns a result dict."""
    path_list = [os.path.abspath(p) for p in paths]
    missing = [p for p in path_list if not os.path.isfile(p)]
    if missing:
        raise LocalSendError(f"not a file: {', '.join(missing)}")
    if not path_list:
        raise LocalSendError("no files given")

    files: dict[str, Any] = {}
    for path in path_list:
        file_id = uuid.uuid4().hex[:12]
        files[file_id] = file_metadata(file_id, path)

    query = f"?pin={pin}" if pin else ""
    status, payload, raw = _request_json(
        peer,
        "POST",
        f"{API_PREFIX}/prepare-upload{query}",
        {"info": info.to_dict(), "files": files},
        timeout=min(30.0, timeout),
    )

    if status == 204:
        return {"status": "finished", "http_status": 204, "detail": "receiver accepted nothing (no transfer needed)", "files": []}
    if status != 200:
        detail = {
            401: "receiver requires a PIN (or the PIN is wrong)",
            403: "receiver rejected the transfer",
            409: "receiver is blocked by another session",
            429: "receiver is rate limiting",
        }.get(status, raw[:200])
        raise LocalSendError(f"prepare-upload returned {status}: {detail}", status=status, body=raw[:500])

    session_id = payload.get("sessionId")
    tokens = payload.get("files") or {}
    if not session_id or not isinstance(tokens, dict):
        raise LocalSendError(f"malformed prepare-upload response: {raw[:200]}")

    accepted = [fid for fid in files if fid in tokens]
    rejected = [fid for fid in files if fid not in tokens]
    by_id = {fid: path for fid, path in zip(files.keys(), path_list)}

    sent: list[dict[str, Any]] = []
    for file_id in accepted:
        path = by_id[file_id]
        upload_path = (
            f"{API_PREFIX}/upload?sessionId={session_id}&fileId={file_id}&token={tokens[file_id]}"
        )
        up_status = _upload_stream(peer, upload_path, path, files[file_id]["size"], timeout=timeout)
        if up_status != 200:
            raise LocalSendError(
                f"upload of {files[file_id]['fileName']} returned {up_status}",
                status=up_status,
            )
        sent.append(
            {
                "file": files[file_id]["fileName"],
                "path": path,
                "bytes": files[file_id]["size"],
                "sha256": files[file_id].get("sha256", ""),
            }
        )

    result: dict[str, Any] = {
        "status": "sent",
        "peer": peer.as_dict(),
        "session": session_id,
        "files": sent,
        "bytes": sum(f["bytes"] for f in sent),
    }
    if rejected:
        result["rejected"] = [files[fid]["fileName"] for fid in rejected]
    return result


def cancel_session(peer: Peer, session_id: str, timeout: float = 10.0) -> int:
    status, _, _ = _request_json(peer, "POST", f"{API_PREFIX}/cancel?sessionId={session_id}", None, timeout=timeout)
    return status


def _normalize_fingerprint(value: str) -> str:
    """Reduce a LocalSend fingerprint to comparable lowercase hex.

    LocalSend advertises uppercase hex with no separators; hex/colon/base64 are
    accepted here so a hand-written value still works.
    """
    import base64
    import binascii
    import re

    raw = (value or "").strip()
    if not raw:
        return ""
    candidates = [raw, raw.replace(":", "").replace("-", "")]
    for candidate in candidates:
        if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
            return candidate.lower()
    try:
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) == 32:
            return binascii.hexlify(decoded).decode()
    except (binascii.Error, ValueError):
        pass
    return ""





def verify_peer_certificate(peer: Peer, timeout: float = 5.0) -> str:
    """Verify an HTTPS peer's certificate against its advertised fingerprint.

    LocalSend's fingerprint *is* the certificate hash (protocol section 2), so a
    mismatch means we are talking to something other than the device that
    announced itself. Returns a warning string when no usable comparison is
    possible, raises LocalSendError on a definite mismatch.
    """
    if peer.protocol != "https":
        return ""
    advertised = _normalize_fingerprint(peer.fingerprint)
    try:
        # LocalSend advertises uppercase hex; compare both sides in one case.
        actual = _normalize_fingerprint(peer_certificate_fingerprint(peer, timeout=timeout))
    except OSError as exc:
        raise LocalSendError(f"cannot verify certificate of {peer.base_url}: {exc}") from exc
    if not actual:
        return "peer presented no certificate; cannot verify fingerprint"
    if not advertised:
        return "peer advertised a fingerprint this plugin cannot parse; certificate hash not verified"
    if advertised != actual:
        raise LocalSendError(
            f"certificate fingerprint mismatch for {peer.ip}:{peer.port} "
            f"(advertised {peer.fingerprint[:16]}…, presented {actual[:16]}…)"
        )
    return ""


# --------------------------------------------------------------------------
# Receiving (upload API server side)
# --------------------------------------------------------------------------


class ReceiveServer:
    """A LocalSend-compatible receiver (headless).

    Serves ``/register``, ``/info``, ``/prepare-upload``, ``/upload`` and
    ``/cancel``; announces itself over multicast so peers see it in their UI.
    """

    def __init__(
        self,
        info: DeviceInfo,
        download_dir: str,
        pin: str = "",
        https: bool = False,
        identity=None,
    ) -> None:
        self.info = info
        self.download_dir = os.path.abspath(download_dir)
        self.pin = pin
        self.https = bool(https)
        self.identity = identity
        self.sessions: dict[str, dict[str, Any]] = {}
        self.received: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.peers: dict[str, Peer] = {}
        self.errors: list[str] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._announce_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at: Optional[float] = None
        self.port = info.port

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> str:
        os.makedirs(self.download_dir, exist_ok=True)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                logger.debug("receiver: %s", args)

            def _json(self, status: int, payload: Optional[dict[str, Any]] = None) -> None:
                body = json.dumps(payload if payload is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_expect_100(self) -> bool:
                self.send_response_only(100)
                self.end_headers()
                return True

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _iter_body(self):
                """Yield the request body, handling Content-Length *and* chunked encoding.

                Mobile clients (iOS LocalSend among them) send bodies with
                ``Transfer-Encoding: chunked`` and no Content-Length. Reading only
                Content-Length silently yields a zero-byte body, which looks like a
                checksum failure rather than a framing bug.
                """
                encoding = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in encoding:
                    while True:
                        line = self.rfile.readline(1024).strip()
                        if b";" in line:
                            line = line.split(b";", 1)[0].strip()
                        if not line:
                            raise ValueError("empty chunk size line")
                        size = int(line, 16)
                        if size == 0:
                            while True:  # consume optional trailers
                                trailer = self.rfile.readline(1024)
                                if trailer in (b"\r\n", b"\n", b""):
                                    break
                            return
                        remaining = size
                        while remaining:
                            piece = self.rfile.read(min(CHUNK, remaining))
                            if not piece:
                                raise ValueError("truncated chunk")
                            remaining -= len(piece)
                            yield piece
                        self.rfile.read(2)  # trailing CRLF
                    return
                remaining = int(self.headers.get("Content-Length") or 0)
                while remaining > 0:
                    piece = self.rfile.read(min(CHUNK, remaining))
                    if not piece:
                        break
                    remaining -= len(piece)
                    yield piece

            def _read_body(self, limit: int = 2 * 1024 * 1024) -> bytes:
                buf = bytearray()
                for piece in self._iter_body():
                    buf.extend(piece)
                    if len(buf) > limit:
                        raise ValueError("body too large")
                return bytes(buf)

            def _body(self) -> dict[str, Any]:
                try:
                    raw = self._read_body()
                except ValueError:
                    return {}
                if not raw:
                    return {}
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    return {}

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                route, query = parsed.path, parse_qs(parsed.query)
                ip = self.client_address[0]
                if route == f"{API_PREFIX}/register":
                    payload = self._body()
                    peer = _peer_from_payload(payload, ip, source="inbound-register")
                    if peer:
                        with server._lock:
                            server.peers[peer.fingerprint or peer.ip] = peer
                    self._json(200, server.info.to_dict())
                    return
                if route == f"{API_PREFIX}/prepare-upload":
                    self._handle_prepare(query, ip)
                    return
                if route == f"{API_PREFIX}/upload":
                    self._handle_upload(query, ip)
                    return
                if route == f"{API_PREFIX}/cancel":
                    session_id = (query.get("sessionId") or [""])[0]
                    with server._lock:
                        server.sessions.pop(session_id, None)
                    self._empty(200)
                    return
                self._empty(404)

            def do_GET(self) -> None:  # noqa: N802
                route = urlparse(self.path).path
                if route == f"{API_PREFIX}/info":
                    self._json(200, server.info.to_dict())
                    return
                if route == "/":
                    self._json(200, {"service": "localsend", "alias": server.info.alias, "version": server.info.version})
                    return
                self._empty(404)

            def _handle_prepare(self, query: dict[str, list[str]], ip: str) -> None:
                payload = self._body()
                pin = (query.get("pin") or [""])[0]
                if server.pin and pin != server.pin:
                    self._json(401, {})
                    return
                files = payload.get("files") or {}
                info = payload.get("info") or {}
                peer = _peer_from_payload(info, ip, source="uploader")
                if peer:
                    # NOTE: the fingerprint only guards self-*discovery* (section 2) —
                    # it must not gate an upload, or a device could never send to its
                    # own receiver (a local Hermes-to-Hermes transfer is legitimate).
                    with server._lock:
                        server.peers[peer.fingerprint or peer.ip] = peer
                if not isinstance(files, dict) or not files:
                    self._json(400, {})
                    return
                session_id = uuid.uuid4().hex[:16]
                tokens: dict[str, str] = {}
                session_files: dict[str, dict[str, Any]] = {}
                for file_id, meta in files.items():
                    token = uuid.uuid4().hex
                    tokens[file_id] = token
                    session_files[file_id] = meta
                with server._lock:
                    server.sessions[session_id] = {
                        "files": session_files,
                        "tokens": tokens,
                        "ip": ip,
                        "created": time.time(),
                        "peer": peer.alias if peer else "",
                    }
                logger.info("localsend: accepted session %s (%d file(s)) from %s", session_id, len(files), ip)
                self._json(200, {"sessionId": session_id, "files": tokens})

            def _handle_upload(self, query: dict[str, list[str]], ip: str) -> None:
                session_id = (query.get("sessionId") or [""])[0]
                file_id = (query.get("fileId") or [""])[0]
                token = (query.get("token") or [""])[0]
                with server._lock:
                    session = server.sessions.get(session_id)
                if not session:
                    self._json(409, {})
                    return
                if session["tokens"].get(file_id) != token or session["ip"] != ip:
                    self._json(403, {})
                    return
                meta = session["files"].get(file_id) or {}
                expected_size = int(meta.get("size") or 0)
                chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
                if not chunked:
                    # Content-Length is authoritative when present; reject early on a mismatch.
                    length = int(self.headers.get("Content-Length") or 0)
                    if length != expected_size:
                        self._json(400, {})
                        return
                safe_name = os.path.basename(str(meta.get("fileName") or file_id))
                target = _unique_path(os.path.join(server.download_dir, safe_name))
                digest = hashlib.sha256()
                read = 0
                try:
                    with open(target, "wb") as fh:
                        for piece in self._iter_body():
                            read += len(piece)
                            digest.update(piece)
                            fh.write(piece)
                except ValueError as exc:
                    if os.path.exists(target):
                        os.unlink(target)
                    server.errors.append(f"bad body framing for {safe_name}: {exc}")
                    self._json(400, {})
                    return
                if expected_size and read != expected_size:
                    os.unlink(target)
                    server.errors.append(f"size mismatch for {safe_name}: got {read}, expected {expected_size}")
                    self._json(400, {})
                    return
                expected = meta.get("sha256")
                if expected and digest.hexdigest().lower() != str(expected).lower():
                    os.unlink(target)
                    server.errors.append(f"checksum mismatch for {safe_name}")
                    self._json(422, {})
                    return
                record = {
                    "file": safe_name,
                    "path": target,
                    "bytes": read,
                    "sha256": digest.hexdigest(),
                    "fileType": meta.get("fileType", ""),
                    "from": session.get("peer") or ip,
                    "from_ip": ip,
                    "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                with server._lock:
                    server.received.append(record)
                logger.info("localsend: received %s (%d bytes) from %s", safe_name, read, ip)
                self._empty(200)

        if self.https:
            # Announce https *before* binding: the transport is part of our identity
            # in discovery, and peers decide plain-vs-TLS from it.
            self.info.protocol = "https"
        try:
            self._server = ThreadingHTTPServer(("", self.port), Handler)
        except OSError as exc:
            raise LocalSendError(
                f"cannot bind TCP port {self.port}: {exc}. Is another LocalSend instance running?"
            ) from exc
        if self.https:
            try:
                self._server.socket = _server_ssl_context(self.identity).wrap_socket(
                    self._server.socket, server_side=True
                )
            except LocalSendError:
                # Close *and* forget it: a server whose serve_forever never ran cannot
                # be shut down, and stop() on it would block forever.
                self._server.server_close()
                self._server = None
                raise
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        self.started_at = time.time()
        self._announce_thread = threading.Thread(target=self._announce_loop, daemon=True)
        self._announce_thread.start()
        transport = "https (server certificate, fingerprint-pinnable)" if self.https else "http"
        return f"listening on {self.port} over {transport}"

    def _announce_loop(self) -> None:
        """Announce presence periodically so peers keep us in their device list."""
        try:
            sock = _multicast_socket(0)
        except OSError as exc:
            self.errors.append(f"multicast announce disabled: {exc}")
            return
        payload = json.dumps(self.info.to_dict(announce=True)).encode()
        try:
            while not self._stop.is_set():
                try:
                    sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
                except OSError as exc:
                    self.errors.append(f"multicast announce failed: {exc}")
                self._stop.wait(15.0)
        finally:
            sock.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            # shutdown() only returns once serve_forever has exited; calling it on a
            # server that never started blocks the caller indefinitely.
            if self._thread and self._thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        if self._announce_thread:
            self._announce_thread.join(timeout=2)

    def status(self) -> dict[str, Any]:
        alive = bool(self._thread and self._thread.is_alive())
        with self._lock:
            return {
                "running": alive,
                "alias": self.info.alias,
                "port": self.port,
                "protocol": self.info.protocol,
                "https": self.https,
                "identity_fingerprint": getattr(self.identity, "fingerprint", "") if self.https else "",
                "download_dir": self.download_dir,
                "pin_required": bool(self.pin),
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 1) if self.started_at and alive else 0,
                "received": list(self.received),
                "peers_seen": [p.as_dict() for p in self.peers.values()],
                "active_sessions": len(self.sessions),
                "errors": list(self.errors),
            }


def _unique_path(path: str) -> str:
    """Never overwrite: append `` (n)`` before the extension."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{stem} ({index}){ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1
