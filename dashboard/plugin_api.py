"""LocalSend backend for the Hermes dashboard and desktop app.

Routes mount under ``/api/plugins/localsend/`` and are served from inside the
Hermes process, so this module drives the same in-process receiver the agent's
``localsend_*`` tools use whenever the plugin package is already imported.
Falls back to loading its own copy of the package when it is not.

Every route answers with ``{"ok": bool, ...}`` and never raises: the desktop UI
renders the error string instead of a blank pane.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import socket
import sys
import time
from typing import Any, Optional

from fastapi import APIRouter

router = APIRouter()
_log = logging.getLogger(__name__)

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_INBOX = os.path.join(os.path.expanduser("~"), "Downloads", "LocalSend")
_load_error: Optional[str] = None


def _loaded_package() -> Optional[Any]:
    """Reuse an already-imported copy of this plugin, if the host process has one.

    The agent half and this backend must share one receiver singleton, otherwise
    two processes each try to bind the LocalSend port and the UI reports a state
    the agent's tools cannot see.
    """
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        if os.path.dirname(os.path.abspath(path)) == PLUGIN_DIR and hasattr(module, "tools"):
            return module
    for name, module in list(sys.modules.items()):
        if name == "localsend" and hasattr(module, "tools"):
            return module
    return None


def _load() -> Any:
    """Return the plugin package (shared copy when available, own copy otherwise)."""
    global _load_error
    package = _loaded_package()
    if package is not None:
        return package
    try:
        spec = importlib.util.spec_from_file_location(
            "localsend_api_backend",
            os.path.join(PLUGIN_DIR, "__init__.py"),
            submodule_search_locations=[PLUGIN_DIR],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot build an import spec for {PLUGIN_DIR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["localsend_api_backend"] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # pragma: no cover - surfaced to the UI instead
        _load_error = f"{type(exc).__name__}: {exc}"
        _log.warning("localsend: backend cannot load the plugin package: %s", _load_error)
        raise


def _tools():
    """A LocalSendTools bound to this process, configured from the same settings."""
    module = _load()
    settings = {
        "port": int(os.environ.get("LOCALSEND_PORT") or 53317),
        "download_dir": _inbox_dir(),
    }
    return module.tools.LocalSendTools(_StatefulContext(), settings), module


class _State:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class _StatefulContext:
    """Minimal ctx stand-in: the tools only need a place to keep their fingerprint."""

    def __init__(self) -> None:
        self.state = _State()


def _inbox_dir() -> str:
    configured = os.environ.get("LOCALSEND_DOWNLOAD_DIR") or ""
    return os.path.expanduser(configured or _DEFAULT_INBOX)


def _port() -> int:
    try:
        return int(os.environ.get("LOCALSEND_PORT") or 53317)
    except (TypeError, ValueError):
        return 53317


def _local_ips() -> list[str]:
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 80))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(a for a in addrs if not a.startswith("127."))


def _inbox_files(limit: int = 50) -> list[dict[str, Any]]:
    """Read the inbox straight off disk: works even when the receiver is stopped."""
    inbox = _inbox_dir()
    try:
        entries = [os.path.join(inbox, name) for name in os.listdir(inbox)]
    except OSError:
        return []
    files = []
    for path in entries:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        files.append(
            {
                "name": os.path.basename(path),
                "path": path,
                "bytes": stat.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
                "mtime": stat.st_mtime,
            }
        )
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files[:limit]


def _listening() -> bool:
    """Is anything (this process or another) holding the LocalSend port?"""
    try:
        with socket.create_connection(("127.0.0.1", _port()), timeout=0.6):
            return True
    except OSError:
        return False


def _error(exc: Exception, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}", **extra}


@router.get("/status")
async def status() -> dict[str, Any]:
    """Receiver + inbox snapshot for the desktop pane."""
    payload: dict[str, Any] = {
        "ok": True,
        "port": _port(),
        "inbox": _inbox_dir(),
        "addresses": _local_ips(),
        "listening": _listening(),
        "running": False,
        "received": [],
        "peers": [],
        "errors": [],
    }
    try:
        tools, _module = _tools()
        snapshot = await asyncio.to_thread(lambda: tools.receive({"action": "status"}))
        import json

        state = json.loads(snapshot)
        payload.update(
            {
                "running": bool(state.get("running")),
                "alias": state.get("alias"),
                "pin_required": bool(state.get("pin_required")),
                "received": state.get("received", []),
                "peers": state.get("peers_seen", []),
                "errors": state.get("errors", []),
                "uptime_s": state.get("uptime_s", 0),
            }
        )
        if not payload["received"]:
            payload["received"] = _inbox_files()
    except Exception as exc:
        payload.update(_error(exc))
    return payload


@router.get("/inbox")
async def inbox(limit: int = 50) -> dict[str, Any]:
    return {"ok": True, "inbox": _inbox_dir(), "files": _inbox_files(limit=limit)}


@router.post("/start")
async def start(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    body = body or {}
    try:
        tools, _module = _tools()
        import json

        result = json.loads(
            await asyncio.to_thread(
                lambda: tools.receive(
                    {
                        "action": "start",
                        "alias": body.get("alias"),
                        "port": body.get("port"),
                        "pin": body.get("pin"),
                    }
                )
            )
        )
        return result
    except Exception as exc:
        return _error(exc)


@router.post("/stop")
async def stop() -> dict[str, Any]:
    try:
        tools, _module = _tools()
        import json

        return json.loads(await asyncio.to_thread(lambda: tools.receive({"action": "stop"})))
    except Exception as exc:
        return _error(exc)


@router.get("/devices")
async def devices(timeout_s: float = 3.0) -> dict[str, Any]:
    """Discover LocalSend peers from the process hosting this backend."""
    try:
        tools, module = _tools()
        import json

        payload = json.loads(
            await asyncio.to_thread(lambda: tools.discover({"timeout_s": timeout_s, "scan_subnets": False}))
        )
        payload["backend_host"] = socket.gethostname()
        payload["send_capable"] = True
        return payload
    except Exception as exc:
        return _error(exc, peers=[])


@router.post("/send")
async def send(body: dict[str, Any]) -> dict[str, Any]:
    """Send files from this machine to a peer (used by the desktop 'send' action)."""
    try:
        tools, _module = _tools()
        import json

        payload = json.loads(
            await asyncio.to_thread(
                lambda: tools.send(
                    {"peer": body.get("peer"), "files": body.get("files") or [], "pin": body.get("pin")}
                )
            )
        )
        return payload
    except Exception as exc:
        return _error(exc)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": _load_error is None,
        "plugin_dir": PLUGIN_DIR,
        "load_error": _load_error,
        "shared_package": _loaded_package() is not None,
    }
