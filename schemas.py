"""Tool schemas — the model-facing surface of the LocalSend plugin."""

DISCOVER = {
    "name": "localsend_discover",
    "description": (
        "Discover LocalSend devices on the local network (phones, desktops, other Hermes instances). "
        "Returns each peer's alias, IP, port, protocol, device type and fingerprint. "
        "Call this before localsend_send to find the target device. "
        "Discovery uses multicast UDP plus a parallel HTTP /register scan of the local /24, so it works "
        "even when a device ignores multicast."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timeout_s": {
                "type": "number",
                "description": "Seconds to listen for multicast replies (default 3, max 15).",
            },
            "scan_subnets": {
                "type": "boolean",
                "description": "Also probe every host on the local /24 over HTTP (default true). Slower but finds non-multicasting devices.",
            },
        },
    },
}

SEND = {
    "name": "localsend_send",
    "description": (
        "Send files to a LocalSend device (phone, desktop, or another LocalSend instance) over the LAN. "
        "Runs the protocol's prepare-upload handshake and uploads each file with a sha256 the receiver verifies. "
        "The receiver must accept the transfer — on a phone that means the user taps Accept. "
        "Identify the target with 'peer' (alias, IP, or IP:port from localsend_discover) or set 'discover' to "
        "auto-pick when exactly one device is present. Use 'text' to send a short message as a text file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {
                "type": "string",
                "description": "Target device: alias (e.g. 'Tyler's iPhone'), IP, or IP:port. Case-insensitive.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths of files to send. Must live under a share root "
                    "(~/.hermes/media, ~/.hermes/output, the inbox, or a configured share_roots entry)."
                ),
            },
            "text": {
                "type": "string",
                "description": "Convenience: send this text as a .txt file instead of (or alongside) 'files'.",
            },
            "discover": {
                "type": "boolean",
                "description": "Run discovery first and auto-select when exactly one peer is found (default false).",
            },
            "pin": {
                "type": "string",
                "description": "PIN required by the receiver, if any. Defaults to the configured localsend pin.",
            },
            "scheme": {
                "type": "string",
                "enum": ["http", "https"],
                "description": (
                    "Transport to use when 'peer' is a bare address and discovery did not report one. "
                    "Use https for LocalSend devices in their default encrypted mode — the plugin "
                    "presents its own certificate and verifies the peer's against the advertised fingerprint. "
                    "Peers found via localsend_discover carry their own transport and need no scheme."
                ),
            },
        },
    },
}

RECEIVE = {
    "name": "localsend_receive",
    "description": (
        "Run or inspect this machine's LocalSend receiver, so phones and other devices can send files TO this "
        "machine. action='start' listens on the LocalSend port, announces itself over multicast so it shows up "
        "in peers' device lists, and saves incoming files into the inbox directory. action='status' lists files "
        "received so far. action='stop' shuts the receiver down. Incoming transfers are saved with the sender's "
        "filename and verified against the sender's sha256."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "stop"],
                "description": "start = begin listening, status = running state + received files, stop = shut down.",
            },
            "alias": {
                "type": "string",
                "description": "Name this device advertises to peers (start only, default from config).",
            },
            "port": {
                "type": "integer",
                "description": "Port to listen on (start only, default 53317 — the LocalSend default).",
            },
            "https": {
                "type": "boolean",
                "description": (
                    "Serve the receiver over TLS (start only, default from config). Peers that force "
                    "encryption can then reach this machine. The certificate identifies the receiver; "
                    "sender certificates are not validated (see README)."
                ),
            },
            "download_dir": {
                "type": "string",
                "description": "Directory for incoming files (start only, default from config; must be inside a share root).",
            },
            "pin": {
                "type": "string",
                "description": (
                    "Require this PIN from senders (start only). Empty = a random 6-digit PIN is generated "
                    "and returned in the result."
                ),
            },
        },
        "required": ["action"],
    },
}
