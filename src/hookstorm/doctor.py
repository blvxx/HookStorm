"""Non-invasive integration preflight diagnostics."""

from __future__ import annotations

import os
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

from hookstorm import __version__
from hookstorm.config import load_scenario
from hookstorm.safety import validate_target
from hookstorm.standard_webhooks import decode_standard_webhooks_secret
from hookstorm.verify_auth import (
    resolve_verification_auth_headers,
    validate_verification_auth_transport,
)

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


def build_preflight_document(
    checks: Sequence[DoctorCheck],
    scenario_path: str | Path,
    *,
    network_mode: str = "none",
) -> dict:
    """Build the stable, secret-safe JSON representation of doctor results."""
    if network_mode not in {"none", "connect", "verbose"}:
        raise ValueError("network_mode must be 'none', 'connect', or 'verbose'")

    counts = {
        "passed": sum(check.status == "PASS" for check in checks),
        "failed": sum(check.status == "FAIL" for check in checks),
        "warnings": sum(check.status == "WARN" for check in checks),
        "info": sum(check.status == "INFO" for check in checks),
    }
    network_check = next((check for check in checks if check.label == "Network"), None)
    network_performed = network_check is not None and network_check.status != "INFO"
    ready = counts["failed"] == 0

    return {
        "schema_version": 1,
        "tool": "HookStorm",
        "tool_version": __version__,
        "command": "doctor",
        "scenario": str(scenario_path),
        "status": "READY" if ready else "NOT_READY",
        "ready": ready,
        "exit_code": 0 if ready else 1,
        "network": {
            "mode": network_mode,
            "requested": network_mode != "none",
            "performed": network_performed,
        },
        "summary": {
            "total_checks": len(checks),
            **counts,
        },
        "checks": [
            {
                "status": check.status,
                "label": check.label,
                "message": check.message,
                "details": list(check.details),
            }
            for check in checks
        ],
    }


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


def _scenario_uses_standard_webhooks(scenario: dict) -> bool:
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
        if (
            delivery["signature"] in {"valid", "invalid"}
            and delivery["signature_provider"] == "standard_webhooks"
        ):
            return True
    return False


def _verification_target_check(
    scenario: dict | None,
    *,
    allow_insecure_http: bool,
) -> DoctorCheck:
    """Validate verification endpoint transport policy without contacting it."""
    if scenario is None:
        return DoctorCheck(
            "INFO",
            "Verification target",
            "skipped because the scenario could not be validated",
        )
    verify_config = scenario.get("verify")
    if verify_config is None:
        return DoctorCheck(
            "PASS",
            "Verification target",
            "not required (no business-state verification probe configured)",
        )
    try:
        validate_target(
            verify_config["url"],
            allow_remote=True,
            allow_insecure_http=allow_insecure_http,
        )
    except ValueError as exc:
        return DoctorCheck("FAIL", "Verification target", str(exc))
    return DoctorCheck(
        "PASS",
        "Verification target",
        "URL and transport policy are valid (no HTTP request was sent)",
    )


def _verification_auth_check(
    scenario: dict | None,
    environment: Mapping[str, str],
) -> DoctorCheck:
    """Check that verification credentials are configured without contacting the endpoint."""
    if scenario is None:
        return DoctorCheck(
            "INFO",
            "Verification auth",
            "skipped because the scenario could not be validated",
        )

    verify_config = scenario.get("verify")
    if verify_config is None:
        return DoctorCheck(
            "PASS",
            "Verification auth",
            "not required (no business-state verification probe configured)",
        )

    auth_config = verify_config.get("auth")
    if auth_config is None:
        return DoctorCheck(
            "PASS",
            "Verification auth",
            "not configured; the verification probe will be unauthenticated",
        )

    try:
        validate_verification_auth_transport(verify_config["url"], auth_config)
        resolve_verification_auth_headers(auth_config, environment)
    except ValueError as exc:
        return DoctorCheck("FAIL", "Verification auth", str(exc))

    env_name = (
        auth_config["token_env"]
        if auth_config["type"] == "bearer"
        else auth_config["key_env"]
    )
    return DoctorCheck(
        "PASS",
        "Verification auth",
        f"{env_name} is set and valid (value hidden)",
        (
            "credential format checked locally; no HTTP request was sent",
            "doctor does not verify whether the endpoint accepts this credential",
        ),
    )


def _parse_target(
    target: str, *, allow_insecure_http: bool = False
) -> tuple[str, SplitResult]:
    """Validate an HTTP(S) URL without resolving or contacting its host."""
    validated = validate_target(
        target, allow_remote=True, allow_insecure_http=allow_insecure_http
    )
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

    addresses: list[tuple[socket.AddressFamily, socket.SocketKind, int, tuple[Any, ...]]] = []
    seen: set[tuple[socket.AddressFamily, tuple[Any, ...]]] = set()
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

                certificate = cast(
                    dict[str, Any], tls_connection.getpeercert() or {}
                )
                subject_value = certificate.get("subject")
                issuer_value = certificate.get("issuer")
                subject = _format_certificate_name(
                    subject_value if isinstance(subject_value, tuple) else ()
                )
                issuer = _format_certificate_name(
                    issuer_value if isinstance(issuer_value, tuple) else ()
                )
                details.append(f"  Certificate subject: {subject}")
                details.append(f"  Certificate issuer: {issuer}")
                not_after_value = certificate.get("notAfter")
                not_after = not_after_value if isinstance(not_after_value, str) else None
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
        except (OSError, ValueError) as exc:
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
    allow_insecure_http: bool = False,
) -> list[DoctorCheck]:
    """Check scenario, endpoint configuration, credentials, and optional socket reachability."""
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
            _, parsed_target = _parse_target(
                configured_target, allow_insecure_http=allow_insecure_http
            )
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
        configured_secret = env.get(secret_env, "")
        if not configured_secret.strip():
            checks.append(
                DoctorCheck(
                    "FAIL",
                    "Signing secret",
                    f"this scenario signs requests; set the {secret_env} environment variable",
                )
            )
        elif _scenario_uses_standard_webhooks(scenario):
            try:
                decode_standard_webhooks_secret(configured_secret)
            except ValueError as exc:
                checks.append(DoctorCheck("FAIL", "Signing secret", str(exc)))
            else:
                checks.append(
                    DoctorCheck(
                        "PASS",
                        "Signing secret",
                        f"{secret_env} is a valid Standard Webhooks secret (value hidden)",
                    )
                )
        else:
            checks.append(
                DoctorCheck("PASS", "Signing secret", f"{secret_env} is set (value hidden)")
            )
    else:
        checks.append(
            DoctorCheck(
                "PASS",
                "Signing secret",
                "not required by this scenario (no valid or invalid signature mode)",
            )
        )

    checks.append(
        _verification_target_check(
            scenario, allow_insecure_http=allow_insecure_http
        )
    )
    checks.append(_verification_auth_check(scenario, env))

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
