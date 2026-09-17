"""Device identity for HTTPS peers: a persistent self-signed certificate.

LocalSend identifies an HTTPS device by the SHA-256 of its certificate
(packages/core/src/crypto/cert.rs → ``fingerprint_from_cert_der``):
RSA-2048, self-signed, and the fingerprint is **uppercase hex, no separators**.

The v2 receiver requires a client certificate, so sending to a peer in its
default (encrypted) mode means presenting one of these and advertising the
matching fingerprint — a mismatch is silently ignored by the peer's /register.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import subprocess
from typing import Optional, Tuple

CERT_FILENAME = "identity.crt"
KEY_FILENAME = "identity.key"
SUBJECT = "/CN=LocalSend User"


class IdentityError(Exception):
    """Certificate generation or loading failed."""


def fingerprint_from_pem(cert_pem: str) -> str:
    """SHA-256 of the DER certificate, uppercase hex (LocalSend's format)."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_pem)
    except ValueError as exc:
        raise IdentityError(f"not a PEM certificate: {exc}") from exc
    return hashlib.sha256(der).hexdigest().upper()


def fingerprint_from_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


class Identity:
    """A persisted self-signed certificate + key pair."""

    def __init__(self, cert_path: str, key_path: str) -> None:
        self.cert_path = cert_path
        self.key_path = key_path
        with open(cert_path, "r", encoding="utf-8") as fh:
            self.cert_pem = fh.read()
        self.fingerprint = fingerprint_from_pem(self.cert_pem)

    def as_dict(self) -> dict:
        return {
            "cert": self.cert_path,
            "key": self.key_path,
            "fingerprint": self.fingerprint,
        }


def _openssl() -> Optional[str]:
    return shutil.which("openssl")


def generate(directory: str, subject: str = SUBJECT) -> Identity:
    """Create (or reuse) a self-signed identity under ``directory``."""
    os.makedirs(directory, exist_ok=True)
    cert_path = os.path.join(directory, CERT_FILENAME)
    key_path = os.path.join(directory, KEY_FILENAME)

    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return Identity(cert_path, key_path)

    binary = _openssl()
    if not binary:
        raise IdentityError(
            "openssl not found on PATH; needed once to create the device certificate "
            "used when talking to peers in encrypted (HTTPS) mode"
        )

    tmp_key = key_path + ".tmp"
    tmp_cert = cert_path + ".tmp"
    command = [
        binary, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", tmp_key, "-out", tmp_cert,
        "-days", "3650", "-subj", subject,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityError(f"openssl failed: {exc}") from exc
    if result.returncode != 0:
        for path in (tmp_key, tmp_cert):
            if os.path.exists(path):
                os.unlink(path)
        raise IdentityError(f"openssl exited {result.returncode}: {result.stderr.strip()[:200]}")

    os.replace(tmp_key, key_path)
    os.replace(tmp_cert, cert_path)
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)
    return Identity(cert_path, key_path)


def load(directory: str) -> Identity:
    """Load an existing identity, or create one."""
    return generate(directory)


def client_context(identity: Identity) -> ssl.SSLContext:
    """TLS client context presenting our certificate (what an HTTPS peer needs)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    # The peer's certificate is self-signed, so the CA chain cannot validate it —
    # identity is established by pinning the fingerprint we learned from discovery.
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.load_cert_chain(identity.cert_path, identity.key_path)
    except (OSError, ssl.SSLError) as exc:
        raise IdentityError(f"cannot load device certificate: {exc}") from exc
    return ctx


def fingerprint_of_peer(der: bytes) -> str:
    """The uppercase-hex fingerprint LocalSend would advertise for this certificate."""
    return fingerprint_from_der(der)
