# hermes-localsend

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that speaks
[LocalSend](https://localsend.org) — the open-source, serverless AirDrop alternative — so your
agent can move files between the machine it runs on and the phones, laptops and tablets on the
same network.

Pure Python standard library. No dependencies, no cloud service, no account.

```
localsend_discover   →  who's on the LAN right now
localsend_send       →  push files or a text note to a device
localsend_receive    →  run a headless receiver so devices can push files here
```

## Why

LocalSend is the one transfer path that works everywhere without an account: iPhone ↔ Mac,
Android ↔ Linux, guest laptop ↔ your homelab. It has no CLI and no API you can script. This
plugin gives Hermes both halves of the protocol, so "send me that file" and "catch the photo my
phone is sending" become ordinary tool calls — schedulable, scriptable, and reachable from any
Hermes channel, including a chat message from your phone.

## Install

```bash
hermes plugins install tylerbrevard/hermes-localsend
hermes plugins enable localsend
```

Then restart the gateway or dashboard so plugin code is re-imported:

```bash
hermes gateway restart
```

## Tools

### `localsend_discover`

Finds LocalSend peers. Sends a multicast announce to `224.0.0.167:53317`, accepts the
`POST /api/localsend/v2/register` callbacks that peers send back, and (unless disabled) also
runs the protocol's legacy HTTP sweep across the local `/24` for devices that ignore multicast.

```json
{"timeout_s": 3, "scan_subnets": true}
```

Returns each peer's `alias`, `ip`, `port`, `protocol`, `deviceType`, `deviceModel`,
`fingerprint` and `base_url`.

### `localsend_send`

Runs the protocol's upload handshake (`prepare-upload` → `upload`) against a peer.

```json
{"peer": "Tyler's iPhone", "files": ["/tmp/report.pdf"], "pin": ""}
{"peer": "192.168.68.83", "text": "build finished, 0 failures"}
```

* `peer` — alias, IP, or `IP:port`. Case-insensitive; substring alias matches work.
* `files` — absolute paths.
* `text` — convenience: writes the text to a timestamped `.txt` in the outbox dir and sends it.
* `discover` — set `true` to auto-select when exactly one peer is visible.
* Every file is sent with its `sha256`, and the receiver verifies it (a mismatch comes back as
  HTTP 422 and is reported as an error, never a silent success).

The receiver must accept the transfer — on a phone that means tapping **Accept**. Expect the tool
to return `403 rejected` if nobody does.

### `localsend_receive`

Runs a headless LocalSend receiver in this Hermes process so devices can push files *to* you.
It serves `/register`, `/info`, `/prepare-upload`, `/upload` and `/cancel`, announces itself on
the multicast group so it appears in every peer's device list, and saves incoming files into the
inbox after verifying the sender's `sha256`.

```json
{"action": "start", "alias": "Hermes", "download_dir": "~/Downloads/LocalSend", "pin": ""}
{"action": "status"}
{"action": "stop"}
```

`status` returns what arrived — path, size, sha256, sender alias, timestamp. Existing files are
never overwritten: a second `photo.jpg` lands as `photo (1).jpg`.

## Configuration

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

## Protocol notes

Implements [LocalSend Protocol v2.2](https://github.com/localsend/protocol)
(what LocalSend 1.18.x ships; v3 is still a draft in that repo).

* **Discovery** — multicast UDP `224.0.0.167:53317` with the `register` callback path, plus the
  documented HTTP legacy sweep as a fallback.
* **Transfer** — `POST /api/localsend/v2/prepare-upload` then
  `POST /api/localsend/v2/upload?sessionId=…&fileId=…&token=…` with a raw binary body.
* **Verification** — senders advertise `sha256`; receivers verify it and answer `422` on
  mismatch. The receiver also enforces Content-Length, per-file tokens and the session's source
  IP, and refuses a `prepare-upload` whose advertised fingerprint is its own.
* **Encryption** — the receiver serves plain HTTP, so no self-signed certificate is needed and
  browsers/CLIs can talk to it. Sending to an HTTPS peer works too: the peer's certificate is
  hashed and compared against the fingerprint it advertised before any file is transferred.
  On iOS and Android, LocalSend's "encryption" toggle must be **off** to send to an HTTP
  receiver.

## Security

* Binds `0.0.0.0` on the LocalSend port while the receiver runs — that is inherent to the
  protocol. Use `pin` (or `action: "stop"`) on untrusted networks.
* Incoming files are written only into the configured inbox, with the sender's filename reduced
  to its basename, and never overwrite an existing file.
* No telemetry, no outbound connections except to the peer you name.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

33 tests, no dependencies and no network access beyond the loopback interface. The suite drives
the plugin against two independent oracles written straight from the protocol spec — a
spec-faithful *receiver* for the sender path and a spec-faithful *sender* for the receiver path —
plus a live multicast round-trip (announce on the wire → unicast reply → peer parsed).

## License

MIT — see [LICENSE](LICENSE). LocalSend itself is an independent project (Apache-2.0) and is not
affiliated with this plugin.
