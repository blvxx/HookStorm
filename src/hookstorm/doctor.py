"""Non-invasive integration preflight diagnostics."""

from __future__ import annotations

import os
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import SplitResult, urlsplit

from hookstorm.config import load_scenario
from hookstorm.safety import validate_target


@dataclass(frozen=True)
class DoctorCheck:
    """One human-readable preflight result."""

    status: str
    label: str
    message: str


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


def run_preflight(
    scenario_path: str | Path = "scenarios/webhook.yaml",
    *,
    target: str | None = None,
    secret_env: str = "HOOKSTORM_SECRET",
    environment: Mapping[str, str] | None = None,
    connect: bool = False,
    timeout: float = 5.0,
) -> list[DoctorCheck]:
    """Check scenario, endpoint configuration, signing secret, and optionally socket reachability."""
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
                "not contacted; pass --connect for DNS/TCP and HTTPS TLS checks (no HTTP request is sent)",
            )
        )
    elif parsed_target is None:
        checks.append(
            DoctorCheck("INFO", "Network", "skipped because the target URL is not valid")
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
