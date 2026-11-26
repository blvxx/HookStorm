"""Non-invasive integration preflight diagnostics."""

from __future__ import annotations

import os
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from hookstorm.config import load_scenario
from hookstorm.safety import validate_target

_MAX_DIAGNOSTIC_ADDRESSES = 12
_CERTIFICATE_WARNING_DAYS = 30


@dataclass(frozen=True)
class DoctorCheck:
    """One human-readable preflight result."""

    status: str
    label: str
    message: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectivityResult:
    """Detailed DNS, socket, and optional TLS diagnostics."""

    status: str
    message: str
    details: tuple[str, ...] = ()


def _scenario_requires_secret(scenario: dict) -> bool:
    defaults = {
        "signature": "none",
        "signature_provider": "generic",
        "signature_timestamp_offset_seconds": 0,
        "timeout_ms": 5_000,
        "duplicate_count": 0,
        "delay_ms": 0,
        "concurrency": 1,
        "order": "in_order",
    }
    for event in scenario["events"]:
        delivery = {**defaults, **scenario["delivery"], **event["delivery"]}
        if delivery["signature"] in {"valid", "invalid"}:
            return True
    return False


def _parse_target(target: str) -> tuple[str, SplitResult]:
    """Validate an HTTP(S) URL without resolving or contacting its host."""
    validated = validate_target(target, allow_remote=True)
    parsed = urlsplit(validated)
    port = parsed.port  # Accessing this property also rejects malformed / out-of-range ports.
    if not parsed.hostname or any(char.isspace() for char in parsed.hostname):
        raise ValueError("Target URL must contain a valid hostname")
    if port == 0:
        raise ValueError("Target URL port must be between 1 and 65535")
    return validated, parsed


