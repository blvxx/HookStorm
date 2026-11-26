"""Shared helpers for environment-backed verification authentication."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from urllib.parse import urlsplit


def resolve_verification_auth_headers(
    auth_config: dict[str, str] | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve configured credentials into HTTP headers without exposing their values."""
    if auth_config is None:
        return {}

    env = os.environ if environment is None else environment
    if auth_config["type"] == "bearer":
        env_name = auth_config["token_env"]
        header_name = "Authorization"
        prefix = "Bearer "
    else:
        env_name = auth_config["key_env"]
        header_name = auth_config["header"]
        prefix = ""

    credential = env.get(env_name, "")
    if not credential:
        raise ValueError(
            f"Missing or empty environment variable '{env_name}' required by verify.auth."
        )
    if (
        len(credential) > 8_192
        or not credential.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in credential)
    ):
        raise ValueError(
            f"Environment variable '{env_name}' must contain 1–8192 printable ASCII "
            "characters without whitespace."
        )
    return {header_name: f"{prefix}{credential}"}


def validate_verification_auth_transport(
    url: str,
    auth_config: dict[str, str] | None,
) -> None:
    """Require TLS before credentials are sent to non-loopback verification endpoints."""
    if auth_config is None:
        return

    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"

    if parsed.scheme != "https" and not is_loopback:
        raise ValueError(
            "Authenticated verification probes require HTTPS for non-loopback URLs."
        )
