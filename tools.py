"""Tool handlers for the LocalSend plugin.

Handlers follow the Hermes plugin contract: ``handler(args: dict, **kwargs) -> str``
returning a JSON string, never raising.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Optional

from . import certs, protocol
from .protocol import DeviceInfo, LocalSendError, Peer, ReceiveServer

logger = logging.getLogger(__name__)

DEFAULT_ALIAS_PREFIX = "Hermes"
DEFAULT_INBOX = os.path.join("~", "Downloads", "LocalSend")
DEFAULT_IDENTITY_DIR = os.path.join("~", ".hermes", "localsend-identity")

# Receiver is a process-wide singleton: one port, one inbox per Hermes process.
_receiver: Optional[ReceiveServer] = None
_receiver_lock = threading.Lock()


def default_device_type() -> str:
    import platform

    system = platform.system().lower()
    if system == "darwin" or system == "windows":
        return "desktop"
    return "headless"


def default_device_model() -> str:
    import platform

    return f"{platform.system()} {platform.machine()}".strip()


class LocalSendTools:
    """Bound to one plugin registration: settings come from ctx.get_config."""

    def __init__(self, ctx: Any, settings: dict[str, Any]):
        self.ctx = ctx
        self.settings = settings
        self._identity: Optional[certs.Identity] = None

    # -- helpers -----------------------------------------------------------
    @property
    def alias_default(self) -> str:
        import socket as _socket

        configured = str(self.settings.get("alias") or "").strip()
        if configured:
            return configured
        host = _socket.gethostname().split(".")[0]
        return f"{DEFAULT_ALIAS_PREFIX} ({host})"

    @property
    def port_default(self) -> int:
        try:
            return int(self.settings.get("port") or protocol.DEFAULT_PORT)
        except (TypeError, ValueError):
            return protocol.DEFAULT_PORT

    @property
    def inbox_default(self) -> str:
        configured = str(self.settings.get("download_dir") or "").strip()
        return os.path.expanduser(configured or DEFAULT_INBOX)

    @property
    def pin_default(self) -> str:
        return str(self.settings.get("pin") or "").strip()

    @property
    def identity_dir(self) -> str:
        configured = str(self.settings.get("identity_dir") or "").strip()
        return os.path.expanduser(configured or DEFAULT_IDENTITY_DIR)

    def identity(self) -> certs.Identity:
        """Device certificate for HTTPS peers (created on first use)."""
        if self._identity is None:
            self._identity = certs.generate(self.identity_dir)
            protocol.set_identity(self._identity)
            logger.info("localsend: device identity %s", self._identity.fingerprint)
        return self._identity

    def _fingerprint(self) -> str:
        """Stable per-profile device fingerprint (peers remember devices by it)."""
        state = getattr(self.ctx, "state", None)
        if state is not None:
            existing = state.get("fingerprint", default="")
            if isinstance(existing, str) and len(existing) >= 16:
                return existing
            import uuid

            fresh = uuid.uuid4().hex
            try:
                state.set("fingerprint", fresh)
            except Exception:  # state is a convenience, never a hard failure
                logger.debug("localsend: could not persist fingerprint", exc_info=True)
            return fresh
        return protocol.DeviceInfo().fingerprint

    def device_info(
        self,
        alias: str = "",
        port: int = 0,
        protocol_name: str = "http",
        fingerprint: str = "",
    ) -> DeviceInfo:
        return DeviceInfo(
            alias=alias or self.alias_default,
            deviceType=default_device_type(),
            deviceModel=default_device_model(),
            fingerprint=fingerprint or self._fingerprint(),
            port=port or self.port_default,
            protocol=protocol_name,
            download=False,
        )

    def _receiver_snapshot(self) -> Optional[ReceiveServer]:
        with _receiver_lock:
            return _receiver

    @staticmethod
    def _ok(payload: dict[str, Any]) -> str:
        payload.setdefault("success", True)
        return json.dumps(payload, indent=2, default=str)

    @staticmethod
    def _err(message: str, **extra: Any) -> str:
        payload: dict[str, Any] = {"success": False, "error": message}
        payload.update(extra)
        return json.dumps(payload, indent=2, default=str)

    # -- discover ----------------------------------------------------------
    def discover(self, args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            timeout = float(args.get("timeout_s") or self.settings.get("discovery_timeout_s") or 3.0)
        except (TypeError, ValueError):
            timeout = 3.0
        timeout = max(0.5, min(15.0, timeout))
        scan = args.get("scan_subnets")
        scan_subnets = bool(self.settings.get("scan_subnets", True)) if scan is None else bool(scan)

        port = self.port_default
        warnings: list[str] = []
        peers: dict[str, Peer] = {}

        running = self._receiver_snapshot()
        if running and running.port == port:
            # Our own receiver already owns the port; reuse its bookkeeping and
            # let its announce loop do the advertising.
            with running._lock:
                for key, peer in running.peers.items():
                    peers[key] = peer
            warnings.append(
                "receiver is running on this port; reusing its peer list (peers it saw while we announced)"
            )
            found: list[Peer] = []
        else:
            info = self.device_info(port=port)
            found, warnings = protocol.discover(
                info, timeout=timeout, scan_subnets=scan_subnets, bind_port=port
            )
        for peer in found:
            peers[peer.fingerprint or peer.ip] = peer

        return self._ok(
            {
                "count": len(peers),
                "peers": [p.as_dict() for p in sorted(peers.values(), key=lambda p: p.alias.lower())],
                "our_alias": self.alias_default,
                "port": port,
                "warnings": warnings,
                "hint": (
                    "Pass a peer's alias or ip to localsend_send. Devices must be on the same LAN or "
                    "reachable subnet, and the LocalSend app must be open on the other device."
                ),
            }
        )

    def _scan_now(self, port: int, scan_subnets: bool) -> list[Peer]:
        """Lightweight HTTP-only sweep used to top up results."""
        if not scan_subnets:
            return []
        info = self.device_info(port=port)
        found: dict[str, Peer] = {}
        protocol._scan_subnet(port, 0.6, info.to_dict(), found, protocol._local_ipv4_addresses())
        return list(found.values())

    # -- send --------------------------------------------------------------
    def send(self, args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        target = str(args.get("peer") or "").strip()
        paths = [os.path.expanduser(str(p)) for p in (args.get("files") or []) if str(p).strip()]
        text = args.get("text")
        pin = args.get("pin")
        pin = self.pin_default if pin is None else str(pin).strip()

        if text is not None and str(text) != "":
            outbox = os.path.expanduser(str(self.settings.get("outbox_dir") or "~/.hermes/localsend-outbox"))
            os.makedirs(outbox, exist_ok=True)
            stamped = time.strftime("%Y%m%d-%H%M%S")
            text_path = os.path.join(outbox, f"hermes-note-{stamped}.txt")
            with open(text_path, "w", encoding="utf-8") as fh:
                fh.write(str(text))
            paths.append(text_path)

        if not paths:
            return self._err("nothing to send: provide 'files', 'text', or both")

        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            return self._err(f"not a file: {', '.join(missing)}")

        discovered: list[dict[str, Any]] = []
        if not target or bool(args.get("discover")):
            try:
                payload = json.loads(self.discover({"timeout_s": self.settings.get("discovery_timeout_s", 3)}, ))
            except Exception as exc:  # pragma: no cover - defensive
                return self._err(f"discovery failed: {exc}")
            discovered = payload.get("peers", [])
            if not target:
                if len(discovered) == 1:
                    target = discovered[0]["alias"]
                elif not discovered:
                    return self._err(
                        "no LocalSend devices found on the network",
                        peers=[],
                        hint="Open the LocalSend app on the target device, confirm both devices share a network, then retry or pass 'peer' as an IP.",
                    )
                else:
                    return self._err(
                        "multiple LocalSend devices found — specify which one",
                        peers=[p.get("alias") for p in discovered],
                    )

        scheme = str(args.get("scheme") or "").strip().lower()
        if scheme and scheme not in {"http", "https"}:
            return self._err(f"scheme must be http or https, got '{scheme}'")
        try:
            peer = self._resolve_peer(target, discovered, scheme=scheme)
        except LocalSendError as exc:
            return self._err(str(exc), peers=[p.get("alias") for p in discovered])

        # Encrypted peers identify us by the certificate we present, not by the
        # random HTTP-mode fingerprint, so load the device identity first and pin
        # the peer's certificate against the fingerprint it announced.
        fingerprint = ""
        identity_note = ""
        if peer.protocol == "https":
            try:
                identity = self.identity()
                protocol.set_identity(identity)
                fingerprint = identity.fingerprint
                identity_note = protocol.verify_peer_certificate(peer)
            except certs.IdentityError as exc:
                return self._err(f"cannot prepare the device certificate: {exc}", peer=peer.as_dict())
            except LocalSendError as exc:
                return self._err(str(exc), peer=peer.as_dict())

        try:
            result = protocol.send_files(
                peer,
                paths,
                self.device_info(
                    port=self.port_default,
                    protocol_name=peer.protocol,
                    fingerprint=fingerprint,
                ),
                pin=pin,
                timeout=float(self.settings.get("send_timeout_s") or 120),
            )
        except LocalSendError as exc:
            return self._err(str(exc), peer=peer.as_dict(), http_status=exc.status)
        except Exception as exc:
            return self._err(f"send failed: {exc}", peer=peer.as_dict())

        result["peer"] = peer.as_dict()
        result["text_file"] = paths[-1] if text else None
        if fingerprint:
            result["identity_fingerprint"] = fingerprint
        if identity_note:
            result["note"] = identity_note
        return self._ok(result)

    def _resolve_peer(self, target: str, discovered: list[dict[str, Any]], scheme: str = "") -> Peer:
        """Match 'alias' | ip | ip:port against discovery results (or use it directly)."""
        needle = target.strip().strip("'\"").lower()
        if not needle:
            raise LocalSendError("no peer specified")

        for item in discovered:
            if needle in {str(item.get("alias", "")).lower(), str(item.get("ip", "")).lower(), str(item.get("base_url", "")).lower()}:
                return Peer(
                    alias=item["alias"],
                    ip=item["ip"],
                    port=int(item["port"]),
                    protocol=item["protocol"],
                    fingerprint=item.get("fingerprint", ""),
                    deviceModel=item.get("deviceModel", ""),
                    deviceType=item.get("deviceType", ""),
                    download=bool(item.get("download", False)),
                    source="discovery",
                )
        for item in discovered:  # substring alias match
            if needle and needle in str(item.get("alias", "")).lower():
                return self._resolve_peer(str(item["alias"]), discovered, scheme=scheme)

        if not discovered:
            fresh = self._scan_now(self.port_default, True)
            if fresh:
                return self._resolve_peer(target, [p.as_dict() for p in fresh], scheme=scheme)

        # Direct address form: ip or ip:port (no discovery needed)
        host, _, port_text = needle.partition(":")
        if host.count(".") == 3 and all(part.isdigit() for part in host.split(".")):
            port = int(port_text) if port_text.isdigit() else self.port_default
            # A directly addressed peer has no advertisement to read the transport
            # from, so it must be stated; HTTP stays the default.
            return Peer(alias=needle, ip=host, port=port, protocol=scheme or "http", source="direct")

        known = ", ".join(f"{p.get('alias')} ({p.get('ip')})" for p in discovered) or "none"
        raise LocalSendError(f"no LocalSend device matched '{target}'. Currently visible: {known}")

    # -- receive -----------------------------------------------------------
    def receive(self, args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        action = str(args.get("action") or "status").lower()
        global _receiver

        if action == "start":
            with _receiver_lock:
                if _receiver and _receiver._thread and _receiver._thread.is_alive():
                    return self._ok(
                        {
                            "already_running": True,
                            "message": "receiver already listening; use action=status to read the inbox",
                            **self._public_status(_receiver),
                        }
                    )
                alias = str(args.get("alias") or self.alias_default)
                try:
                    port = int(args.get("port") or self.port_default)
                except (TypeError, ValueError):
                    port = self.port_default
                download_dir = os.path.expanduser(str(args.get("download_dir") or self.inbox_default))
                pin = args.get("pin")
                pin = self.pin_default if pin is None else str(pin).strip()

                info = self.device_info(alias=alias, port=port)
                server = ReceiveServer(info=info, download_dir=download_dir, pin=pin)
                try:
                    detail = server.start()
                except LocalSendError as exc:
                    return self._err(str(exc))
                _receiver = server

            return self._ok(
                {
                    "message": f"LocalSend receiver up ({detail}); peers will see '{alias}'",
                    **self._public_status(server),
                    "next": "Run localsend_receive with action='status' after the sender accepts, to collect the files.",
                }
            )

        if action == "stop":
            with _receiver_lock:
                server = _receiver
                _receiver = None
            if not server:
                return self._ok({"stopped": False, "message": "receiver was not running"})
            server.stop()
            # Snapshot AFTER stopping: taken before, it reported running=True next to
            # stopped=True and readers (the pane's chip, an agent) got a contradiction.
            snapshot = self._public_status(server)
            return self._ok({"stopped": True, **snapshot})

        if action == "status":
            server = self._receiver_snapshot()
            if not server:
                return self._ok(
                    {
                        "running": False,
                        "message": "receiver is not running — start it with action='start' to receive files",
                        "download_dir": self.inbox_default,
                    }
                )
            return self._ok(self._public_status(server))

        return self._err(f"unknown action '{action}' (expected start, status, or stop)")

    @staticmethod
    def _public_status(server: ReceiveServer) -> dict[str, Any]:
        status = server.status()
        status["inbox"] = status.pop("download_dir")
        status["received_count"] = len(status.get("received", []))
        return status


def temp_dir() -> str:  # pragma: no cover - convenience for manual testing
    return tempfile.mkdtemp(prefix="localsend-")