def _check_connectivity(parsed: SplitResult, timeout: float) -> str:
    """Perform DNS + TCP, and TLS certificate verification for HTTPS; send no HTTP request."""
    host = parsed.hostname
    if host is None:  # Defensive; _parse_target has already validated this.
        raise ValueError("Target URL has no hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((host, port), timeout=timeout) as connection:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            with context.wrap_socket(connection, server_hostname=host):
                pass
            return "TCP connection and TLS certificate verification succeeded"
    return "TCP connection succeeded"


def _format_certificate_name(name: tuple) -> str:
    values = [
        f"{key}={value}"
        for relative_name in name
        for key, value in relative_name
    ]
    return ", ".join(values) if values else "unavailable"


def _check_connectivity_detailed(
    parsed: SplitResult, timeout: float
) -> ConnectivityResult:
    """Resolve each address and report TCP / TLS details without sending HTTP traffic."""
    host = parsed.hostname
    if host is None:
        return ConnectivityResult("FAIL", "target URL has no hostname")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    dns_started = time.perf_counter()
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        elapsed_ms = (time.perf_counter() - dns_started) * 1000
        return ConnectivityResult(
            "FAIL",
            f"DNS lookup failed for {host}: {type(exc).__name__}: {exc}",
            (f"DNS lookup time: {elapsed_ms:.2f} ms",),
        )
    dns_ms = (time.perf_counter() - dns_started) * 1000

    addresses: list[tuple[int, int, int, tuple]] = []
    seen: set[tuple[int, tuple]] = set()
    for family, socktype, proto, _canonical_name, sockaddr in resolved:
        key = (family, sockaddr)
        if key not in seen:
            addresses.append((family, socktype, proto, sockaddr))
            seen.add(key)

    details = [f"DNS lookup completed in {dns_ms:.2f} ms ({len(addresses)} unique address(es))"]
    if not addresses:
        return ConnectivityResult("FAIL", "DNS returned no usable addresses", tuple(details))

    skipped = max(0, len(addresses) - _MAX_DIAGNOSTIC_ADDRESSES)
    addresses = addresses[:_MAX_DIAGNOSTIC_ADDRESSES]
    for family, _socktype, _proto, sockaddr in addresses:
        address = sockaddr[0]
        family_name = "IPv6" if family == socket.AF_INET6 else "IPv4"
        details.append(f"DNS {family_name}: {address}")
    if skipped:
        details.append(
            f"Only the first {_MAX_DIAGNOSTIC_ADDRESSES} addresses will be checked; {skipped} skipped"
        )

    successful = 0
    failed = 0
    certificate_warning = False
    for family, socktype, proto, sockaddr in addresses:
        address = sockaddr[0]
        family_name = "IPv6" if family == socket.AF_INET6 else "IPv4"
        started = time.perf_counter()
        connection: socket.socket | ssl.SSLSocket | None = None
        try:
            connection = socket.socket(family, socktype, proto)
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            tcp_ms = (time.perf_counter() - started) * 1000
            details.append(f"{family_name} {address}: TCP connected in {tcp_ms:.2f} ms")

            if parsed.scheme == "https":
                tls_started = time.perf_counter()
                context = ssl.create_default_context()
                tls_connection = context.wrap_socket(connection, server_hostname=host)
                connection = tls_connection
                tls_ms = (time.perf_counter() - tls_started) * 1000
                cipher = tls_connection.cipher()
                cipher_name = cipher[0] if cipher else "unavailable"
                details.append(
                    f"  TLS: {tls_connection.version() or 'unknown'}; "
                    f"cipher: {cipher_name}; handshake: {tls_ms:.2f} ms"
                )

                certificate = tls_connection.getpeercert()
                subject = _format_certificate_name(certificate.get("subject", ()))
                issuer = _format_certificate_name(certificate.get("issuer", ()))
                details.append(f"  Certificate subject: {subject}")
                details.append(f"  Certificate issuer: {issuer}")
                not_after = certificate.get("notAfter")
                if not_after:
                    expires_at = datetime.fromtimestamp(
                        ssl.cert_time_to_seconds(not_after), UTC
                    )
                    remaining = expires_at - datetime.now(UTC)
                    remaining_days = remaining.total_seconds() / 86_400
                    details.append(f"  Certificate expires: {expires_at.isoformat()}")
                    if remaining_days < 0:
                        certificate_warning = True
                        details.append(
                            f"  WARNING: certificate expired {-remaining_days:.1f} day(s) ago"
                        )
                    elif remaining_days <= _CERTIFICATE_WARNING_DAYS:
                        certificate_warning = True
                        details.append(
                            f"  WARNING: certificate expires in {remaining_days:.1f} day(s)"
                        )
                else:
                    details.append("  Certificate expiry: unavailable")

            successful += 1
        except (OSError, ssl.SSLError, ValueError) as exc:
            failed += 1
            details.append(
                f"{family_name} {address}: connection failed "
                f"({type(exc).__name__}: {exc})"
            )
        finally:
            if connection is not None:
                connection.close()

    if successful == 0:
        return ConnectivityResult(
            "FAIL",
            f"all {failed} resolved address(es) failed to connect",
            tuple(details),
        )
    if failed:
        return ConnectivityResult(
            "WARN",
            f"{successful}/{successful + failed} address(es) reachable; "
            f"{failed} address(es) failed",
            tuple(details),
        )
    return ConnectivityResult(
        "WARN" if certificate_warning else "PASS",
        f"all {successful} resolved address(es) reachable"
        + (" with verified TLS" if parsed.scheme == "https" else ""),
        tuple(details),
    )


def run_preflight(
    scenario_path: str | Path = "scenarios/webhook.yaml",
    *,
    target: str | None = None,
    secret_env: str = "HOOKSTORM_SECRET",
    environment: Mapping[str, str] | None = None,
    connect: bool = False,
    verbose: bool = False,
    timeout: float = 5.0,
) -> list[DoctorCheck]:
    """Check scenario, endpoint configuration, signing secret, and optional socket reachability."""
    env = os.environ if environment is None else environment
    checks: list[DoctorCheck] = []

    scenario = None
    try:
        scenario = load_scenario(scenario_path)
    except (OSError, ValueError) as exc:
        checks.append(DoctorCheck("FAIL", "Scenario", str(exc)))
    else:
        checks.append(
            DoctorCheck(
                "PASS",
                "Scenario",
                f"{scenario['name']} ({len(scenario['events'])} event(s))",
            )
        )

    configured_target = target if target is not None else env.get("HOOKSTORM_TARGET_URL")
    parsed_target = None
    if not configured_target or not configured_target.strip():
        checks.append(
            DoctorCheck(
                "FAIL",
                "Target",
                "set HOOKSTORM_TARGET_URL or pass --target with an HTTP(S) endpoint",
            )
        )
    else:
        try:
            _, parsed_target = _parse_target(configured_target)
        except ValueError as exc:
            checks.append(DoctorCheck("FAIL", "Target", str(exc)))
        else:
            display_host = parsed_target.hostname or "unknown"
            if ":" in display_host:
                display_host = f"[{display_host}]"
            port = parsed_target.port
            endpoint = f"{display_host}:{port}" if port else display_host
            checks.append(
                DoctorCheck(
                    "PASS",
                    "Target",
                    f"{parsed_target.scheme.upper()} endpoint configured ({endpoint}); full URL hidden",
                )
            )
            if parsed_target.query:
                checks.append(
                    DoctorCheck(
                        "WARN",
                        "Target URL",
                        "query parameters are present; do not put credentials or tokens in the URL",
                    )
                )

    if scenario is None:
        checks.append(
            DoctorCheck("INFO", "Signing secret", "skipped because the scenario could not be validated")
        )
    elif _scenario_requires_secret(scenario):
        if env.get(secret_env, "").strip():
            checks.append(
                DoctorCheck("PASS", "Signing secret", f"{secret_env} is set (value hidden)")
            )
        else:
            checks.append(
                DoctorCheck(
                    "FAIL",
                    "Signing secret",
                    f"this scenario signs requests; set the {secret_env} environment variable",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "PASS",
                "Signing secret",
                "not required by this scenario (no valid or invalid signature mode)",
            )
        )

    if not connect:
        checks.append(
            DoctorCheck(
                "INFO",
                "Network",
                "not contacted; pass --connect for a socket check or --verbose for detailed DNS/TCP/TLS output",
            )
        )
    elif parsed_target is None:
        checks.append(
            DoctorCheck("INFO", "Network", "skipped because the target URL is not valid")
        )
    elif verbose:
        result = _check_connectivity_detailed(parsed_target, timeout)
        checks.append(
            DoctorCheck(result.status, "Network", result.message, result.details)
        )
    else:
        try:
            detail = _check_connectivity(parsed_target, timeout)
        except (OSError, ValueError) as exc:
            checks.append(
                DoctorCheck(
                    "FAIL",
                    "Network",
                    f"could not reach the endpoint socket: {type(exc).__name__}: {exc}",
                )
            )
        else:
            checks.append(DoctorCheck("PASS", "Network", detail))

    return checks
