"""Alexa request verification for the self-hosted (HTTPS) endpoint.

Implements Amazon's checks for skills hosted as a web service:
  1. SignatureCertChainUrl is https://s3.amazonaws.com/echo.api/... (port 443).
  2. The certificate chain is valid now, chains to a trusted root, and the
     leaf's SAN contains echo-api.amazon.com.
  3. Signature-256 is a valid SHA256withRSA signature of the raw body.
  4. request.timestamp is within 150 s of now.
The Lambda path does not need this: Alexa invokes the function directly and
the Lambda trigger is restricted to the skill ID.
"""

from __future__ import annotations

import base64
import posixpath
import threading
import time
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

CERT_HOST = "s3.amazonaws.com"
CERT_PATH_PREFIX = "/echo.api/"
SIGNING_DOMAIN = "echo-api.amazon.com"
TIMESTAMP_TOLERANCE_SECONDS = 150
CERT_CACHE_SECONDS = 3600


class RequestNotVerified(Exception):
    pass


def check_cert_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise RequestNotVerified("cert URL is not https")
    if (parts.hostname or "").lower() != CERT_HOST:
        raise RequestNotVerified("cert URL host is not s3.amazonaws.com")
    if parts.port not in (None, 443):
        raise RequestNotVerified("cert URL port is not 443")
    path = posixpath.normpath(parts.path)
    if not path.startswith(CERT_PATH_PREFIX):
        raise RequestNotVerified("cert URL path is not under /echo.api/")


def check_timestamp(timestamp: str, now: datetime) -> None:
    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestNotVerified("bad request timestamp") from exc
    if abs((now - sent).total_seconds()) > TIMESTAMP_TOLERANCE_SECONDS:
        raise RequestNotVerified("request timestamp outside 150 s tolerance")


def _load_roots() -> Store:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # certifi carries one cert with a non-positive serial
        roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    return Store(roots)


def verify_chain(pem: bytes, roots: Store, at: datetime) -> x509.Certificate:
    """Return the leaf certificate if the chain is valid for echo-api.amazon.com at `at`."""
    certs = x509.load_pem_x509_certificates(pem)
    if not certs:
        raise RequestNotVerified("empty certificate chain")
    leaf, intermediates = certs[0], certs[1:]
    verifier = (
        PolicyBuilder().store(roots).time(at).build_server_verifier(x509.DNSName(SIGNING_DOMAIN))
    )
    try:
        verifier.verify(leaf, intermediates)
    except VerificationError as exc:
        raise RequestNotVerified(f"certificate chain rejected: {exc}") from exc
    return leaf


class AlexaVerifier:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http
        self._roots = _load_roots()
        self._cache: dict[str, tuple[float, x509.Certificate]] = {}
        self._lock = threading.Lock()

    def _leaf_for(self, url: str) -> x509.Certificate:
        with self._lock:
            hit = self._cache.get(url)
        if hit and time.monotonic() - hit[0] < CERT_CACHE_SECONDS and hit[1].not_valid_after_utc > datetime.now(UTC):
            return hit[1]
        resp = self._http.get(url, timeout=3.0)
        if resp.status_code != 200:
            raise RequestNotVerified(f"could not fetch cert chain (HTTP {resp.status_code})")
        leaf = verify_chain(resp.content, self._roots, datetime.now(UTC))
        with self._lock:
            self._cache[url] = (time.monotonic(), leaf)
        return leaf

    def verify(self, headers: Mapping[str, str], body: bytes, timestamp: str) -> None:
        url = headers.get("signaturecertchainurl") or headers.get("SignatureCertChainUrl")
        sig = headers.get("signature-256") or headers.get("Signature-256")
        if not url or not sig:
            raise RequestNotVerified("missing signature headers")
        check_cert_url(url)
        leaf = self._leaf_for(url)
        key = leaf.public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise RequestNotVerified("unexpected signing key type")
        try:
            key.verify(base64.b64decode(sig), body, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError) as exc:
            raise RequestNotVerified("signature does not match body") from exc
        check_timestamp(timestamp, datetime.now(UTC))
