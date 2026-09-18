"""Hermes LocalSend plugin — registration entry point.

Exposes three tools:
  * localsend_discover — find LocalSend peers on the LAN
  * localsend_send     — push files/text to a peer (upload API)
  * localsend_receive  — run a headless receiver so peers can push files here
"""

from __future__ import annotations

import logging

from . import schemas
from .tools import LocalSendTools

logger = logging.getLogger(__name__)

# Keys read from plugins.entries.localsend.settings (all optional).
_SETTING_DEFAULTS = {
    "alias": "",
    "port": 53317,
    "download_dir": "",
    "outbox_dir": "",
    "pin": "",
    "discovery_timeout_s": 3,
    "send_timeout_s": 120,
    "scan_subnets": True,
    "identity_dir": "",
    "receive_https": False,
    "share_roots": [],
    "allow_public_peers": False,
    "max_transfer_bytes": 2 * 1024 ** 3,
}


def _load_settings(ctx) -> dict:
    settings = {}
    for key, default in _SETTING_DEFAULTS.items():
        try:
            settings[key] = ctx.get_config(key, default=default)
        except Exception:  # pragma: no cover - config surface is best-effort
            settings[key] = default
    return settings


def register(ctx):
    """Wire schemas to handlers."""
    tools = LocalSendTools(ctx, _load_settings(ctx))

    ctx.register_tool(
        name="localsend_discover",
        toolset="localsend",
        schema=schemas.DISCOVER,
        handler=tools.discover,
    )
    ctx.register_tool(
        name="localsend_send",
        toolset="localsend",
        schema=schemas.SEND,
        handler=tools.send,
    )
    ctx.register_tool(
        name="localsend_receive",
        toolset="localsend",
        schema=schemas.RECEIVE,
        handler=tools.receive,
    )
    logger.info("localsend plugin registered: discover, send, receive")
