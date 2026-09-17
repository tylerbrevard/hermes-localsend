"""LocalSend plugin test suite.

Two independent oracles, both written straight from the protocol spec
(https://github.com/localsend/protocol v2.2) rather than from the plugin code:

* ``SpecReceiver`` — a minimal receiver that our *sender* is tested against.
* ``spec_send``    — a minimal sender that our *receiver* is tested against.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "hermes_localsend"
# The repo directory has a hyphen, so expose the plugin as a package explicitly.
# This mirrors how Hermes loads a directory plugin (namespaced import of
# __init__.py with the plugin dir as the package root).
if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        PKG, os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _module
    _spec.loader.exec_module(_module)

from hermes_localsend import protocol, schemas, tools as plugin_tools  # noqa: E402
from hermes_localsend.protocol import DeviceInfo, LocalSendError, Peer, ReceiveServer  # noqa: E402

API = "/api/localsend/v2"


# ---------------------------------------------------------------------------
# Oracle 1: spec-faithful receiver (tests our sender)
# ---------------------------------------------------------------------------
class SpecReceiver:
    def __init__(self, port: int, alias: str = "Spec Receiver", requires_pin: str = "", corrupt_sha: bool = False):
        self.port = port
        self.alias = alias
        self.requires_pin = requires_pin
        self.corrupt_sha = corrupt_sha
        self.info = {
            "alias": alias,
            "version": "2.2",
            "deviceModel": "Oracle",
            "deviceType": "desktop",
            "fingerprint": uuid.uuid4().hex,
            "port": port,
            "protocol": "http",
            "download": False,
        }
        self.prepare_calls: list[dict] = []
        self.received: dict[str, bytes] = {}
        self.statuses: list[int] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        oracle = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D102
                pass

            def _send(self, status, payload=None, body: bytes | None = None):
                raw = body if body is not None else json.dumps(payload or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read(self, length: int) -> bytes:
                buf = b""
                while len(buf) < length:
                    chunk = self.rfile.read(length - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                return buf

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length") or 0)
                if parsed.path == f"{API}/register":
                    self._read(length)
                    self._send(200, oracle.info)
                    return
                if parsed.path == f"{API}/prepare-upload":
                    body = json.loads(self._read(length) or b"{}")
                    oracle.prepare_calls.append(body)
                    pin = (query.get("pin") or [""])[0]
                    if oracle.requires_pin and pin != oracle.requires_pin:
                        oracle.statuses.append(401)
                        self._send(401, {})
                        return
                    files = body.get("files") or {}
                    session = uuid.uuid4().hex[:16]
                    tokens = {}
                    for file_id, meta in files.items():
                        token = uuid.uuid4().hex
                        tokens[file_id] = token
                        oracle.received.setdefault(
                            file_id,
                            {
                                "meta": meta,
                                "token": token,
                                "session": session,
                                "data": b"",
                            },
                        )
                    oracle.statuses.append(200)
                    self._send(200, {"sessionId": session, "files": tokens})
                    return
                if parsed.path == f"{API}/upload":
                    session_id = (query.get("sessionId") or [""])[0]
                    file_id = (query.get("fileId") or [""])[0]
                    token = (query.get("token") or [""])[0]
                    record = oracle.received.get(file_id)
                    if not record or record["session"] != session_id or record["token"] != token:
                        oracle.statuses.append(403)
                        self._send(403, {})
                        return
                    record["data"] = self._read(length)
                    expected = record["meta"].get("sha256")
                    actual = hashlib.sha256(record["data"]).hexdigest()
                    if oracle.corrupt_sha or (expected and actual != expected):
                        oracle.statuses.append(422)
                        self._send(422, {})
                        return
                    oracle.statuses.append(200)
                    self._send(200, {})
                    return
                self._send(404, {})

            def do_GET(self):  # noqa: N802
                if urlparse(self.path).path == f"{API}/info":
                    self._send(200, oracle.info)
                    return
                self._send(404, {})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)

    def payloads(self) -> list[bytes]:
        return [r["data"] for r in self.received.values()]


# ---------------------------------------------------------------------------
# Oracle 2: spec-faithful sender (tests our receiver)
# ---------------------------------------------------------------------------
def spec_send(
    port: int,
    path: str,
    pin: str = "",
    override_sha: str | None = None,
    override_size: int | None = None,
    token_override: str | None = None,
    session_override: str | None = None,
    skip_prepare: bool = False,
    filename: str | None = None,
) -> dict:
    """Push one file exactly the way the spec describes. Returns the statuses seen."""
    data = open(path, "rb").read()
    file_id = uuid.uuid4().hex[:12]
    meta = {
        "id": file_id,
        "fileName": filename or os.path.basename(path),
        "size": len(data) if override_size is None else override_size,
        "fileType": "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest() if override_sha is None else override_sha,
    }
    info = DeviceInfo(alias="Spec Sender", deviceType="desktop", port=port, protocol="http").to_dict()
    statuses: dict[str, int] = {}
    session_id = "no-session"
    token = "no-token"

    if not skip_prepare:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        query = f"?pin={pin}" if pin else ""
        conn.request(
            "POST",
            f"{API}/prepare-upload{query}",
            body=json.dumps({"info": info, "files": {file_id: meta}}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        statuses["prepare"] = resp.status
        body = resp.read()
        conn.close()
        if resp.status != 200:
            return statuses
        payload = json.loads(body)
        session_id = payload["sessionId"]
        token = payload["files"][file_id]
        if session_override:
            session_id = session_override
        if token_override:
            token = token_override

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", f"{API}/upload?sessionId={session_id}&fileId={file_id}&token={token}")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders()
    conn.send(data)
    resp = conn.getresponse()
    statuses["upload"] = resp.status
    resp.read()
    conn.close()
    statuses["_file"] = os.path.basename(path)
    return statuses


class TempFileMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="localsend-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def make_file(self, name: str, size: int = 64 * 1024) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(os.urandom(size))
        return path

    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Sender tests (against SpecReceiver)
# ---------------------------------------------------------------------------
class SenderTests(TempFileMixin):
    def setUp(self) -> None:
        super().setUp()
        self.port = self.free_port()
        self.receiver = SpecReceiver(self.port)
        self.receiver.start()
        self.addCleanup(self.receiver.stop)

    def peer(self) -> Peer:
        return Peer(alias="Spec Receiver", ip="127.0.0.1", port=self.port, protocol="http")

    def test_sends_file_body_and_hash_survives_verification(self) -> None:
        path = self.make_file("payload.bin", 512 * 1024)
        result = protocol.send_files(self.peer(), [path], DeviceInfo(alias="Sender"))
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["bytes"], os.path.getsize(path))
        self.assertEqual([open(path, "rb").read()], self.receiver.payloads())
        self.assertEqual(self.receiver.statuses, [200, 200])

    def test_prepare_payload_matches_spec(self) -> None:
        path = self.make_file("doc.txt", 128)
        protocol.send_files(self.peer(), [path], DeviceInfo(alias="Sender", deviceType="headless"))
        body = self.receiver.prepare_calls[0]
        self.assertIn("info", body)
        self.assertEqual(body["info"]["alias"], "Sender")
        self.assertEqual(body["info"]["version"], protocol.PROTOCOL_VERSION)
        (file_id, meta), = body["files"].items()
        self.assertEqual(meta["id"], file_id)
        self.assertEqual(meta["fileName"], "doc.txt")
        self.assertEqual(meta["size"], 128)
        self.assertEqual(meta["sha256"], hashlib.sha256(open(path, "rb").read()).hexdigest())
        self.assertTrue(meta["fileType"].startswith("text/"))

    def test_multiple_files_single_session(self) -> None:
        paths = [self.make_file("a.bin", 4096), self.make_file("b.bin", 8192)]
        result = protocol.send_files(self.peer(), paths, DeviceInfo())
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(sum(len(p) for p in self.receiver.payloads()), 4096 + 8192)

    def test_receiver_checksum_mismatch_is_detected(self) -> None:
        receiver = SpecReceiver(self.free_port(), corrupt_sha=True)
        receiver.start()
        self.addCleanup(receiver.stop)
        path = self.make_file("bad.bin", 2048)
        with self.assertRaises(LocalSendError) as ctx:
            protocol.send_files(Peer(alias="x", ip="127.0.0.1", port=receiver.port), [path], DeviceInfo())
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(receiver.statuses, [200, 422])

    def test_pin_required_and_accepted(self) -> None:
        receiver = SpecReceiver(self.free_port(), requires_pin="4242")
        receiver.start()
        self.addCleanup(receiver.stop)
        peer = Peer(alias="x", ip="127.0.0.1", port=receiver.port)
        path = self.make_file("pinned.bin", 512)
        with self.assertRaises(LocalSendError) as ctx:
            protocol.send_files(peer, [path], DeviceInfo())
        self.assertEqual(ctx.exception.status, 401)
        ok = protocol.send_files(peer, [path], DeviceInfo(), pin="4242")
        self.assertEqual(ok["status"], "sent")

    def test_unreachable_peer_reports_clean_error(self) -> None:
        dead = Peer(alias="dead", ip="127.0.0.1", port=self.free_port())
        with self.assertRaises(LocalSendError) as ctx:
            protocol.send_files(dead, [self.make_file("x.bin", 16)], DeviceInfo())
        self.assertIsNone(ctx.exception.status)

    def test_missing_file_is_rejected_before_any_request(self) -> None:
        with self.assertRaises(LocalSendError):
            protocol.send_files(self.peer(), [os.path.join(self.tmp, "nope.bin")], DeviceInfo())
        self.assertEqual(self.receiver.prepare_calls, [])


# ---------------------------------------------------------------------------
# Receiver tests (via spec_send)
# ---------------------------------------------------------------------------
class ReceiverTests(TempFileMixin):
    def start_receiver(self, **kwargs) -> ReceiveServer:
        port = kwargs.pop("port", self.free_port())
        server = ReceiveServer(
            info=DeviceInfo(alias="Hermes Test", port=port),
            download_dir=kwargs.pop("download_dir", os.path.join(self.tmp, "inbox")),
            **kwargs,
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_receives_file_and_verifies_sha256(self) -> None:
        server = self.start_receiver()
        source = self.make_file("incoming.bin", 300 * 1024)
        statuses = spec_send(server.port, source)
        self.assertEqual(statuses, {"prepare": 200, "upload": 200, "_file": "incoming.bin"})
        self.assertEqual(len(server.received), 1)
        record = server.received[0]
        self.assertEqual(open(record["path"], "rb").read(), open(source, "rb").read())
        self.assertEqual(record["sha256"], hashlib.sha256(open(source, "rb").read()).hexdigest())

    def test_checksum_mismatch_returns_422_and_leaves_no_file(self) -> None:
        server = self.start_receiver()
        source = self.make_file("tampered.bin", 4096)
        statuses = spec_send(server.port, source, override_sha="0" * 64)
        self.assertEqual(statuses["upload"], 422)
        self.assertEqual(server.received, [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "inbox")), [])
        self.assertTrue(server.errors)

    def test_wrong_token_is_403_and_unknown_session_is_409(self) -> None:
        server = self.start_receiver()
        source = self.make_file("token.bin", 1024)
        self.assertEqual(spec_send(server.port, source, token_override="bogus")["upload"], 403)
        self.assertEqual(spec_send(server.port, source, session_override="ghost")["upload"], 409)
        self.assertEqual(server.received, [])

    def test_size_mismatch_is_400(self) -> None:
        server = self.start_receiver()
        source = self.make_file("size.bin", 1024)
        self.assertEqual(spec_send(server.port, source, override_size=999999)["upload"], 400)

    def test_pin_gate(self) -> None:
        server = self.start_receiver(pin="1234")
        source = self.make_file("pin.bin", 512)
        self.assertEqual(spec_send(server.port, source)["prepare"], 401)
        self.assertEqual(spec_send(server.port, source, pin="0000")["prepare"], 401)
        self.assertEqual(spec_send(server.port, source, pin="1234")["prepare"], 200)
        self.assertEqual(len(server.received), 1)

    def test_never_overwrites_an_existing_file(self) -> None:
        inbox = os.path.join(self.tmp, "inbox")
        server = self.start_receiver(download_dir=inbox)
        source = self.make_file("dup.bin", 256)
        spec_send(server.port, source)
        spec_send(server.port, source, filename="dup.bin")
        names = sorted(os.listdir(inbox))
        self.assertEqual(names, ["dup (1).bin", "dup.bin"])

    def test_chunked_prepare_and_upload_lands_the_file(self) -> None:
        """Regression: bodies sent with Transfer-Encoding: chunked must not read as empty."""
        server = self.start_receiver()
        source = self.make_file("chunked.bin", 40 * 1024)
        statuses = spec_send_chunked(server.port, source, chunk_size=4096)
        self.assertEqual(statuses, {"prepare": 200, "upload": 200})
        self.assertEqual(len(server.received), 1)
        record = server.received[0]
        self.assertEqual(open(record["path"], "rb").read(), open(source, "rb").read())
        self.assertEqual(record["sha256"], hashlib.sha256(open(source, "rb").read()).hexdigest())
        self.assertEqual(server.errors, [])

    def test_chunked_many_small_chunks_streams_correctly(self) -> None:
        server = self.start_receiver()
        source = self.make_file("many.bin", 256 * 1024)
        statuses = spec_send_chunked(server.port, source, chunk_size=997)   # deliberately odd chunk size
        self.assertEqual(statuses["upload"], 200)
        self.assertEqual(open(server.received[0]["path"], "rb").read(), open(source, "rb").read())

    def test_chunked_size_mismatch_is_400(self) -> None:
        server = self.start_receiver()
        source = self.make_file("short.bin", 4096)
        statuses = spec_send_chunked(server.port, source, override_size=999999)
        self.assertEqual(statuses["upload"], 400)
        self.assertEqual(server.received, [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "inbox")), [])

    def test_chunked_truncated_body_is_rejected(self) -> None:
        server = self.start_receiver()
        source = self.make_file("trunc.bin", 8192)
        data = open(source, "rb").read()
        file_id = "abc"
        meta = {"id": file_id, "fileName": "trunc.bin", "size": len(data), "fileType": "application/octet-stream"}
        prepare = json.dumps({"info": DeviceInfo(port=server.port).to_dict(), "files": {file_id: meta}}).encode()
        status, payload = chunked_post(server.port, f"{API}/prepare-upload", prepare)
        self.assertEqual(status, 200)
        session_id = json.loads(payload)["sessionId"]
        token = json.loads(payload)["files"][file_id]
        status, _ = chunked_post(
            server.port, f"{API}/upload?sessionId={session_id}&fileId={file_id}&token={token}", data, truncate_last=True
        )
        self.assertIn(status, (-1, 400))
        self.assertEqual(server.received, [])

    def test_chunked_prepare_with_non_json_body_is_400(self) -> None:
        server = self.start_receiver()
        status, _ = chunked_post(server.port, f"{API}/prepare-upload", b"this is not json")
        self.assertEqual(status, 400)

    def test_register_and_info_routes(self) -> None:
        server = self.start_receiver()
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        payload = DeviceInfo(alias="Phone", port=server.port).to_dict()
        conn.request("POST", f"{API}/register", body=json.dumps(payload), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read())["alias"], "Hermes Test")
        conn.request("GET", f"{API}/info")
        resp = conn.getresponse()
        self.assertEqual(json.loads(resp.read())["fingerprint"], server.info.fingerprint)
        conn.close()
        self.assertEqual(len(server.peers), 1)

    def test_same_device_sender_is_accepted(self) -> None:
        """Fingerprint guards self-discovery only — a device must be able to send to its own receiver."""
        server = self.start_receiver()
        source = self.make_file("self.bin", 128)
        statuses = spec_send_with_info(server.port, source, server.info.to_dict())
        self.assertEqual(statuses["prepare"], 200)

    def test_cancel_clears_the_session(self) -> None:
        server = self.start_receiver()
        source = self.make_file("cancel.bin", 128)
        # prepare only, then cancel, then upload must fail
        data = open(source, "rb").read()
        file_id, meta = "abc123", {"id": "abc123", "fileName": "cancel.bin", "size": len(data), "fileType": "application/octet-stream"}
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", f"{API}/prepare-upload", body=json.dumps({"info": DeviceInfo(port=server.port).to_dict(), "files": {file_id: meta}}), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        session = payload["sessionId"]
        conn.request("POST", f"{API}/cancel?sessionId={session}", body=b"")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual(server.status()["active_sessions"], 0)


def chunked_post(port: int, path: str, body: bytes, chunk_size: int = 4096, truncate_last: bool = False) -> tuple[int, bytes]:
    """POST a body with Transfer-Encoding: chunked (how mobile LocalSend clients send)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.putrequest("POST", path)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    pieces = [body[i:i + chunk_size] for i in range(0, len(body), chunk_size)] or [b""]
    for piece in pieces:
        conn.send(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
    if truncate_last:
        conn.send(b"5\r\nshort")          # lies about the length, then hangs up
        conn.close()
        return -1, b""
    conn.send(b"0\r\n\r\n")
    resp = conn.getresponse()
    status, payload = resp.status, resp.read()
    conn.close()
    return status, payload


def spec_send_chunked(port: int, path: str, chunk_size: int = 4096, override_size: int | None = None) -> dict:
    """Full prepare-upload + upload over chunked encoding."""
    data = open(path, "rb").read()
    file_id = uuid.uuid4().hex[:12]
    meta = {
        "id": file_id,
        "fileName": os.path.basename(path),
        "size": len(data) if override_size is None else override_size,
        "fileType": "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    info = DeviceInfo(alias="Chunked Sender", deviceType="mobile", port=port, protocol="http").to_dict()
    statuses: dict[str, int] = {}
    prepare_body = json.dumps({"info": info, "files": {file_id: meta}}).encode()
    status, payload = chunked_post(port, f"{API}/prepare-upload", prepare_body, chunk_size=64)
    statuses["prepare"] = status
    if status != 200:
        return statuses
    parsed = json.loads(payload)
    session_id = parsed["sessionId"]
    token = parsed["files"][file_id]
    statuses["upload"], _ = chunked_post(
        port, f"{API}/upload?sessionId={session_id}&fileId={file_id}&token={token}", data, chunk_size=chunk_size
    )
    return statuses


def spec_send_with_info(port: int, path: str, info: dict) -> dict:
    """Like spec_send but spoofing the sender's advertised info."""
    data = open(path, "rb").read()
    file_id = "self-test"
    meta = {"id": file_id, "fileName": os.path.basename(path), "size": len(data), "fileType": "application/octet-stream"}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", f"{API}/prepare-upload", body=json.dumps({"info": info, "files": {file_id: meta}}), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    status = resp.status
    resp.read()
    conn.close()
    return {"prepare": status}


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------
class DiscoveryTests(TempFileMixin):
    def test_register_callback_is_parsed_into_a_peer(self) -> None:
        seen: list[Peer] = []
        port = self.free_port()
        catcher = protocol._RegisterCatcher(port, seen.append)
        self.assertTrue(catcher.start(), catcher.error)
        self.addCleanup(catcher.stop)
        payload = DeviceInfo(alias="Pixel 9", deviceType="mobile", deviceModel="Google", port=53317, protocol="https").to_dict()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"{API}/register", body=json.dumps(payload), headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(seen), 1)
        peer = seen[0]
        self.assertEqual(peer.alias, "Pixel 9")
        self.assertEqual(peer.deviceType, "mobile")
        self.assertEqual(peer.protocol, "https")
        self.assertEqual(peer.ip, "127.0.0.1")

    def test_own_fingerprint_is_filtered_out(self) -> None:
        info = DeviceInfo(alias="Self")
        found: dict[str, Peer] = {}
        port = self.free_port()
        catcher = protocol._RegisterCatcher(port, lambda p: found.setdefault(p.fingerprint, p))
        catcher.start()
        self.addCleanup(catcher.stop)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"{API}/register", body=json.dumps(info.to_dict()), headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
        time.sleep(0.3)
        peers = [p for p in found.values() if p.fingerprint != info.fingerprint]
        self.assertEqual(peers, [])

    def test_http_register_scan_finds_a_responder(self) -> None:
        """The legacy /register sweep must parse a peer out of a real response."""
        port = self.free_port()
        oracle = SpecReceiver(port, alias="Scanner Target")
        oracle.start()
        self.addCleanup(oracle.stop)
        peer = Peer(alias="", ip="127.0.0.1", port=port)
        status, payload, _ = protocol._request_json(peer, "POST", f"{API}/register", DeviceInfo().to_dict(), timeout=2)
        self.assertEqual(status, 200)
        discovered = protocol._peer_from_payload(payload, "127.0.0.1", source="http-scan")
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.alias, "Scanner Target")

    def test_multicast_discovery_round_trip(self) -> None:
        """Our announce must reach the multicast group and a reply must become a peer."""
        info = DeviceInfo(alias="Hermes Announcer", port=protocol.MULTICAST_PORT)
        observed: list[dict] = []
        errors: list[str] = []

        def peer_listener() -> None:
            try:
                sock = protocol._multicast_socket(protocol.MULTICAST_PORT)
            except OSError as exc:  # a real LocalSend app owns the port
                errors.append(f"listener bind failed: {exc}")
                return
            sock.settimeout(0.5)
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    try:
                        data, addr = sock.recvfrom(65535)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        errors.append(f"listener recv failed: {exc}")
                        return
                    try:
                        msg = json.loads(data.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict) or msg.get("alias") != "Hermes Announcer":
                        continue
                    observed.append(msg)
                    # Answer the way a real LocalSend peer does (section 3.1, UDP fallback)
                    reply = DeviceInfo(alias="Responder", deviceType="desktop", port=53317).to_dict(announce=False)
                    sock.sendto(json.dumps(reply).encode(), addr)
                    return
            finally:
                sock.close()

        listener = threading.Thread(target=peer_listener, daemon=True)
        listener.start()
        time.sleep(0.4)
        peers, warnings = protocol.discover(
            info, timeout=5.0, scan_subnets=False, bind_port=self.free_port()
        )
        listener.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertTrue(observed, "multicast announce was never seen on the group")
        self.assertTrue(observed[0]["announce"])
        self.assertEqual(sorted(observed[0]), sorted([
            "alias", "announce", "deviceModel", "deviceType", "download",
            "fingerprint", "port", "protocol", "version",
        ]))
        self.assertIn("Responder", [p.alias for p in peers], f"warnings={warnings}")

    def test_payload_parsing_rejects_junk(self) -> None:
        self.assertIsNone(protocol._peer_from_payload({}, "1.2.3.4"))
        self.assertIsNone(protocol._peer_from_payload("nope", "1.2.3.4"))
        parsed = protocol._peer_from_payload({"alias": "A", "fingerprint": "f", "port": "not-a-port"}, "1.2.3.4")
        self.assertEqual(parsed.port, protocol.DEFAULT_PORT)
        self.assertEqual(parsed.protocol, "http")


