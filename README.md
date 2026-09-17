<img src="docs/banner.png" alt="hermes-localsend — peer-to-peer file transfer for Hermes Agent" width="100%">

<div align="center">

**Move files between the machine your agent runs on and the phones, laptops and tablets on your LAN.**
No account, no cloud, and nothing to install on the other device — it just talks [LocalSend](https://localsend.org).

[![Release](https://img.shields.io/github/v/release/tylerbrevard/hermes-localsend?style=flat-square&color=22d3ee&label=release)](https://github.com/tylerbrevard/hermes-localsend/releases)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-33%20passing-brightgreen?style=flat-square)](tests/)
[![Protocol](https://img.shields.io/badge/LocalSend%20protocol-2.2-22d3ee?style=flat-square)](https://github.com/localsend/protocol)
[![Dependencies](https://img.shields.io/badge/dependencies-none-success?style=flat-square)](#why-standard-library-only)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey?style=flat-square)](#limits)
[![Hermes](https://img.shields.io/badge/Hermes%20Agent-plugin-8b5cf6?style=flat-square)](https://github.com/NousResearch/hermes-agent)

```bash
hermes plugins install localsend && hermes plugins enable localsend
```

</div>

---

## Why this exists

LocalSend is the one transfer path that works everywhere without an account: iPhone ↔ Mac, Android ↔
Linux, a guest's laptop ↔ your homelab. It's also the one transfer path an agent **cannot** use —
there's no CLI, no API, no scripting surface.

This plugin gives Hermes both halves of the protocol, so "send me that file" and "catch the photo my
phone is pushing" become ordinary tool calls: schedulable, scriptable, and reachable from any Hermes
channel — including a chat message from the phone itself.

| | |
| --- | --- |
| **Discover** | `localsend_discover` — multicast announce, `/register` callbacks, plus the protocol's HTTP sweep for devices that ignore multicast |
| **Send** | `localsend_send` — `prepare-upload` → `upload`, every file carrying a `sha256` the receiver verifies |
| **Receive** | `localsend_receive` — a headless receiver that announces itself, so phones see this machine in their device list |

<a id="why-standard-library-only"></a>

**Why standard library only:** the plugin runs inside Hermes' own interpreter, and Hermes reinstalls
its venv from a lockfile on every update. Zero dependencies means this plugin can never be dropped
from that resolution, never pins a shared package against core, and installs offline.

## Install

```bash
# From the plugin catalog (name resolves to a reviewed, pinned commit)
hermes plugins install localsend
hermes plugins enable localsend

# Or straight from this repository
hermes plugins install tylerbrevard/hermes-localsend
hermes plugins enable localsend
```

Restart the gateway afterwards so the tools load into chat channels:

```bash
hermes gateway restart
```

Nothing else to configure — no dependencies, no keys, no service. Verify it landed:

```bash
hermes plugins doctor localsend     # → registrations: 3 tool(s), 0 hook(s)
```

### In Hermes Desktop

One install ships three surfaces. The package carries a `desktop/` half and a `dashboard/`
backend, so the app copies the UI into `~/.hermes/desktop-plugins/localsend/` and derives a
**LOCALSEND** pane, a status-bar chip, and three ⌘K commands. Both switches default to off:

| Surface | Where | Turn it on |
| --- | --- | --- |
| Agent tools | every Hermes session | `plugins.enabled` in `config.yaml` (`hermes plugins enable localsend`) |
| Desktop pane / chip / commands | Hermes Desktop | **Capabilities → Plugins → LocalSend** |
| Backend routes (`/api/plugins/localsend/…`) | dashboard + desktop pane | the same `plugins.enabled` allow-list (Python is gated separately from the UI toggle) |

The pane shows whether the receiver is up, this machine's LAN address, the devices currently
visible, and the inbox — newest first, with **reveal** to open it in Finder. Buttons start/stop the
receiver and rescan the network. ⌘K: *LocalSend: start receiver*, *stop receiver*, *scan for
devices*. The status chip shows a live dot plus how many files have arrived.

```text
/api/plugins/localsend/status    receiver + inbox + addresses
/api/plugins/localsend/start     POST — start the receiver
/api/plugins/localsend/stop      POST — stop it
/api/plugins/localsend/devices   GET  — discover peers
/api/plugins/localsend/send      POST — push files to a peer
/api/plugins/localsend/inbox     GET  — inbox listing straight off disk
```

## Use

```text
"Who's on the network?"                          → localsend_discover
"Send /tmp/report.pdf to Tyler's iPhone"         → localsend_send
"Push that photo back the other way"             → localsend_receive (start) → phone sends → (status)
"Send my phone a note that the build passed"     → localsend_send {"text": "build passed, 0 failures"}
```

### `localsend_discover`

```json
{"timeout_s": 3, "scan_subnets": true}
```

Returns each peer's `alias`, `ip`, `port`, `protocol`, `deviceType`, `deviceModel`, `fingerprint`
and `base_url`.

### `localsend_send`

<details>
<summary><b>Parameters</b></summary>

| Field | Type | Meaning |
| --- | --- | --- |
| `peer` | string | Target device — alias (`"Tyler's iPhone"`), IP, or `IP:port`. Case-insensitive; substring alias matches work. |
| `files` | string[] | Absolute paths to send. |
| `text` | string | Convenience: writes the text to a timestamped `.txt` in the outbox dir and sends it. |
| `discover` | boolean | Run discovery first and auto-select when exactly one peer is visible. |
| `pin` | string | PIN required by the receiver. Defaults to the configured `pin`. |

</details>

The receiver must accept the transfer — on a phone, that's someone tapping **Accept**. If nobody does,
the tool returns `403 rejected` rather than hanging.

### `localsend_receive`

```json
{"action": "start", "alias": "Hermes", "download_dir": "~/Downloads/LocalSend", "pin": ""}
{"action": "status"}
{"action": "stop"}
```

It serves `/register`, `/info`, `/prepare-upload`, `/upload` and `/cancel`, announces itself on the
multicast group so it shows up in every peer's device list, and saves incoming files into the inbox
after verifying the sender's `sha256`. `status` reports each file with its path, size, hash, sender
and timestamp. A second `photo.jpg` lands as `photo (1).jpg` — **an incoming file never overwrites an
existing one**.

## What it looks like

Real output from the installed plugin, not a mock-up:

```console
$ hermes -z "Call localsend_discover with timeout_s=2 and scan_subnets=false"
{
  "count": 0,
  "peers": [],
  "our_alias": "Hermes (Tylers-Mac-mini)",
  "port": 53317,
  "warnings": [],
  "hint": "Pass a peer's alias or ip to localsend_send. Devices must be on the same LAN or
           reachable subnet, and the LocalSend app must be open on the other device.",
  "success": true
}
```

```console
$ python3 ls-smoke.py     # 300 KB; the plugin's sender and receiver over real sockets
receiver: {"running": true, "alias": "Hermes Smoke", "port": 53317, "inbox": ".../inbox"}
send:     {"status": "sent", "bytes": 300000, "files": ["smoke.bin"]}
received: {"file": "smoke.bin", "bytes": 300000, "from": "Hermes (Tylers-Mac-mini)"}
sha source : 9d54d10a0f22a61d56e17940bb034acc271edf590f627d42d2258ea50c736145
sha on disk: 9d54d10a0f22a61d56e17940bb034acc271edf590f627d42d2258ea50c736145
RESULT: PASS — installed plugin moved 300000 bytes over the wire, sha256 verified
```

## How it works

```mermaid
sequenceDiagram
    participant H as Hermes (sender)
    participant P as Phone (receiver)
    H->>P: POST /api/localsend/v2/prepare-upload {info, files[{fileName, size, sha256}]}
    Note over P: user taps Accept
    P-->>H: 200 {sessionId, files:{fileId: token}}
    H->>P: POST /api/localsend/v2/upload?sessionId&fileId&token  (raw binary body)
    Note over P: verifies Content-Length + sha256
    P-->>H: 200  |  422 on checksum mismatch
```

**Discovery** (protocol §3) runs two paths, because no single one is reliable: a multicast announce to
`224.0.0.167:53317`, whose replies arrive as `POST /register` callbacks on our own listener, and the
documented HTTP sweep of the local `/24` for devices that ignore multicast or sit behind a network
that filters it.

**Identity** — the fingerprint exists to ignore our *own* announce, and nothing else. It deliberately
does not gate uploads: a machine must be able to send to its own receiver.

**HTTPS peers** — the peer's certificate is hashed and compared against the fingerprint it advertised
before any bytes move. A mismatch aborts the transfer.

Implements [LocalSend Protocol v2.2](https://github.com/localsend/protocol) — what LocalSend 1.18.x
ships; v3 is still a draft in that repository.

<a id="encrypted-peers"></a>

### Encrypted peers (LocalSend's default mode)

LocalSend's default is HTTPS with **mandatory client certificates**, and the certificate *is* the
identity — the JSON `fingerprint` is only checked against it. Talking to a peer like that needs three
things, all of which this plugin now does:

1. **A device certificate.** On first use the plugin creates a persistent RSA-2048 self-signed
   certificate (`CN=LocalSend User`, the same shape LocalSend generates) under
   `~/.hermes/localsend-identity/`, key mode `0600`. It is never regenerated, because peers remember
   devices by its fingerprint.
2. **The matching fingerprint.** The fingerprint is the SHA-256 of the DER certificate in **uppercase
   hex** (LocalSend's `fingerprint_from_cert_der` format). That — not the HTTP-mode random value — is
   what gets announced when sending to an encrypted peer, since a mismatch is silently ignored.
3. **Certificate pinning on the peer.** The peer's certificate is hashed and compared against the
   fingerprint it advertised before any bytes move; a mismatch aborts the transfer.

`localsend_send` picks this up automatically for any peer whose discovery record says `https`. For a
peer addressed directly by address, state it: `{"peer": "192.168.68.83", "scheme": "https", "files": [...]}`.

### Configuration

Optional, under `plugins.entries.localsend.settings` in `~/.hermes/config.yaml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `alias` | `Hermes (<hostname>)` | Name peers see in their device list |
| `port` | `53317` | LocalSend port (UDP + TCP) |
| `download_dir` | `~/Downloads/LocalSend` | Inbox for received files |
| `outbox_dir` | `~/.hermes/localsend-outbox` | Where `text` is written before sending |
| `pin` | *(empty)* | PIN to require from senders, and to send to receivers |
| `discovery_timeout_s` | `3` | Multicast listen window |
| `send_timeout_s` | `120` | Per-file upload timeout |
| `scan_subnets` | `true` | Also sweep the local `/24` over HTTP |
| `identity_dir` | `~/.hermes/localsend-identity` | Where the certificate for HTTPS peers lives |

## Troubleshooting

**The phone is running LocalSend, but discovery finds nothing, and plain HTTP requests time out.**
`nc -z <phone> 53317` succeeding while an HTTP request times out does **not** tell you the app is
asleep. LocalSend's default mode is **HTTPS with mutual TLS**, and a TLS-only listener never answers
plaintext — the TCP connection is accepted and then simply sits there. Confirm which mode the peer is
in before concluding anything: if a TLS handshake reaches the peer, it is awake. You may see
`tlsv13 alert certificate required`, which means the listener wants *your* client certificate.

**This plugin speaks HTTP mode.** If a peer is in encrypted (HTTPS/mTLS) mode it cannot be driven by
these tools, and it cannot push to this receiver. Turn encryption off on that peer, or track the
mTLS work below.

**The device is found, but `prepare-upload` returns 403.**
Someone has to accept the transfer on the receiving device. On a phone that means tapping the
incoming request — nothing is accepted silently.

**Send fails against a phone on a network that blocks multicast.**
Leave `scan_subnets` on (the default). The HTTP sweep finds peers that never answer the multicast
announce.

**`cannot bind TCP port 53317`.**
Another LocalSend instance owns the port — the desktop app, or a second Hermes profile with a
receiver running. Quit it, or configure a different `port`. The receiver reports the exact bind error
instead of failing silently.

**Multicast is fine, but macOS still sees nothing.**
macOS gates local-network traffic behind Local Network permission — and it is granted **per process**,
so where the receiver runs matters. Measured on macOS 26: from a `launchd` agent, multicast sends
*and* a plain TCP connect to another LAN host both fail with `EHOSTUNREACH` (Errno 65) while a local
listen still succeeds; the same calls from the Hermes process tree work. Run the receiver inside
Hermes (the gateway keeps it alive), not as its own launchd job — and if it must be a service, grant
it Local Network access in *System Settings → Privacy & Security → Local Network*.

**`409 blocked by another session`.**
The receiver is mid-transfer with another device. Retry when it finishes.

**Where did the files go?**
Into `download_dir` (`~/Downloads/LocalSend` by default) — a `status` call names the exact path. The
receiver lives in the process that started it: the gateway keeps it alive across tool calls, while a
one-shot `hermes -z` process exits and takes the receiver with it.

## Security

- While the receiver runs it binds `0.0.0.0` on the LocalSend port — inherent to being discoverable on
  a LAN. It is **not** running by default: nothing listens until `localsend_receive` is called with
  `action: "start"`. Set a `pin`, or `action: "stop"`, on untrusted networks.
- Incoming uploads are validated before they touch disk: session token, source IP, `Content-Length`,
  and the sender's `sha256`. A mismatch returns `422` and deletes the partial file.
- Filenames are reduced to their basename, so a sender cannot write outside the inbox.
- No telemetry and no outbound connections except to the peer named in the call.

## Development

```bash
python3 -m unittest discover -s tests -v     # 33 tests, no dependencies, loopback only
hermes plugins doctor . --ci                 # registration contract
hermes plugins validate .                    # catalog admission checks
```

The suite does not test the plugin against itself. Two **independent oracles** are written straight
from the protocol spec:

| Oracle | Drives | Covers |
| --- | --- | --- |
| `SpecReceiver` | the send path | payload shape, session + token handling, pin gate (401), rejection (403), checksum mismatch (422), unreachable peer |
| `spec_send` | the receive path | uploads landing on disk, wrong token (403), unknown session (409), size mismatch (400), checksum mismatch (422), never-overwrite |

Plus a **live multicast round-trip**: our announce is read off the group by a listener, the listener
answers the way a real peer does, and the reply has to come back parsed as a peer.

The desktop half gets its own offline contract check — it stubs `@hermes/plugin-sdk`, `react` and
`react/jsx-runtime`, imports the plugin, calls `register()`, then runs every `render()` in three
states (receiving, stopped, backend unreachable) and every palette command:

```bash
node tools/desktop-contract-check.mjs desktop/plugin.js
```

`git log` is the changelog; each release notes what changed and what was verified.

<a id="limits"></a>

## Limits

- **Encrypted peers are send-only for now.** Sending *to* a peer in LocalSend's default HTTPS mode
  works (see [Encrypted peers](#encrypted-peers)); receiving from one still needs the receiver to serve
  HTTPS with its own certificate request, which is not implemented. Announce HTTP to keep the receive
  direction working, or turn the sender's encryption off.
- **No download/reverse-transfer API** (protocol §5) — the upload path is what phones use by default.
- **Protocol v2.2.** v3 is a draft upstream; current clients ship v2.2.
- **No mDNS/Bonjour, no relay, no NAT traversal.** Same L2 network, or a subnet you can route to.
- **The receiver lives in one process.** Whichever process starts it owns it — the gateway for the
  agent tools, the dashboard/desktop backend for the pane. They share a single receiver when both run
  in one process; across two processes the second start reports the bind error instead of stealing
  the port.

## License

[MIT](LICENSE). LocalSend is an independent project (Apache-2.0); this plugin is not affiliated with it.
