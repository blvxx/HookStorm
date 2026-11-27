"""Target URL validation to reduce accidental remote or internal-network requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeTargetError(ValueError):
    """Raised when a target is outside the default local-only policy."""


def _is_literal_loopback(hostname: str) -> bool:
    """Recognize localhost and literal loopback IP addresses without DNS lookups."""
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_target(
    target: str,
    *,
    allow_remote: bool = False,
    allow_insecure_http: bool = False,
) -> str:
    """Validate an HTTP(S) URL, requiring a separate opt-in for remote plaintext HTTP."""
    if not isinstance(target, str) or not target.strip():
        raise UnsafeTargetError("Target URL must be a non-empty string")
    if allow_insecure_http and not allow_remote:
        raise UnsafeTargetError("--allow-insecure-http requires --allow-remote")
    parsed = urlsplit(target.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeTargetError("Target must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise UnsafeTargetError("Target URL must not contain embedded credentials")
    if parsed.fragment:
        raise UnsafeTargetError("Target URL must not contain a fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if allow_remote:
        if parsed.scheme == "http" and not _is_literal_loopback(hostname) and not allow_insecure_http:
            raise UnsafeTargetError(
                "Remote HTTP targets are unencrypted; use HTTPS or explicitly pass "
                "--allow-remote --allow-insecure-http"
            )
        return target.strip()

    try:
        address = ipaddress.ip_address(hostname)
        if address.is_loopback:
            return target.strip()
        raise UnsafeTargetError("Remote IP targets are blocked by default; pass --allow-remote to opt in")
    except ValueError as exc:
        if isinstance(exc, UnsafeTargetError):
            raise
        if hostname != "localhost":
            raise UnsafeTargetError("Only localhost targets are allowed by default; pass --allow-remote to opt in") from exc

    try:
        resolved = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise UnsafeTargetError(f"Could not resolve local target host: {exc}") from exc
    addresses = {item[4][0].split("%", 1)[0] for item in resolved}
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise UnsafeTargetError("localhost resolved to a non-loopback address; refusing the request")
    return target.strip()