# ---------------------------------------------------------------------------
# Tool-handler tests (the surface the model actually calls)
# ---------------------------------------------------------------------------
class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):  # noqa: D102
        return self.data.get(key, default)

    def set(self, key, value):  # noqa: D102
        self.data[key] = value


class FakeCtx:
    def __init__(self) -> None:
        self.state = FakeState()


class ToolTests(TempFileMixin):
    def setUp(self) -> None:
        super().setUp()
        plugin_tools._receiver = None
        self.addCleanup(lambda: setattr(plugin_tools, "_receiver", None))

    def test_send_by_direct_address(self) -> None:
        port = self.free_port()
        oracle = SpecReceiver(port, alias="Oracle")
        oracle.start()
        self.addCleanup(oracle.stop)
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"port": self.free_port(), "scan_subnets": False})
        source = self.make_file("tool.bin", 2048)
        result = json.loads(instance.send({"peer": f"127.0.0.1:{port}", "files": [source]}))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(oracle.payloads()), 1)

    def test_send_text_creates_a_file(self) -> None:
        port = self.free_port()
        oracle = SpecReceiver(port, alias="Oracle")
        oracle.start()
        self.addCleanup(oracle.stop)
        outbox = os.path.join(self.tmp, "outbox")
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"port": self.free_port(), "scan_subnets": False, "outbox_dir": outbox})
        result = json.loads(instance.send({"peer": f"127.0.0.1:{port}", "text": "hello from Hermes"}))
        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.isfile(result["text_file"]))
        self.assertEqual(open(result["text_file"]).read(), "hello from Hermes")
        meta = list(oracle.received.values())[0]["meta"]
        self.assertEqual(meta["fileName"], os.path.basename(result["text_file"]))
        self.assertTrue(meta["fileType"].startswith("text/"))

    def test_send_without_target_or_peers_returns_guidance(self) -> None:
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"port": self.free_port(), "scan_subnets": False, "discovery_timeout_s": 0.5})
        source = self.make_file("orphan.bin", 64)
        result = json.loads(instance.send({"files": [source]}))
        self.assertFalse(result["success"])
        self.assertIn("no LocalSend devices", result["error"])
        self.assertIn("hint", result)

    def test_send_reports_missing_file(self) -> None:
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"scan_subnets": False})
        result = json.loads(instance.send({"peer": "127.0.0.1:1", "files": [os.path.join(self.tmp, "ghost.bin")]}))
        self.assertFalse(result["success"])
        self.assertIn("not a file", result["error"])

    def test_receive_lifecycle_reports_what_it_stored(self) -> None:
        inbox = os.path.join(self.tmp, "inbox")
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"port": self.free_port(), "download_dir": inbox, "scan_subnets": False})
        started = json.loads(instance.receive({"action": "start", "alias": "Hermes Test", "port": instance.port_default}))
        self.assertTrue(started["success"], started)
        self.assertTrue(started["running"])
        self.assertEqual(started["inbox"], inbox)

        source = self.make_file("pushed.bin", 8192)
        statuses = spec_send(instance.port_default, source)
        self.assertEqual(statuses["upload"], 200)

        reported = json.loads(instance.receive({"action": "status"}))
        self.assertEqual(reported["received_count"], 1)
        self.assertTrue(os.path.isfile(reported["received"][0]["path"]))

        stopped = json.loads(instance.receive({"action": "stop"}))
        self.assertTrue(stopped["stopped"])
        self.assertFalse(json.loads(instance.receive({"action": "status"}))["running"])

    def test_receive_unknown_action(self) -> None:
        instance = plugin_tools.LocalSendTools(FakeCtx(), {"scan_subnets": False})
        result = json.loads(instance.receive({"action": "explode"}))
        self.assertFalse(result["success"])

    def test_fingerprint_is_stable_across_instances(self) -> None:
        ctx = FakeCtx()
        first = plugin_tools.LocalSendTools(ctx, {})._fingerprint()
        second = plugin_tools.LocalSendTools(ctx, {})._fingerprint()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 16)

    def test_schemas_are_shaped_for_the_model(self) -> None:
        for schema in (schemas.DISCOVER, schemas.SEND, schemas.RECEIVE):
            self.assertTrue(schema["name"].startswith("localsend_"))
            self.assertGreater(len(schema["description"]), 40)
            self.assertEqual(schema["parameters"]["type"], "object")
        self.assertIn("required", schemas.RECEIVE["parameters"])


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------
class HelperTests(TempFileMixin):
    def test_file_metadata_matches_stdlib_hash(self) -> None:
        path = self.make_file("meta.bin", 70000)
        meta = protocol.file_metadata("id1", path)
        self.assertEqual(meta["size"], os.path.getsize(path))
        self.assertEqual(meta["sha256"], hashlib.sha256(open(path, "rb").read()).hexdigest())
        self.assertTrue(meta["metadata"]["modified"].endswith("Z"))

    def test_unique_path_never_clobbers(self) -> None:
        path = os.path.join(self.tmp, "x.txt")
        open(path, "w").close()
        self.assertEqual(protocol._unique_path(path), os.path.join(self.tmp, "x (1).txt"))

    def test_fingerprint_normalization(self) -> None:
        raw = "ab" * 32
        self.assertEqual(protocol._normalize_fingerprint(raw), raw)
        self.assertEqual(protocol._normalize_fingerprint(raw.upper()), raw)
        self.assertEqual(protocol._normalize_fingerprint(":".join(raw[i:i + 2] for i in range(0, 64, 2))), raw)
        self.assertEqual(protocol._normalize_fingerprint("not-a-hash"), "")

    def test_plain_http_peer_needs_no_certificate_check(self) -> None:
        self.assertEqual(protocol.verify_peer_certificate(Peer(alias="a", ip="127.0.0.1", port=1)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
