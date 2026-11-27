"""Target URL validation to reduce accidental remote or internal-network requests."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import idna


class UnsafeTargetError(ValueError):
    """Raised when a target is outside the default local-only policy."""


def canonicalize_hostname(hostname: str) -> str:
    """Return the canonical host spelling used by HTTPX/httpcore for DNS pin keys.

    HTTPX converts IDNs to IDNA ASCII before passing the host to httpcore. Pin
    maps must use the same form or a Unicode hostname could miss the pin and
    fall back to a fresh DNS lookup.
    """
    normalized = hostname.rstrip(".").lower()
    try:
        return ipaddress.ip_address(normalized).compressed.lower()
    except ValueError:
        try:
            return idna.encode(normalized, uts46=True).decode("ascii").lower()
        except idna.IDNAError as exc:
            raise UnsafeTargetError("Target hostname is not a valid IDNA hostname") from exc


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
    try:
        parsed = urlsplit(target.strip())
        port = parsed.port
    except ValueError as exc:
        raise UnsafeTargetError("Target URL contains an invalid hostname or port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeTargetError("Target must be an absolute http:// or https:// URL")
    if port == 0:
        raise UnsafeTargetError("Target URL port must be between 1 and 65535")
    if any(character.isspace() for character in parsed.hostname):
        raise UnsafeTargetError("Target URL hostname must not contain whitespace")
    # Fail before network access if the hostname cannot be normalized to
    # the same IDNA ASCII representation HTTPX/httpcore will use.
    canonicalize_hostname(parsed.hostname)
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
    addresses = {str(item[4][0]).split("%", 1)[0] for item in resolved}
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise UnsafeTargetError("localhost resolved to a non-loopback address; refusing the request")
    return target.strip()

@dataclass(frozen=True)
class TargetResolution:
    """A validated target and the IP addresses approved for its connection."""

    url: str
    hostname: str
    addresses: tuple[str, ...]


def resolve_target(
    target: str,
    *,
    allow_remote: bool = False,
    allow_insecure_http: bool = False,
) -> TargetResolution:
    """Validate a target and freeze its DNS answers for the subsequent connection.

    ``validate_target`` remains the public policy helper used by diagnostics.
    The runner connects only to one of the addresses returned here.
    """
    validated = validate_target(
        target,
        allow_remote=allow_remote,
        allow_insecure_http=allow_insecure_http,
    )
    parsed = urlsplit(validated)
    raw_hostname = parsed.hostname or ""
    hostname = canonicalize_hostname(raw_hostname)
    try:
        literal = ipaddress.ip_address(raw_hostname)
    except ValueError:
        literal = None

    if literal is not None:
        return TargetResolution(validated, hostname, (hostname,))

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        # Resolve exactly the ASCII IDNA form that HTTPX will pass to httpcore.
        resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeTargetError(
            f"Could not resolve target host before connecting: {type(exc).__name__}"
        ) from exc

    addresses = tuple(dict.fromkeys(str(item[4][0]) for item in resolved))
    if not addresses:
        raise UnsafeTargetError("Target host resolved to no usable IP addresses")

    if not allow_remote:
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError as exc:
                raise UnsafeTargetError(
                    "Target host resolved to an invalid IP address"
                ) from exc
            if not parsed_address.is_loopback:
                raise UnsafeTargetError(
                    "localhost resolved to a non-loopback address; refusing the request"
                )

    return TargetResolution(validated, hostname, addresses)
